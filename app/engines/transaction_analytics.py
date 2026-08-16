from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from app.schemas.operations import (
    DailySales,
    HourlySales,
    ObservationWindow,
    ProductSales,
    TransactionAnalytics,
    TransactionInsight,
)

JAKARTA = ZoneInfo("Asia/Jakarta")
ANALYTICS_THRESHOLD_DAYS = 7


@dataclass(frozen=True, slots=True)
class AnalyticsProduct:
    id: UUID
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class AnalyticsLine:
    product_id: UUID
    occurred_at: datetime
    transaction_id: UUID
    quantity: int
    line_total_idr: int


def to_jakarta_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=JAKARTA)
    return value.astimezone(JAKARTA)


def build_transaction_analytics(
    *,
    business_id: UUID,
    products: list[AnalyticsProduct],
    lines: list[AnalyticsLine],
) -> TransactionAnalytics:
    recorded_dates = sorted({to_jakarta_datetime(line.occurred_at).date() for line in lines})
    days_recorded = len(recorded_dates)
    if days_recorded < ANALYTICS_THRESHOLD_DAYS:
        return TransactionAnalytics(
            status="collecting",
            business_id=business_id,
            days_recorded=days_recorded,
            observation_window=None,
            daily_sales=[],
            product_sales=[],
            top_product=None,
            bottom_product=None,
            hourly_sales=[],
            insights=[],
            limitations=["Analitik tersedia setelah transaksi tercatat pada tujuh hari berbeda."],
        )

    window_end = recorded_dates[-1]
    window_start = window_end - timedelta(days=6)
    window = ObservationWindow(start=window_start, end=window_end)
    window_lines = [
        line
        for line in lines
        if window_start <= to_jakarta_datetime(line.occurred_at).date() <= window_end
    ]
    product_by_id = {product.id: product for product in products}

    daily: dict[date, tuple[set[UUID], int]] = {}
    hourly: dict[int, tuple[set[UUID], int]] = {}
    product_totals: dict[UUID, tuple[int, int]] = {}
    for line in window_lines:
        local = to_jakarta_datetime(line.occurred_at)
        day_transactions, day_revenue = daily.setdefault(local.date(), (set(), 0))
        day_transactions.add(line.transaction_id)
        daily[local.date()] = (day_transactions, day_revenue + line.line_total_idr)

        hour_transactions, hour_revenue = hourly.setdefault(local.hour, (set(), 0))
        hour_transactions.add(line.transaction_id)
        hourly[local.hour] = (hour_transactions, hour_revenue + line.line_total_idr)

        quantity, revenue = product_totals.get(line.product_id, (0, 0))
        product_totals[line.product_id] = (
            quantity + line.quantity,
            revenue + line.line_total_idr,
        )

    daily_sales = [
        DailySales(date=day, transaction_count=len(transactions), revenue_idr=revenue)
        for day, (transactions, revenue) in sorted(daily.items())
    ]
    hourly_sales = [
        HourlySales(hour=hour, transaction_count=len(transactions), revenue_idr=revenue)
        for hour, (transactions, revenue) in sorted(hourly.items())
    ]

    product_sales: list[ProductSales] = []
    for product_id, (quantity, revenue) in product_totals.items():
        product = product_by_id.get(product_id)
        if product is None:
            continue
        created_date = to_jakarta_datetime(product.created_at).date()
        exposure_start = max(created_date, window_start)
        exposure_days = max((window_end - exposure_start).days + 1, 0)
        product_sales.append(
            ProductSales(
                product_id=product.id,
                product_name=product.name,
                quantity=quantity,
                revenue_idr=revenue,
                exposure_days=exposure_days,
            )
        )

    eligible = [
        product for product in product_sales if product.exposure_days >= ANALYTICS_THRESHOLD_DAYS
    ]
    top_product = max(eligible, key=lambda item: (item.quantity, item.product_name), default=None)
    bottom_product = min(
        eligible, key=lambda item: (item.quantity, item.product_name), default=None
    )
    product_sales.sort(key=lambda item: (-item.quantity, item.product_name))

    insights: list[TransactionInsight] = []
    total_revenue = sum(item.revenue_idr for item in product_sales)
    if top_product is not None and total_revenue > 0:
        share_percent = (top_product.revenue_idr * 100 + total_revenue // 2) // total_revenue
        insights.append(
            TransactionInsight(
                message=(
                    f"{top_product.product_name} menyumbang {share_percent}% pendapatan "
                    "pada periode pengamatan. Evaluasi ketergantungan produk sebelum "
                    "mengubah menu atau promosi."
                ),
                observation_window=window,
            )
        )

    return TransactionAnalytics(
        status="available",
        business_id=business_id,
        days_recorded=days_recorded,
        observation_window=window,
        daily_sales=daily_sales,
        product_sales=product_sales,
        top_product=top_product,
        bottom_product=bottom_product,
        hourly_sales=hourly_sales,
        insights=insights,
        limitations=[
            "Exposure produk dihitung dari usia katalog; ketersediaan stok harian belum tercatat."
        ],
    )
