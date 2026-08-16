from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.correlation import get_correlation_id
from app.core.errors import (
    FieldError,
    NotFoundError,
    ReceiptDraftVersionError,
    ReceiptTotalMismatchError,
    ReceiptUploadError,
    ValidationFailedError,
)
from app.core.time import as_utc
from app.domain.artifacts import ObjectNotFoundError, ObjectStorage
from app.domain.auth import ActorContext, IdentityContext
from app.domain.receipts import ReceiptImageValidationError
from app.integrations.receipt_image import validate_and_sanitize_receipt_image
from app.persistence.models import (
    Product,
    ReceiptDraft,
    ReceiptDraftItem,
    ReceiptImport,
    Transaction,
    TransactionItem,
)
from app.repositories.business import BusinessRepository
from app.repositories.operations import TransactionRepository
from app.repositories.receipts import ReceiptRepository
from app.schemas.operations import TransactionItemRead, TransactionRead
from app.schemas.receipts import (
    ConfidenceField,
    ReceiptConfirm,
    ReceiptConfirmResult,
    ReceiptDraftItemRead,
    ReceiptDraftRead,
    ReceiptDraftUpdate,
    ReceiptImportCreate,
    ReceiptImportCreated,
    ReceiptImportRead,
    SignedTransfer,
)
from app.services.artifact_queue import CeleryReceiptDispatcher, ReceiptDispatcher


class ReceiptService:
    def __init__(
        self,
        session: AsyncSession,
        identity: IdentityContext,
        settings: Settings,
        storage: ObjectStorage,
        dispatcher: ReceiptDispatcher | None = None,
    ) -> None:
        self._session = session
        self._identity = identity
        self._settings = settings
        self._storage = storage
        self._dispatcher = dispatcher or CeleryReceiptDispatcher()
        self._businesses = BusinessRepository(session)

    async def create(self, business_id: UUID, payload: ReceiptImportCreate) -> ReceiptImportCreated:
        actor = await self._actor(business_id)
        if payload.size_bytes > self._settings.receipt_max_size_bytes:
            raise ValidationFailedError(
                fields=[FieldError(path="size_bytes", reason="file_too_large")]
            )
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._settings.object_storage_signed_url_seconds)
        suffix = ".jpg" if payload.content_type == "image/jpeg" else ".png"
        object_key = f"receipts/{actor.business_id}/{actor.user_id}/{uuid4()}{suffix}"
        file_name = PurePath(payload.file_name).name
        repository = ReceiptRepository(self._session, actor)
        record = await repository.create(
            object_key=object_key,
            original_file_name=file_name,
            sha256=payload.sha256.lower(),
            mime_type=payload.content_type,
            size_bytes=payload.size_bytes,
            upload_expires_at=expires_at,
            image_retention_until=now + timedelta(days=self._settings.receipt_image_retention_days),
        )
        url = await self._storage.create_upload_url(
            object_key,
            content_type=payload.content_type,
            expires_seconds=self._settings.object_storage_signed_url_seconds,
        )
        await repository.commit()
        return ReceiptImportCreated(
            receipt_import_id=record.id,
            status="uploading",
            upload=SignedTransfer(method="PUT", url=url, expires_at=expires_at),
        )

    async def complete_upload(
        self, business_id: UUID, receipt_import_id: UUID
    ) -> ReceiptImportRead:
        actor = await self._actor(business_id)
        repository = ReceiptRepository(self._session, actor)
        record = await repository.get(receipt_import_id, for_update=True)
        if record is None:
            raise NotFoundError()
        if record.status not in {"uploading", "queued"}:
            return await self._read(repository, record)
        if as_utc(record.upload_expires_at) < datetime.now(UTC):
            raise ReceiptUploadError("Sesi unggah telah kedaluwarsa. Unggah foto kembali.")
        try:
            stored = await self._storage.read(record.object_key)
            sanitized, actual_mime = validate_and_sanitize_receipt_image(
                stored.body,
                expected_size=record.size_bytes,
                expected_sha256=record.sha256,
                maximum_size=self._settings.receipt_max_size_bytes,
                maximum_pixels=self._settings.receipt_max_pixels,
            )
        except (ObjectNotFoundError, ReceiptImageValidationError) as exc:
            code = (
                exc.code if isinstance(exc, ReceiptImageValidationError) else "RECEIPT_NOT_UPLOADED"
            )
            record.status = "failed"
            record.failure_code = code
            await repository.commit()
            raise ReceiptUploadError() from exc
        if actual_mime != record.mime_type:
            record.status = "failed"
            record.failure_code = "RECEIPT_MIME_MISMATCH"
            await repository.commit()
            raise ReceiptUploadError()
        await self._storage.write(record.object_key, sanitized, content_type=actual_mime)
        record.sha256 = hashlib.sha256(sanitized).hexdigest()
        record.size_bytes = len(sanitized)
        record.status = "queued"
        await repository.commit()
        self._dispatcher.dispatch(record.id, UUID(get_correlation_id()))
        return await self._read(repository, record)

    async def get(self, business_id: UUID, receipt_import_id: UUID) -> ReceiptImportRead:
        actor = await self._actor(business_id)
        repository = ReceiptRepository(self._session, actor)
        record = await repository.get(receipt_import_id)
        if record is None:
            raise NotFoundError()
        return await self._read(repository, record)

    async def update_draft(
        self, business_id: UUID, receipt_import_id: UUID, payload: ReceiptDraftUpdate
    ) -> ReceiptImportRead:
        actor = await self._actor(business_id)
        repository = ReceiptRepository(self._session, actor)
        record = await repository.get(receipt_import_id, for_update=True)
        if record is None or record.status != "ready_for_review":
            raise NotFoundError()
        draft = await repository.draft(record.id)
        if draft is None:
            raise NotFoundError()
        if draft.version != payload.version:
            raise ReceiptDraftVersionError()
        product_ids = [item.matched_product_id for item in payload.items if item.matched_product_id]
        products = await repository.products(product_ids)
        if len(products) != len(set(product_ids)):
            raise ValidationFailedError(
                fields=[FieldError(path="items.matched_product_id", reason="product_not_found")]
            )
        await repository.replace_draft(
            draft,
            merchant_name=payload.merchant_name,
            occurred_at=payload.occurred_at,
            total_idr=payload.total_idr,
            items=[
                (item.raw_name, item.matched_product_id, item.quantity, item.unit_price_idr)
                for item in payload.items
            ],
        )
        await repository.commit()
        return await self._read(repository, record)

    async def confirm(
        self, business_id: UUID, receipt_import_id: UUID, payload: ReceiptConfirm
    ) -> ReceiptConfirmResult:
        actor = await self._actor(business_id)
        repository = ReceiptRepository(self._session, actor)
        record = await repository.get(receipt_import_id, for_update=True)
        if record is None:
            raise NotFoundError()
        if record.status == "committed" and record.confirmed_transaction_id:
            transaction = await repository.transaction(record.confirmed_transaction_id)
            if transaction is None:
                raise NotFoundError()
            return ReceiptConfirmResult(
                receipt_import_id=record.id,
                status="committed",
                transaction=await self._transaction_read(repository, transaction),
            )
        if record.status != "ready_for_review":
            raise NotFoundError()
        draft = await repository.draft(record.id)
        if draft is None:
            raise NotFoundError()
        if draft.version != payload.version:
            raise ReceiptDraftVersionError()
        items = await repository.draft_items(draft.id)
        if not items or any(item.matched_product_id is None for item in items):
            raise ValidationFailedError(
                fields=[FieldError(path="items.matched_product_id", reason="required")]
            )
        calculated_total = sum(item.line_total_idr for item in items)
        if calculated_total != draft.total_idr and not payload.accept_total_mismatch:
            raise ReceiptTotalMismatchError()
        products = await repository.products(
            [item.matched_product_id for item in items if item.matched_product_id]
        )
        if len(products) != len(items):
            raise ValidationFailedError(
                fields=[FieldError(path="items.matched_product_id", reason="product_not_found")]
            )
        record.status = "confirmed"
        transaction = await TransactionRepository(self._session, actor).create(
            occurred_at=draft.occurred_at or datetime.now(UTC),
            channel=payload.channel,
            gross_total_idr=calculated_total,
            client_reference=f"receipt:{record.id}",
            items=[
                (
                    item.matched_product_id,
                    item.quantity,
                    item.unit_price_idr,
                    item.line_total_idr,
                )
                for item in items
                if item.matched_product_id is not None
            ],
            source="receipt_ocr",
            receipt_import_id=record.id,
        )
        record.status = "committed"
        record.confirmed_transaction_id = transaction.id
        record.confirmed_at = datetime.now(UTC)
        await repository.commit()
        return ReceiptConfirmResult(
            receipt_import_id=record.id,
            status="committed",
            transaction=self._transaction_from_parts(transaction, items, products),
        )

    async def _actor(self, business_id: UUID) -> ActorContext:
        actor = await self._businesses.get_actor(self._identity, business_id)
        if actor is None:
            raise NotFoundError()
        return actor

    async def _read(
        self, repository: ReceiptRepository, record: ReceiptImport
    ) -> ReceiptImportRead:
        draft = await repository.draft(record.id)
        draft_read = None
        warnings: list[str] = []
        if draft is not None:
            items = await repository.draft_items(draft.id)
            calculated_total = sum(item.line_total_idr for item in items)
            if any(item.matched_product_id is None for item in items):
                warnings.append("Cocokkan semua item dengan produk sebelum mengonfirmasi.")
            if calculated_total != draft.total_idr:
                warnings.append("Jumlah item berbeda dari total struk.")
            draft_read = self._draft_read(draft, items, calculated_total)
        image = None
        if record.status not in {"failed", "cancelled"} and record.object_key:
            expires_at = datetime.now(UTC) + timedelta(
                seconds=self._settings.object_storage_signed_url_seconds
            )
            image = SignedTransfer(
                method="GET",
                url=await self._storage.create_download_url(
                    record.object_key,
                    expires_seconds=self._settings.object_storage_signed_url_seconds,
                ),
                expires_at=expires_at,
            )
        transaction_read = None
        if record.confirmed_transaction_id:
            transaction = await repository.transaction(record.confirmed_transaction_id)
            if transaction:
                transaction_read = await self._transaction_read(repository, transaction)
        return ReceiptImportRead(
            receipt_import_id=record.id,
            business_id=record.business_id,
            status=record.status,
            draft=draft_read,
            warnings=warnings,
            image=image,
            failure_code=record.failure_code,
            transaction=transaction_read,
        )

    @staticmethod
    def _draft_read(
        draft: ReceiptDraft, items: list[ReceiptDraftItem], calculated_total: int
    ) -> ReceiptDraftRead:
        return ReceiptDraftRead(
            version=draft.version,
            merchant_name=ConfidenceField(
                value=draft.merchant_name,
                confidence=_confidence(draft.merchant_confidence_bps),
            ),
            occurred_at=ConfidenceField(
                value=draft.occurred_at,
                confidence=_confidence(draft.occurred_at_confidence_bps),
            ),
            items=[
                ReceiptDraftItemRead(
                    position=item.position,
                    raw_name=item.raw_name,
                    matched_product_id=item.matched_product_id,
                    quantity=item.quantity,
                    unit_price_idr=item.unit_price_idr,
                    line_total_idr=item.line_total_idr,
                    confidence=_confidence(item.confidence_bps),
                    corrected=item.corrected,
                )
                for item in items
            ],
            total_idr=ConfidenceField(
                value=draft.total_idr,
                confidence=_confidence(draft.total_confidence_bps),
            ),
            calculated_items_total_idr=calculated_total,
            total_matches_items=calculated_total == draft.total_idr,
        )

    async def _transaction_read(
        self, repository: ReceiptRepository, transaction: Transaction
    ) -> TransactionRead:
        items = await repository.transaction_items(transaction.id)
        products = await repository.products([item.product_id for item in items])
        return self._transaction_from_parts(transaction, items, products)

    @staticmethod
    def _transaction_from_parts(
        transaction: Transaction,
        items: Sequence[TransactionItem | ReceiptDraftItem],
        products: dict[UUID, Product],
    ) -> TransactionRead:
        return TransactionRead(
            id=transaction.id,
            business_id=transaction.business_id,
            occurred_at=transaction.occurred_at,
            channel=transaction.channel,
            gross_total_idr=transaction.gross_total_idr,
            source=transaction.source,
            client_reference=transaction.client_reference,
            items=[
                TransactionItemRead(
                    product_id=_item_product_id(item),
                    product_name=products[_item_product_id(item)].name,
                    quantity=item.quantity,
                    unit_price_idr=item.unit_price_idr,
                    line_total_idr=item.line_total_idr,
                )
                for item in items
            ],
        )


def _confidence(value: int | None) -> float | None:
    return None if value is None else value / 10_000


def _item_product_id(item: TransactionItem | ReceiptDraftItem) -> UUID:
    if isinstance(item, TransactionItem):
        return item.product_id
    if item.matched_product_id is None:
        raise ValueError("Confirmed receipt item must reference a product")
    return item.matched_product_id
