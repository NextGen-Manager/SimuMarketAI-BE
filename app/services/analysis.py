from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.correlation import get_correlation_id
from app.core.errors import (
    ConflictError,
    EducationContentUnavailableError,
    EducationGateError,
    NotFoundError,
)
from app.domain.analysis_state import (
    DETERMINISTIC_STAGE_PLAN,
    SKIP_REASON_SIMULATING,
    STAGE_MESSAGES,
    AnalysisStage,
    AnalysisStatus,
)
from app.domain.auth import IdentityContext
from app.domain.evidence import (
    METRIC_LABELS,
    REQUIRED_EVIDENCE_METRICS,
    EvidenceProvider,
    EvidenceRequest,
    EvidenceSnapshot,
    Geography,
)
from app.domain.taxonomy import BusinessType
from app.engines.evidence_confidence import calculate_evidence_confidence
from app.engines.finance import (
    InvalidFinanceInputError,
    calculate_finance,
    finance_input_from_analysis,
)
from app.engines.report import compose_report
from app.engines.report_validation import validate_report
from app.engines.scoring import ScoringInput, calculate_score
from app.integrations.evidence import select_evidence_provider
from app.persistence.models import (
    AnalysisReportRecord,
    AnalysisRun,
    EvidenceItem,
)
from app.repositories.analysis import AnalysisRepository
from app.repositories.business import BusinessRepository
from app.repositories.education import EducationRepository
from app.repositories.identity import IdentityRepository
from app.schemas.analysis import (
    SCORING_RULE_VERSION,
    AnalysisAccepted,
    AnalysisCreate,
    AnalysisInput,
    AnalysisListItem,
    AnalysisProgress,
    AnalysisRead,
    AnalysisReport,
    AnalysisWarning,
)
from app.services.access import require_owner_workspace
from app.services.audit import audit_event
from app.services.education import EducationService

logger = logging.getLogger(__name__)


def _correlation_uuid() -> UUID:
    raw = get_correlation_id()
    return UUID(raw) if raw else uuid4()


class AnalysisService:
    """Runs the deterministic analysis pipeline and owns its state machine.

    Phase 3 executes the pipeline inline. The stage plan, warnings, and
    persistence are already shaped the way a Celery task would use them, so
    moving execution to a worker in phase 4 does not change the contract.
    """

    def __init__(
        self,
        session: AsyncSession,
        identity: IdentityContext,
        settings: Settings,
        evidence_provider: EvidenceProvider | None = None,
    ) -> None:
        self._session = session
        self._identity = identity
        self._settings = settings
        self._runs = AnalysisRepository(session, identity)
        self._businesses = BusinessRepository(session)
        self._identity_repository = IdentityRepository(session)
        self._education = EducationService(
            EducationRepository(session, identity), self._businesses, identity
        )
        self._provider = select_evidence_provider(settings, evidence_provider)

    # ------------------------------------------------------------------ use cases

    async def create(
        self, payload: AnalysisCreate, *, idempotency_key: str | None = None
    ) -> AnalysisAccepted:
        await require_owner_workspace(self._businesses, self._identity)

        analysis_input = AnalysisInput.model_validate(payload.model_dump())
        if idempotency_key:
            existing = await self._runs.find_by_idempotency_key(idempotency_key)
            if existing is not None:
                await self._require_same_input(existing, analysis_input)
                return self._accepted(existing)

        prerequisites = await self._education.prerequisites(payload.business_type)
        if not prerequisites.content_available:
            raise EducationContentUnavailableError()
        if not prerequisites.satisfied:
            titles = ", ".join(module.title for module in prerequisites.outstanding)
            raise EducationGateError(
                f"Selesaikan modul edukasi prasyarat sebelum menjalankan analisis: {titles}."
            )

        run, created = await self._create_run(analysis_input, idempotency_key)
        if not created:
            await self._require_same_input(run, analysis_input)
            return self._accepted(run)
        await self._execute(
            run,
            analysis_input,
        )
        return self._accepted(run)

    async def list_analyses(self) -> list[AnalysisListItem]:
        await require_owner_workspace(self._businesses, self._identity)
        runs = await self._runs.list_runs()
        return [
            AnalysisListItem(
                analysis_id=run.id,
                status=cast(AnalysisStatus, run.status),
                concept_name=run.concept_name,
                area_name=run.area_name,
                business_type=cast(BusinessType, run.business_type),
                score=run.score,
                interpretation=run.interpretation,
                rule_version=run.rule_version or SCORING_RULE_VERSION,
                created_at=run.created_at,
            )
            for run in runs
        ]

    async def get(self, analysis_id: UUID) -> AnalysisRead:
        run = await self._require_run(analysis_id)
        completed = [cast(AnalysisStage, stage) for stage in run.completed_stages]
        current = cast(AnalysisStage, run.current_stage)
        return AnalysisRead(
            analysis_id=run.id,
            status=cast(AnalysisStatus, run.status),
            concept_name=run.concept_name,
            area_name=run.area_name,
            business_type=cast(BusinessType, run.business_type),
            score=run.score,
            interpretation=run.interpretation,
            rule_version=run.rule_version or SCORING_RULE_VERSION,
            evidence_snapshot_version=run.evidence_snapshot_version or "",
            correlation_id=run.correlation_id,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            failure_code=run.failure_code,
            progress=AnalysisProgress(
                completed_stages=completed,
                skipped_stages=[cast(AnalysisStage, stage) for stage in run.skipped_stages],
                current_stage=current,
                message=STAGE_MESSAGES[current],
                percent=DETERMINISTIC_STAGE_PLAN.percent(completed),
            ),
            warnings=[AnalysisWarning.model_validate(item) for item in run.warnings],
        )

    async def get_report(self, analysis_id: UUID) -> AnalysisReport:
        await self._require_run(analysis_id)
        record = await self._runs.get_report(analysis_id)
        if record is None:
            raise NotFoundError("Laporan untuk analisis ini belum tersedia.")
        return AnalysisReport.model_validate(record.payload)

    # ------------------------------------------------------------------ pipeline

    async def _create_run(
        self, analysis_input: AnalysisInput, idempotency_key: str | None
    ) -> tuple[AnalysisRun, bool]:
        snapshot = await self._runs.create_snapshot(analysis_input.model_dump(mode="json"))
        run = AnalysisRun(
            id=uuid4(),
            user_id=self._identity.user_id,
            status="queued",
            current_stage="queued",
            completed_stages=["queued"],
            skipped_stages=list(DETERMINISTIC_STAGE_PLAN.skipped),
            warnings=[],
            concept_name=analysis_input.concept_name,
            area_name=analysis_input.location.area_name or analysis_input.location.area_id,
            business_type=analysis_input.business_type,
            input_snapshot_id=snapshot.id,
            correlation_id=_correlation_uuid(),
            idempotency_key=idempotency_key,
            rule_version=SCORING_RULE_VERSION,
            started_at=datetime.now(UTC),
        )
        self._runs.add_run(run)
        self._identity_repository.add_audit_event(
            audit_event(
                actor_user_id=self._identity.user_id,
                action="analysis.create",
                resource_type="analysis_run",
                resource_id=run.id,
                outcome="success",
            )
        )
        try:
            await self._runs.commit()
        except IntegrityError:
            await self._runs.rollback()
            if idempotency_key:
                existing = await self._runs.find_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return existing, False
            raise
        return run, True

    async def _execute(
        self,
        run: AnalysisRun,
        analysis_input: AnalysisInput,
    ) -> None:
        try:
            await self._run_pipeline(run, analysis_input)
        except InvalidFinanceInputError:
            await self._fail(run, "invalid_finance_input")
        except Exception:
            # The trace goes to the log; the run carries only a safe failure code.
            logger.exception(
                "analysis_pipeline_failed",
                extra={"correlation_id": str(run.correlation_id)},
            )
            await self._fail(run, "internal_error")

    async def _run_pipeline(
        self,
        run: AnalysisRun,
        analysis_input: AnalysisInput,
    ) -> None:
        plan = DETERMINISTIC_STAGE_PLAN
        completed: list[AnalysisStage] = ["queued"]
        current: AnalysisStage = "queued"
        warnings: list[AnalysisWarning] = [
            AnalysisWarning(
                code="simulation_skipped",
                stage="simulating",
                message=SKIP_REASON_SIMULATING,
            )
        ]

        def enter(stage: AnalysisStage) -> None:
            nonlocal current
            plan.require_transition(current, stage)
            current = stage
            run.status = stage
            run.current_stage = stage

        enter("collecting_evidence")
        evidence = await self._provider.collect(self._evidence_requests(analysis_input))
        run.evidence_snapshot_version = evidence.snapshot_version
        completed.append("collecting_evidence")

        enter("building_context")
        scoring_input = ScoringInput(
            planned_price_idr=analysis_input.pricing.average_selling_price_idr,
            analysis_radius_m=analysis_input.location.analysis_radius_m,
            capacity_units_day=analysis_input.operations.capacity_units_day,
            base_volume_units_day=analysis_input.operations.volume_units_day.base,
            fixed_cost_month_idr=analysis_input.operations.fixed_cost_month_idr,
            finance=calculate_finance(finance_input_from_analysis(analysis_input)),
            evidence=evidence,
        )
        completed.append("building_context")

        enter("calculating_finance")
        finance = scoring_input.finance
        completed.append("calculating_finance")

        enter("scoring")
        score = calculate_score(scoring_input)
        confidence = calculate_evidence_confidence(evidence)
        completed.append("scoring")

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

        status: AnalysisStatus = (
            "partial" if evidence.missing or score.status == "unavailable" else "completed"
        )

        enter("composing_report")
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
        )
        completed.append("composing_report")

        enter("validating_report")
        violations = validate_report(report)
        completed.append("validating_report")
        if violations:
            logger.error(
                "report_validation_failed",
                extra={
                    "correlation_id": str(run.correlation_id),
                    "violations": violations,
                },
            )
            await self._fail(run, "report_validation_failed")
            return

        self._persist(run, evidence, report)
        run.status = status
        run.completed_stages = [str(stage) for stage in completed]
        run.warnings = [warning.model_dump(mode="json") for warning in warnings]
        run.score = score.score
        run.interpretation = score.interpretation_label
        run.rule_version = SCORING_RULE_VERSION
        run.completed_at = datetime.now(UTC)
        await self._runs.commit()

    def _persist(
        self, run: AnalysisRun, evidence: EvidenceSnapshot, report: AnalysisReport
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
        self._runs.add_report(
            AnalysisReportRecord(
                analysis_run_id=run.id,
                report_version=report.report_version,
                payload=report.model_dump(mode="json"),
            )
        )

    async def _fail(self, run: AnalysisRun, failure_code: str) -> None:
        await self._runs.rollback()
        reloaded = await self._runs.get(run.id)
        if reloaded is None:
            return
        reloaded.status = "failed"
        reloaded.failure_code = failure_code
        reloaded.completed_at = datetime.now(UTC)
        await self._runs.commit()

    # ------------------------------------------------------------------ helpers

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

    async def _require_run(self, analysis_id: UUID) -> AnalysisRun:
        await require_owner_workspace(self._businesses, self._identity)
        run = await self._runs.get(analysis_id)
        if run is None:
            raise NotFoundError()
        return run

    async def _require_same_input(
        self,
        run: AnalysisRun,
        analysis_input: AnalysisInput,
    ) -> None:
        if run.input_snapshot_id is None:
            raise ConflictError(
                "Idempotency-Key sudah digunakan oleh run tanpa snapshot input yang valid."
            )
        snapshot = await self._runs.get_snapshot(run.input_snapshot_id)
        expected = analysis_input.model_dump(mode="json")
        if snapshot is None or snapshot.payload != expected:
            raise ConflictError(
                "Idempotency-Key sudah digunakan untuk isian analisis yang berbeda."
            )

    @staticmethod
    def _accepted(run: AnalysisRun) -> AnalysisAccepted:
        return AnalysisAccepted(
            analysis_id=run.id,
            status=cast(AnalysisStatus, run.status),
            created_at=run.created_at,
            status_url=f"/v1/analyses/{run.id}",
            events_url=f"/v1/analyses/{run.id}/events",
        )
