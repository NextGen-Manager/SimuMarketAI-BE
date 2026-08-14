from __future__ import annotations

from typing import Annotated

from fastapi import Cookie, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import UnauthorizedError
from app.core.rate_limit import AuthRateLimiter
from app.core.security import ACCESS_COOKIE
from app.domain.auth import IdentityContext
from app.persistence.database import get_session
from app.persistence.redis import get_redis
from app.services.auth import AuthService, resolve_identity
from app.services.business import BusinessService
from app.services.factories import build_auth_service, build_business_service

DatabaseSession = Annotated[AsyncSession, Depends(get_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_auth_rate_limiter() -> AuthRateLimiter:
    return AuthRateLimiter(get_redis())


def get_auth_service(session: DatabaseSession, settings: AppSettings) -> AuthService:
    return build_auth_service(session, settings)


def get_business_service(session: DatabaseSession, settings: AppSettings) -> BusinessService:
    return build_business_service(session, settings)


async def get_identity(
    session: DatabaseSession,
    settings: AppSettings,
    access_token: Annotated[str | None, Cookie(alias=ACCESS_COOKIE)] = None,
) -> IdentityContext:
    if access_token is None:
        raise UnauthorizedError()
    return await resolve_identity(session, settings, access_token)


CurrentIdentity = Annotated[IdentityContext, Depends(get_identity)]
AuthServiceDependency = Annotated[AuthService, Depends(get_auth_service)]
BusinessServiceDependency = Annotated[BusinessService, Depends(get_business_service)]
AuthRateLimiterDependency = Annotated[AuthRateLimiter, Depends(get_auth_rate_limiter)]
