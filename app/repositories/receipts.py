from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import ActorContext
from app.domain.receipts import ReceiptExtraction
from app.persistence.models import (
    OcrAttempt,
    Product,
    ReceiptDraft,
    ReceiptDraftItem,
    ReceiptImport,
    Transaction,
    TransactionItem,
)


class ReceiptRepository:
    def __init__(self, session: AsyncSession, actor: ActorContext) -> None:
        self._session = session
        self._actor = actor

    async def create(
        self,
        *,
        object_key: str,
        original_file_name: str,
        sha256: str,
        mime_type: str,
        size_bytes: int,
        upload_expires_at: datetime,
        image_retention_until: datetime,
    ) -> ReceiptImport:
        record = ReceiptImport(
            user_id=self._actor.user_id,
            business_id=self._actor.business_id,
            status="uploading",
            object_key=object_key,
            original_file_name=original_file_name,
            sha256=sha256,
            mime_type=mime_type,
            size_bytes=size_bytes,
            upload_expires_at=upload_expires_at,
            image_retention_until=image_retention_until,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def get(
        self, receipt_import_id: UUID, *, for_update: bool = False
    ) -> ReceiptImport | None:
        query = select(ReceiptImport).where(
            ReceiptImport.id == receipt_import_id,
            ReceiptImport.user_id == self._actor.user_id,
            ReceiptImport.business_id == self._actor.business_id,
        )
        if for_update:
            query = query.with_for_update()
        return cast(ReceiptImport | None, await self._session.scalar(query))

    async def draft(self, receipt_import_id: UUID) -> ReceiptDraft | None:
        return cast(
            ReceiptDraft | None,
            await self._session.scalar(
                select(ReceiptDraft).where(ReceiptDraft.receipt_import_id == receipt_import_id)
            ),
        )

    async def draft_items(self, draft_id: UUID) -> list[ReceiptDraftItem]:
        rows = await self._session.scalars(
            select(ReceiptDraftItem)
            .where(ReceiptDraftItem.receipt_draft_id == draft_id)
            .order_by(ReceiptDraftItem.position)
        )
        return list(rows)

    async def replace_draft(
        self,
        draft: ReceiptDraft,
        *,
        merchant_name: str | None,
        occurred_at: datetime,
        total_idr: int,
        items: list[tuple[str, UUID | None, int, int]],
    ) -> None:
        draft.merchant_name = merchant_name
        draft.occurred_at = occurred_at
        draft.total_idr = total_idr
        draft.version += 1
        draft.updated_by_user_id = self._actor.user_id
        await self._session.execute(
            delete(ReceiptDraftItem).where(ReceiptDraftItem.receipt_draft_id == draft.id)
        )
        self._session.add_all(
            [
                ReceiptDraftItem(
                    receipt_draft_id=draft.id,
                    position=position,
                    raw_name=raw_name,
                    normalized_name=raw_name.casefold().strip(),
                    matched_product_id=product_id,
                    quantity=quantity,
                    unit_price_idr=unit_price,
                    line_total_idr=quantity * unit_price,
                    confidence_bps=None,
                    corrected=True,
                )
                for position, (raw_name, product_id, quantity, unit_price) in enumerate(items)
            ]
        )
        await self._session.flush()

    async def transaction(self, transaction_id: UUID) -> Transaction | None:
        return cast(
            Transaction | None,
            await self._session.scalar(
                select(Transaction).where(
                    Transaction.id == transaction_id,
                    Transaction.business_id == self._actor.business_id,
                )
            ),
        )

    async def transaction_items(self, transaction_id: UUID) -> list[TransactionItem]:
        rows = await self._session.scalars(
            select(TransactionItem).where(TransactionItem.transaction_id == transaction_id)
        )
        return list(rows)

    async def products(self, product_ids: list[UUID]) -> dict[UUID, Product]:
        if not product_ids:
            return {}
        rows = await self._session.scalars(
            select(Product).where(
                Product.business_id == self._actor.business_id,
                Product.id.in_(product_ids),
                Product.is_active.is_(True),
            )
        )
        return {product.id: product for product in rows}

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


class ReceiptWorkerRepository:
    """Worker-only repository; API code must use the actor-scoped repository above."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, receipt_import_id: UUID) -> ReceiptImport | None:
        return cast(
            ReceiptImport | None,
            await self._session.scalar(
                select(ReceiptImport)
                .where(
                    ReceiptImport.id == receipt_import_id,
                    ReceiptImport.status == "queued",
                )
                .with_for_update(skip_locked=True)
            ),
        )

    async def next_attempt_number(self, receipt_import_id: UUID) -> int:
        current = await self._session.scalar(
            select(func.max(OcrAttempt.attempt_number)).where(
                OcrAttempt.receipt_import_id == receipt_import_id
            )
        )
        return int(current or 0) + 1

    async def save_extraction(
        self,
        record: ReceiptImport,
        extraction: ReceiptExtraction,
        *,
        attempt_number: int,
        duration_ms: int,
        raw_text_object_key: str,
        preprocessing_version: str,
    ) -> None:
        attempt = OcrAttempt(
            receipt_import_id=record.id,
            attempt_number=attempt_number,
            engine_version=extraction.engine_version,
            preprocessing_version=preprocessing_version,
            duration_ms=duration_ms,
            raw_text_object_key=raw_text_object_key,
            structured_extraction={
                "merchant_name": extraction.merchant_name,
                "occurred_at": extraction.occurred_at.isoformat()
                if extraction.occurred_at
                else None,
                "item_count": len(extraction.items),
                "total_idr": extraction.total_idr,
            },
            confidence_bps=extraction.aggregate_confidence_bps,
        )
        draft = ReceiptDraft(
            receipt_import_id=record.id,
            merchant_name=extraction.merchant_name,
            merchant_confidence_bps=extraction.merchant_confidence_bps,
            occurred_at=extraction.occurred_at,
            occurred_at_confidence_bps=extraction.occurred_at_confidence_bps,
            total_idr=extraction.total_idr,
            total_confidence_bps=extraction.total_confidence_bps,
            updated_by_user_id=record.user_id,
        )
        self._session.add_all([attempt, draft])
        await self._session.flush()
        self._session.add_all(
            [
                ReceiptDraftItem(
                    receipt_draft_id=draft.id,
                    position=position,
                    raw_name=item.raw_name,
                    normalized_name=item.raw_name.casefold().strip(),
                    quantity=item.quantity,
                    unit_price_idr=item.unit_price_idr,
                    line_total_idr=item.quantity * item.unit_price_idr,
                    confidence_bps=item.confidence_bps,
                    corrected=False,
                )
                for position, item in enumerate(extraction.items)
            ]
        )
        record.status = "ready_for_review"
        await self._session.commit()

    async def fail(
        self,
        record: ReceiptImport,
        *,
        attempt_number: int,
        engine_version: str,
        preprocessing_version: str,
        duration_ms: int,
        error_code: str,
    ) -> None:
        self._session.add(
            OcrAttempt(
                receipt_import_id=record.id,
                attempt_number=attempt_number,
                engine_version=engine_version,
                preprocessing_version=preprocessing_version,
                duration_ms=duration_ms,
                error_code=error_code,
            )
        )
        record.status = "failed"
        record.failure_code = error_code
        await self._session.commit()

    async def commit_stage(self, record: ReceiptImport, status: str) -> None:
        record.status = status
        await self._session.commit()
