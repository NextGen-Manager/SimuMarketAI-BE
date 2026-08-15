from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import IdentityContext
from app.persistence.models import (
    EducationModule,
    EducationProgress,
    EducationQuestion,
)


class EducationRepository:
    """Content is public; progress is always scoped to the authenticated user."""

    def __init__(self, session: AsyncSession, identity: IdentityContext) -> None:
        self._session = session
        self._identity = identity

    async def list_published(self) -> list[EducationModule]:
        rows = await self._session.scalars(
            select(EducationModule)
            .where(EducationModule.published_at.is_not(None))
            .order_by(EducationModule.position, EducationModule.title)
        )
        return list(rows)

    async def get_published(self, module_id: UUID) -> EducationModule | None:
        return cast(
            EducationModule | None,
            await self._session.scalar(
                select(EducationModule).where(
                    EducationModule.id == module_id,
                    EducationModule.published_at.is_not(None),
                )
            ),
        )

    async def questions_for(self, module_id: UUID) -> list[EducationQuestion]:
        rows = await self._session.scalars(
            select(EducationQuestion)
            .where(EducationQuestion.module_id == module_id)
            .order_by(EducationQuestion.position)
        )
        return list(rows)

    async def progress_for(
        self, module_ids: list[UUID]
    ) -> dict[tuple[UUID, str], EducationProgress]:
        if not module_ids:
            return {}
        rows = await self._session.scalars(
            select(EducationProgress).where(
                EducationProgress.user_id == self._identity.user_id,
                EducationProgress.module_id.in_(module_ids),
            )
        )
        return {(row.module_id, row.content_version): row for row in rows}

    async def get_progress(self, module_id: UUID, content_version: str) -> EducationProgress | None:
        return cast(
            EducationProgress | None,
            await self._session.scalar(
                select(EducationProgress).where(
                    EducationProgress.user_id == self._identity.user_id,
                    EducationProgress.module_id == module_id,
                    EducationProgress.content_version == content_version,
                )
            ),
        )

    def add_progress(self, progress: EducationProgress) -> None:
        self._session.add(progress)

    async def flush(self) -> None:
        await self._session.flush()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
