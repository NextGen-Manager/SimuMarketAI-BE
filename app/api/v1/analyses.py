from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header
from fastapi.responses import Response

from app.api.analysis_dependencies import AnalysisServiceDependency
from app.schemas.analysis import (
    AnalysisAccepted,
    AnalysisCreate,
    AnalysisListItem,
    AnalysisRead,
    AnalysisReport,
)

router = APIRouter(prefix="/analyses", tags=["analyses"])

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key", max_length=120)]


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


@router.get("/{analysis_id}/events", response_class=Response)
async def get_analysis_events(
    analysis_id: UUID,
    service: AnalysisServiceDependency,
) -> Response:
    """Return a valid one-event SSE stream until Phase 4 adds live worker events."""
    detail = await service.get(analysis_id)
    payload = json.dumps(detail.model_dump(mode="json"), separators=(",", ":"))
    return Response(
        content=f"event: status\ndata: {payload}\n\n",
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/{analysis_id}/report", response_model=AnalysisReport)
async def get_analysis_report(
    analysis_id: UUID,
    service: AnalysisServiceDependency,
) -> AnalysisReport:
    return await service.get_report(analysis_id)
