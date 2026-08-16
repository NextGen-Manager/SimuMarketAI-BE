from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.analysis_dependencies import get_analysis_dispatcher
from app.api.artifact_dependencies import (
    get_export_dispatcher,
    get_object_storage,
    get_receipt_dispatcher,
)
from app.api.dependencies import get_auth_rate_limiter
from app.core.config import Settings, get_settings
from app.integrations.object_storage import MemoryObjectStorage
from app.main import create_app
from app.persistence import models  # noqa: F401
from app.persistence.database import Base, get_session
from app.services.analysis_events import NullEventPublisher
from app.services.analysis_queue import RecordingDispatcher
from app.services.artifact_queue import RecordingExportDispatcher, RecordingReceiptDispatcher


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client


class NoopRateLimiter:
    async def check(self, action: str, identifier: str) -> None:
        return None


@pytest.fixture
async def database_app() -> AsyncIterator[FastAPI]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            yield session

    settings = Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        jwt_secret="test-secret-with-at-least-thirty-two-characters",
        # Tests drive the worker directly, so a stream must not sit waiting on a
        # poll interval that only makes sense against a real broker.
        sse_poll_interval_seconds=0,
        sse_heartbeat_seconds=1,
        sse_max_duration_seconds=2,
    )

    # No test may reach a broker. The dispatcher records instead of queueing, so
    # a test that forgets to run the pipeline sees a queued run, not a hang.
    dispatcher = RecordingDispatcher()
    receipt_dispatcher = RecordingReceiptDispatcher()
    export_dispatcher = RecordingExportDispatcher()
    object_storage = MemoryObjectStorage()

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_auth_rate_limiter] = NoopRateLimiter
    app.dependency_overrides[get_analysis_dispatcher] = lambda: dispatcher
    app.dependency_overrides[get_receipt_dispatcher] = lambda: receipt_dispatcher
    app.dependency_overrides[get_export_dispatcher] = lambda: export_dispatcher
    app.dependency_overrides[get_object_storage] = lambda: object_storage
    app.state.test_session_factory = session_factory
    app.state.test_settings = settings
    app.state.test_dispatcher = dispatcher
    app.state.test_receipt_dispatcher = receipt_dispatcher
    app.state.test_export_dispatcher = export_dispatcher
    app.state.test_object_storage = object_storage
    app.state.test_publisher = NullEventPublisher()
    app.state.test_evidence_provider = None
    app.state.test_oasis_adapter = None
    yield app
    await engine.dispose()


@pytest.fixture
async def auth_client(database_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=database_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
