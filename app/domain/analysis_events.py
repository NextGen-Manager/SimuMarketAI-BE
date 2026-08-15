"""Progress event contract shared by the worker, Redis, SSE, and the frontend.

One shape is used everywhere. The worker writes it to PostgreSQL first and only
then publishes it to Redis, so a stream that reconnects after a Redis outage can
still replay every transition from the system of record.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.analysis_state import AnalysisStage, AnalysisStatus

EVENT_NAME = "status"
EVENT_SCHEMA_VERSION = "analysis-event-v1"


class AnalysisEventWarning(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    stage: AnalysisStage | None = None
    message: str


class AnalysisEvent(BaseModel):
    """A single observed transition of one run.

    `event_id` is a monotonic per-run sequence rendered as a string so that a
    browser can hand it back through `Last-Event-ID` without any client-side
    bookkeeping.
    """

    model_config = ConfigDict(frozen=True)

    schema_version: str = EVENT_SCHEMA_VERSION
    event_id: str
    analysis_id: UUID
    status: AnalysisStatus
    current_stage: AnalysisStage
    completed_stages: list[AnalysisStage] = Field(default_factory=list)
    skipped_stages: list[AnalysisStage] = Field(default_factory=list)
    percent: int = Field(ge=0, le=100)
    message: str
    warnings: list[AnalysisEventWarning] = Field(default_factory=list)
    # Carried on the event itself so a client that only ever sees the stream can
    # name the failure. Deriving it from the first warning was wrong: a run can
    # fail with warnings that describe something else entirely.
    failure_code: str | None = None
    correlation_id: UUID
    occurred_at: datetime
