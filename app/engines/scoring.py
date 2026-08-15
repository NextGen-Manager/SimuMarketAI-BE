"""Launch Readiness Score engine, rule set `lrs-v0.2-unvalidated`.

Weights come from `Docs/docs/05-data-evidence-and-scoring.md` and ADR-003:
market saturation 20, demand potential 25, price positioning 15, operational
readiness 40. Changing either a weight or a threshold requires a new rule set
version and an ADR; nothing here rewrites them.

Two rules keep the output honest. A dimension without its required inputs is
marked `not_scorable` instead of receiving a default. When any dimension is not
scorable the total is withheld entirely, because redistributing the missing
weight over the remaining dimensions would be a silent reweight.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.domain.evidence import (
    METRIC_COMPARABLE_PRICE_MEDIAN,
    METRIC_COMPARABLE_PRICE_SAMPLE_SIZE,
    METRIC_COMPETITOR_COUNT,
    METRIC_POPULATION_COUNT,
    EvidenceSnapshot,
)
from app.schemas.analysis import (
    DimensionKey,
    DimensionScore,
    FinanceResult,
    ScoreResult,
)

DIMENSION_WEIGHTS: dict[DimensionKey, int] = {
    "market_saturation": 20,
    "demand_potential": 25,
    "price_positioning": 15,
    "operational_readiness": 40,
}

DIMENSION_LABELS: dict[DimensionKey, str] = {
    "market_saturation": "Saturasi pasar",
    "demand_potential": "Potensi permintaan",
    "price_positioning": "Posisi harga",
    "operational_readiness": "Kesiapan operasional",
}

INTERPRETATIONS: tuple[tuple[int, str, str], ...] = (
    (80, "sangat_layak", "Sangat layak dilanjutkan"),
    (65, "layak_dengan_mitigasi", "Layak dengan mitigasi"),
    (50, "perlu_evaluasi_ulang", "Perlu evaluasi ulang"),
    (0, "tidak_disarankan", "Tidak disarankan pada kondisi tersebut"),
)

# Below this many observations a price percentile is not a measurement.
MIN_PRICE_SAMPLE_SIZE = 5

PI = Decimal("3.14159265358979323846")


@dataclass(frozen=True, slots=True)
class ScoringInput:
    planned_price_idr: int
    analysis_radius_m: int
    capacity_units_day: int
    base_volume_units_day: int
    fixed_cost_month_idr: int
    finance: FinanceResult
    evidence: EvidenceSnapshot


@dataclass(frozen=True, slots=True)
class _Outcome:
    score: int | None
    applied_rules: list[str]
    rationale: str
    missing_inputs: list[str]
    evidence_metrics: list[str]


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def interpret(score: int) -> tuple[str, str]:
    for threshold, code, label in INTERPRETATIONS:
        if score >= threshold:
            return code, label
    return INTERPRETATIONS[-1][1], INTERPRETATIONS[-1][2]


def _market_saturation(value: ScoringInput) -> _Outcome:
    metrics = [METRIC_COMPETITOR_COUNT]
    record = value.evidence.find(METRIC_COMPETITOR_COUNT)
    if record is None:
        return _Outcome(None, [], "Jumlah kompetitor belum tersedia.", metrics, metrics)
    if record.geography.meters != value.analysis_radius_m:
        return _Outcome(
            None,
            [],
            "Radius bukti kompetitor tidak sama dengan radius analisis.",
            metrics,
            metrics,
        )

    radius = Decimal(value.analysis_radius_m)
    area_km2 = (PI * radius * radius) / Decimal(1_000_000)
    if area_km2 <= 0:
        return _Outcome(None, [], "Luas catchment tidak dapat dihitung.", metrics, metrics)
    density_x100 = _round_half_up(Decimal(record.value * 100) / area_km2)

    if density_x100 <= 200:
        rule, score = "MS-001", 90
    elif density_x100 <= 500:
        rule, score = "MS-002", 75
    elif density_x100 <= 1_000:
        rule, score = "MS-003", 60
    elif density_x100 <= 2_000:
        rule, score = "MS-004", 40
    else:
        rule, score = "MS-005", 20

    return _Outcome(
        score,
        [rule],
        (
            f"{record.value} kompetitor pada radius {value.analysis_radius_m} m, "
            f"setara {density_x100 // 100},{density_x100 % 100:02d} gerai per km persegi."
        ),
        [],
        metrics,
    )


def _demand_potential(value: ScoringInput) -> _Outcome:
    metrics = [METRIC_POPULATION_COUNT, METRIC_COMPETITOR_COUNT]
    population = value.evidence.find(METRIC_POPULATION_COUNT)
    competitors = value.evidence.find(METRIC_COMPETITOR_COUNT)
    missing = [
        metric
        for metric, record in (
            (METRIC_POPULATION_COUNT, population),
            (METRIC_COMPETITOR_COUNT, competitors),
        )
        if record is None
    ]
    if population is None or competitors is None:
        return _Outcome(None, [], "Data populasi atau kompetitor belum tersedia.", missing, metrics)
    if population.geography.meters != value.analysis_radius_m:
        return _Outcome(
            None,
            [],
            "Radius bukti populasi tidak sama dengan radius analisis.",
            [METRIC_POPULATION_COUNT],
            metrics,
        )

    per_outlet = population.value // (competitors.value + 1)
    if per_outlet >= 5_000:
        rule, score = "DP-001", 85
    elif per_outlet >= 2_500:
        rule, score = "DP-002", 70
    elif per_outlet >= 1_000:
        rule, score = "DP-003", 55
    elif per_outlet >= 400:
        rule, score = "DP-004", 35
    else:
        rule, score = "DP-005", 20

    return _Outcome(
        score,
        [rule],
        (
            f"{population.value} penduduk pada radius analisis dibagi "
            f"{competitors.value + 1} gerai menghasilkan {per_outlet} penduduk per gerai."
        ),
        [],
        metrics,
    )


def _price_positioning(value: ScoringInput) -> _Outcome:
    metrics = [METRIC_COMPARABLE_PRICE_MEDIAN, METRIC_COMPARABLE_PRICE_SAMPLE_SIZE]
    median = value.evidence.find(METRIC_COMPARABLE_PRICE_MEDIAN)
    sample = value.evidence.find(METRIC_COMPARABLE_PRICE_SAMPLE_SIZE)
    missing = [
        metric
        for metric, record in (
            (METRIC_COMPARABLE_PRICE_MEDIAN, median),
            (METRIC_COMPARABLE_PRICE_SAMPLE_SIZE, sample),
        )
        if record is None
    ]
    if median is None or sample is None:
        return _Outcome(None, [], "Harga pembanding pasar belum tersedia.", missing, metrics)
    if sample.value < MIN_PRICE_SAMPLE_SIZE:
        return _Outcome(
            None,
            [],
            (
                f"Observasi harga pembanding hanya {sample.value}, "
                f"di bawah minimum {MIN_PRICE_SAMPLE_SIZE}."
            ),
            [METRIC_COMPARABLE_PRICE_SAMPLE_SIZE],
            metrics,
        )
    if median.value <= 0:
        return _Outcome(
            None,
            [],
            "Median harga pembanding nol sehingga posisi harga tidak dapat dihitung.",
            [METRIC_COMPARABLE_PRICE_MEDIAN],
            metrics,
        )

    ratio = _round_half_up(Decimal(value.planned_price_idr * 100) / Decimal(median.value))
    if 80 <= ratio <= 120:
        rule, score = "PP-001", 80
    elif 60 <= ratio < 80:
        rule, score = "PP-002", 65
    elif 120 < ratio <= 150:
        rule, score = "PP-003", 55
    elif ratio < 60:
        rule, score = "PP-004", 40
    else:
        rule, score = "PP-005", 30

    applied = [rule]
    if value.finance.contribution_margin_per_unit_idr <= 0 and score > 20:
        score = 20
        applied.append("PP-006")

    return _Outcome(
        score,
        applied,
        (f"Harga rencana {ratio}% dari median pembanding ({sample.value} observasi)."),
        [],
        metrics,
    )


def _operational_readiness(value: ScoringInput) -> _Outcome:
    """Scored from validated user input and the deterministic finance result.

    No external evidence is required, so this dimension stays scorable while
    market sources are unavailable. The trade-off is that its inputs are
    self-reported, which the report states as a limitation.
    """
    applied: list[str] = []
    margin = value.finance.contribution_margin_per_unit_idr
    if margin > 0:
        margin_points = 40
        applied.append("OR-001a")
    elif margin == 0:
        margin_points = 10
        applied.append("OR-001b")
    else:
        margin_points = 0
        applied.append("OR-001c")

    bep_units_day = value.finance.bep_units_day
    if bep_units_day is None:
        capacity_points = 0
        applied.append("OR-002c")
    elif value.capacity_units_day >= bep_units_day * 2:
        capacity_points = 30
        applied.append("OR-002a")
    elif value.capacity_units_day >= bep_units_day:
        capacity_points = 20
        applied.append("OR-002b")
    else:
        capacity_points = 0
        applied.append("OR-002c")

    runway = value.finance.runway_months
    if value.fixed_cost_month_idr == 0:
        runway_points = 20
        applied.append("OR-003a")
    elif runway is None:
        runway_points = 0
        applied.append("OR-003d")
    elif runway >= 6:
        runway_points = 20
        applied.append("OR-003a")
    elif runway >= 3:
        runway_points = 12
        applied.append("OR-003b")
    elif runway >= 1:
        runway_points = 6
        applied.append("OR-003c")
    else:
        runway_points = 0
        applied.append("OR-003d")

    if value.base_volume_units_day <= value.capacity_units_day:
        volume_points = 10
        applied.append("OR-004a")
    else:
        volume_points = 0
        applied.append("OR-004b")

    score = margin_points + capacity_points + runway_points + volume_points
    return _Outcome(
        score,
        applied,
        (
            f"Marjin kontribusi {margin_points}/40, kapasitas terhadap BEP {capacity_points}/30, "
            f"runway {runway_points}/20, volume terhadap kapasitas {volume_points}/10."
        ),
        [],
        [],
    )


def calculate_score(value: ScoringInput) -> ScoreResult:
    outcomes: dict[DimensionKey, _Outcome] = {
        "market_saturation": _market_saturation(value),
        "demand_potential": _demand_potential(value),
        "price_positioning": _price_positioning(value),
        "operational_readiness": _operational_readiness(value),
    }

    dimensions = [
        DimensionScore(
            key=key,
            label=DIMENSION_LABELS[key],
            weight_percent=DIMENSION_WEIGHTS[key],
            status="scored" if outcome.score is not None else "not_scorable",
            score=outcome.score,
            applied_rules=outcome.applied_rules,
            rationale=outcome.rationale,
            missing_inputs=outcome.missing_inputs,
            evidence_metrics=outcome.evidence_metrics,
        )
        for key, outcome in outcomes.items()
    ]

    missing_dimensions = [
        dimension.key for dimension in dimensions if dimension.status == "not_scorable"
    ]
    if missing_dimensions:
        return ScoreResult(
            status="unavailable",
            score=None,
            interpretation=None,
            interpretation_label=None,
            dimensions=dimensions,
            missing_dimensions=missing_dimensions,
        )

    weighted = sum(
        DIMENSION_WEIGHTS[dimension.key] * (dimension.score or 0) for dimension in dimensions
    )
    total = _round_half_up(Decimal(weighted) / Decimal(100))
    code, label = interpret(total)
    return ScoreResult(
        status="available",
        score=total,
        interpretation=code,
        interpretation_label=label,
        dimensions=dimensions,
        missing_dimensions=[],
    )
