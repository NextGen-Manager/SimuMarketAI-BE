from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import get_settings
from app.persistence.database import get_engine
from app.persistence.redis import get_redis

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    environment: str


class ReadinessResponse(BaseModel):
    status: str
    database: str
    redis: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness. Deliberately touches no dependency."""
    return HealthResponse(status="ok", environment=get_settings().environment)


@router.get("/ready", response_model=ReadinessResponse)
async def ready() -> ReadinessResponse:
    """Readiness. Reports a failing dependency as degraded rather than hiding it."""
    database_status = "ok"
    redis_status = "ok"

    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception:
        database_status = "unreachable"

    try:
        await get_redis().ping()
    except Exception:
        redis_status = "unreachable"

    status = "ok" if database_status == redis_status == "ok" else "degraded"
    return ReadinessResponse(status=status, database=database_status, redis=redis_status)
