from __future__ import annotations

import logging
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
from app.domain.analysis_events import AnalysisEvent, AnalysisEventWarning
from app.domain.analysis_state import (
    STAGE_MESSAGES,
    AnalysisStage,
    AnalysisStatus,
    plan_from_stored,
)
from app.domain.auth import IdentityContext
from app.domain.taxonomy import BusinessType
from app.persistence.models import AnalysisEventRecord, AnalysisRun
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
from app.services.analysis_queue import AnalysisDispatcher, CeleryAnalysisDispatcher
from app.services.audit import audit_event
from app.services.education import EducationService

logger = logging.getLogger(__name__)


def _correlation_uuid() -> UUID:
    raw = get_correlation_id()
    return UUID(raw) if raw else uuid4()


class AnalysisService:
    """Owns run creation and every authenticated read of a run.

    It does not execute the pipeline. `create` validates, persists a `queued`
    run, commits, and hands the ID to the worker, so the HTTP response returns
    long before the simulation does any work. Execution lives in
    `app.services.analysis_pipeline`, driven by `app.workers.analysis`.
    """

    def __init__(
        self,
        session: AsyncSession,
        identity: IdentityContext,
        settings: Settings,
        dispatcher: AnalysisDispatcher | None = None,
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
        self._dispatcher = dispatcher or CeleryAnalysisDispatcher()

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
            # A concurrent request won the unique constraint. Returning its run
            # is what keeps two callers with one key from executing twice.
            await self._require_same_input(run, analysis_input)
            return self._accepted(run)

        self._dispatcher.dispatch(run.id, run.correlation_id)
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
        return self._read(run)

    async def get_report(self, analysis_id: UUID) -> AnalysisReport:
        await self._require_run(analysis_id)
        record = await self._runs.get_report(analysis_id)
        if record is None:
            raise NotFoundError("Laporan untuk analisis ini belum tersedia.")
        return AnalysisReport.model_validate(record.payload)

    # ------------------------------------------------------------------ events

    async def events_since(self, analysis_id: UUID, after_sequence: int) -> list[AnalysisEvent]:
        """Replay persisted transitions, or synthesise one from the run itself.

        PostgreSQL is the source of truth for progress. A client that connects
        after a run finished still gets a terminal event this way, instead of
        waiting for a publish that will never come. The fallback covers a run
        that has not emitted anything yet: it is still `queued`, and saying so
        is better than an empty stream.
        """
        run = await self._require_run(analysis_id)
        records = await self._runs.list_events(analysis_id, after_sequence=after_sequence)
        if records:
            return [self._event_from_record(record) for record in records]
        if after_sequence == 0:
            return [self._event_from_run(run, sequence=0)]
        return []

    async def release(self) -> None:
        """End the current read transaction without closing the session.

        A long-lived SSE stream polls the same session repeatedly; leaving each
        read transaction open would pin a snapshot for the life of the stream.
        """
        await self._runs.rollback()

    # ------------------------------------------------------------------ creation

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
            skipped_stages=[],
            warnings=[],
            concept_name=analysis_input.concept_name,
            area_name=analysis_input.location.area_name or analysis_input.location.area_id,
            business_type=analysis_input.business_type,
            input_snapshot_id=snapshot.id,
            correlation_id=_correlation_uuid(),
            idempotency_key=idempotency_key,
            rule_version=SCORING_RULE_VERSION,
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
            # Commit before dispatch: the worker must never be handed an ID that
            # is not yet durable.
            await self._runs.commit()
        except IntegrityError:
            await self._runs.rollback()
            if idempotency_key:
                existing = await self._runs.find_by_idempotency_key(idempotency_key)
                if existing is not None:
                    return existing, False
            raise
        return run, True

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _read(run: AnalysisRun) -> AnalysisRead:
        completed = [cast(AnalysisStage, stage) for stage in run.completed_stages]
        current = cast(AnalysisStage, run.current_stage)
        plan = plan_from_stored([str(stage) for stage in run.skipped_stages])
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
                percent=plan.percent(completed),
            ),
            warnings=[AnalysisWarning.model_validate(item) for item in run.warnings],
        )

    @staticmethod
    def _event_from_record(record: AnalysisEventRecord) -> AnalysisEvent:
        return AnalysisEvent(
            event_id=str(record.sequence),
            analysis_id=record.analysis_run_id,
            status=cast(AnalysisStatus, record.status),
            current_stage=cast(AnalysisStage, record.current_stage),
            completed_stages=[cast(AnalysisStage, item) for item in record.completed_stages],
            skipped_stages=[cast(AnalysisStage, item) for item in record.skipped_stages],
            percent=record.percent,
            message=record.message,
            warnings=[AnalysisEventWarning.model_validate(item) for item in record.warnings],
            correlation_id=record.correlation_id,
            occurred_at=record.occurred_at,
        )

    @staticmethod
    def _event_from_run(run: AnalysisRun, *, sequence: int) -> AnalysisEvent:
        completed = [cast(AnalysisStage, stage) for stage in run.completed_stages]
        current = cast(AnalysisStage, run.current_stage)
        plan = plan_from_stored([str(stage) for stage in run.skipped_stages])
        return AnalysisEvent(
            event_id=str(sequence),
            analysis_id=run.id,
            status=cast(AnalysisStatus, run.status),
            current_stage=current,
            completed_stages=completed,
            skipped_stages=[cast(AnalysisStage, stage) for stage in run.skipped_stages],
            percent=plan.percent(completed),
            message=STAGE_MESSAGES[current],
            warnings=[AnalysisEventWarning.model_validate(item) for item in run.warnings],
            correlation_id=run.correlation_id,
            occurred_at=run.completed_at or run.started_at or run.created_at,
        )

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
