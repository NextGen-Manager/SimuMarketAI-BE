from typing import Annotated

from fastapi import Depends

from app.api.dependencies import AppSettings, CurrentIdentity, DatabaseSession
from app.domain.artifacts import ObjectStorage
from app.integrations.object_storage import build_object_storage
from app.services.artifact_queue import (
    CeleryExportDispatcher,
    CeleryReceiptDispatcher,
    ExportDispatcher,
    ReceiptDispatcher,
)
from app.services.exports import ExportService
from app.services.receipts import ReceiptService


def get_object_storage(settings: AppSettings) -> ObjectStorage:
    return build_object_storage(settings)


def get_receipt_dispatcher() -> ReceiptDispatcher:
    return CeleryReceiptDispatcher()


def get_export_dispatcher() -> ExportDispatcher:
    return CeleryExportDispatcher()


ObjectStorageDependency = Annotated[ObjectStorage, Depends(get_object_storage)]
ReceiptDispatcherDependency = Annotated[ReceiptDispatcher, Depends(get_receipt_dispatcher)]
ExportDispatcherDependency = Annotated[ExportDispatcher, Depends(get_export_dispatcher)]


def get_receipt_service(
    session: DatabaseSession,
    identity: CurrentIdentity,
    settings: AppSettings,
    storage: ObjectStorageDependency,
    dispatcher: ReceiptDispatcherDependency,
) -> ReceiptService:
    return ReceiptService(session, identity, settings, storage, dispatcher)


def get_export_service(
    session: DatabaseSession,
    identity: CurrentIdentity,
    settings: AppSettings,
    storage: ObjectStorageDependency,
    dispatcher: ExportDispatcherDependency,
) -> ExportService:
    return ExportService(session, identity, settings, storage, dispatcher)


ReceiptServiceDependency = Annotated[ReceiptService, Depends(get_receipt_service)]
ExportServiceDependency = Annotated[ExportService, Depends(get_export_service)]
