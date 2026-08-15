from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import IdentityContext
from app.persistence.models import (
    AnalysisReportRecord,
    AnalysisRun,
    ExportArtifact,
    OcrAttempt,
    Product,
    ReceiptImport,
    Transaction,
    TransactionItem,
)


class ExportRepository:
    def __init__(self, session: AsyncSession, identity: IdentityContext) -> None:
        self._session = session
        self._identity = identity

    async def analysis(self, analysis_id: UUID) -> AnalysisRun | None:
        return cast(
            AnalysisRun | None,
            await self._session.scalar(
                select(AnalysisRun).where(
                    AnalysisRun.id == analysis_id,
                    AnalysisRun.user_id == self._identity.user_id,
                    AnalysisRun.status.in_(["completed", "partial"]),
                )
            ),
        )

    async def find_by_idempotency(self, key: str) -> ExportArtifact | None:
        return cast(
            ExportArtifact | None,
            await self._session.scalar(
                select(ExportArtifact).where(
                    ExportArtifact.requested_by_user_id == self._identity.user_id,
                    ExportArtifact.idempotency_key == key,
                )
            ),
        )

    async def create(
        self,
        *,
        kind: str,
        idempotency_key: str,
        retention_until: datetime,
        request_snapshot: dict[str, object],
        business_id: UUID | None = None,
        analysis_run_id: UUID | None = None,
    ) -> ExportArtifact:
        artifact = ExportArtifact(
            requested_by_user_id=self._identity.user_id,
            business_id=business_id,
            analysis_run_id=analysis_run_id,
            kind=kind,
            status="queued",
            request_snapshot=request_snapshot,
            idempotency_key=idempotency_key,
            retention_until=retention_until,
        )
        self._session.add(artifact)
        await self._session.flush()
        return artifact

    async def get(self, export_id: UUID) -> ExportArtifact | None:
        return cast(
            ExportArtifact | None,
            await self._session.scalar(
                select(ExportArtifact).where(
                    ExportArtifact.id == export_id,
                    ExportArtifact.requested_by_user_id == self._identity.user_id,
                )
            ),
        )

    async def commit(self) -> None:
        await self._session.commit()


class ExportWorkerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim(self, export_id: UUID) -> ExportArtifact | None:
        artifact = cast(
            ExportArtifact | None,
            await self._session.scalar(
                select(ExportArtifact)
                .where(ExportArtifact.id == export_id, ExportArtifact.status == "queued")
                .with_for_update(skip_locked=True)
            ),
        )
        if artifact is not None:
            artifact.status = "processing"
            await self._session.commit()
        return artifact

    async def analysis_report(self, analysis_id: UUID) -> AnalysisReportRecord | None:
        return cast(
            AnalysisReportRecord | None,
            await self._session.scalar(
                select(AnalysisReportRecord).where(
                    AnalysisReportRecord.analysis_run_id == analysis_id
                )
            ),
        )

    async def transactions(
        self, business_id: UUID, *, start: datetime, end: datetime
    ) -> tuple[list[Transaction], list[TransactionItem], dict[UUID, Product]]:
        transactions = list(
            await self._session.scalars(
                select(Transaction)
                .where(
                    Transaction.business_id == business_id,
                    Transaction.occurred_at >= start,
                    Transaction.occurred_at <= end,
                )
                .order_by(Transaction.occurred_at)
            )
        )
        transaction_ids = [transaction.id for transaction in transactions]
        items = (
            list(
                await self._session.scalars(
                    select(TransactionItem).where(
                        TransactionItem.transaction_id.in_(transaction_ids)
                    )
                )
            )
            if transaction_ids
            else []
        )
        product_ids = [item.product_id for item in items]
        products = (
            {
                product.id: product
                for product in await self._session.scalars(
                    select(Product).where(Product.id.in_(product_ids))
                )
            }
            if product_ids
            else {}
        )
        return transactions, items, products

    async def ready(
        self, artifact: ExportArtifact, *, object_key: str, sha256: str, size_bytes: int
    ) -> None:
        artifact.status = "ready"
        artifact.object_key = object_key
        artifact.sha256 = sha256
        artifact.size_bytes = size_bytes
        artifact.completed_at = datetime.now(UTC)
        await self._session.commit()

    async def fail(self, artifact: ExportArtifact, code: str) -> None:
        artifact.status = "failed"
        artifact.failure_code = code
        await self._session.commit()

    async def expired_exports(self, now: datetime) -> list[ExportArtifact]:
        return list(
            await self._session.scalars(
                select(ExportArtifact).where(
                    ExportArtifact.retention_until <= now,
                    ExportArtifact.status.in_(["ready", "failed"]),
                )
            )
        )

    async def expired_receipts(self, now: datetime) -> list[ReceiptImport]:
        return list(
            await self._session.scalars(
                select(ReceiptImport).where(
                    ReceiptImport.image_retention_until <= now,
                    ReceiptImport.object_key != "",
                )
            )
        )

    async def raw_ocr_attempts(self, receipt_ids: list[UUID]) -> list[OcrAttempt]:
        if not receipt_ids:
            return []
        return list(
            await self._session.scalars(
                select(OcrAttempt).where(
                    OcrAttempt.receipt_import_id.in_(receipt_ids),
                    OcrAttempt.raw_text_object_key.is_not(None),
                )
            )
        )

    async def commit(self) -> None:
        await self._session.commit()
