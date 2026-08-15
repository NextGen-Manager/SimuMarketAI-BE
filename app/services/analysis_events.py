"""Publishing and channel naming for live analysis progress.

Redis is transport. Every event is written to PostgreSQL first and published
second, and a publish failure is logged rather than raised: losing a live
notification degrades the stream to polling, whereas failing the run would turn
a broker hiccup into a lost analysis.
"""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from redis.asyncio import Redis

from app.domain.analysis_events import AnalysisEvent

logger = logging.getLogger(__name__)


def channel_for(analysis_id: UUID) -> str:
    return f"analysis:events:{analysis_id}"


class AnalysisEventPublisher(Protocol):
    async def publish(self, event: AnalysisEvent) -> None: ...


class NullEventPublisher:
    """Used when no broker is configured, and by tests that assert no network."""

    def __init__(self) -> None:
        self.published: list[AnalysisEvent] = []

    async def publish(self, event: AnalysisEvent) -> None:
        self.published.append(event)


class RedisEventPublisher:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, event: AnalysisEvent) -> None:
        try:
            await self._redis.publish(
                channel_for(event.analysis_id),
                event.model_dump_json(),
            )
        except Exception:
            # PostgreSQL already has the transition; the stream falls back to
            # reading it from there, so this is a degradation, not a failure.
            logger.warning(
                "analysis_event_publish_failed",
                extra={"correlation_id": str(event.correlation_id)},
            )
