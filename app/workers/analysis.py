"""The analysis Celery task.

The task itself is intentionally thin: it restores the correlation ID, builds
the runtime dependencies, and hands off to `AnalysisPipeline`. Everything
testable lives in `execute_analysis`, which takes its session factory and
dependencies as arguments so a test can drive the whole pipeline against SQLite
with no broker, no Redis, and no provider.

Retries cover transient failures only. A schema, policy, or validation error is
recorded and left alone, because retrying it without a change would just burn
budget on the same outcome.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from celery import Task
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.correlation import set_correlation_id
from app.domain.agents import OasisAdapter
from app.domain.evidence import EvidenceProvider
from app.integrations.evidence import select_evidence_provider
from app.integrations.oasis import select_oasis_adapter
from app.persistence.database import get_session_factory
from app.persistence.redis import get_redis
from app.services.analysis_events import (
    AnalysisEventPublisher,
    NullEventPublisher,
    RedisEventPublisher,
)
from app.services.analysis_pipeline import TRANSIENT_ERRORS, build_pipeline
from app.services.analysis_queue import AnalysisDispatcher, CeleryAnalysisDispatcher
from app.services.analysis_recovery import RecoveryReport, recover_stuck_runs
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def execute_analysis(
    analysis_id: UUID,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    evidence_provider: EvidenceProvider | None = None,
    oasis_adapter: OasisAdapter | None = None,
    publisher: AnalysisEventPublisher | None = None,
) -> None:
    async with session_factory() as session:
        pipeline = build_pipeline(
            session,
            settings=settings,
            evidence_provider=select_evidence_provider(settings, evidence_provider),
            oasis_adapter=select_oasis_adapter(settings, oasis_adapter),
            publisher=publisher or NullEventPublisher(),
        )
        await pipeline.run(analysis_id)


def _redis_publisher() -> AnalysisEventPublisher:
    try:
        redis: Redis = get_redis()
    except Exception:
        logger.warning("analysis_event_publisher_unavailable")
        return NullEventPublisher()
    return RedisEventPublisher(redis)


async def execute_recovery(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    dispatcher: AnalysisDispatcher | None = None,
    publisher: AnalysisEventPublisher | None = None,
) -> RecoveryReport:
    async with session_factory() as session:
        return await recover_stuck_runs(
            session,
            settings=settings,
            dispatcher=dispatcher or CeleryAnalysisDispatcher(),
            publisher=publisher or NullEventPublisher(),
        )


@celery_app.task(
    bind=True,
    name="analysis.run",
    autoretry_for=TRANSIENT_ERRORS,
    retry_backoff=True,
    retry_jitter=True,
)
def run_analysis(self: Task, analysis_id: str, correlation_id: str) -> None:
    """Entry point registered with Celery.

    `correlation_id` is carried explicitly rather than read from a context
    variable: the worker runs in a different process from the request that
    created the run, and docs/11 requires one ID from API to worker to report.
    """
    set_correlation_id(correlation_id)
    settings = get_settings()
    # Read from settings rather than hardcoded, so the retry budget documented
    # in `.env.example` is the one that actually applies.
    self.max_retries = settings.celery_analysis_max_retries
    logger.info(
        "analysis_task_started",
        extra={"analysis_id": analysis_id, "correlation_id": correlation_id},
    )
    asyncio.run(
        execute_analysis(
            UUID(analysis_id),
            session_factory=get_session_factory(),
            settings=settings,
            publisher=_redis_publisher(),
        )
    )


@celery_app.task(name="analysis.recover")
def recover_analyses() -> dict[str, list[str]]:
    """Periodic reconciliation of runs no worker is executing.

    Celery cannot notice a task that never arrived or a worker that died mid-run,
    so docs/11 puts recovery on PostgreSQL state. This is the job that reads it.
    """
    settings = get_settings()
    report = asyncio.run(
        execute_recovery(
            session_factory=get_session_factory(),
            settings=settings,
            publisher=_redis_publisher(),
        )
    )
    if report.total:
        logger.warning(
            "analysis_recovery_completed",
            extra={"requeued": len(report.requeued), "failed": len(report.failed)},
        )
    return {"requeued": report.requeued, "failed": report.failed}


def enqueue_analysis(analysis_id: UUID, correlation_id: UUID) -> Any:
    return run_analysis.delay(str(analysis_id), str(correlation_id))
