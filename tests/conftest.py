from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.dependencies import get_auth_rate_limiter
from app.core.config import Settings, get_settings
from app.main import create_app
from app.persistence import models  # noqa: F401
from app.persistence.database import Base, get_session


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

    app = create_app()
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        jwt_secret="test-secret-with-at-least-thirty-two-characters",
    )
    app.dependency_overrides[get_auth_rate_limiter] = NoopRateLimiter
    app.state.test_session_factory = session_factory
    yield app
    await engine.dispose()


@pytest.fixture
async def auth_client(database_app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=database_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
