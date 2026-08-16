from __future__ import annotations

from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ProductCreate(BaseModel):
    business_id: UUID
    name: str = Field(min_length=2, max_length=120)
    selling_price_idr: int = Field(ge=0)
    hpp_idr: int = Field(ge=0)


class ProductUpdate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    selling_price_idr: int = Field(ge=0)
    hpp_idr: int = Field(ge=0)
    is_active: bool


class OwnerProductRead(BaseModel):
    access: Literal["owner"] = "owner"
    id: UUID
    business_id: UUID
    name: str
    selling_price_idr: int
    hpp_idr: int
    margin_idr: int
    is_active: bool


class CashierProductRead(BaseModel):
    access: Literal["cashier"] = "cashier"
    id: UUID
    business_id: UUID
    name: str
    selling_price_idr: int
    is_active: bool


ProductRead = OwnerProductRead | CashierProductRead


class TransactionItemWrite(BaseModel):
    product_id: UUID
    quantity: int = Field(gt=0, le=10_000)
    unit_price_idr: int = Field(ge=0)


class TransactionCreate(BaseModel):
    business_id: UUID
    occurred_at: datetime
    channel: Literal["dine_in", "takeaway", "delivery"]
    client_reference: str | None = Field(default=None, min_length=1, max_length=120)
    items: list[TransactionItemWrite] = Field(min_length=1, max_length=100)


class TransactionBatchCreate(BaseModel):
    transactions: list[TransactionCreate] = Field(min_length=1, max_length=100)


class TransactionItemRead(BaseModel):
    product_id: UUID
    product_name: str
    quantity: int
    unit_price_idr: int
    line_total_idr: int


class TransactionRead(BaseModel):
    id: UUID
    business_id: UUID
    occurred_at: datetime
    channel: str
    gross_total_idr: int
    source: str
    client_reference: str | None
    items: list[TransactionItemRead]


class ObservationWindow(BaseModel):
    start: date
    end: date
    timezone: Literal["Asia/Jakarta"] = "Asia/Jakarta"


class DailySales(BaseModel):
    date: date
    transaction_count: int
    revenue_idr: int


class ProductSales(BaseModel):
    product_id: UUID
    product_name: str
    quantity: int
    revenue_idr: int
    exposure_days: int


class HourlySales(BaseModel):
    hour: int = Field(ge=0, le=23)
    transaction_count: int
    revenue_idr: int


class TransactionInsight(BaseModel):
    rule_version: Literal["transaction-insight-v1"] = "transaction-insight-v1"
    message: str
    observation_window: ObservationWindow


class TransactionAnalytics(BaseModel):
    status: Literal["collecting", "available"]
    business_id: UUID
    days_recorded: int
    threshold_days: Literal[7] = 7
    observation_window: ObservationWindow | None
    daily_sales: list[DailySales]
    product_sales: list[ProductSales]
    top_product: ProductSales | None
    bottom_product: ProductSales | None
    hourly_sales: list[HourlySales]
    insights: list[TransactionInsight]
    limitations: list[str]


class DashboardAnalysis(BaseModel):
    id: UUID
    nama: str
    area: str
    skor: int
    interpretasi: str
    rule_version: str
    dibuat: datetime


class DashboardPlan(BaseModel):
    total: int = 0
    selesai: int = 0
    berikutnya: list[str] = Field(default_factory=list)


class DashboardToday(BaseModel):
    jumlah: int
    pendapatan_idr: int


class DashboardTransactions(BaseModel):
    hari_tercatat: int
    ambang: Literal[7] = 7
    hari_ini: DashboardToday


class DashboardEducation(BaseModel):
    total: int = 0
    selesai: int = 0


class DashboardResponse(BaseModel):
    keadaan: Literal[
        "belum_ada_data",
        "sudah_menganalisis",
        "usaha_berjalan_data_kurang",
        "usaha_berjalan_data_cukup",
        "kasir_belum_mencatat",
        "kasir_sudah_mencatat",
    ]
    analisis_terakhir: DashboardAnalysis | None
    rencana_30_hari: DashboardPlan
    transaksi: DashboardTransactions
    insight_terbaru: TransactionInsight | None
    edukasi: DashboardEducation
    riwayat_analisis: list[DashboardAnalysis]
