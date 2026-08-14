from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.dependencies import (
    AppSettings,
    AuthRateLimiterDependency,
    AuthServiceDependency,
    CurrentIdentity,
)
from app.core.config import Settings
from app.core.errors import UnauthorizedError
from app.core.security import ACCESS_COOKIE, REFRESH_COOKIE
from app.schemas.auth import (
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    SessionResponse,
)
from app.services.auth import IssuedSession

router = APIRouter(tags=["identity"])


def _set_session_cookies(response: Response, issued: IssuedSession, settings: Settings) -> None:
    response.set_cookie(
        ACCESS_COOKIE,
        issued.access_token,
        max_age=settings.access_token_minutes * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        issued.refresh_token,
        max_age=settings.refresh_token_days * 24 * 60 * 60,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/v1/auth",
    )


@router.post("/auth/register", response_model=SessionResponse, status_code=201)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    service: AuthServiceDependency,
    rate_limiter: AuthRateLimiterDependency,
    settings: AppSettings,
) -> SessionResponse:
    client_host = request.client.host if request.client else "unknown"
    await rate_limiter.check("register", f"{client_host}:{payload.email}")
    issued = await service.register(
        email=str(payload.email),
        display_name=payload.display_name,
        password=payload.password,
    )
    _set_session_cookies(response, issued, settings)
    return issued.response


@router.post("/auth/login", response_model=SessionResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AuthServiceDependency,
    rate_limiter: AuthRateLimiterDependency,
    settings: AppSettings,
) -> SessionResponse:
    client_host = request.client.host if request.client else "unknown"
    await rate_limiter.check("login", f"{client_host}:{payload.email}")
    issued = await service.login(email=str(payload.email), password=payload.password)
    _set_session_cookies(response, issued, settings)
    return issued.response


@router.post("/auth/refresh", response_model=SessionResponse)
async def refresh(
    request: Request,
    response: Response,
    service: AuthServiceDependency,
    settings: AppSettings,
) -> SessionResponse:
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    if refresh_token is None:
        raise UnauthorizedError("Sesi sudah berakhir. Silakan masuk kembali.")
    issued = await service.refresh(refresh_token)
    _set_session_cookies(response, issued, settings)
    return issued.response


@router.post("/auth/logout", response_model=MessageResponse)
async def logout(
    identity: CurrentIdentity,
    response: Response,
    service: AuthServiceDependency,
) -> MessageResponse:
    await service.logout(identity)
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/v1/auth")
    return MessageResponse(message="Anda sudah keluar.")


@router.get("/me", response_model=SessionResponse)
async def me(
    identity: CurrentIdentity,
    service: AuthServiceDependency,
) -> SessionResponse:
    return await service.me(identity)
