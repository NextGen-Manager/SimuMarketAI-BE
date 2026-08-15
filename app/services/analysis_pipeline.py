"""The analysis pipeline, as executed by the Celery worker.

The order of stages is the one in docs/02, and every transition is persisted
before it is published. Three properties are what this module exists to
guarantee:

- a run is claimed exactly once, so a redelivered task cannot duplicate work;
- finance and scoring run from typed input regardless of what the agents did,
  so an OASIS failure costs the simulation section and nothing else;
- a failure is recorded as `partial` with a stated reason, never smoothed into
  `completed`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.domain.agents import (
    AgentRunRecord,
    ArtifactType,
    CohortManifest,
    FinanceBounds,
    FinanceTool,
    FinanceToolCall,
    OasisAdapter,
    OasisError,
    RunManifest,
    SimulationBudget,
    SimulationOutcome,
    SimulationRequest,
)
from app.domain.analysis_events import AnalysisEvent, AnalysisEventWarning
from app.domain.analysis_state import (
    STAGE_MESSAGES,
    WORKER_LOST_FAILURE_CODE,
    WORKER_LOST_MESSAGE,
    WORKER_LOST_WARNING,
    AnalysisStage,
    AnalysisStatus,
    StagePlan,
    stage_plan_for,
)
from app.domain.evidence import (
    METRIC_LABELS,
    REQUIRED_EVIDENCE_METRICS,
    EvidenceProvider,
    EvidenceRequest,
    EvidenceSnapshot,
    Geography,
)
from app.engines.evidence_confidence import calculate_evidence_confidence
from app.engines.finance import (
    FinanceInput,
    InvalidFinanceInputError,
    calculate_finance,
    finance_input_from_analysis,
)
from app.engines.report import compose_report
from app.engines.report_validation import validate_report
from app.engines.scoring import ScoringInput, calculate_score
from app.integrations.oasis import simulation_is_planned
from app.integrations.oasis.runtime import (
    TRACE_FILE_NAME,
    allocate_trace_directory,
    budget_from_settings,
    build_manifest,
    cohort_from_settings,
    environment_id,
    input_snapshot_hash,
    trace_artifact,
)
from app.integrations.oasis.sanitizer import build_simulation_request
from app.persistence.models import (
    AgentArtifact,
    AgentInstance,
    AgentRun,
    AgentTraceArtifact,
    AnalysisEventRecord,
    AnalysisReportRecord,
    AnalysisRun,
    EvidenceItem,
)
from app.repositories.analysis_worker import AnalysisWorkerRepository
from app.schemas.analysis import (
    FINANCE_RULE_VERSION,
    SCORING_RULE_VERSION,
    AnalysisInput,
    AnalysisReport,
    AnalysisWarning,
    ScoreResult,
    VolumeRange,
)
from app.services.analysis_events import AnalysisEventPublisher, NullEventPublisher

logger = logging.getLogger(__name__)

SIMULATION_FAILED_WARNING = "simulation_failed"
SIMULATION_PARTIAL_WARNING = "simulation_partial"

# Failures worth retrying: the database, the broker, or the filesystem blinked,
# not the run itself. They are re-raised so Celery's `autoretry_for` can see
# them; a schema or policy failure is recorded and left alone, because retrying
# it unchanged would only burn budget on the same outcome.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError)


class AnalysisLeaseLostError(RuntimeError):
    """The reconciler fenced this worker because its attempt lease expired."""


@dataclass(frozen=True, slots=True)
class PipelineDependencies:
    settings: Settings
    evidence_provider: EvidenceProvider
    oasis_adapter: OasisAdapter
    publisher: AnalysisEventPublisher


@dataclass(frozen=True, slots=True)
class SimulationAttempt:
    """What one simulation stage left behind, successful or not.

    `manifest` is present whenever a trace directory was allocated, even if the
    adapter then failed. The directory exists on disk either way, so its
    retention record has to exist too — otherwise a failed run leaks a directory
    that no maintenance job knows to clean up.
    """

    outcome: SimulationOutcome | None
    reason: str | None
    manifest: RunManifest | None


def _checksum(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _finance_tool_for(analysis_input: AnalysisInput) -> FinanceTool:
    """Bind the deterministic calculator as the only tool the Finance council has.

    Requested volumes are clamped to the range the user actually supplied. An
    agent may choose a bound, per docs/03, but it may not widen its own mandate
    by proposing a volume the input never allowed.
    """
    base_input = finance_input_from_analysis(analysis_input)
    volume = analysis_input.operations.volume_units_day

    def call(assumptions: Mapping[str, int]) -> FinanceToolCall:
        requested = int(assumptions.get("volume_units_day", volume.base))
        chosen = max(volume.min, min(volume.max, requested))
        scoped: FinanceInput = replace(
            base_input,
            volume_units_day=VolumeRange(min=chosen, base=chosen, max=chosen),
        )
        result = calculate_finance(scoped)
        scenario = result.scenario("base")
        return FinanceToolCall(
            tool_call_id=f"finance-volume-{chosen}",
            rule_version=FINANCE_RULE_VERSION,
            assumptions={"volume_units_day": chosen},
            outputs={
                "contribution_margin_per_unit_idr": result.contribution_margin_per_unit_idr,
                "bep_units_month": result.bep_units_month,
                "monthly_revenue_idr": scenario.monthly_revenue_idr if scenario else None,
                "monthly_operating_profit_idr": (
                    scenario.monthly_operating_profit_idr if scenario else None
                ),
                "payback_months": scenario.payback_months if scenario else None,
            },
        )

    return call


class AnalysisPipeline:
    def __init__(self, session: AsyncSession, dependencies: PipelineDependencies) -> None:
        self._session = session
        self._deps = dependencies
        self._runs = AnalysisWorkerRepository(session)

    # ------------------------------------------------------------------ entry

    async def run(self, analysis_id: UUID) -> None:
        plan = stage_plan_for(simulation_planned=simulation_is_planned(self._deps.oasis_adapter))
        run = await self._runs.claim(
            analysis_id,
            first_stage="collecting_evidence",
            lease_seconds=self._deps.settings.analysis_lease_seconds,
            max_attempts=self._deps.settings.analysis_max_attempts,
        )
        if run is None:
            # Either another worker already claimed it or the run is no longer
            # queued. Both cases mean this delivery has nothing to do.
            logger.info("analysis_task_skipped", extra={"analysis_id": str(analysis_id)})
            return

        run.skipped_stages = [str(stage) for stage in plan.skipped]
        await self._runs.commit()

        try:
            await self._execute(run, plan)
        except AnalysisLeaseLostError:
            await self._runs.rollback()
            logger.warning(
                "analysis_attempt_fenced",
                extra={
                    "analysis_id": str(analysis_id),
                    "attempt_count": run.attempt_count,
                },
            )
        except InvalidFinanceInputError:
            await self._fail(run, "invalid_finance_input", plan)
        except TRANSIENT_ERRORS:
            # Not the run's fault. Put it back into the only state a redelivered
            # Celery task is allowed to claim, then propagate the exception so
            # Celery applies its retry policy. The reconciler remains the
            # fallback when publishing that retry is itself lost.
            logger.warning(
                "analysis_pipeline_transient_failure",
                extra={"correlation_id": str(run.correlation_id)},
            )
            attempt_count = run.attempt_count
            if attempt_count >= self._deps.settings.analysis_max_attempts:
                await self._fail(run, WORKER_LOST_FAILURE_CODE, plan)
                raise

            await self._runs.rollback()
            candidate = await self._runs.get(analysis_id)
            if (
                candidate is not None
                and candidate.attempt_count == attempt_count
                and candidate.status
                not in {"queued", "completed", "partial", "failed", "cancelled"}
            ):
                await self._runs.requeue(
                    candidate,
                    queue_grace_seconds=self._deps.settings.analysis_queue_grace_seconds,
                )
            raise
        except Exception:
            # The trace goes to the log; the run keeps only a safe failure code.
            logger.exception(
                "analysis_pipeline_failed",
                extra={"correlation_id": str(run.correlation_id)},
            )
            await self._fail(run, "internal_error", plan)

    # --------------------------------------------------------------- pipeline

    async def _execute(self, run: AnalysisRun, plan: StagePlan) -> None:
        snapshot = (
            await self._runs.get_snapshot(run.input_snapshot_id)
            if run.input_snapshot_id is not None
            else None
        )
        if snapshot is None:
            await self._fail(run, "input_snapshot_missing", plan)
            return
        analysis_input = AnalysisInput.model_validate(snapshot.payload)

        completed: list[AnalysisStage] = ["queued"]
        warnings: list[AnalysisWarning] = []
        current: AnalysisStage = "collecting_evidence"
        await self._emit(
            run, plan, status=current, stage=current, completed=completed, warnings=warnings
        )

        evidence = await self._deps.evidence_provider.collect(
            self._evidence_requests(analysis_input)
        )
        run.evidence_snapshot_version = evidence.snapshot_version
        completed.append("collecting_evidence")

        current = await self._enter(run, plan, "building_context", current, completed, warnings)
        finance_tool = _finance_tool_for(analysis_input)
        completed.append("building_context")

        attempt = SimulationAttempt(outcome=None, reason=None, manifest=None)
        if "simulating" in plan.stages:
            current = await self._enter(run, plan, "simulating", current, completed, warnings)
            attempt = await self._simulate(run, analysis_input, evidence, finance_tool)
            completed.append("simulating")
            warnings.extend(self._simulation_warnings(attempt.outcome, attempt.reason))
        simulation = attempt.outcome
        simulation_reason = attempt.reason

        current = await self._enter(run, plan, "calculating_finance", current, completed, warnings)
        finance = calculate_finance(finance_input_from_analysis(analysis_input))
        completed.append("calculating_finance")

        current = await self._enter(run, plan, "scoring", current, completed, warnings)
        score = calculate_score(
            ScoringInput(
                planned_price_idr=analysis_input.pricing.average_selling_price_idr,
                analysis_radius_m=analysis_input.location.analysis_radius_m,
                capacity_units_day=analysis_input.operations.capacity_units_day,
                base_volume_units_day=analysis_input.operations.volume_units_day.base,
                fixed_cost_month_idr=analysis_input.operations.fixed_cost_month_idr,
                finance=finance,
                evidence=evidence,
            )
        )
        confidence = calculate_evidence_confidence(evidence)
        completed.append("scoring")

        warnings.extend(self._deterministic_warnings(evidence, score))
        status = self._final_status(evidence, score, simulation)

        current = await self._enter(run, plan, "composing_report", current, completed, warnings)
        report = compose_report(
            analysis_id=run.id,
            status=status,
            generated_at=datetime.now(UTC),
            analysis_input=analysis_input,
            evidence=evidence,
            finance=finance,
            score=score,
            confidence=confidence,
            warnings=warnings,
            simulation=simulation,
            simulation_reason=simulation_reason,
        )
        completed.append("composing_report")

        current = await self._enter(run, plan, "validating_report", current, completed, warnings)
        violations = validate_report(report)
        completed.append("validating_report")
        if violations:
            # The violations name fields, never provider text, so they are safe
            # to log. The client only ever sees the failure code.
            logger.error(
                "report_validation_failed",
                extra={
                    "correlation_id": str(run.correlation_id),
                    "violations": violations,
                },
            )
            await self._fail(run, "report_validation_failed", plan, attempt=attempt)
            return

        await self._renew_or_raise(run)
        self._persist(run, evidence, report, attempt)
        run.status = status
        run.current_stage = "validating_report"
        run.completed_stages = [str(stage) for stage in completed]
        run.warnings = [warning.model_dump(mode="json") for warning in warnings]
        run.score = score.score
        run.interpretation = score.interpretation_label
        run.rule_version = SCORING_RULE_VERSION
        run.completed_at = datetime.now(UTC)
        run.lease_expires_at = None
        await self._runs.commit()
        await self._emit(
            run,
            plan,
            status=status,
            stage="validating_report",
            completed=completed,
            warnings=warnings,
        )

    # -------------------------------------------------------------- simulation

    async def _simulate(
        self,
        run: AnalysisRun,
        analysis_input: AnalysisInput,
        evidence: EvidenceSnapshot,
        finance_tool: FinanceTool,
    ) -> SimulationAttempt:
        settings = self._deps.settings
        environment = environment_id(run.id)
        try:
            directory = allocate_trace_directory(settings, environment)
        except OSError:
            logger.exception(
                "oasis_trace_allocation_failed",
                extra={"correlation_id": str(run.correlation_id)},
            )
            return SimulationAttempt(
                outcome=None,
                reason="Direktori trace untuk run ini tidak dapat disiapkan.",
                manifest=None,
            )

        cohort = cohort_from_settings(settings)
        budget = budget_from_settings(settings)
        manifest = build_manifest(
            settings,
            adapter_id=self._deps.oasis_adapter.adapter_id,
            environment=environment,
            cohort=cohort,
            budget=budget,
            trace=trace_artifact(directory, retention_days=settings.oasis_trace_retention_days),
            evidence_snapshot_version=evidence.snapshot_version,
            snapshot_hash=input_snapshot_hash(analysis_input.model_dump(mode="json")),
        )
        manifest = manifest.model_copy(
            update={
                "trace": manifest.trace.model_copy(
                    update={"object_key": str(directory / TRACE_FILE_NAME)}
                )
            }
        )

        request = self._build_request(run, analysis_input, evidence, cohort, budget)
        try:
            outcome = await self._deps.oasis_adapter.simulate(
                request,
                finance_tool=finance_tool,
                manifest=manifest,
            )
        except OasisError as error:
            logger.warning(
                "oasis_simulation_failed",
                extra={
                    "correlation_id": str(run.correlation_id),
                    "failure_code": error.failure_code,
                },
            )
            # The manifest is kept: the directory was created before the adapter
            # ran, so its retention record is owed regardless of the outcome.
            return SimulationAttempt(
                outcome=None,
                reason=error.reason,
                manifest=self._settle_trace(manifest, directory),
            )

        final = self._settle_trace(outcome.manifest, directory)
        return SimulationAttempt(
            outcome=outcome.model_copy(update={"manifest": final}),
            reason=None,
            manifest=final,
        )

    def _settle_trace(self, manifest: RunManifest, directory: Path) -> RunManifest:
        """Re-read the trace so checksum and size describe what is on disk."""
        settled = trace_artifact(
            directory, retention_days=self._deps.settings.oasis_trace_retention_days
        )
        return manifest.model_copy(
            update={"trace": settled.model_copy(update={"object_key": manifest.trace.object_key})}
        )

    def _build_request(
        self,
        run: AnalysisRun,
        analysis_input: AnalysisInput,
        evidence: EvidenceSnapshot,
        cohort: CohortManifest,
        budget: SimulationBudget,
    ) -> SimulationRequest:
        volume = analysis_input.operations.volume_units_day
        return build_simulation_request(
            analysis_id=run.id,
            correlation_id=run.correlation_id,
            salt=self._deps.settings.jwt_secret,
            business_type=analysis_input.business_type,
            concept_name=analysis_input.concept_name,
            area_id=analysis_input.location.area_id,
            analysis_radius_m=analysis_input.location.analysis_radius_m,
            price_idr=analysis_input.pricing.average_selling_price_idr,
            variable_cost_per_unit_idr=analysis_input.pricing.variable_cost_per_unit_idr,
            channels=list(analysis_input.channels),
            value_proposition=analysis_input.value_proposition,
            evidence=evidence,
            finance_bounds=FinanceBounds(
                volume_units_day_min=volume.min,
                volume_units_day_base=volume.base,
                volume_units_day_max=volume.max,
                variable_cost_per_unit_idr=analysis_input.pricing.variable_cost_per_unit_idr,
            ),
            finance_rule_version=FINANCE_RULE_VERSION,
            budget=budget,
            cohort=cohort,
            seed=self._deps.settings.oasis_seed,
        )

    # --------------------------------------------------------------- warnings

    @staticmethod
    def _simulation_warnings(
        simulation: SimulationOutcome | None, reason: str | None
    ) -> list[AnalysisWarning]:
        if simulation is None:
            return [
                AnalysisWarning(
                    code=SIMULATION_FAILED_WARNING,
                    stage="simulating",
                    message=(
                        "Simulasi agent tidak tersedia sehingga laporan hanya memuat "
                        "hasil deterministik. " + (reason or "")
                    ).strip(),
                )
            ]
        if simulation.status == "completed":
            return []
        return [
            AnalysisWarning(
                code=SIMULATION_PARTIAL_WARNING,
                stage="simulating",
                message=(
                    "Sebagian council agent tidak menghasilkan artifact yang valid "
                    "sehingga bagian simulasi tidak lengkap."
                ),
            )
        ]

    @staticmethod
    def _deterministic_warnings(
        evidence: EvidenceSnapshot, score: ScoreResult
    ) -> list[AnalysisWarning]:
        warnings: list[AnalysisWarning] = []
        if evidence.missing:
            missing_labels = ", ".join(
                METRIC_LABELS.get(entry.metric, entry.metric).lower() for entry in evidence.missing
            )
            warnings.append(
                AnalysisWarning(
                    code="evidence_missing",
                    stage="collecting_evidence",
                    message=f"Bukti berikut belum tersedia: {missing_labels}.",
                )
            )
        if score.status == "unavailable":
            warnings.append(
                AnalysisWarning(
                    code="score_unavailable",
                    stage="scoring",
                    message=(
                        "Skor kelayakan tidak dihitung karena sebagian dimensi belum "
                        "dapat dinilai. Bobot dimensi yang hilang tidak dialihkan."
                    ),
                )
            )
        return warnings

    @staticmethod
    def _final_status(
        evidence: EvidenceSnapshot,
        score: ScoreResult,
        simulation: SimulationOutcome | None,
    ) -> AnalysisStatus:
        degraded = bool(evidence.missing) or score.status == "unavailable"
        if simulation is None or simulation.status != "completed":
            degraded = True
        return "partial" if degraded else "completed"

    # -------------------------------------------------------------- persistence

    def _persist(
        self,
        run: AnalysisRun,
        evidence: EvidenceSnapshot,
        report: AnalysisReport,
        attempt: SimulationAttempt,
    ) -> None:
        self._runs.add_evidence_items(
            [
                EvidenceItem(
                    analysis_run_id=run.id,
                    metric=record.metric,
                    value=record.value,
                    unit=record.unit,
                    geography=record.geography.model_dump(mode="json"),
                    category_mapping_version=record.category_mapping_version,
                    source=record.source,
                    source_url=record.source_url,
                    observed_at=record.observed_at,
                    retrieved_at=record.retrieved_at,
                    quality=record.quality.model_dump(mode="json"),
                    limitations=list(record.limitations),
                )
                for record in evidence.items
            ]
        )
        if attempt.manifest is not None:
            self._persist_trace(run, attempt.manifest)
        if attempt.outcome is not None:
            self._persist_simulation(run, attempt.outcome)
        self._runs.add_report(
            AnalysisReportRecord(
                analysis_run_id=run.id,
                report_version=report.report_version,
                payload=report.model_dump(mode="json"),
            )
        )

    def _persist_trace(self, run: AnalysisRun, manifest: RunManifest) -> None:
        self._runs.add_trace_artifact(
            AgentTraceArtifact(
                analysis_run_id=run.id,
                environment_id=manifest.environment_id,
                object_key=manifest.trace.object_key,
                checksum=manifest.trace.checksum,
                byte_size=manifest.trace.byte_size,
                retention_until=datetime.now(UTC) + timedelta(days=manifest.trace.retention_days),
                access_scope=manifest.trace.access_scope,
                manifest=manifest.model_dump(mode="json"),
            )
        )

    def _persist_simulation(self, run: AnalysisRun, simulation: SimulationOutcome) -> None:
        manifest = simulation.manifest
        run.oasis_version = manifest.oasis_version
        run.camel_version = manifest.camel_version
        run.model_manifest = {
            "adapter_id": manifest.adapter_id,
            "provider": manifest.provider,
            "model_id": manifest.model_id,
            "seed": manifest.seed,
            "budget": manifest.budget.model_dump(mode="json"),
        }
        run.prompt_manifest = {
            "prompt_version": manifest.prompt_version,
            "cohort": manifest.cohort.model_dump(mode="json"),
        }

        produced: dict[ArtifactType, str] = {}
        for record in simulation.agent_runs:
            agent_run = AgentRun(
                id=uuid4(),
                analysis_run_id=run.id,
                agent_type=record.role,
                status=record.status,
                model_id=manifest.model_id,
                prompt_version=manifest.prompt_version,
                cohort_version=manifest.cohort.cohort_version,
                seed=manifest.seed,
                persona_count=manifest.budget.persona_count,
                round_limit=manifest.budget.round_limit,
                token_budget=manifest.budget.token_budget,
                total_tokens=record.total_tokens,
                duration_ms=record.duration_ms,
                schema_failures=record.schema_failures,
                failure_code=record.failure_code,
            )
            self._runs.add_agent_run(agent_run)
            self._runs.add_agent_instances(
                [
                    AgentInstance(
                        agent_run_id=agent_run.id,
                        agent_id=instance.agent_id,
                        role=instance.role,
                        archetype=instance.archetype,
                        profile_version=instance.profile_version,
                        model_id=instance.model_id,
                        allowed_actions=list(instance.allowed_actions),
                        activation_order=instance.activation_order,
                        total_tokens=instance.total_tokens,
                        duration_ms=instance.duration_ms,
                        outcome=instance.outcome,
                    )
                    for instance in record.instances
                ]
            )
            artifact_id = self._persist_artifact(run, agent_run, record, produced)
            if artifact_id is not None and record.artifact is not None:
                produced[record.artifact.artifact_type] = artifact_id

    def _persist_artifact(
        self,
        run: AnalysisRun,
        agent_run: AgentRun,
        record: AgentRunRecord,
        produced: dict[ArtifactType, str],
    ) -> str | None:
        if record.artifact is None:
            return None
        payload = record.artifact.model_dump(mode="json")
        # Provenance is what the council actually read, reported by the adapter
        # that built the prompt — not what the pipeline assumes it should have
        # read. A council that received nothing upstream records nothing, so a
        # broken hand-off shows up in the database instead of being papered over
        # by plausible-looking artifact IDs.
        sources = [
            produced[artifact_type]
            for artifact_type in record.consumed_artifact_types
            if artifact_type in produced
        ]
        artifact = AgentArtifact(
            id=uuid4(),
            analysis_run_id=run.id,
            agent_run_id=agent_run.id,
            artifact_type=record.artifact.artifact_type,
            schema_version=record.artifact.schema_version,
            payload=payload,
            source_artifact_ids=sources,
            validation_status=record.validation_status,
            checksum=_checksum(payload),
        )
        self._runs.add_agent_artifact(artifact)
        return str(artifact.id)

    # ------------------------------------------------------------------ events

    async def _enter(
        self,
        run: AnalysisRun,
        plan: StagePlan,
        stage: AnalysisStage,
        current: AnalysisStage,
        completed: list[AnalysisStage],
        warnings: list[AnalysisWarning],
    ) -> AnalysisStage:
        plan.require_transition(current, stage)
        run.status = stage
        run.current_stage = stage
        # Renewed on every transition: the lease says "a worker is still moving
        # this run forward", and a stage boundary is the only evidence of that
        # which cannot be faked by a process that has already died.
        await self._renew_or_raise(run)
        await self._runs.commit()
        await self._emit(
            run, plan, status=stage, stage=stage, completed=completed, warnings=warnings
        )
        return stage

    async def _emit(
        self,
        run: AnalysisRun,
        plan: StagePlan,
        *,
        status: AnalysisStatus,
        stage: AnalysisStage,
        completed: list[AnalysisStage],
        warnings: list[AnalysisWarning],
        failure_code: str | None = None,
    ) -> None:
        sequence = await self._runs.next_event_sequence(run.id)
        occurred_at = datetime.now(UTC)
        record = AnalysisEventRecord(
            analysis_run_id=run.id,
            sequence=sequence,
            status=status,
            current_stage=stage,
            completed_stages=[str(item) for item in completed],
            skipped_stages=[str(item) for item in plan.skipped],
            percent=plan.percent(completed),
            message=STAGE_MESSAGES[stage],
            warnings=[warning.model_dump(mode="json") for warning in warnings],
            failure_code=failure_code,
            correlation_id=run.correlation_id,
            occurred_at=occurred_at,
        )
        self._runs.add_event(record)
        await self._runs.commit()
        await self._deps.publisher.publish(
            AnalysisEvent(
                event_id=str(sequence),
                analysis_id=run.id,
                status=status,
                current_stage=stage,
                completed_stages=list(completed),
                skipped_stages=list(plan.skipped),
                percent=plan.percent(completed),
                message=STAGE_MESSAGES[stage],
                warnings=[
                    AnalysisEventWarning(
                        code=warning.code, stage=warning.stage, message=warning.message
                    )
                    for warning in warnings
                ],
                failure_code=failure_code,
                correlation_id=run.correlation_id,
                occurred_at=occurred_at,
            )
        )

    async def _fail(
        self,
        run: AnalysisRun,
        failure_code: str,
        plan: StagePlan,
        *,
        attempt: SimulationAttempt | None = None,
    ) -> None:
        attempt_count = run.attempt_count
        await self._runs.rollback()
        reloaded = await self._runs.get(run.id)
        if (
            reloaded is None
            or reloaded.attempt_count != attempt_count
            or reloaded.status in {"queued", "completed", "partial", "failed", "cancelled"}
        ):
            logger.warning(
                "analysis_failure_ignored_for_fenced_attempt",
                extra={"analysis_id": str(run.id), "attempt_count": attempt_count},
            )
            return
        stage = cast(AnalysisStage, reloaded.current_stage)
        reloaded.status = "failed"
        reloaded.failure_code = failure_code
        reloaded.completed_at = datetime.now(UTC)
        reloaded.lease_expires_at = None
        if failure_code == WORKER_LOST_FAILURE_CODE and all(
            item.get("code") != WORKER_LOST_WARNING for item in reloaded.warnings
        ):
            reloaded.warnings = [
                *reloaded.warnings,
                AnalysisWarning(
                    code=WORKER_LOST_WARNING,
                    stage=cast(AnalysisStage, reloaded.current_stage),
                    message=WORKER_LOST_MESSAGE,
                ).model_dump(mode="json"),
            ]
        if attempt is not None and attempt.manifest is not None:
            # A failed run still allocated a trace directory, and retention has
            # to know about it.
            self._persist_trace(reloaded, attempt.manifest)
        await self._runs.commit()
        await self._emit(
            reloaded,
            plan,
            status="failed",
            stage=stage,
            completed=[cast(AnalysisStage, item) for item in reloaded.completed_stages],
            warnings=[AnalysisWarning.model_validate(item) for item in reloaded.warnings],
            failure_code=failure_code,
        )

    async def _renew_or_raise(self, run: AnalysisRun) -> None:
        owned = await self._runs.renew_lease(
            run.id,
            attempt_count=run.attempt_count,
            lease_seconds=self._deps.settings.analysis_lease_seconds,
        )
        if not owned:
            raise AnalysisLeaseLostError

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def _evidence_requests(analysis_input: AnalysisInput) -> list[EvidenceRequest]:
        geography = Geography(
            type="radius",
            area_id=analysis_input.location.area_id,
            center_id=analysis_input.location.area_id,
            meters=analysis_input.location.analysis_radius_m,
        )
        return [
            EvidenceRequest(metric=metric, geography=geography)
            for metric in REQUIRED_EVIDENCE_METRICS
        ]


def build_pipeline(
    session: AsyncSession,
    *,
    settings: Settings,
    evidence_provider: EvidenceProvider,
    oasis_adapter: OasisAdapter,
    publisher: AnalysisEventPublisher | None = None,
) -> AnalysisPipeline:
    return AnalysisPipeline(
        session,
        PipelineDependencies(
            settings=settings,
            evidence_provider=evidence_provider,
            oasis_adapter=oasis_adapter,
            publisher=publisher or NullEventPublisher(),
        ),
    )
