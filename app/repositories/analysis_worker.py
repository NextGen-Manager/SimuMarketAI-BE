"""Database access for the analysis worker.

The worker has no authenticated user, so this repository is deliberately
unscoped — and deliberately separate from `AnalysisRepository`, which must never
grow an unscoped accessor. Nothing here is reachable from an HTTP request.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

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

    async def claim(self, analysis_id: UUID, *, first_stage: str) -> AnalysisRun | None:
        """Move a run out of `queued` exactly once.

        This is the whole defence against duplicate Celery delivery. The update
        is conditional on the run still being `queued`, so a redelivered task
        finds nothing to claim and returns instead of producing a second set of
        evidence, artifacts, and reports. It also blocks reprocessing a run that
        already reached a terminal status, since such a run is no longer queued.
        """
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(AnalysisRun)
                .where(AnalysisRun.id == analysis_id, AnalysisRun.status == "queued")
                .values(
                    status=first_stage,
                    current_stage=first_stage,
                    started_at=datetime.now(UTC),
                )
            ),
        )
        await self._session.commit()
        if result.rowcount != 1:
            return None
        return await self.get(analysis_id)

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
