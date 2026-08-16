from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header
from fastapi.responses import StreamingResponse

from app.api.analysis_dependencies import AnalysisServiceDependency
from app.api.dependencies import AppSettings
from app.core.config import Settings
from app.domain.analysis_events import EVENT_NAME, AnalysisEvent
from app.domain.analysis_state import is_terminal
from app.schemas.analysis import (
    AnalysisAccepted,
    AnalysisCreate,
    AnalysisListItem,
    AnalysisRead,
    AnalysisReport,
)
from app.services.analysis import AnalysisService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analyses", tags=["analyses"])

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=120)]
LastEventId = Annotated[str | None, Header(alias="Last-Event-ID", max_length=32)]

# Proxies buffer by default, which would hold a stage change until the run ends.
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("", response_model=AnalysisAccepted, status_code=202)
async def create_analysis(
    payload: AnalysisCreate,
    service: AnalysisServiceDependency,
    idempotency_key: IdempotencyKey = None,
) -> AnalysisAccepted:
    return await service.create(payload, idempotency_key=idempotency_key)


@router.get("", response_model=list[AnalysisListItem])
async def list_analyses(service: AnalysisServiceDependency) -> list[AnalysisListItem]:
    return await service.list_analyses()


@router.get("/{analysis_id}", response_model=AnalysisRead)
async def get_analysis(
    analysis_id: UUID,
    service: AnalysisServiceDependency,
) -> AnalysisRead:
    return await service.get(analysis_id)


def _parse_last_event_id(raw: str | None) -> int:
    if not raw:
        return 0
    try:
        return max(0, int(raw))
    except ValueError:
        # A malformed header must not abort the stream; replay from the start.
        return 0


def _render(event: AnalysisEvent) -> str:
    return f"id: {event.event_id}\nevent: {EVENT_NAME}\ndata: {event.model_dump_json()}\n\n"


async def _stream(
    service: AnalysisService,
    analysis_id: UUID,
    settings: Settings,
    after_sequence: int,
) -> AsyncIterator[str]:
    """Poll PostgreSQL for new transitions until the run reaches a terminal state.

    Reading from the system of record rather than subscribing to Redis is the
    point: the stream keeps working when the broker is down, and Redis never
    becomes the authority on whether a run finished. The publish side still
    exists so a future push transport can shorten latency without changing the
    contract or the truth.
    """
    deadline = asyncio.get_running_loop().time() + settings.sse_max_duration_seconds
    idle = 0.0
    while True:
        events = await service.events_since(analysis_id, after_sequence)
        # Release the read transaction between polls; a stream can stay open for
        # minutes and an idle transaction that long holds up vacuum for nothing.
        await service.release()
        for event in events:
            after_sequence = max(after_sequence, int(event.event_id))
            yield _render(event)
            if is_terminal(event.status):
                return

        if events:
            idle = 0.0
        else:
            idle += settings.sse_poll_interval_seconds
            if idle >= settings.sse_heartbeat_seconds:
                idle = 0.0
                # A comment keeps intermediaries from closing an idle stream and
                # is ignored by every EventSource implementation.
                yield ": heartbeat\n\n"

        if asyncio.get_running_loop().time() >= deadline:
            yield ": stream-timeout\n\n"
            return
        await asyncio.sleep(settings.sse_poll_interval_seconds)


@router.get("/{analysis_id}/events")
async def get_analysis_events(
    analysis_id: UUID,
    service: AnalysisServiceDependency,
    settings: AppSettings,
    last_event_id: LastEventId = None,
) -> StreamingResponse:
    """Live progress for one run.

    Access is checked before the response starts, so another tenant gets 404
    rather than an empty stream that would confirm the run exists.
    """
    after_sequence = _parse_last_event_id(last_event_id)
    await service.events_since(analysis_id, after_sequence)

    async def body() -> AsyncIterator[str]:
        with contextlib.suppress(asyncio.CancelledError):
            async for chunk in _stream(service, analysis_id, settings, after_sequence):
                yield chunk

    return StreamingResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/{analysis_id}/report", response_model=AnalysisReport)
async def get_analysis_report(
    analysis_id: UUID,
    service: AnalysisServiceDependency,
) -> AnalysisReport:
    return await service.get_report(analysis_id)
