"""Golden tests for the deterministic finance engine.

Expected values are worked out by hand from the formulas in
`Docs/docs/05-data-evidence-and-scoring.md`. If a number here changes, either
the formula changed (which needs a document update) or the engine has a bug.
"""

import pytest

from app.engines.finance import (
    InvalidFinanceInputError,
    calculate_finance,
    finance_input_from_analysis,
)
from app.schemas.analysis import FinanceResult, VolumeRange
from tests.support.analysis_payload import finance_input, golden_input


def warning_codes(result: FinanceResult) -> set[str]:
    return {warning.code for warning in result.warnings}


def test_golden_case_matches_hand_calculated_values() -> None:
    result = calculate_finance(finance_input())

    assert result.currency == "IDR"
    assert result.rule_version == "finance-v1"
    assert result.contribution_margin_per_unit_idr == 7_000
    assert result.contribution_margin_ratio_bps == 3_889
    assert result.bep_units_month == 715
    assert result.bep_units_day == 28
    assert result.bep_revenue_month_idr == 12_857_143
    assert result.runway_months == 3

    conservative = result.scenario("conservative")
    base = result.scenario("base")
    optimistic = result.scenario("optimistic")
    assert conservative is not None and base is not None and optimistic is not None

    assert conservative.monthly_units == 650
    assert conservative.monthly_revenue_idr == 11_700_000
    assert conservative.monthly_operating_profit_idr == -450_000
    assert conservative.payback_months is None

    assert base.monthly_units == 1_040
    assert base.monthly_revenue_idr == 18_720_000
    assert base.monthly_operating_profit_idr == 2_280_000
    assert base.payback_months == 7

    assert optimistic.monthly_operating_profit_idr == 5_010_000
    assert optimistic.payback_months == 3

    assert "operating_profit_not_positive" in warning_codes(result)


def test_result_is_identical_across_repeated_runs() -> None:
    first = calculate_finance(finance_input())
    second = calculate_finance(finance_input())
    assert first.model_dump() == second.model_dump()


def test_analysis_input_maps_to_the_same_finance_input() -> None:
    from_schema = calculate_finance(finance_input_from_analysis(golden_input()))
    assert from_schema.model_dump() == calculate_finance(finance_input()).model_dump()


def test_positive_margin_defines_bep_and_payback() -> None:
    result = calculate_finance(
        finance_input(selling_price_idr=20_000, variable_cost_per_unit_idr=10_000)
    )
    assert result.contribution_margin_per_unit_idr == 10_000
    assert result.bep_units_month == 500
    assert result.bep_revenue_month_idr == 10_000_000
    base = result.scenario("base")
    assert base is not None
    assert base.payback_months is not None


def test_zero_margin_leaves_bep_and_payback_undefined() -> None:
    result = calculate_finance(
        finance_input(selling_price_idr=15_000, variable_cost_per_unit_idr=15_000)
    )

    assert result.contribution_margin_per_unit_idr == 0
    assert result.contribution_margin_ratio_bps == 0
    assert result.bep_units_month is None
    assert result.bep_units_day is None
    assert result.bep_revenue_month_idr is None
    assert all(scenario.payback_months is None for scenario in result.scenarios)
    assert "contribution_margin_zero" in warning_codes(result)


def test_negative_margin_leaves_bep_and_payback_undefined() -> None:
    result = calculate_finance(
        finance_input(selling_price_idr=10_000, variable_cost_per_unit_idr=12_000)
    )

    assert result.contribution_margin_per_unit_idr == -2_000
    assert result.contribution_margin_ratio_bps == -2_000
    assert result.bep_units_month is None
    assert result.bep_revenue_month_idr is None
    assert all(scenario.payback_months is None for scenario in result.scenarios)
    assert "contribution_margin_negative" in warning_codes(result)


def test_negative_operating_profit_leaves_payback_undefined() -> None:
    result = calculate_finance(finance_input(fixed_cost_month_idr=40_000_000))

    assert all(scenario.monthly_operating_profit_idr < 0 for scenario in result.scenarios)
    assert all(scenario.payback_months is None for scenario in result.scenarios)
    assert "operating_profit_not_positive" in warning_codes(result)


def test_zero_capital_pays_back_immediately_when_profit_is_positive() -> None:
    result = calculate_finance(finance_input(initial_investment_idr=0))
    base = result.scenario("base")
    assert base is not None
    assert base.monthly_operating_profit_idr > 0
    assert base.payback_months == 0
    assert result.runway_months == 0


def test_zero_volume_produces_no_revenue_and_no_payback() -> None:
    result = calculate_finance(finance_input(volume=(0, 0, 0)))

    assert all(scenario.monthly_revenue_idr == 0 for scenario in result.scenarios)
    assert all(scenario.monthly_operating_profit_idr == -5_000_000 for scenario in result.scenarios)
    assert all(scenario.payback_months is None for scenario in result.scenarios)
    assert "volume_zero" in warning_codes(result)


def test_zero_selling_price_leaves_the_ratio_undefined() -> None:
    result = calculate_finance(finance_input(selling_price_idr=0, variable_cost_per_unit_idr=0))
    assert result.contribution_margin_ratio_bps is None
    assert "selling_price_zero" in warning_codes(result)


def test_bep_above_capacity_is_warned() -> None:
    result = calculate_finance(finance_input(capacity_units_day=10))
    assert "bep_volume_above_capacity" in warning_codes(result)
    assert "volume_exceeds_capacity" in warning_codes(result)


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"selling_price_idr": -1}, "pricing.average_selling_price_idr"),
        ({"variable_cost_per_unit_idr": -1}, "pricing.variable_cost_per_unit_idr"),
        ({"fixed_cost_month_idr": -1}, "operations.fixed_cost_month_idr"),
        ({"initial_investment_idr": -1}, "operations.initial_investment_idr"),
        ({"capacity_units_day": -1}, "operations.capacity_units_day"),
        ({"operating_days_month": 0}, "operations.operating_days_month"),
        ({"operating_days_month": 32}, "operations.operating_days_month"),
    ],
)
def test_invalid_input_is_rejected_instead_of_calculated(
    kwargs: dict[str, int], field: str
) -> None:
    with pytest.raises(InvalidFinanceInputError) as caught:
        calculate_finance(finance_input(**kwargs))
    assert field in caught.value.fields


def test_volume_range_rejects_unordered_bounds() -> None:
    with pytest.raises(ValueError):
        VolumeRange(min=50, base=10, max=20)


def test_no_money_field_is_a_float() -> None:
    result = calculate_finance(finance_input())
    payload = result.model_dump()
    money = {key: value for key, value in payload.items() if key.endswith("_idr")}
    assert money
    assert all(isinstance(value, int) for value in money.values())
    for scenario in payload["scenarios"]:
        for key, value in scenario.items():
            if key.endswith("_idr"):
                assert isinstance(value, int)
                assert not isinstance(value, bool)
