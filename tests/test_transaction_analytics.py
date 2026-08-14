from datetime import UTC, datetime
from uuid import UUID

from app.engines.transaction_analytics import (
    AnalyticsLine,
    AnalyticsProduct,
    build_transaction_analytics,
)

BUSINESS_ID = UUID("00000000-0000-0000-0000-000000000001")
COFFEE_ID = UUID("00000000-0000-0000-0000-000000000010")
TEA_ID = UUID("00000000-0000-0000-0000-000000000020")


def _line(*, product_id: UUID, day: int, quantity: int, line_total_idr: int) -> AnalyticsLine:
    return AnalyticsLine(
        product_id=product_id,
        occurred_at=datetime(2026, 8, day, 5, tzinfo=UTC),
        transaction_id=UUID(f"00000000-0000-0000-0000-{day:012d}"),
        quantity=quantity,
        line_total_idr=line_total_idr,
    )


def test_analytics_gate_returns_progress_before_seven_days() -> None:
    result = build_transaction_analytics(
        business_id=BUSINESS_ID,
        products=[],
        lines=[
            _line(product_id=COFFEE_ID, day=day, quantity=1, line_total_idr=18_000)
            for day in range(1, 7)
        ],
    )

    assert result.status == "collecting"
    assert result.days_recorded == 6
    assert result.threshold_days == 7
    assert result.daily_sales == []


def test_golden_analytics_is_reproducible() -> None:
    products = [
        AnalyticsProduct(
            id=COFFEE_ID,
            name="Kopi Susu",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        ),
        AnalyticsProduct(
            id=TEA_ID,
            name="Teh Lemon",
            created_at=datetime(2026, 7, 1, tzinfo=UTC),
        ),
    ]
    lines = [
        _line(product_id=COFFEE_ID, day=day, quantity=2, line_total_idr=36_000)
        for day in range(1, 8)
    ] + [
        _line(product_id=TEA_ID, day=day, quantity=1, line_total_idr=12_000) for day in range(1, 8)
    ]

    first = build_transaction_analytics(
        business_id=BUSINESS_ID,
        products=products,
        lines=lines,
    )
    second = build_transaction_analytics(
        business_id=BUSINESS_ID,
        products=products,
        lines=lines,
    )

    assert first == second
    assert first.status == "available"
    assert [item.revenue_idr for item in first.daily_sales] == [48_000] * 7
    assert first.top_product is not None
    assert first.top_product.product_name == "Kopi Susu"
    assert first.top_product.quantity == 14
    assert first.bottom_product is not None
    assert first.bottom_product.product_name == "Teh Lemon"
    assert first.hourly_sales[0].hour == 12
    assert first.hourly_sales[0].revenue_idr == 336_000
    assert "75%" in first.insights[0].message
