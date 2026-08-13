from oasis_spike.contracts import FinanceInput
from oasis_spike.finance import calculate_finance


def test_finance_is_deterministic_and_uses_integer_rupiah() -> None:
    result = calculate_finance(
        FinanceInput(
            fixed_cost_idr=24_000_000,
            selling_price_idr=18_000,
            variable_cost_idr=9_000,
        )
    )

    assert result.contribution_margin_idr == 9_000
    assert result.break_even_units == 2_667
    assert result.status == "defined"


def test_non_positive_margin_has_no_break_even_value() -> None:
    result = calculate_finance(
        FinanceInput(
            fixed_cost_idr=24_000_000,
            selling_price_idr=9_000,
            variable_cost_idr=9_000,
        )
    )

    assert result.break_even_units is None
    assert result.status == "undefined"
    assert result.warning is not None
