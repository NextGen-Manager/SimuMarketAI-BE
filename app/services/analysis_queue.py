"""Handing a created run to the worker.

The dispatcher is a seam, not indirection for its own sake. Tests replace it to
assert that the API returns before any work happens, and the API keeps no
dependency on task code beyond this module.

Dispatch never happens inside the creating transaction. The run must be durable
in PostgreSQL before the worker can be told about it, otherwise a worker could
pick up an ID that has not been committed yet.
"""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

logger = logging.getLogger(__name__)


class AnalysisDispatcher(Protocol):
    def dispatch(self, analysis_id: UUID, correlation_id: UUID) -> None: ...


class CeleryAnalysisDispatcher:
    def dispatch(self, analysis_id: UUID, correlation_id: UUID) -> None:
        from app.workers.analysis import enqueue_analysis

        enqueue_analysis(analysis_id, correlation_id)


class RecordingDispatcher:
    """Records dispatches instead of queueing them. Used by tests and by the
    development server when no broker is running."""

    def __init__(self) -> None:
        self.dispatched: list[tuple[UUID, UUID]] = []

    def dispatch(self, analysis_id: UUID, correlation_id: UUID) -> None:
        self.dispatched.append((analysis_id, correlation_id))
