from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.operations import TransactionRead

ReceiptStatus = Literal[
    "created",
    "uploading",
    "queued",
    "preprocessing",
    "extracting",
    "ready_for_review",
    "confirmed",
    "committed",
    "failed",
    "cancelled",
]


class ReceiptImportCreate(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    content_type: Literal["image/jpeg", "image/png"]
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")


class SignedTransfer(BaseModel):
    method: Literal["PUT", "GET"]
    url: str
    expires_at: datetime


class ReceiptImportCreated(BaseModel):
    receipt_import_id: UUID
    status: Literal["uploading"]
    upload: SignedTransfer


class ConfidenceField(BaseModel):
    value: str | datetime | int | None
    confidence: float | None = Field(ge=0, le=1)


class ReceiptDraftItemRead(BaseModel):
    position: int
    raw_name: str
    matched_product_id: UUID | None
    quantity: int
    unit_price_idr: int
    line_total_idr: int
    confidence: float | None = Field(ge=0, le=1)
    corrected: bool


class ReceiptDraftRead(BaseModel):
    version: int
    merchant_name: ConfidenceField
    occurred_at: ConfidenceField
    items: list[ReceiptDraftItemRead]
    total_idr: ConfidenceField
    calculated_items_total_idr: int
    total_matches_items: bool


class ReceiptImportRead(BaseModel):
    receipt_import_id: UUID
    business_id: UUID
    status: ReceiptStatus
    draft: ReceiptDraftRead | None
    warnings: list[str]
    image: SignedTransfer | None
    failure_code: str | None
    transaction: TransactionRead | None = None


class ReceiptDraftItemUpdate(BaseModel):
    raw_name: str = Field(min_length=1, max_length=180)
    matched_product_id: UUID | None
    quantity: int = Field(gt=0, le=1000)
    unit_price_idr: int = Field(ge=0, le=1_000_000_000)


class ReceiptDraftUpdate(BaseModel):
    version: int = Field(gt=0)
    merchant_name: str | None = Field(default=None, max_length=180)
    occurred_at: datetime
    total_idr: int = Field(ge=0, le=10_000_000_000)
    items: list[ReceiptDraftItemUpdate] = Field(min_length=1, max_length=200)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at harus memiliki zona waktu")
        return value


class ReceiptConfirm(BaseModel):
    version: int = Field(gt=0)
    channel: Literal["dine_in", "takeaway", "delivery"]
    accept_total_mismatch: bool = False


class ReceiptConfirmResult(BaseModel):
    receipt_import_id: UUID
    status: Literal["committed"]
    transaction: TransactionRead
