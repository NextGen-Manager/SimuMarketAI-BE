"""Shared analysis input used by golden tests and API tests."""

from __future__ import annotations

from typing import Any

from app.engines.finance import FinanceInput
from app.schemas.analysis import AnalysisInput, VolumeRange

# The worked example from Docs/docs/06-api-contract.md.
GOLDEN_PAYLOAD: dict[str, Any] = {
    "business_type": "food_stall",
    "concept_name": "Rice Bowl Sambal",
    "location": {
        "area_id": "jabodetabek-tebet",
        "area_name": "Tebet, Jakarta Selatan",
        "latitude": -6.2,
        "longitude": 106.8,
        "analysis_radius_m": 1500,
    },
    "pricing": {
        "average_selling_price_idr": 18_000,
        "variable_cost_per_unit_idr": 11_000,
    },
    "operations": {
        "initial_investment_idr": 15_000_000,
        "fixed_cost_month_idr": 5_000_000,
        "operating_days_month": 26,
        "capacity_units_day": 80,
        "volume_units_day": {"min": 25, "base": 40, "max": 55},
    },
    "channels": ["takeaway", "delivery"],
    "value_proposition": "Makan siang cepat dengan pilihan sambal",
}


def golden_input() -> AnalysisInput:
    return AnalysisInput.model_validate(GOLDEN_PAYLOAD)


def finance_input(
    *,
    selling_price_idr: int = 18_000,
    variable_cost_per_unit_idr: int = 11_000,
    fixed_cost_month_idr: int = 5_000_000,
    initial_investment_idr: int = 15_000_000,
    operating_days_month: int = 26,
    capacity_units_day: int = 80,
    volume: tuple[int, int, int] = (25, 40, 55),
) -> FinanceInput:
    return FinanceInput(
        selling_price_idr=selling_price_idr,
        variable_cost_per_unit_idr=variable_cost_per_unit_idr,
        fixed_cost_month_idr=fixed_cost_month_idr,
        initial_investment_idr=initial_investment_idr,
        operating_days_month=operating_days_month,
        capacity_units_day=capacity_units_day,
        volume_units_day=VolumeRange(min=volume[0], base=volume[1], max=volume[2]),
    )
