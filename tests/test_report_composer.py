"""Deterministic report composition with no LLM anywhere in the process."""

from __future__ import annotations

import ast
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import app.engines as engines_package
from app.domain.evidence import REQUIRED_EVIDENCE_METRICS, EvidenceRequest, Geography
from app.engines.evidence_confidence import calculate_evidence_confidence
from app.engines.finance import calculate_finance, finance_input_from_analysis
from app.engines.report import compose_report
from app.engines.report_validation import validate_report
from app.engines.scoring import ScoringInput, calculate_score
from app.integrations.evidence.unavailable import UnavailableEvidenceProvider
from app.schemas.analysis import DSS_DISCLAIMER, AnalysisInput, AnalysisReport
from tests.support.analysis_payload import golden_input
from tests.support.evidence import COMPLETE_FIXTURE_VALUES, FixtureEvidenceProvider

FORBIDDEN_MODULE_HINTS = ("oasis", "camel", "langchain", "google.generativeai", "openai")


def requests_for(analysis_input: AnalysisInput) -> list[EvidenceRequest]:
    geography = Geography(
        type="radius",
        area_id=analysis_input.location.area_id,
        center_id=analysis_input.location.area_id,
        meters=analysis_input.location.analysis_radius_m,
    )
    return [
        EvidenceRequest(metric=metric, geography=geography) for metric in REQUIRED_EVIDENCE_METRICS
    ]


async def build_report(provider: object, *, status: str) -> AnalysisReport:
    analysis_input = golden_input()
    evidence = await provider.collect(requests_for(analysis_input))  # type: ignore[attr-defined]
    finance = calculate_finance(finance_input_from_analysis(analysis_input))
    score = calculate_score(
        ScoringInput(
            planned_price_idr=analysis_input.pricing.average_selling_price_idr,
            analysis_radius_m=analysis_input.location.analysis_radius_m,
            capacity_units_day=analysis_input.operations.capacity_units_day,
            base_volume_units_day=analysis_input.operations.volume_units_day.base,
            fixed_cost_month_idr=analysis_input.operations.fixed_cost_month_idr,
            finance=finance,
            evidence=evidence,
        )
    )
    return compose_report(
        analysis_id=uuid4(),
        status=status,  # type: ignore[arg-type]
        generated_at=datetime(2026, 8, 15, tzinfo=UTC),
        analysis_input=analysis_input,
        evidence=evidence,
        finance=finance,
        score=score,
        confidence=calculate_evidence_confidence(evidence),
    )


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_engines_never_import_integrations_or_an_llm_client() -> None:
    engines = Path(engines_package.__file__ or "").parent
    offenders: list[str] = []
    for source in sorted(engines.glob("*.py")):
        for name in imported_modules(source):
            lowered = name.lower()
            if lowered.startswith("app.integrations") or any(
                hint in lowered for hint in FORBIDDEN_MODULE_HINTS
            ):
                offenders.append(f"{source.name} -> {name}")

    assert not offenders, f"deterministic engines must stay LLM-free: {offenders}"

    # Nothing pulled an agent framework into the interpreter either.
    loaded = {name.lower() for name in sys.modules}
    assert not any(hint in name for name in loaded for hint in ("oasis", "camel", "langchain"))


async def test_report_without_evidence_is_complete_and_honest() -> None:
    report = await build_report(UnavailableEvidenceProvider(), status="partial")

    assert report.status == "partial"
    assert report.report_version == "report-v1"
    assert report.rule_version == "lrs-v0.2-unvalidated"
    assert report.disclaimer == DSS_DISCLAIMER

    assert report.readiness.status == "unavailable"
    assert report.readiness.score is None
    assert report.evidence_confidence.label == "tidak_tersedia"
    assert report.evidence == []
    assert len(report.missing_evidence) == len(REQUIRED_EVIDENCE_METRICS)

    assert report.market.competitor_count is None
    assert report.market.population_count is None

    assert report.finance.bep_units_month == 715
    assert report.limitations
    assert report.risks
    assert report.recommendations
    assert validate_report(report) == []


async def test_synthetic_simulation_is_unavailable_not_invented() -> None:
    report = await build_report(UnavailableEvidenceProvider(), status="partial")

    assert report.synthetic_simulation.status == "unavailable"
    assert report.synthetic_simulation.reason
    assert "OASIS" in report.synthetic_simulation.reason
    assert report.synthetic_simulation.cohort_size is None
    assert report.synthetic_simulation.metrics == {}
    assert report.synthetic_simulation.limitations

    # There is no field a fabricated persona quote could even be placed in.
    payload = report.model_dump(mode="json")
    assert set(payload["synthetic_simulation"]) == {
        "status",
        "reason",
        "cohort_size",
        "metrics",
        "limitations",
    }


async def test_every_recommendation_and_risk_names_its_source() -> None:
    report = await build_report(UnavailableEvidenceProvider(), status="partial")

    assert all(recommendation.source for recommendation in report.recommendations)
    assert all(risk.source for risk in report.risks)
    assert any(
        recommendation.source.startswith("evidence.missing.")
        for recommendation in report.recommendations
    )


async def test_complete_evidence_produces_a_scored_report() -> None:
    report = await build_report(
        FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES), status="completed"
    )

    assert report.status == "completed"
    assert report.readiness.status == "available"
    assert report.readiness.score == 78
    assert report.market.competitor_count == 18
    assert report.evidence_confidence.score == 0.74
    assert validate_report(report) == []


async def test_validation_rejects_a_market_number_without_evidence() -> None:
    report = await build_report(UnavailableEvidenceProvider(), status="partial")
    tampered = report.model_copy(
        update={"market": report.market.model_copy(update={"competitor_count": 12})}
    )

    assert "market_value_without_evidence:competitor_count" in validate_report(tampered)


async def test_validation_rejects_a_default_score_on_an_unscorable_dimension() -> None:
    report = await build_report(UnavailableEvidenceProvider(), status="partial")
    dimensions = [
        dimension.model_copy(update={"score": 50})
        if dimension.key == "market_saturation"
        else dimension
        for dimension in report.readiness.dimensions
    ]
    tampered = report.model_copy(
        update={"readiness": report.readiness.model_copy(update={"dimensions": dimensions})}
    )

    assert "dimension_default_score:market_saturation" in validate_report(tampered)


async def test_report_round_trips_through_json() -> None:
    report = await build_report(UnavailableEvidenceProvider(), status="partial")
    restored = AnalysisReport.model_validate(report.model_dump(mode="json"))
    assert restored.model_dump() == report.model_dump()
