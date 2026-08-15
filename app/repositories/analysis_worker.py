"""Database access for the analysis worker.

The worker has no authenticated user, so this repository is deliberately
unscoped — and deliberately separate from `AnalysisRepository`, which must never
grow an unscoped accessor. Nothing here is reachable from an HTTP request.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, case, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.analysis_state import TERMINAL_STATUSES
from app.persistence.models import (
    AgentArtifact,
    AgentInstance,
    AgentRun,
    AgentTraceArtifact,
    AnalysisEventRecord,
    AnalysisReportRecord,
    AnalysisRun,
    EvidenceItem,
    InputSnapshot,
)


class AnalysisWorkerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(
        self,
        analysis_id: UUID,
        *,
        first_stage: str,
        lease_seconds: int,
        max_attempts: int,
    ) -> AnalysisRun | None:
        """Move a run out of `queued` exactly once and take a lease on it.

        This is the whole defence against duplicate Celery delivery. The update
        is conditional on the run still being `queued`, so a redelivered task
        finds nothing to claim and returns instead of producing a second set of
        evidence, artifacts, and reports. It also blocks reprocessing a run that
        already reached a terminal status, since such a run is no longer queued.

        The lease is what makes a crashed worker recoverable. Only the
        reconciler may put a run back into `queued`, so the claim itself stays
        strictly one-shot and recovery stays one explicit, auditable place.
        """
        now = datetime.now(UTC)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(AnalysisRun)
                .where(
                    AnalysisRun.id == analysis_id,
                    AnalysisRun.status == "queued",
                    AnalysisRun.attempt_count <= max_attempts,
                )
                .values(
                    status=first_stage,
                    current_stage=first_stage,
                    started_at=now,
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    # A recovered queued run already reserved its next attempt
                    # when it was requeued. A newly created run has no dispatch
                    # lease yet and starts attempt one here.
                    attempt_count=case(
                        (
                            AnalysisRun.lease_expires_at.is_(None),
                            AnalysisRun.attempt_count + 1,
                        ),
                        else_=AnalysisRun.attempt_count,
                    ),
                )
            ),
        )
        await self._session.commit()
        if result.rowcount != 1:
            return None
        return await self.get(analysis_id)

    async def renew_lease(
        self, analysis_id: UUID, *, attempt_count: int, lease_seconds: int
    ) -> bool:
        """Extend a lease only when this worker still owns the attempt."""
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(AnalysisRun)
                .where(
                    AnalysisRun.id == analysis_id,
                    AnalysisRun.attempt_count == attempt_count,
                    AnalysisRun.status.not_in(tuple(TERMINAL_STATUSES)),
                    AnalysisRun.status != "queued",
                )
                .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds))
            ),
        )
        return result.rowcount == 1

    async def clear_lease(self, analysis_id: UUID) -> None:
        """Forget the lease of a run that has reached a terminal status."""
        await self._session.execute(
            update(AnalysisRun).where(AnalysisRun.id == analysis_id).values(lease_expires_at=None)
        )

    # ------------------------------------------------------------- reconciler

    async def find_stuck(
        self, *, now: datetime, queue_grace_seconds: int, limit: int
    ) -> Sequence[AnalysisRun]:
        """Runs that no worker is executing.

        Two shapes qualify. A run still `queued` past the grace period never
        reached the broker — the API committed it and the dispatch failed. A run
        in a working stage with a lapsed lease had a worker that died. Both are
        invisible to Celery, which is why PostgreSQL has to be the one to notice.
        """
        stale_queue = now - timedelta(seconds=queue_grace_seconds)
        statement = (
            select(AnalysisRun)
            .where(
                AnalysisRun.status.not_in(tuple(TERMINAL_STATUSES)),
                or_(
                    (AnalysisRun.status == "queued")
                    & or_(
                        (AnalysisRun.lease_expires_at.is_(None))
                        & (AnalysisRun.created_at < stale_queue),
                        (AnalysisRun.lease_expires_at.is_not(None))
                        & (AnalysisRun.lease_expires_at < now),
                    ),
                    (AnalysisRun.status != "queued")
                    & (AnalysisRun.lease_expires_at.is_not(None))
                    & (AnalysisRun.lease_expires_at < now),
                    (AnalysisRun.status != "queued") & (AnalysisRun.lease_expires_at.is_(None)),
                ),
            )
            .order_by(AnalysisRun.created_at)
            .limit(limit)
        )
        return (await self._session.scalars(statement)).all()

    async def requeue(self, run: AnalysisRun, *, queue_grace_seconds: int) -> bool:
        """Put a stuck run back into `queued`, discarding the aborted attempt.

        Rows written by the dead attempt are removed after this process wins a
        conditional transition to `queued`. A rerun would
        otherwise append a second evidence set and a second batch of agent
        artifacts to the same analysis, and the report would silently describe
        two runs at once. Events are kept: they are the audit trail of what was
        attempted, and the SSE sequence has to keep moving forward.
        """
        now = datetime.now(UTC)
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(AnalysisRun)
                .where(
                    AnalysisRun.id == run.id,
                    AnalysisRun.status.not_in(tuple(TERMINAL_STATUSES)),
                    AnalysisRun.status == run.status,
                    AnalysisRun.attempt_count == run.attempt_count,
                    AnalysisRun.lease_expires_at == run.lease_expires_at,
                )
                .values(
                    status="queued",
                    current_stage="queued",
                    completed_stages=["queued"],
                    # While queued this timestamp is a dispatch grace deadline.
                    # If the recovery dispatch is itself lost, the next scan can
                    # safely try again after it expires without using created_at.
                    lease_expires_at=now + timedelta(seconds=queue_grace_seconds),
                    started_at=None,
                    attempt_count=AnalysisRun.attempt_count + 1,
                )
            ),
        )
        if result.rowcount != 1:
            await self._session.rollback()
            return False
        # Delete only after the conditional ownership transition succeeded. A
        # terminal run that won the race above must keep its report and trace.
        await self.discard_attempt(run.id)
        await self._session.commit()
        return True

    async def discard_attempt(self, analysis_id: UUID) -> None:
        agent_runs = select(AgentRun.id).where(AgentRun.analysis_run_id == analysis_id)
        await self._session.execute(
            delete(AgentInstance).where(AgentInstance.agent_run_id.in_(agent_runs))
        )
        await self._session.execute(
            delete(AgentArtifact).where(AgentArtifact.analysis_run_id == analysis_id)
        )
        await self._session.execute(delete(AgentRun).where(AgentRun.analysis_run_id == analysis_id))
        await self._session.execute(
            delete(AgentTraceArtifact).where(AgentTraceArtifact.analysis_run_id == analysis_id)
        )
        await self._session.execute(
            delete(EvidenceItem).where(EvidenceItem.analysis_run_id == analysis_id)
        )
        await self._session.execute(
            delete(AnalysisReportRecord).where(AnalysisReportRecord.analysis_run_id == analysis_id)
        )

    async def get(self, analysis_id: UUID) -> AnalysisRun | None:
        return cast(
            AnalysisRun | None,
            await self._session.scalar(select(AnalysisRun).where(AnalysisRun.id == analysis_id)),
        )

    async def get_snapshot(self, snapshot_id: UUID) -> InputSnapshot | None:
        return cast(
            InputSnapshot | None,
            await self._session.scalar(
                select(InputSnapshot).where(InputSnapshot.id == snapshot_id)
            ),
        )

    async def next_event_sequence(self, analysis_id: UUID) -> int:
        highest = await self._session.scalar(
            select(func.max(AnalysisEventRecord.sequence)).where(
                AnalysisEventRecord.analysis_run_id == analysis_id
            )
        )
        return int(highest or 0) + 1

    def add_event(self, record: AnalysisEventRecord) -> None:
        self._session.add(record)

    def add_evidence_items(self, items: Sequence[EvidenceItem]) -> None:
        self._session.add_all(list(items))

    def add_report(self, record: AnalysisReportRecord) -> None:
        self._session.add(record)

    def add_agent_run(self, record: AgentRun) -> None:
        self._session.add(record)

    def add_agent_instances(self, records: Sequence[AgentInstance]) -> None:
        self._session.add_all(list(records))

    def add_agent_artifact(self, record: AgentArtifact) -> None:
        self._session.add(record)

    def add_trace_artifact(self, record: AgentTraceArtifact) -> None:
        self._session.add(record)

    async def flush(self) -> None:
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
