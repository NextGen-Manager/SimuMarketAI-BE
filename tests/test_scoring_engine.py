"""Golden tests for the Launch Readiness Score engine.

Expected values follow the weights in `Docs/docs/05-data-evidence-and-scoring.md`
(20/25/15/40) and the rule thresholds in `app/engines/scoring.py`. The most
important assertions here are the negative ones: a dimension without evidence
gets no default, and the total is withheld rather than reweighted.
"""

from __future__ import annotations

import pytest

from app.domain.evidence import (
    METRIC_COMPARABLE_PRICE_MEDIAN,
    METRIC_COMPARABLE_PRICE_SAMPLE_SIZE,
    METRIC_COMPETITOR_COUNT,
    METRIC_POPULATION_COUNT,
    REQUIRED_EVIDENCE_METRICS,
    EvidenceRequest,
    EvidenceSnapshot,
    Geography,
)
from app.engines.finance import calculate_finance
from app.engines.scoring import DIMENSION_WEIGHTS, ScoringInput, calculate_score, interpret
from app.integrations.evidence.unavailable import UnavailableEvidenceProvider
from app.schemas.analysis import ScoreResult
from tests.support.analysis_payload import finance_input
from tests.support.evidence import COMPLETE_FIXTURE_VALUES, FixtureEvidenceProvider

RADIUS = 1_500
GEOGRAPHY = Geography(
    type="radius", area_id="jabodetabek-tebet", center_id="jabodetabek-tebet", meters=RADIUS
)
REQUESTS = [
    EvidenceRequest(metric=metric, geography=GEOGRAPHY) for metric in REQUIRED_EVIDENCE_METRICS
]


async def snapshot_from(values: dict[str, int], **kwargs: object) -> EvidenceSnapshot:
    provider = FixtureEvidenceProvider(values, **kwargs)  # type: ignore[arg-type]
    return await provider.collect(REQUESTS)


def scoring_input(evidence: EvidenceSnapshot, **finance_kwargs: object) -> ScoringInput:
    return ScoringInput(
        planned_price_idr=18_000,
        analysis_radius_m=RADIUS,
        capacity_units_day=80,
        base_volume_units_day=40,
        fixed_cost_month_idr=5_000_000,
        finance=calculate_finance(finance_input(**finance_kwargs)),  # type: ignore[arg-type]
        evidence=evidence,
    )


def dimension(result: ScoreResult, key: str) -> object:
    return next(item for item in result.dimensions if item.key == key)


def test_weights_match_the_documented_rule_set() -> None:
    assert DIMENSION_WEIGHTS == {
        "market_saturation": 20,
        "demand_potential": 25,
        "price_positioning": 15,
        "operational_readiness": 40,
    }
    assert sum(DIMENSION_WEIGHTS.values()) == 100


async def test_complete_evidence_produces_the_hand_calculated_total() -> None:
    evidence = await snapshot_from(COMPLETE_FIXTURE_VALUES)
    result = calculate_score(scoring_input(evidence))

    scores = {item.key: item.score for item in result.dimensions}
    assert scores["market_saturation"] == 75
    assert scores["demand_potential"] == 55
    assert scores["price_positioning"] == 80
    assert scores["operational_readiness"] == 92

    # (20*75 + 25*55 + 15*80 + 40*92) / 100 = 7755 / 100 = 77.55 -> 78
    assert result.status == "available"
    assert result.score == 78
    assert result.interpretation == "layak_dengan_mitigasi"
    assert result.interpretation_label == "Layak dengan mitigasi"
    assert result.missing_dimensions == []


async def test_rule_version_and_unvalidated_label_are_always_present() -> None:
    evidence = await snapshot_from(COMPLETE_FIXTURE_VALUES)
    result = calculate_score(scoring_input(evidence))

    assert result.rule_version == "lrs-v0.2-unvalidated"
    assert result.validation_status == "unvalidated"
    assert "unvalidated" in result.rule_version

    empty = calculate_score(scoring_input(await UnavailableEvidenceProvider().collect(REQUESTS)))
    assert empty.rule_version == "lrs-v0.2-unvalidated"
    assert empty.validation_status == "unvalidated"


async def test_scoring_is_deterministic() -> None:
    evidence = await snapshot_from(COMPLETE_FIXTURE_VALUES)
    first = calculate_score(scoring_input(evidence))
    second = calculate_score(scoring_input(evidence))
    assert first.model_dump() == second.model_dump()


async def test_missing_evidence_yields_no_default_score_and_no_reweight() -> None:
    evidence = await UnavailableEvidenceProvider().collect(REQUESTS)
    result = calculate_score(scoring_input(evidence))

    assert result.status == "unavailable"
    assert result.score is None
    assert result.interpretation is None
    assert set(result.missing_dimensions) == {
        "market_saturation",
        "demand_potential",
        "price_positioning",
    }

    by_key = {item.key: item for item in result.dimensions}
    for key in result.missing_dimensions:
        assert by_key[key].status == "not_scorable"
        assert by_key[key].score is None
    # Operational readiness is still measurable from validated input, and its
    # weight is not inflated to compensate for the missing dimensions.
    assert by_key["operational_readiness"].status == "scored"
    assert by_key["operational_readiness"].weight_percent == 40


async def test_partial_evidence_still_withholds_the_total() -> None:
    evidence = await snapshot_from({METRIC_COMPETITOR_COUNT: 18})
    result = calculate_score(scoring_input(evidence))

    by_key = {item.key: item for item in result.dimensions}
    assert by_key["market_saturation"].status == "scored"
    assert result.status == "unavailable"
    assert result.score is None


async def test_price_sample_below_minimum_is_not_scorable() -> None:
    values = dict(COMPLETE_FIXTURE_VALUES)
    values[METRIC_COMPARABLE_PRICE_SAMPLE_SIZE] = 3
    result = calculate_score(scoring_input(await snapshot_from(values)))

    by_key = {item.key: item for item in result.dimensions}
    assert by_key["price_positioning"].status == "not_scorable"
    assert by_key["price_positioning"].score is None
    assert METRIC_COMPARABLE_PRICE_SAMPLE_SIZE in by_key["price_positioning"].missing_inputs


async def test_geography_mismatch_is_not_scorable() -> None:
    evidence = await snapshot_from(COMPLETE_FIXTURE_VALUES, radius_override=3_000)
    result = calculate_score(scoring_input(evidence))

    by_key = {item.key: item for item in result.dimensions}
    assert by_key["market_saturation"].status == "not_scorable"
    assert by_key["demand_potential"].status == "not_scorable"


async def test_non_positive_margin_caps_price_positioning() -> None:
    evidence = await snapshot_from(COMPLETE_FIXTURE_VALUES)
    result = calculate_score(
        scoring_input(evidence, variable_cost_per_unit_idr=18_000),
    )

    by_key = {item.key: item for item in result.dimensions}
    assert by_key["price_positioning"].score == 20
    assert "PP-006" in by_key["price_positioning"].applied_rules
    assert by_key["operational_readiness"].score == 10 + 0 + 12 + 10


async def test_zero_population_lowers_demand_potential_without_failing() -> None:
    values = dict(COMPLETE_FIXTURE_VALUES)
    values[METRIC_POPULATION_COUNT] = 100
    result = calculate_score(scoring_input(await snapshot_from(values)))

    by_key = {item.key: item for item in result.dimensions}
    assert by_key["demand_potential"].score == 20
    assert by_key["demand_potential"].applied_rules == ["DP-005"]


async def test_zero_median_price_is_not_scorable() -> None:
    values = dict(COMPLETE_FIXTURE_VALUES)
    values[METRIC_COMPARABLE_PRICE_MEDIAN] = 0
    result = calculate_score(scoring_input(await snapshot_from(values)))

    by_key = {item.key: item for item in result.dimensions}
    assert by_key["price_positioning"].status == "not_scorable"


@pytest.mark.parametrize(
    ("score", "code"),
    [
        (100, "sangat_layak"),
        (80, "sangat_layak"),
        (79, "layak_dengan_mitigasi"),
        (65, "layak_dengan_mitigasi"),
        (64, "perlu_evaluasi_ulang"),
        (50, "perlu_evaluasi_ulang"),
        (49, "tidak_disarankan"),
        (0, "tidak_disarankan"),
    ],
)
def test_interpretation_bands(score: int, code: str) -> None:
    assert interpret(score)[0] == code
