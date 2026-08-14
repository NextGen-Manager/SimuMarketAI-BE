from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models import AuditEvent, AuthSession, User


class IdentityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_user(self, *, email: str, display_name: str, password_hash: str) -> User:
        user = User(email=email, display_name=display_name, password_hash=password_hash)
        self._session.add(user)
        await self._session.flush()
        return user

    async def get_user_by_email(self, email: str) -> User | None:
        return cast(
            User | None, await self._session.scalar(select(User).where(User.email == email))
        )

    async def get_user(self, user_id: UUID) -> User | None:
        return cast(
            User | None,
            await self._session.scalar(
                select(User).where(User.id == user_id, User.is_active.is_(True))
            ),
        )

    async def create_session(
        self, *, user_id: UUID, refresh_token_hash: str, expires_at: datetime
    ) -> AuthSession:
        auth_session = AuthSession(
            user_id=user_id,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
        )
        self._session.add(auth_session)
        await self._session.flush()
        return auth_session

    async def get_active_session(
        self, *, session_id: UUID, user_id: UUID, now: datetime
    ) -> AuthSession | None:
        return cast(
            AuthSession | None,
            await self._session.scalar(
                select(AuthSession).where(
                    AuthSession.id == session_id,
                    AuthSession.user_id == user_id,
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                )
            ),
        )

    async def get_session_for_refresh(self, *, refresh_token_hash: str) -> AuthSession | None:
        return cast(
            AuthSession | None,
            await self._session.scalar(
                select(AuthSession)
                .where(AuthSession.refresh_token_hash == refresh_token_hash)
                .with_for_update()
            ),
        )

    async def revoke_session(
        self, auth_session: AuthSession, *, now: datetime, replaced_by_id: UUID | None = None
    ) -> None:
        auth_session.revoked_at = now
        auth_session.last_used_at = now
        auth_session.replaced_by_id = replaced_by_id
        await self._session.flush()

    def add_audit_event(self, event: AuditEvent) -> None:
        self._session.add(event)

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
