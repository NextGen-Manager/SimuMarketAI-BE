"""Configuration failures that must stop a non-local deployment."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app

PRODUCTION_SECRET = "production-secret-with-at-least-thirty-two-characters"


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "debug": False,
        "jwt_secret": PRODUCTION_SECRET,
        "auth_cookie_secure": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_wildcard_cors_is_rejected_in_every_environment() -> None:
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(cors_origins=["*"])


def test_debug_mode_is_rejected_in_production() -> None:
    with pytest.raises(ValueError, match="DEBUG"):
        _production_settings(debug=True)


async def test_production_responses_enable_hsts(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _production_settings()
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://api.example.test"
    ) as client:
        response = await client.get("/v1/health")

    assert response.headers["strict-transport-security"] == ("max-age=31536000; includeSubDomains")
