"""Fixture evidence provider.

It lives under `tests/` on purpose. Nothing in `app/` may import it, so there is
no code path that could let invented numbers reach a production report; the
guard in `app.integrations.evidence.select_evidence_provider` is the second
line of defence and is exercised by `tests/test_evidence_contract.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from app.domain.evidence import (
    EvidenceQuality,
    EvidenceRecord,
    EvidenceRequest,
    EvidenceSnapshot,
    Geography,
    MissingEvidence,
)

FIXTURE_OBSERVED_AT = datetime(2026, 8, 1, tzinfo=UTC)
FIXTURE_RETRIEVED_AT = datetime(2026, 8, 14, tzinfo=UTC)

DEFAULT_QUALITY = EvidenceQuality(
    coverage="partial",
    freshness="recent",
    geographic_fit=0.7,
    sample_sufficiency=0.6,
    cross_source_consistency=0.5,
    source_quality=0.8,
    sample_size=12,
)

DEFAULT_UNITS = {
    "competitor_count": "places",
    "population_count": "people",
    "comparable_price_median_idr": "IDR",
    "comparable_price_sample_size": "observations",
}


class FixtureEvidenceProvider:
    provider_id = "fixture"
    snapshot_version = "evidence-snapshot-fixture-v1"
    is_fixture = True

    def __init__(
        self,
        values: dict[str, int],
        *,
        quality: EvidenceQuality | None = None,
        radius_override: int | None = None,
        limitations: list[str] | None = None,
    ) -> None:
        self._values = values
        self._quality = quality or DEFAULT_QUALITY
        self._radius_override = radius_override
        self._limitations = limitations or ["Data fixture, bukan observasi lapangan."]

    async def collect(self, requests: Sequence[EvidenceRequest]) -> EvidenceSnapshot:
        items: list[EvidenceRecord] = []
        missing: list[MissingEvidence] = []
        for request in requests:
            if request.metric not in self._values:
                missing.append(
                    MissingEvidence(
                        metric=request.metric,
                        reason_code="not_in_fixture",
                        reason="Metrik ini tidak ada pada fixture uji.",
                    )
                )
                continue
            geography = request.geography
            if self._radius_override is not None:
                geography = Geography(
                    type=geography.type,
                    area_id=geography.area_id,
                    center_id=geography.center_id,
                    meters=self._radius_override,
                )
            items.append(
                EvidenceRecord(
                    metric=request.metric,
                    value=self._values[request.metric],
                    unit=DEFAULT_UNITS.get(request.metric, "unit"),
                    geography=geography,
                    category_mapping_version="fnb-taxonomy-v1",
                    source="fixture",
                    source_url=None,
                    observed_at=FIXTURE_OBSERVED_AT,
                    retrieved_at=FIXTURE_RETRIEVED_AT,
                    quality=self._quality,
                    limitations=list(self._limitations),
                )
            )
        return EvidenceSnapshot(
            snapshot_version=self.snapshot_version,
            provider_id=self.provider_id,
            retrieved_at=FIXTURE_RETRIEVED_AT,
            items=items,
            missing=missing,
        )


COMPLETE_FIXTURE_VALUES: dict[str, int] = {
    "competitor_count": 18,
    "population_count": 42_000,
    "comparable_price_median_idr": 17_000,
    "comparable_price_sample_size": 12,
}
