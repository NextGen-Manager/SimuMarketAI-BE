from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ReceiptItemExtraction:
    raw_name: str
    quantity: int
    unit_price_idr: int
    confidence_bps: int


@dataclass(frozen=True)
class ReceiptExtraction:
    merchant_name: str | None
    merchant_confidence_bps: int | None
    occurred_at: datetime | None
    occurred_at_confidence_bps: int | None
    items: tuple[ReceiptItemExtraction, ...]
    total_idr: int
    total_confidence_bps: int | None
    raw_text: str
    aggregate_confidence_bps: int
    engine_version: str


class ReceiptOcr(Protocol):
    async def extract(self, image: bytes) -> ReceiptExtraction: ...


class ReceiptOcrUnavailableError(Exception):
    pass


class ReceiptImageValidationError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
