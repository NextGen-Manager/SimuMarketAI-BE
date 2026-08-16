from __future__ import annotations

from typing import Protocol
from uuid import UUID


class ReceiptDispatcher(Protocol):
    def dispatch(self, receipt_import_id: UUID, correlation_id: UUID) -> None: ...


class ExportDispatcher(Protocol):
    def dispatch(self, export_id: UUID, correlation_id: UUID) -> None: ...


class CeleryReceiptDispatcher:
    def dispatch(self, receipt_import_id: UUID, correlation_id: UUID) -> None:
        from app.workers.artifacts import enqueue_receipt

        enqueue_receipt(receipt_import_id, correlation_id)


class CeleryExportDispatcher:
    def dispatch(self, export_id: UUID, correlation_id: UUID) -> None:
        from app.workers.artifacts import enqueue_export

        enqueue_export(export_id, correlation_id)


class RecordingReceiptDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[tuple[UUID, UUID]] = []

    def dispatch(self, receipt_import_id: UUID, correlation_id: UUID) -> None:
        self.dispatched.append((receipt_import_id, correlation_id))


class RecordingExportDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[tuple[UUID, UUID]] = []

    def dispatch(self, export_id: UUID, correlation_id: UUID) -> None:
        self.dispatched.append((export_id, correlation_id))
