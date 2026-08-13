import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.correlation import CORRELATION_HEADER


async def test_health_is_ok(client: AsyncClient) -> None:
    response = await client.get("/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_health_returns_a_correlation_id(client: AsyncClient) -> None:
    response = await client.get("/v1/health")

    uuid.UUID(response.headers[CORRELATION_HEADER])


async def test_client_correlation_id_is_echoed(client: AsyncClient) -> None:
    supplied = str(uuid.uuid4())

    response = await client.get("/v1/health", headers={CORRELATION_HEADER: supplied})

    assert response.headers[CORRELATION_HEADER] == supplied


async def test_malformed_correlation_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/v1/health", headers={CORRELATION_HEADER: "not-a-uuid"})

    returned = response.headers[CORRELATION_HEADER]
    assert returned != "not-a-uuid"
    uuid.UUID(returned)


class ReadyEngine:
    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        connection = AsyncMock(spec=AsyncConnection)
        yield connection


class FailingEngine:
    @asynccontextmanager
    async def connect(self) -> AsyncIterator[AsyncConnection]:
        raise ConnectionError("database unavailable")
        yield AsyncMock(spec=AsyncConnection)


async def test_ready_checks_database_and_redis(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis = AsyncMock()
    redis.ping.return_value = True
    monkeypatch.setattr("app.api.v1.health.get_engine", lambda: ReadyEngine())
    monkeypatch.setattr("app.api.v1.health.get_redis", lambda: redis)

    response = await client.get("/v1/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}
    redis.ping.assert_awaited_once()


async def test_ready_reports_each_failed_dependency(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis = AsyncMock()
    redis.ping.side_effect = ConnectionError("redis unavailable")
    monkeypatch.setattr("app.api.v1.health.get_engine", lambda: FailingEngine())
    monkeypatch.setattr("app.api.v1.health.get_redis", lambda: redis)

    response = await client.get("/v1/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "database": "unreachable",
        "redis": "unreachable",
    }
