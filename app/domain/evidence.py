"""Evidence contract shared by providers, engines, and the report composer.

Every data point that reaches a report as fact must arrive wrapped in an
`EvidenceRecord`. Docs/05 makes provenance a product claim, not a nicety: a
number without source, observation time, and quality is not allowed to be
presented as fact. Missing evidence therefore has a first-class shape too, so
that "we do not know" can travel through the pipeline without being rounded
into a default.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

GeographyType = Literal["radius", "area", "national"]
CoverageLevel = Literal["complete", "partial", "unknown"]
FreshnessLevel = Literal["recent", "aging", "stale", "unknown"]

METRIC_COMPETITOR_COUNT = "competitor_count"
METRIC_POPULATION_COUNT = "population_count"
METRIC_COMPARABLE_PRICE_MEDIAN = "comparable_price_median_idr"
METRIC_COMPARABLE_PRICE_SAMPLE_SIZE = "comparable_price_sample_size"

REQUIRED_EVIDENCE_METRICS: tuple[str, ...] = (
    METRIC_COMPETITOR_COUNT,
    METRIC_POPULATION_COUNT,
    METRIC_COMPARABLE_PRICE_MEDIAN,
    METRIC_COMPARABLE_PRICE_SAMPLE_SIZE,
)

METRIC_LABELS: dict[str, str] = {
    METRIC_COMPETITOR_COUNT: "Jumlah kompetitor pada radius analisis",
    METRIC_POPULATION_COUNT: "Populasi pada radius analisis",
    METRIC_COMPARABLE_PRICE_MEDIAN: "Median harga produk pembanding",
    METRIC_COMPARABLE_PRICE_SAMPLE_SIZE: "Jumlah observasi harga pembanding",
}


class Geography(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: GeographyType
    area_id: str | None = None
    center_id: str | None = None
    meters: int | None = Field(default=None, ge=0)


class EvidenceQuality(BaseModel):
    model_config = ConfigDict(frozen=True)

    coverage: CoverageLevel
    freshness: FreshnessLevel
    geographic_fit: float = Field(ge=0, le=1)
    sample_sufficiency: float = Field(ge=0, le=1)
    cross_source_consistency: float = Field(ge=0, le=1)
    source_quality: float = Field(ge=0, le=1)
    sample_size: int | None = Field(default=None, ge=0)


class EvidenceRecord(BaseModel):
    """A single observed value with the provenance docs/05 requires.

    `value` is an integer because every MVP metric is a count or an integer
    rupiah amount. A metric that genuinely needs fractions must extend this
    contract with a new version rather than silently introduce floats.
    """

    model_config = ConfigDict(frozen=True)

    metric: str
    value: int
    unit: str
    geography: Geography
    category_mapping_version: str | None = None
    source: str
    source_url: str | None = None
    observed_at: datetime
    retrieved_at: datetime
    quality: EvidenceQuality
    limitations: list[str] = Field(default_factory=list)


class MissingEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: str
    reason_code: str
    reason: str


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    metric: str
    geography: Geography


class EvidenceSnapshot(BaseModel):
    """Frozen set of evidence for one analysis run."""

    model_config = ConfigDict(frozen=True)

    snapshot_version: str
    provider_id: str
    retrieved_at: datetime
    items: list[EvidenceRecord] = Field(default_factory=list)
    missing: list[MissingEvidence] = Field(default_factory=list)

    def find(self, metric: str) -> EvidenceRecord | None:
        for item in self.items:
            if item.metric == metric:
                return item
        return None

    def missing_metrics(self) -> list[str]:
        return [entry.metric for entry in self.missing]


@runtime_checkable
class EvidenceProvider(Protocol):
    """Source of evidence records.

    Implementations never invent values. When a metric cannot be retrieved they
    report it through `EvidenceSnapshot.missing` so the gap survives all the way
    to the report.
    """

    provider_id: str
    snapshot_version: str
    is_fixture: bool

    async def collect(self, requests: Sequence[EvidenceRequest]) -> EvidenceSnapshot: ...
