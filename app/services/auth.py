from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from anyio import to_thread
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, UnauthorizedError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    verify_password,
)
from app.core.time import as_utc
from app.domain.auth import IdentityContext
from app.persistence.models import User
from app.repositories.business import BusinessRepository
from app.repositories.identity import IdentityRepository
from app.schemas.auth import MembershipRead, SessionResponse, UserRead
from app.services.audit import audit_event


@dataclass(frozen=True, slots=True)
class IssuedSession:
    response: SessionResponse
    access_token: str
    refresh_token: str
    session_id: UUID


class AuthService:
    def __init__(
        self,
        identity_repository: IdentityRepository,
        business_repository: BusinessRepository,
        settings: Settings,
    ) -> None:
        self._identity = identity_repository
        self._businesses = business_repository
        self._settings = settings

    async def register(self, *, email: str, display_name: str, password: str) -> IssuedSession:
        normalized_email = email.strip().casefold()
        if await self._identity.get_user_by_email(normalized_email) is not None:
            raise ConflictError("Email sudah terdaftar.")

        password_digest = await to_thread.run_sync(hash_password, password)
        try:
            user = await self._identity.create_user(
                email=normalized_email,
                display_name=display_name.strip(),
                password_hash=password_digest,
            )
            issued = await self._issue_session(user)
            self._identity.add_audit_event(
                audit_event(
                    actor_user_id=user.id,
                    action="auth.register",
                    resource_type="user",
                    resource_id=user.id,
                    outcome="success",
                )
            )
            await self._identity.commit()
            return issued
        except IntegrityError as exc:
            await self._identity.rollback()
            raise ConflictError("Email sudah terdaftar.") from exc

    async def login(self, *, email: str, password: str) -> IssuedSession:
        normalized_email = email.strip().casefold()
        user = await self._identity.get_user_by_email(normalized_email)
        password_matches = await to_thread.run_sync(
            verify_password, password, user.password_hash if user else None
        )
        if user is None or not user.is_active or not password_matches:
            self._identity.add_audit_event(
                audit_event(
                    actor_user_id=user.id if user else None,
                    action="auth.login",
                    resource_type="session",
                    resource_id=None,
                    outcome="failure",
                )
            )
            await self._identity.commit()
            raise UnauthorizedError("Email atau kata sandi tidak sesuai.")

        issued = await self._issue_session(user)
        self._identity.add_audit_event(
            audit_event(
                actor_user_id=user.id,
                action="auth.login",
                resource_type="session",
                resource_id=None,
                outcome="success",
            )
        )
        await self._identity.commit()
        return issued

    async def refresh(self, refresh_token: str) -> IssuedSession:
        now = datetime.now(UTC)
        old_session = await self._identity.get_session_for_refresh(
            refresh_token_hash=hash_refresh_token(refresh_token)
        )
        if (
            old_session is None
            or old_session.revoked_at is not None
            or as_utc(old_session.expires_at) <= now
        ):
            raise UnauthorizedError("Sesi sudah berakhir. Silakan masuk kembali.")

        user = await self._identity.get_user(old_session.user_id)
        if user is None:
            raise UnauthorizedError("Sesi sudah berakhir. Silakan masuk kembali.")

        issued = await self._issue_session(user)
        await self._identity.revoke_session(
            old_session,
            now=now,
            replaced_by_id=issued.session_id,
        )
        self._identity.add_audit_event(
            audit_event(
                actor_user_id=user.id,
                action="auth.refresh",
                resource_type="session",
                resource_id=old_session.id,
                outcome="success",
            )
        )
        await self._identity.commit()
        return issued

    async def logout(self, identity: IdentityContext) -> None:
        now = datetime.now(UTC)
        auth_session = await self._identity.get_active_session(
            session_id=identity.session_id,
            user_id=identity.user_id,
            now=now,
        )
        if auth_session is not None:
            await self._identity.revoke_session(auth_session, now=now)
            self._identity.add_audit_event(
                audit_event(
                    actor_user_id=identity.user_id,
                    action="auth.logout",
                    resource_type="session",
                    resource_id=identity.session_id,
                    outcome="success",
                )
            )
            await self._identity.commit()

    async def me(self, identity: IdentityContext) -> SessionResponse:
        user = await self._identity.get_user(identity.user_id)
        if user is None:
            raise UnauthorizedError()
        return await self._session_response(user, identity.session_id)

    async def _issue_session(self, user: User) -> IssuedSession:
        refresh_token = new_refresh_token()
        auth_session = await self._identity.create_session(
            user_id=user.id,
            refresh_token_hash=hash_refresh_token(refresh_token),
            expires_at=datetime.now(UTC) + timedelta(days=self._settings.refresh_token_days),
        )
        response = await self._session_response(user, auth_session.id)
        access_token = create_access_token(
            user_id=user.id,
            session_id=auth_session.id,
            settings=self._settings,
        )
        return IssuedSession(
            response=response,
            access_token=access_token,
            refresh_token=refresh_token,
            session_id=auth_session.id,
        )

    async def _session_response(self, user: User, session_id: UUID) -> SessionResponse:
        identity = IdentityContext(user_id=user.id, session_id=session_id)
        businesses = await self._businesses.list_for_user(identity)
        return SessionResponse(
            user=UserRead.model_validate(user),
            memberships=[
                MembershipRead(
                    business_id=business.id,
                    business_name=business.name,
                    location_name=business.location_name,
                    role=membership.role,
                )
                for business, membership in businesses
            ],
        )


async def resolve_identity(
    session: AsyncSession,
    settings: Settings,
    access_token: str,
) -> IdentityContext:
    try:
        user_id, session_id = decode_access_token(access_token, settings)
    except ValueError as exc:
        raise UnauthorizedError("Sesi tidak valid. Silakan masuk kembali.") from exc

    repository = IdentityRepository(session)
    auth_session = await repository.get_active_session(
        session_id=session_id,
        user_id=user_id,
        now=datetime.now(UTC),
    )
    user = await repository.get_user(user_id)
    if auth_session is None or user is None:
        raise UnauthorizedError("Sesi sudah berakhir. Silakan masuk kembali.")
    return IdentityContext(user_id=user_id, session_id=session_id)
