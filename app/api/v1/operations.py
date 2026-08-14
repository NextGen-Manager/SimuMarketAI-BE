from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.operation_dependencies import OperationsServiceDependency
from app.schemas.operations import (
    DashboardResponse,
    OwnerProductRead,
    ProductCreate,
    ProductRead,
    ProductUpdate,
    TransactionAnalytics,
    TransactionBatchCreate,
    TransactionCreate,
    TransactionRead,
)

router = APIRouter(tags=["operations"])
OptionalDateTimeQuery = Annotated[datetime | None, Query()]


@router.get("/products", response_model=list[ProductRead])
async def list_products(
    business_id: UUID,
    service: OperationsServiceDependency,
) -> list[ProductRead]:
    return await service.list_products(business_id)


@router.post("/products", response_model=OwnerProductRead, status_code=201)
async def create_product(
    payload: ProductCreate,
    service: OperationsServiceDependency,
) -> OwnerProductRead:
    return await service.create_product(payload)


@router.patch("/products/{product_id}", response_model=OwnerProductRead)
async def update_product(
    product_id: UUID,
    business_id: UUID,
    payload: ProductUpdate,
    service: OperationsServiceDependency,
) -> OwnerProductRead:
    return await service.update_product(business_id, product_id, payload)


@router.post("/transactions", response_model=TransactionRead, status_code=201)
async def create_transaction(
    payload: TransactionCreate,
    service: OperationsServiceDependency,
) -> TransactionRead:
    return await service.create_transaction(payload)


@router.post("/transactions/batch", response_model=list[TransactionRead], status_code=201)
async def create_transaction_batch(
    payload: TransactionBatchCreate,
    service: OperationsServiceDependency,
) -> list[TransactionRead]:
    return await service.create_transaction_batch(payload)


@router.get("/transactions", response_model=list[TransactionRead])
async def list_transactions(
    business_id: UUID,
    service: OperationsServiceDependency,
    start: OptionalDateTimeQuery = None,
    end: OptionalDateTimeQuery = None,
) -> list[TransactionRead]:
    return await service.list_transactions(business_id, start=start, end=end)


@router.get("/transaction-analytics", response_model=TransactionAnalytics)
async def transaction_analytics(
    business_id: UUID,
    service: OperationsServiceDependency,
) -> TransactionAnalytics:
    return await service.transaction_analytics(business_id)


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard(
    service: OperationsServiceDependency,
    business_id: UUID | None = None,
) -> DashboardResponse:
    return await service.dashboard(business_id)
