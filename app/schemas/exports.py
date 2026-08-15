from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.receipts import SignedTransfer


class AnalysisExportCreate(BaseModel):
    format: Literal["pdf"] = "pdf"


class TransactionExportCreate(BaseModel):
    business_id: UUID
    start: datetime
    end: datetime
    format: Literal["pdf"] = "pdf"


class ExportRead(BaseModel):
    export_id: UUID
    kind: Literal["analysis_report", "transaction_summary"]
    status: Literal["queued", "processing", "ready", "failed", "expired"]
    created_at: datetime
    download: SignedTransfer | None
    failure_code: str | None
