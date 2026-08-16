from __future__ import annotations

from .contracts import FinanceInput, FinanceResult


def calculate_finance(inputs: FinanceInput) -> FinanceResult:
    contribution_margin = inputs.selling_price_idr - inputs.variable_cost_idr
    if contribution_margin <= 0:
        return FinanceResult(
            artifact_id="finance-spike-v1",
            contribution_margin_idr=contribution_margin,
            break_even_units=None,
            status="undefined",
            warning="BEP tidak terdefinisi karena margin kontribusi tidak positif.",
            source="deterministic-finance-spike-v1",
        )

    break_even_units = (inputs.fixed_cost_idr + contribution_margin - 1) // contribution_margin
    return FinanceResult(
        artifact_id="finance-spike-v1",
        contribution_margin_idr=contribution_margin,
        break_even_units=break_even_units,
        status="defined",
        warning=None,
        source="deterministic-finance-spike-v1",
    )
