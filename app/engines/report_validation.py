"""Arithmetic and provenance checks run before a report is stored.

Docs/02 puts a validation stage between composing and completing a run. Even
without agent prose there is something real to check: that the numbers in the
report reconcile with the inputs they claim to come from, and that every
recommendation and risk names its source.

With agents wired in there is a second job. Any number written by an agent must
already exist in the evidence snapshot, the finance result, the score result,
the input snapshot, or the deterministic simulation counts. A figure that
appears nowhere else was invented, and docs/07 treats an invented number in a
report as a defect even when it happens to look reasonable.
"""

from __future__ import annotations

import re

from app.schemas.analysis import AnalysisReport

SCENARIO_VOLUMES = ("conservative", "base", "optimistic")

# Digit groups, with Indonesian thousand separators and decimal commas folded in
# so "Rp 18.000" and "18000" compare as the same value.
_NUMBER = re.compile(r"\d[\d.,]*")


def _numbers_in(text: str) -> set[int]:
    found: set[int] = set()
    for match in _NUMBER.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if digits:
            found.add(int(digits))
    return found


def _allowed_numbers(report: AnalysisReport) -> set[int]:
    """Every number an agent is permitted to repeat.

    Built from values a deterministic engine produced or the user supplied. The
    set is deliberately generous — repeating a legitimate figure must never
    fail — because the check exists to catch figures with no origin at all.
    """
    allowed: set[int] = set()

    def collect(node: object) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, int):
            allowed.add(abs(node))
        elif isinstance(node, dict):
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(report.input_snapshot.model_dump(mode="python"))
    collect(report.finance.model_dump(mode="python"))
    collect(report.readiness.model_dump(mode="python"))
    collect(report.market.model_dump(mode="python"))
    collect(report.synthetic_simulation.model_dump(mode="python"))
    for record in report.evidence:
        allowed.add(abs(record.value))
        if record.geography.meters is not None:
            allowed.add(record.geography.meters)
    if report.agent_review.manifest is not None:
        collect(report.agent_review.manifest.model_dump(mode="python"))
    return allowed


def _agent_authored_text(report: AnalysisReport) -> list[tuple[str, str]]:
    review = report.agent_review
    entries: list[tuple[str, str]] = []
    for section in review.narrative_sections:
        entries.append((f"narrative:{section.id}", section.body))
        entries.append((f"narrative_title:{section.id}", section.title))
    for index, finding in enumerate(review.red_team_findings):
        entries.append((f"red_team:{index}", finding))
    for observation in review.market_observations:
        entries.append((f"observation:{observation.id}", observation.claim))
    for critique in review.finance_critiques:
        entries.append((f"critique:{critique.id}", f"{critique.assumption} {critique.concern}"))
    for index, assumption in enumerate(review.fragile_assumptions):
        entries.append((f"fragile_assumption:{index}", assumption))
    for quote in report.synthetic_simulation.quotes:
        entries.append((f"quote:{quote.agent_id}", quote.text))
    return entries


def validate_agent_numbers(report: AnalysisReport) -> list[str]:
    """Reject agent text that introduces a number nothing else produced."""
    allowed = _allowed_numbers(report)
    violations: list[str] = []
    for label, text in _agent_authored_text(report):
        for value in sorted(_numbers_in(text) - allowed):
            violations.append(f"unsourced_number_in_narrative:{label}:{value}")
    return violations


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

    violations.extend(_validate_simulation(report))
    violations.extend(validate_agent_numbers(report))
    return violations


def _validate_simulation(report: AnalysisReport) -> list[str]:
    """Simulation counts must stay internally consistent and clearly labelled."""
    violations: list[str] = []
    simulation = report.synthetic_simulation
    review = report.agent_review

    if simulation.status == "unavailable":
        if simulation.quotes or simulation.metrics:
            violations.append("simulation_unavailable_with_data")
        if simulation.reason is None:
            violations.append("simulation_unavailable_without_reason")
        return violations

    if simulation.cohort_size is None:
        violations.append("simulation_without_cohort_size")
        return violations

    activated = simulation.metrics.get("activated_persona_count", 0)
    purchase = simulation.metrics.get("purchase_intent_count", 0)
    positive = simulation.metrics.get("positive_reaction_count", 0)
    if activated > simulation.cohort_size:
        violations.append("simulation_activated_above_cohort")
    if purchase > activated or positive > activated:
        violations.append("simulation_counts_above_activated")
    if review.manifest is None:
        violations.append("simulation_without_manifest")

    for observation in review.market_observations:
        if observation.stance != "uncertainty" and not observation.evidence_metrics:
            violations.append(f"agent_observation_without_evidence:{observation.id}")
    for critique in review.finance_critiques:
        if not critique.tool_call_ids:
            violations.append(f"agent_critique_without_tool_call:{critique.id}")
    for section in review.narrative_sections:
        if not section.source_artifact_types:
            violations.append(f"narrative_section_without_source:{section.id}")
    return violations
