from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query

from app.api.artifact_dependencies import ExportServiceDependency, ReceiptServiceDependency
from app.schemas.exports import AnalysisExportCreate, ExportRead, TransactionExportCreate
from app.schemas.receipts import (
    ReceiptConfirm,
    ReceiptConfirmResult,
    ReceiptDraftUpdate,
    ReceiptImportCreate,
    ReceiptImportCreated,
    ReceiptImportRead,
)

router = APIRouter()
BusinessQuery = Annotated[UUID, Query(alias="business_id")]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=8, max_length=120),
]


@router.post(
    "/receipt-imports",
    response_model=ReceiptImportCreated,
    status_code=201,
    tags=["receipts"],
)
async def create_receipt_import(
    payload: ReceiptImportCreate,
    business_id: BusinessQuery,
    service: ReceiptServiceDependency,
) -> ReceiptImportCreated:
    return await service.create(business_id, payload)


@router.post(
    "/receipt-imports/{receipt_import_id}/complete-upload",
    response_model=ReceiptImportRead,
    status_code=202,
    tags=["receipts"],
)
async def complete_receipt_upload(
    receipt_import_id: UUID,
    business_id: BusinessQuery,
    service: ReceiptServiceDependency,
) -> ReceiptImportRead:
    return await service.complete_upload(business_id, receipt_import_id)


@router.get(
    "/receipt-imports/{receipt_import_id}",
    response_model=ReceiptImportRead,
    tags=["receipts"],
)
async def get_receipt_import(
    receipt_import_id: UUID,
    business_id: BusinessQuery,
    service: ReceiptServiceDependency,
) -> ReceiptImportRead:
    return await service.get(business_id, receipt_import_id)


@router.patch(
    "/receipt-imports/{receipt_import_id}/draft",
    response_model=ReceiptImportRead,
    tags=["receipts"],
)
async def update_receipt_draft(
    receipt_import_id: UUID,
    payload: ReceiptDraftUpdate,
    business_id: BusinessQuery,
    service: ReceiptServiceDependency,
) -> ReceiptImportRead:
    return await service.update_draft(business_id, receipt_import_id, payload)


@router.post(
    "/receipt-imports/{receipt_import_id}/confirm",
    response_model=ReceiptConfirmResult,
    tags=["receipts"],
)
async def confirm_receipt_import(
    receipt_import_id: UUID,
    payload: ReceiptConfirm,
    business_id: BusinessQuery,
    service: ReceiptServiceDependency,
) -> ReceiptConfirmResult:
    return await service.confirm(business_id, receipt_import_id, payload)


@router.post(
    "/analyses/{analysis_id}/exports",
    response_model=ExportRead,
    status_code=202,
    tags=["exports"],
)
async def create_analysis_export(
    analysis_id: UUID,
    _: AnalysisExportCreate,
    idempotency_key: IdempotencyHeader,
    service: ExportServiceDependency,
) -> ExportRead:
    return await service.create_analysis(analysis_id, idempotency_key)


@router.post(
    "/transaction-exports",
    response_model=ExportRead,
    status_code=202,
    tags=["exports"],
)
async def create_transaction_export(
    payload: TransactionExportCreate,
    idempotency_key: IdempotencyHeader,
    service: ExportServiceDependency,
) -> ExportRead:
    return await service.create_transaction(payload, idempotency_key)


@router.get("/exports/{export_id}", response_model=ExportRead, tags=["exports"])
async def get_export(export_id: UUID, service: ExportServiceDependency) -> ExportRead:
    return await service.get(export_id)
