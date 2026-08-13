import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import NotFoundError


async def test_unknown_route_returns_the_stable_error_shape(client: AsyncClient) -> None:
    response = await client.get("/v1/tidak-ada")

    assert response.status_code == 404
    error = response.json()["error"]
    assert set(error) == {"code", "message", "fields", "correlation_id", "retryable"}
    uuid.UUID(error["correlation_id"])
    assert error["retryable"] is False


async def test_error_message_is_indonesian(client: AsyncClient) -> None:
    response = await client.get("/v1/tidak-ada")

    assert response.json()["error"]["message"] == "Halaman atau data tidak ditemukan."


async def test_unexpected_error_never_leaks_internals() -> None:
    from app.core.errors import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("connection string postgres://user:secret@host/db")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom")

    assert response.status_code == 500
    body = response.text
    assert "secret" not in body
    assert "RuntimeError" not in body
    assert response.json()["error"]["retryable"] is True


async def test_app_error_carries_its_own_message() -> None:
    error = NotFoundError()

    assert error.status_code == 404
    assert error.detail == "Data yang diminta tidak ditemukan."
