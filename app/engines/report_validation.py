"""Arithmetic and provenance checks run before a report is stored.

Docs/02 puts a validation stage between composing and completing a run. Even
without agent prose there is something real to check: that the numbers in the
report reconcile with the inputs they claim to come from, and that every
recommendation and risk names its source.
"""

from __future__ import annotations

from app.schemas.analysis import AnalysisReport

SCENARIO_VOLUMES = ("conservative", "base", "optimistic")


def validate_report(report: AnalysisReport) -> list[str]:
    violations: list[str] = []
    finance = report.finance
    operations = report.input_snapshot.operations
    price = report.input_snapshot.pricing.average_selling_price_idr
    margin = finance.contribution_margin_per_unit_idr

    expected_margin = price - report.input_snapshot.pricing.variable_cost_per_unit_idr
    if margin != expected_margin:
        violations.append("contribution_margin_mismatch")

    if {scenario.name for scenario in finance.scenarios} != set(SCENARIO_VOLUMES):
        violations.append("scenario_set_incomplete")

    for scenario in finance.scenarios:
        expected_units = scenario.volume_units_day * operations.operating_days_month
        if scenario.monthly_units != expected_units:
            violations.append(f"monthly_units_mismatch:{scenario.name}")
        if scenario.monthly_revenue_idr != scenario.monthly_units * price:
            violations.append(f"monthly_revenue_mismatch:{scenario.name}")
        expected_profit = scenario.monthly_units * margin - operations.fixed_cost_month_idr
        if scenario.monthly_operating_profit_idr != expected_profit:
            violations.append(f"operating_profit_mismatch:{scenario.name}")
        if scenario.monthly_operating_profit_idr <= 0 and scenario.payback_months is not None:
            violations.append(f"payback_defined_without_profit:{scenario.name}")

    if margin <= 0 and (
        finance.bep_units_month is not None or finance.bep_revenue_month_idr is not None
    ):
        violations.append("bep_defined_without_margin")

    readiness = report.readiness
    if readiness.status == "unavailable" and readiness.score is not None:
        violations.append("score_present_while_unavailable")
    if readiness.status == "available" and readiness.score is None:
        violations.append("score_missing_while_available")
    if readiness.status == "available" and readiness.missing_dimensions:
        violations.append("score_available_with_missing_dimensions")
    for dimension in readiness.dimensions:
        if dimension.status == "not_scorable" and dimension.score is not None:
            violations.append(f"dimension_default_score:{dimension.key}")

    for recommendation in report.recommendations:
        if not recommendation.source:
            violations.append(f"recommendation_without_source:{recommendation.id}")
    for risk in report.risks:
        if not risk.source:
            violations.append(f"risk_without_source:{risk.id}")

    reported_metrics = {
        "competitor_count": report.market.competitor_count,
        "population_count": report.market.population_count,
        "comparable_price_median_idr": report.market.comparable_price_median_idr,
        "comparable_price_sample_size": report.market.comparable_price_sample_size,
    }
    evidence_values = {record.metric: record.value for record in report.evidence}
    for metric, value in reported_metrics.items():
        if value is not None and evidence_values.get(metric) != value:
            violations.append(f"market_value_without_evidence:{metric}")

    return violations
