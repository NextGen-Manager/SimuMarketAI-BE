from __future__ import annotations

from datetime import UTC, date, datetime
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.domain.auth import ActorContext, BusinessRole, IdentityContext
from app.engines.dashboard_state import determine_owner_dashboard_state
from app.engines.transaction_analytics import build_transaction_analytics
from app.persistence.models import AnalysisRun, Product, Transaction, TransactionItem
from app.repositories.business import BusinessRepository
from app.repositories.identity import IdentityRepository
from app.repositories.operations import (
    DashboardRepository,
    ProductRepository,
    TransactionRepository,
)
from app.schemas.operations import (
    CashierProductRead,
    DashboardAnalysis,
    DashboardEducation,
    DashboardPlan,
    DashboardResponse,
    DashboardToday,
    DashboardTransactions,
    OwnerProductRead,
    ProductCreate,
    ProductRead,
    ProductUpdate,
    TransactionAnalytics,
    TransactionBatchCreate,
    TransactionCreate,
    TransactionInsight,
    TransactionItemRead,
    TransactionRead,
)
from app.services.audit import audit_event

JAKARTA = ZoneInfo("Asia/Jakarta")


class ProductService:
    def __init__(
        self,
        repository: ProductRepository,
        identity_repository: IdentityRepository,
        actor: ActorContext,
    ) -> None:
        self._products = repository
        self._identity = identity_repository
        self._actor = actor

    async def list_products(self) -> list[ProductRead]:
        return [self._read(product) for product in await self._products.list_products()]

    async def create_product(
        self, *, name: str, selling_price_idr: int, hpp_idr: int
    ) -> OwnerProductRead:
        self._require_owner()
        try:
            product = await self._products.create(
                name=name.strip(),
                selling_price_idr=selling_price_idr,
                hpp_idr=hpp_idr,
            )
            self._identity.add_audit_event(
                audit_event(
                    actor_user_id=self._actor.user_id,
                    action="product.create",
                    resource_type="product",
                    resource_id=product.id,
                    outcome="success",
                )
            )
            await self._products.commit()
        except IntegrityError as exc:
            await self._products.rollback()
            raise ConflictError("Nama produk sudah digunakan pada usaha ini.") from exc
        return self._owner_read(product)

    async def update_product(
        self,
        product_id: UUID,
        *,
        name: str,
        selling_price_idr: int,
        hpp_idr: int,
        is_active: bool,
    ) -> OwnerProductRead:
        self._require_owner()
        product = await self._products.get(product_id)
        if product is None:
            raise NotFoundError()
        product.name = name.strip()
        product.selling_price_idr = selling_price_idr
        product.hpp_idr = hpp_idr
        product.is_active = is_active
        try:
            self._identity.add_audit_event(
                audit_event(
                    actor_user_id=self._actor.user_id,
                    action="product.update",
                    resource_type="product",
                    resource_id=product.id,
                    outcome="success",
                )
            )
            await self._products.commit()
        except IntegrityError as exc:
            await self._products.rollback()
            raise ConflictError("Nama produk sudah digunakan pada usaha ini.") from exc
        return self._owner_read(product)

    def _read(self, product: Product) -> ProductRead:
        if self._actor.role is BusinessRole.CASHIER:
            return CashierProductRead(
                id=product.id,
                business_id=product.business_id,
                name=product.name,
                selling_price_idr=product.selling_price_idr,
                is_active=product.is_active,
            )
        return self._owner_read(product)

    @staticmethod
    def _owner_read(product: Product) -> OwnerProductRead:
        return OwnerProductRead(
            id=product.id,
            business_id=product.business_id,
            name=product.name,
            selling_price_idr=product.selling_price_idr,
            hpp_idr=product.hpp_idr,
            margin_idr=product.selling_price_idr - product.hpp_idr,
            is_active=product.is_active,
        )

    def _require_owner(self) -> None:
        if not self._actor.is_owner():
            raise NotFoundError()


class TransactionService:
    def __init__(
        self,
        repository: TransactionRepository,
        product_repository: ProductRepository,
        identity_repository: IdentityRepository,
        actor: ActorContext,
    ) -> None:
        self._transactions = repository
        self._products = product_repository
        self._identity = identity_repository
        self._actor = actor

    async def create_transaction(self, payload: TransactionCreate) -> TransactionRead:
        if payload.client_reference:
            existing = await self._transactions.find_by_client_reference(payload.client_reference)
            if existing is not None:
                return await self._read(existing)

        try:
            transaction = await self._create_uncommitted(payload)
            await self._transactions.commit()
        except IntegrityError as exc:
            await self._transactions.rollback()
            if payload.client_reference:
                existing = await self._transactions.find_by_client_reference(
                    payload.client_reference
                )
                if existing is not None:
                    return await self._read(existing)
            raise ConflictError("Transaksi dengan referensi tersebut sudah tercatat.") from exc
        return await self._read(transaction)

    async def create_batch(self, payloads: list[TransactionCreate]) -> list[TransactionRead]:
        transactions: list[Transaction] = []
        try:
            for payload in payloads:
                if payload.business_id != self._actor.business_id:
                    raise NotFoundError()
                existing = (
                    await self._transactions.find_by_client_reference(payload.client_reference)
                    if payload.client_reference
                    else None
                )
                transactions.append(
                    existing if existing is not None else await self._create_uncommitted(payload)
                )
            await self._transactions.commit()
        except IntegrityError as exc:
            await self._transactions.rollback()
            raise ConflictError("Salah satu transaksi sudah tercatat.") from exc
        return await self._read_many(transactions)

    async def _create_uncommitted(self, payload: TransactionCreate) -> Transaction:

        item_values: list[tuple[UUID, int, int, int]] = []
        for item in payload.items:
            product = await self._products.get(item.product_id)
            if product is None or not product.is_active:
                raise NotFoundError("Produk tidak ditemukan atau tidak aktif.")
            item_values.append(
                (
                    product.id,
                    item.quantity,
                    item.unit_price_idr,
                    item.quantity * item.unit_price_idr,
                )
            )
        gross_total_idr = sum(item[3] for item in item_values)
        transaction = await self._transactions.create(
            occurred_at=payload.occurred_at,
            channel=payload.channel,
            gross_total_idr=gross_total_idr,
            client_reference=payload.client_reference,
            items=item_values,
        )
        self._identity.add_audit_event(
            audit_event(
                actor_user_id=self._actor.user_id,
                action="transaction.create",
                resource_type="transaction",
                resource_id=transaction.id,
                outcome="success",
            )
        )
        return transaction

    async def list_transactions(
        self, *, start: datetime | None, end: datetime | None
    ) -> list[TransactionRead]:
        self._require_owner()
        transactions = await self._transactions.list_transactions(start=start, end=end)
        return await self._read_many(transactions)

    async def analytics(self) -> TransactionAnalytics:
        self._require_owner()
        products, lines = await self._transactions.analytics_inputs()
        return build_transaction_analytics(
            business_id=self._actor.business_id,
            products=products,
            lines=lines,
        )

    async def _read(self, transaction: Transaction) -> TransactionRead:
        return (await self._read_many([transaction]))[0]

    async def _read_many(self, transactions: list[Transaction]) -> list[TransactionRead]:
        items = await self._transactions.items_for([item.id for item in transactions])
        product_ids = list({item.product_id for item in items})
        products = {product.id: product for product in await self._products.get_many(product_ids)}
        items_by_transaction: dict[UUID, list[TransactionItem]] = {}
        for item in items:
            items_by_transaction.setdefault(item.transaction_id, []).append(item)
        return [
            TransactionRead(
                id=transaction.id,
                business_id=transaction.business_id,
                occurred_at=transaction.occurred_at,
                channel=transaction.channel,
                gross_total_idr=transaction.gross_total_idr,
                source=transaction.source,
                client_reference=transaction.client_reference,
                items=[
                    TransactionItemRead(
                        product_id=item.product_id,
                        product_name=products[item.product_id].name,
                        quantity=item.quantity,
                        unit_price_idr=item.unit_price_idr,
                        line_total_idr=item.line_total_idr,
                    )
                    for item in items_by_transaction.get(transaction.id, [])
                ],
            )
            for transaction in transactions
        ]

    def _require_owner(self) -> None:
        if not self._actor.is_owner():
            raise NotFoundError()


class DashboardService:
    def __init__(
        self,
        dashboard_repository: DashboardRepository,
        business_repository: BusinessRepository,
        session: AsyncSession,
    ) -> None:
        self._dashboard = dashboard_repository
        self._businesses = business_repository
        self._session = session

    async def get_dashboard(
        self,
        identity: IdentityContext,
        *,
        business_id: UUID | None,
        now: datetime | None = None,
    ) -> DashboardResponse:
        current = now or datetime.now(UTC)
        local_today = current.astimezone(JAKARTA).date()
        if business_id is not None:
            actor = await self._businesses.get_actor(identity, business_id)
            if actor is None:
                raise NotFoundError()
            if actor.role is BusinessRole.CASHIER:
                return await self._cashier_dashboard(actor, local_today)

        owned_business_ids = await self._dashboard.owned_business_ids(identity)
        if not owned_business_ids:
            memberships = await self._businesses.list_for_user(identity)
            cashier_memberships = [
                membership
                for _, membership in memberships
                if membership.role == BusinessRole.CASHIER.value
            ]
            if len(cashier_memberships) == 1:
                actor = await self._businesses.get_actor(
                    identity, cashier_memberships[0].business_id
                )
                if actor is not None:
                    return await self._cashier_dashboard(actor, local_today)

        analyses = await self._dashboard.analyses(identity)
        recorded_days, today_count, today_revenue = await self._dashboard.transaction_summary(
            owned_business_ids,
            local_today=local_today,
        )
        state = determine_owner_dashboard_state(
            has_business=bool(owned_business_ids),
            has_analysis=bool(analyses),
            recorded_days=recorded_days,
        )
        history = [self._analysis_read(item) for item in analyses]
        insight = await self._latest_owner_insight(identity, owned_business_ids)
        return DashboardResponse(
            keadaan=state,
            analisis_terakhir=history[0] if history else None,
            rencana_30_hari=DashboardPlan(),
            transaksi=DashboardTransactions(
                hari_tercatat=recorded_days,
                hari_ini=DashboardToday(
                    jumlah=today_count,
                    pendapatan_idr=today_revenue,
                ),
            ),
            insight_terbaru=insight,
            edukasi=DashboardEducation(),
            riwayat_analisis=history,
        )

    async def _cashier_dashboard(self, actor: ActorContext, local_today: date) -> DashboardResponse:
        recorded_days, today_count, today_revenue = await self._dashboard.transaction_summary(
            [actor.business_id],
            local_today=local_today,
        )
        return DashboardResponse(
            keadaan="kasir_sudah_mencatat" if today_count else "kasir_belum_mencatat",
            analisis_terakhir=None,
            rencana_30_hari=DashboardPlan(),
            transaksi=DashboardTransactions(
                hari_tercatat=recorded_days,
                hari_ini=DashboardToday(
                    jumlah=today_count,
                    pendapatan_idr=today_revenue,
                ),
            ),
            insight_terbaru=None,
            edukasi=DashboardEducation(),
            riwayat_analisis=[],
        )

    async def _latest_owner_insight(
        self, identity: IdentityContext, business_ids: list[UUID]
    ) -> TransactionInsight | None:
        for business_id in business_ids:
            actor = await self._businesses.get_actor(identity, business_id)
            if actor is None:
                continue
            repository = TransactionRepository(self._session, actor)
            products, lines = await repository.analytics_inputs()
            analytics = build_transaction_analytics(
                business_id=business_id,
                products=products,
                lines=lines,
            )
            if analytics.insights:
                return analytics.insights[0]
        return None

    @staticmethod
    def _analysis_read(analysis: AnalysisRun) -> DashboardAnalysis:
        return DashboardAnalysis(
            id=analysis.id,
            nama=analysis.concept_name,
            area=analysis.area_name,
            skor=cast(int, analysis.score),
            interpretasi=cast(str, analysis.interpretation),
            rule_version=cast(str, analysis.rule_version),
            dibuat=analysis.created_at,
        )


class OperationsService:
    """Application boundary for tenant-scoped operational use cases."""

    def __init__(self, session: AsyncSession, identity: IdentityContext) -> None:
        self._session = session
        self._identity = identity
        self._businesses = BusinessRepository(session)
        self._identity_repository = IdentityRepository(session)

    async def list_products(self, business_id: UUID) -> list[ProductRead]:
        actor = await self._actor(business_id)
        return await self._product_service(actor).list_products()

    async def create_product(self, payload: ProductCreate) -> OwnerProductRead:
        actor = await self._actor(payload.business_id)
        return await self._product_service(actor).create_product(
            name=payload.name,
            selling_price_idr=payload.selling_price_idr,
            hpp_idr=payload.hpp_idr,
        )

    async def update_product(
        self, business_id: UUID, product_id: UUID, payload: ProductUpdate
    ) -> OwnerProductRead:
        actor = await self._actor(business_id)
        return await self._product_service(actor).update_product(
            product_id,
            name=payload.name,
            selling_price_idr=payload.selling_price_idr,
            hpp_idr=payload.hpp_idr,
            is_active=payload.is_active,
        )

    async def create_transaction(self, payload: TransactionCreate) -> TransactionRead:
        actor = await self._actor(payload.business_id)
        return await self._transaction_service(actor).create_transaction(payload)

    async def create_transaction_batch(
        self, payload: TransactionBatchCreate
    ) -> list[TransactionRead]:
        business_ids = {transaction.business_id for transaction in payload.transactions}
        if len(business_ids) != 1:
            raise NotFoundError()
        actor = await self._actor(business_ids.pop())
        return await self._transaction_service(actor).create_batch(payload.transactions)

    async def list_transactions(
        self,
        business_id: UUID,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> list[TransactionRead]:
        actor = await self._actor(business_id)
        return await self._transaction_service(actor).list_transactions(start=start, end=end)

    async def transaction_analytics(self, business_id: UUID) -> TransactionAnalytics:
        actor = await self._actor(business_id)
        return await self._transaction_service(actor).analytics()

    async def dashboard(self, business_id: UUID | None) -> DashboardResponse:
        return await DashboardService(
            DashboardRepository(self._session),
            self._businesses,
            self._session,
        ).get_dashboard(self._identity, business_id=business_id)

    async def _actor(self, business_id: UUID) -> ActorContext:
        actor = await self._businesses.get_actor(self._identity, business_id)
        if actor is None:
            raise NotFoundError()
        return actor

    def _product_service(self, actor: ActorContext) -> ProductService:
        return ProductService(
            ProductRepository(self._session, actor),
            self._identity_repository,
            actor,
        )

    def _transaction_service(self, actor: ActorContext) -> TransactionService:
        return TransactionService(
            TransactionRepository(self._session, actor),
            ProductRepository(self._session, actor),
            self._identity_repository,
            actor,
        )
