"""Evidence Confidence, draft formula from docs/05.

Confidence sits beside the readiness score and never modifies it. A required
metric that is missing contributes zero to the mean, which is how docs/05 wants
absent market prices handled: they lower confidence rather than get filled in.

When nothing at all was retrieved there is no mean to take, so the score is
`None` and the label is `tidak_tersedia` rather than a misleading 0.00.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from app.domain.evidence import (
    REQUIRED_EVIDENCE_METRICS,
    EvidenceRecord,
    EvidenceSnapshot,
    FreshnessLevel,
)
from app.schemas.analysis import EvidenceConfidence

FRESHNESS_SCORES: dict[FreshnessLevel, Decimal] = {
    "recent": Decimal("1.00"),
    "aging": Decimal("0.60"),
    "stale": Decimal("0.30"),
    "unknown": Decimal("0.20"),
}

# Weights of the five quality dimensions named in docs/05.
QUALITY_WEIGHTS: tuple[tuple[str, Decimal], ...] = (
    ("source_quality", Decimal("0.25")),
    ("freshness", Decimal("0.20")),
    ("geographic_fit", Decimal("0.20")),
    ("sample_sufficiency", Decimal("0.20")),
    ("cross_source_consistency", Decimal("0.15")),
)

HIGH_THRESHOLD = Decimal("0.75")
MEDIUM_THRESHOLD = Decimal("0.50")


def _record_confidence(record: EvidenceRecord) -> Decimal:
    quality = record.quality
    values: dict[str, Decimal] = {
        "source_quality": Decimal(str(quality.source_quality)),
        "freshness": FRESHNESS_SCORES[quality.freshness],
        "geographic_fit": Decimal(str(quality.geographic_fit)),
        "sample_sufficiency": Decimal(str(quality.sample_sufficiency)),
        "cross_source_consistency": Decimal(str(quality.cross_source_consistency)),
    }
    return sum((values[name] * weight for name, weight in QUALITY_WEIGHTS), Decimal(0))


def calculate_evidence_confidence(snapshot: EvidenceSnapshot) -> EvidenceConfidence:
    present = [
        record
        for metric in REQUIRED_EVIDENCE_METRICS
        if (record := snapshot.find(metric)) is not None
    ]
    missing = [metric for metric in REQUIRED_EVIDENCE_METRICS if snapshot.find(metric) is None]

    if not present:
        return EvidenceConfidence(score=None, label="tidak_tersedia", missing=missing)

    total = sum((_record_confidence(record) for record in present), Decimal(0))
    mean = total / Decimal(len(REQUIRED_EVIDENCE_METRICS))
    rounded = mean.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    label: Literal["tinggi", "sedang", "rendah"]
    if rounded >= HIGH_THRESHOLD:
        label = "tinggi"
    elif rounded >= MEDIUM_THRESHOLD:
        label = "sedang"
    else:
        label = "rendah"

    return EvidenceConfidence(score=float(rounded), label=label, missing=missing)
