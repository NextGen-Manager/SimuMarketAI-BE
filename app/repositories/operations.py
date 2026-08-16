from __future__ import annotations

from datetime import date, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.auth import ActorContext, BusinessRole, IdentityContext
from app.engines.transaction_analytics import (
    AnalyticsLine,
    AnalyticsProduct,
    to_jakarta_datetime,
)
from app.persistence.models import (
    AnalysisRun,
    Membership,
    Product,
    Transaction,
    TransactionItem,
)


class ProductRepository:
    def __init__(self, session: AsyncSession, actor: ActorContext) -> None:
        self._session = session
        self._actor = actor

    async def list_products(self) -> list[Product]:
        query = select(Product).where(Product.business_id == self._actor.business_id)
        if self._actor.role is BusinessRole.CASHIER:
            query = query.where(Product.is_active.is_(True))
        rows = await self._session.scalars(query.order_by(Product.name))
        return list(rows)

    async def get(self, product_id: UUID) -> Product | None:
        return cast(
            Product | None,
            await self._session.scalar(
                select(Product).where(
                    Product.id == product_id,
                    Product.business_id == self._actor.business_id,
                )
            ),
        )

    async def get_many(self, product_ids: list[UUID]) -> list[Product]:
        if not product_ids:
            return []
        rows = await self._session.scalars(
            select(Product).where(
                Product.business_id == self._actor.business_id,
                Product.id.in_(product_ids),
            )
        )
        return list(rows)

    async def create(self, *, name: str, selling_price_idr: int, hpp_idr: int) -> Product:
        product = Product(
            business_id=self._actor.business_id,
            name=name,
            selling_price_idr=selling_price_idr,
            hpp_idr=hpp_idr,
        )
        self._session.add(product)
        await self._session.flush()
        return product

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


class TransactionRepository:
    def __init__(self, session: AsyncSession, actor: ActorContext) -> None:
        self._session = session
        self._actor = actor

    async def find_by_client_reference(self, value: str) -> Transaction | None:
        return cast(
            Transaction | None,
            await self._session.scalar(
                select(Transaction).where(
                    Transaction.business_id == self._actor.business_id,
                    Transaction.client_reference == value,
                )
            ),
        )

    async def create(
        self,
        *,
        occurred_at: datetime,
        channel: str,
        gross_total_idr: int,
        client_reference: str | None,
        items: list[tuple[UUID, int, int, int]],
        source: str = "manual",
        receipt_import_id: UUID | None = None,
    ) -> Transaction:
        transaction = Transaction(
            business_id=self._actor.business_id,
            recorded_by_user_id=self._actor.user_id,
            occurred_at=occurred_at,
            channel=channel,
            gross_total_idr=gross_total_idr,
            source=source,
            client_reference=client_reference,
            receipt_import_id=receipt_import_id,
        )
        self._session.add(transaction)
        await self._session.flush()
        self._session.add_all(
            [
                TransactionItem(
                    transaction_id=transaction.id,
                    product_id=product_id,
                    quantity=quantity,
                    unit_price_idr=unit_price_idr,
                    line_total_idr=line_total_idr,
                )
                for product_id, quantity, unit_price_idr, line_total_idr in items
            ]
        )
        await self._session.flush()
        return transaction

    async def list_transactions(
        self, *, start: datetime | None = None, end: datetime | None = None
    ) -> list[Transaction]:
        query = select(Transaction).where(Transaction.business_id == self._actor.business_id)
        if start is not None:
            query = query.where(Transaction.occurred_at >= start)
        if end is not None:
            query = query.where(Transaction.occurred_at <= end)
        rows = await self._session.scalars(query.order_by(Transaction.occurred_at.desc()))
        return list(rows)

    async def items_for(self, transaction_ids: list[UUID]) -> list[TransactionItem]:
        if not transaction_ids:
            return []
        rows = await self._session.scalars(
            select(TransactionItem).where(TransactionItem.transaction_id.in_(transaction_ids))
        )
        return list(rows)

    async def analytics_inputs(self) -> tuple[list[AnalyticsProduct], list[AnalyticsLine]]:
        products = await self._session.scalars(
            select(Product).where(Product.business_id == self._actor.business_id)
        )
        rows = await self._session.execute(
            select(TransactionItem, Transaction)
            .join(Transaction, Transaction.id == TransactionItem.transaction_id)
            .where(Transaction.business_id == self._actor.business_id)
        )
        return (
            [
                AnalyticsProduct(id=product.id, name=product.name, created_at=product.created_at)
                for product in products
            ],
            [
                AnalyticsLine(
                    product_id=item.product_id,
                    occurred_at=transaction.occurred_at,
                    transaction_id=transaction.id,
                    quantity=item.quantity,
                    line_total_idr=item.line_total_idr,
                )
                for item, transaction in rows.tuples()
            ],
        )

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


class DashboardRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def owned_business_ids(self, identity: IdentityContext) -> list[UUID]:
        values = await self._session.scalars(
            select(Membership.business_id).where(
                Membership.user_id == identity.user_id,
                Membership.role == BusinessRole.OWNER.value,
            )
        )
        return list(values)

    async def analyses(self, identity: IdentityContext, *, limit: int = 5) -> list[AnalysisRun]:
        rows = await self._session.scalars(
            select(AnalysisRun)
            .where(
                AnalysisRun.user_id == identity.user_id,
                AnalysisRun.status.in_(["completed", "partial"]),
                AnalysisRun.score.is_not(None),
            )
            .order_by(AnalysisRun.created_at.desc())
            .limit(limit)
        )
        return list(rows)

    async def transaction_summary(
        self, business_ids: list[UUID], *, local_today: date
    ) -> tuple[int, int, int]:
        if not business_ids:
            return 0, 0, 0
        transactions = await self._session.scalars(
            select(Transaction).where(Transaction.business_id.in_(business_ids))
        )
        rows = list(transactions)
        local_dates = [self._local_date(item.occurred_at) for item in rows]
        today_rows = [
            item
            for item, item_date in zip(rows, local_dates, strict=True)
            if item_date == local_today
        ]
        return (
            len(set(local_dates)),
            len(today_rows),
            sum(item.gross_total_idr for item in today_rows),
        )

    @staticmethod
    def _local_date(value: datetime) -> date:
        return to_jakarta_datetime(value).date()
