from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import IdentityContext
from app.persistence.models import (
    AnalysisReportRecord,
    AnalysisRun,
    EvidenceItem,
    InputSnapshot,
)


class AnalysisRepository:
    """Every read is scoped to the authenticated user.

    There is no unscoped accessor on purpose: another tenant's run must be
    indistinguishable from a run that does not exist.
    """

    def __init__(self, session: AsyncSession, identity: IdentityContext) -> None:
        self._session = session
        self._identity = identity

    async def find_by_idempotency_key(self, key: str) -> AnalysisRun | None:
        return cast(
            AnalysisRun | None,
            await self._session.scalar(
                select(AnalysisRun).where(
                    AnalysisRun.user_id == self._identity.user_id,
                    AnalysisRun.idempotency_key == key,
                )
            ),
        )

    async def get(self, analysis_id: UUID) -> AnalysisRun | None:
        return cast(
            AnalysisRun | None,
            await self._session.scalar(
                select(AnalysisRun).where(
                    AnalysisRun.id == analysis_id,
                    AnalysisRun.user_id == self._identity.user_id,
                )
            ),
        )

    async def list_runs(self, *, limit: int = 50) -> list[AnalysisRun]:
        rows = await self._session.scalars(
            select(AnalysisRun)
            .where(AnalysisRun.user_id == self._identity.user_id)
            .order_by(AnalysisRun.created_at.desc())
            .limit(limit)
        )
        return list(rows)

    async def create_snapshot(self, payload: dict[str, object]) -> InputSnapshot:
        snapshot = InputSnapshot(payload=payload)
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def get_snapshot(self, snapshot_id: UUID) -> InputSnapshot | None:
        return cast(
            InputSnapshot | None,
            await self._session.scalar(
                select(InputSnapshot).where(InputSnapshot.id == snapshot_id)
            ),
        )

    def add_run(self, run: AnalysisRun) -> None:
        self._session.add(run)

    def add_evidence_items(self, items: list[EvidenceItem]) -> None:
        self._session.add_all(items)

    def add_report(self, record: AnalysisReportRecord) -> None:
        self._session.add(record)

    async def get_report(self, analysis_run_id: UUID) -> AnalysisReportRecord | None:
        return cast(
            AnalysisReportRecord | None,
            await self._session.scalar(
                select(AnalysisReportRecord)
                .join(AnalysisRun, AnalysisRun.id == AnalysisReportRecord.analysis_run_id)
                .where(
                    AnalysisReportRecord.analysis_run_id == analysis_run_id,
                    AnalysisRun.user_id == self._identity.user_id,
                )
            ),
        )

    async def flush(self) -> None:
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
