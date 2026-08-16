from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from jwt.exceptions import InvalidTokenError
from pwdlib import PasswordHash

from app.core.config import Settings

ACCESS_COOKIE = "simumarket_access"
REFRESH_COOKIE = "simumarket_refresh"
JWT_ALGORITHM = "HS256"

_password_hash = PasswordHash.recommended()
_dummy_hash = _password_hash.hash("dummy-password-used-only-for-timing-equality")


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    return _password_hash.verify(password, password_hash or _dummy_hash)


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_invite_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(8))


def hash_invite_code(code: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), code.upper().encode("utf-8"), hashlib.sha256
    ).hexdigest()


def create_access_token(*, user_id: UUID, session_id: UUID, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "sid": str(session_id),
        "typ": "access",
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_minutes),
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> tuple[UUID, UUID]:
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[JWT_ALGORITHM],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "sid", "typ", "iat", "exp"]},
        )
        if payload["typ"] != "access":
            raise InvalidTokenError("unexpected token type")
        return UUID(payload["sub"]), UUID(payload["sid"])
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid access token") from exc
