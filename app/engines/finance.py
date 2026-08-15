"""Deterministic finance engine.

Formulas are transcribed from `Docs/docs/05-data-evidence-and-scoring.md` and
must not be rewritten from memory. Every value on the money path is an integer
rupiah; `Decimal` appears only where an exact ratio is needed and is quantised
back to an integer before it leaves.

Undefined stays undefined. When contribution margin or operating profit makes a
division impossible the result is `None` plus a warning, never zero, infinity,
or NaN.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.schemas.analysis import (
    AnalysisInput,
    FinanceResult,
    FinanceScenario,
    FinanceWarning,
    ScenarioName,
    VolumeRange,
)

SCENARIO_LABELS: dict[ScenarioName, str] = {
    "conservative": "Konservatif",
    "base": "Dasar",
    "optimistic": "Optimistis",
}

ASSUMPTIONS_INCLUDED: tuple[str, ...] = (
    "Biaya variabel per unit (HPP) sesuai input pengguna",
    "Biaya tetap bulanan sesuai input pengguna",
    "Modal awal sesuai input pengguna",
    "Harga jual rata-rata sama pada seluruh skenario",
)

ASSUMPTIONS_EXCLUDED: tuple[str, ...] = (
    "Pajak",
    "Depresiasi",
    "Gaji pemilik",
    "Biaya pembiayaan dan bunga",
    "Susut bahan (spoilage)",
    "Biaya platform pesan-antar",
    "Biaya promo dan diskon",
)

ROUNDING_NOTE = (
    "BEP unit, BEP pendapatan, dan payback dibulatkan ke atas ke satuan penuh "
    "agar tidak menyatakan target yang lebih ringan dari kenyataan."
)


class InvalidFinanceInputError(ValueError):
    """Raised when finance input cannot be used for a calculation at all."""

    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("; ".join(f"{path}: {reason}" for path, reason in fields.items()))
        self.fields = fields


@dataclass(frozen=True, slots=True)
class FinanceInput:
    selling_price_idr: int
    variable_cost_per_unit_idr: int
    fixed_cost_month_idr: int
    initial_investment_idr: int
    operating_days_month: int
    capacity_units_day: int
    volume_units_day: VolumeRange


def _ceil_div(numerator: int, denominator: int) -> int:
    """Exact integer ceiling division. `denominator` must be positive."""
    return -(-numerator // denominator)


def _validate(value: FinanceInput) -> None:
    fields: dict[str, str] = {}
    if value.selling_price_idr < 0:
        fields["pricing.average_selling_price_idr"] = "tidak boleh negatif"
    if value.variable_cost_per_unit_idr < 0:
        fields["pricing.variable_cost_per_unit_idr"] = "tidak boleh negatif"
    if value.fixed_cost_month_idr < 0:
        fields["operations.fixed_cost_month_idr"] = "tidak boleh negatif"
    if value.initial_investment_idr < 0:
        fields["operations.initial_investment_idr"] = "tidak boleh negatif"
    if value.capacity_units_day < 0:
        fields["operations.capacity_units_day"] = "tidak boleh negatif"
    if not 1 <= value.operating_days_month <= 31:
        fields["operations.operating_days_month"] = "harus antara 1 dan 31"
    volume = value.volume_units_day
    if volume.min < 0 or volume.base < 0 or volume.max < 0:
        fields["operations.volume_units_day"] = "tidak boleh negatif"
    elif not volume.min <= volume.base <= volume.max:
        fields["operations.volume_units_day"] = "harus memenuhi min <= base <= max"
    if fields:
        raise InvalidFinanceInputError(fields)


def _contribution_margin_ratio_bps(margin: int, selling_price: int) -> int | None:
    if selling_price <= 0:
        return None
    exact = Decimal(margin * 10_000) / Decimal(selling_price)
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def calculate_finance(value: FinanceInput) -> FinanceResult:
    _validate(value)

    warnings: list[FinanceWarning] = []
    margin = value.selling_price_idr - value.variable_cost_per_unit_idr
    ratio_bps = _contribution_margin_ratio_bps(margin, value.selling_price_idr)

    if value.selling_price_idr == 0:
        warnings.append(
            FinanceWarning(
                code="selling_price_zero",
                message=("Harga jual nol sehingga rasio marjin kontribusi tidak terdefinisi."),
            )
        )

    bep_units_month: int | None = None
    bep_units_day: int | None = None
    bep_revenue_month_idr: int | None = None
    if margin > 0:
        bep_units_month = _ceil_div(value.fixed_cost_month_idr, margin)
        bep_units_day = _ceil_div(bep_units_month, value.operating_days_month)
        bep_revenue_month_idr = _ceil_div(
            value.fixed_cost_month_idr * value.selling_price_idr, margin
        )
    elif margin == 0:
        warnings.append(
            FinanceWarning(
                code="contribution_margin_zero",
                message=(
                    "Marjin kontribusi nol sehingga biaya tetap tidak pernah tertutup. "
                    "BEP dan payback tidak terdefinisi."
                ),
            )
        )
    else:
        warnings.append(
            FinanceWarning(
                code="contribution_margin_negative",
                message=(
                    "Marjin kontribusi negatif sehingga setiap unit terjual menambah "
                    "kerugian. BEP dan payback tidak terdefinisi."
                ),
            )
        )

    if bep_units_day is not None and bep_units_day > value.capacity_units_day:
        warnings.append(
            FinanceWarning(
                code="bep_volume_above_capacity",
                message=(
                    "Volume BEP harian melampaui kapasitas yang direncanakan sehingga "
                    "BEP tidak tercapai pada kapasitas tersebut."
                ),
            )
        )

    volumes: dict[ScenarioName, int] = {
        "conservative": value.volume_units_day.min,
        "base": value.volume_units_day.base,
        "optimistic": value.volume_units_day.max,
    }
    if volumes["base"] == 0:
        warnings.append(
            FinanceWarning(
                code="volume_zero",
                message="Volume rencana pada skenario dasar nol sehingga tidak ada pendapatan.",
            )
        )

    scenarios: list[FinanceScenario] = []
    for name, volume_day in volumes.items():
        monthly_units = volume_day * value.operating_days_month
        monthly_revenue = monthly_units * value.selling_price_idr
        operating_profit = monthly_units * margin - value.fixed_cost_month_idr

        payback_months: int | None = None
        if operating_profit > 0:
            payback_months = _ceil_div(value.initial_investment_idr, operating_profit)
        else:
            warnings.append(
                FinanceWarning(
                    code="operating_profit_not_positive",
                    scenario=name,
                    message=(
                        f"Laba operasional bulanan skenario {SCENARIO_LABELS[name].lower()} "
                        "tidak positif sehingga payback tidak terdefinisi."
                    ),
                )
            )

        exceeds_capacity = volume_day > value.capacity_units_day
        if exceeds_capacity:
            warnings.append(
                FinanceWarning(
                    code="volume_exceeds_capacity",
                    scenario=name,
                    message=(
                        f"Volume skenario {SCENARIO_LABELS[name].lower()} melampaui "
                        "kapasitas harian yang direncanakan."
                    ),
                )
            )

        scenarios.append(
            FinanceScenario(
                name=name,
                label=SCENARIO_LABELS[name],
                volume_units_day=volume_day,
                monthly_units=monthly_units,
                monthly_revenue_idr=monthly_revenue,
                monthly_operating_profit_idr=operating_profit,
                payback_months=payback_months,
                exceeds_capacity=exceeds_capacity,
            )
        )

    runway_months: int | None = None
    if value.fixed_cost_month_idr > 0:
        runway_months = value.initial_investment_idr // value.fixed_cost_month_idr

    return FinanceResult(
        contribution_margin_per_unit_idr=margin,
        contribution_margin_ratio_bps=ratio_bps,
        bep_units_month=bep_units_month,
        bep_units_day=bep_units_day,
        bep_revenue_month_idr=bep_revenue_month_idr,
        runway_months=runway_months,
        scenarios=scenarios,
        assumptions_included=list(ASSUMPTIONS_INCLUDED),
        assumptions_excluded=list(ASSUMPTIONS_EXCLUDED),
        warnings=warnings,
    )


def finance_input_from_analysis(value: AnalysisInput) -> FinanceInput:
    return FinanceInput(
        selling_price_idr=value.pricing.average_selling_price_idr,
        variable_cost_per_unit_idr=value.pricing.variable_cost_per_unit_idr,
        fixed_cost_month_idr=value.operations.fixed_cost_month_idr,
        initial_investment_idr=value.operations.initial_investment_idr,
        operating_days_month=value.operations.operating_days_month,
        capacity_units_day=value.operations.capacity_units_day,
        volume_units_day=value.operations.volume_units_day,
    )
