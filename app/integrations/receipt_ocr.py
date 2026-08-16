from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings
from app.domain.receipts import (
    ReceiptExtraction,
    ReceiptItemExtraction,
    ReceiptOcr,
    ReceiptOcrUnavailableError,
)

MONEY_PATTERN = re.compile(r"(?<!\d)(\d{1,3}(?:[.,]\d{3})+|\d{4,})(?!\d)")
QUANTITY_PATTERN = re.compile(r"^\s*(\d+)\s*(?:x|X|\u00d7)\s*(.+?)\s+(\d[\d.,]*)\s*$")
DATE_PATTERNS = (
    re.compile(r"\b(\d{2})[/-](\d{2})[/-](\d{4})\b"),
    re.compile(r"\b(\d{4})[/-](\d{2})[/-](\d{2})\b"),
)


class UnavailableReceiptOcr:
    async def extract(self, image: bytes) -> ReceiptExtraction:
        raise ReceiptOcrUnavailableError("RECEIPT_OCR_UNAVAILABLE")


class PaddleReceiptOcr:
    """PP-StructureV3 adapter; numeric parsing remains deterministic application code."""

    def __init__(self, engine_version: str) -> None:
        self._engine_version = engine_version

    async def extract(self, image: bytes) -> ReceiptExtraction:
        return await asyncio.to_thread(self._extract_sync, image)

    def _extract_sync(self, image: bytes) -> ReceiptExtraction:
        try:
            from paddleocr import PPStructureV3
        except ImportError as exc:
            raise ReceiptOcrUnavailableError("RECEIPT_OCR_UNAVAILABLE") from exc

        with tempfile.TemporaryDirectory(prefix="simumarket-receipt-") as directory:
            path = Path(directory) / "receipt.jpg"
            path.write_bytes(image)
            pipeline = PPStructureV3(
                lang="en",
                use_formula_recognition=False,
                use_chart_recognition=False,
            )
            results = list(pipeline.predict(str(path)))
        lines = list(_collect_text(results))
        return parse_receipt_lines(lines, engine_version=self._engine_version)


def _collect_text(value: object) -> Iterable[str]:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            yield stripped
        return
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).lower() in {"text", "rec_text", "markdown_text"} or isinstance(
                nested, (Mapping, list, tuple)
            ):
                yield from _collect_text(nested)
        return
    if isinstance(value, (list, tuple)):
        for nested in value:
            yield from _collect_text(nested)
        return
    json_value = getattr(value, "json", None)
    if json_value is not None:
        yield from _collect_text(json_value)


def parse_receipt_lines(lines: Iterable[str], *, engine_version: str) -> ReceiptExtraction:
    normalized = [line.strip() for line in lines if line.strip()]
    raw_text = "\n".join(normalized)
    merchant = normalized[0][:180] if normalized else None
    occurred_at = _parse_date(normalized)
    items: list[ReceiptItemExtraction] = []
    total_idr = 0
    for line in normalized:
        lowered = line.casefold()
        amounts = [_money_to_int(value) for value in MONEY_PATTERN.findall(line)]
        if any(label in lowered for label in ("grand total", "total bayar", "total")) and amounts:
            total_idr = amounts[-1]
            continue
        item_match = QUANTITY_PATTERN.match(line)
        if item_match:
            quantity = int(item_match.group(1))
            unit_price = _money_to_int(item_match.group(3))
            if quantity > 0:
                items.append(
                    ReceiptItemExtraction(
                        raw_name=item_match.group(2)[:180],
                        quantity=quantity,
                        unit_price_idr=unit_price,
                        confidence_bps=7000,
                    )
                )
    if total_idr == 0 and items:
        total_idr = sum(item.quantity * item.unit_price_idr for item in items)
    confidence = 7500 if items and total_idr else 4000
    return ReceiptExtraction(
        merchant_name=merchant,
        merchant_confidence_bps=6500 if merchant else None,
        occurred_at=occurred_at,
        occurred_at_confidence_bps=7000 if occurred_at else None,
        items=tuple(items),
        total_idr=total_idr,
        total_confidence_bps=7500 if total_idr else None,
        raw_text=raw_text,
        aggregate_confidence_bps=confidence,
        engine_version=engine_version,
    )


def _money_to_int(value: str) -> int:
    return int(value.replace(".", "").replace(",", ""))


def _parse_date(lines: Iterable[str]) -> datetime | None:
    for line in lines:
        for index, pattern in enumerate(DATE_PATTERNS):
            match = pattern.search(line)
            if match is None:
                continue
            values = [int(value) for value in match.groups()]
            year, month, day = (values[2], values[1], values[0]) if index == 0 else tuple(values)
            try:
                return datetime(year, month, day, tzinfo=UTC)
            except ValueError:
                continue
    return None


def build_receipt_ocr(settings: Settings) -> ReceiptOcr:
    if settings.receipt_ocr_provider == "unavailable":
        return UnavailableReceiptOcr()
    return PaddleReceiptOcr(settings.receipt_ocr_engine_version)
