"""Evidence contract, provider selection, and the confidence formula."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.domain.evidence import (
    REQUIRED_EVIDENCE_METRICS,
    EvidenceProvider,
    EvidenceRequest,
    Geography,
)
from app.engines.evidence_confidence import calculate_evidence_confidence
from app.integrations.evidence import (
    FixtureProviderNotAllowedError,
    UnavailableEvidenceProvider,
    select_evidence_provider,
)
from tests.support.evidence import COMPLETE_FIXTURE_VALUES, FixtureEvidenceProvider

GEOGRAPHY = Geography(type="radius", area_id="jabodetabek-tebet", meters=1_500)
REQUESTS = [
    EvidenceRequest(metric=metric, geography=GEOGRAPHY) for metric in REQUIRED_EVIDENCE_METRICS
]


def settings_for(environment: str) -> Settings:
    return Settings(
        environment=environment,  # type: ignore[arg-type]
        jwt_secret="test-secret-with-at-least-thirty-two-characters",
        auth_cookie_secure=True,
    )


async def test_unavailable_provider_reports_every_metric_as_missing() -> None:
    snapshot = await UnavailableEvidenceProvider().collect(REQUESTS)

    assert snapshot.items == []
    assert set(snapshot.missing_metrics()) == set(REQUIRED_EVIDENCE_METRICS)
    assert all(entry.reason_code == "source_not_configured" for entry in snapshot.missing)


async def test_missing_evidence_is_never_replaced_by_a_default_value() -> None:
    snapshot = await UnavailableEvidenceProvider().collect(REQUESTS)
    for metric in REQUIRED_EVIDENCE_METRICS:
        assert snapshot.find(metric) is None


async def test_evidence_records_carry_full_provenance() -> None:
    snapshot = await FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES).collect(REQUESTS)

    for record in snapshot.items:
        assert record.metric
        assert record.unit
        assert record.source
        assert record.geography.type == "radius"
        assert record.observed_at is not None
        assert record.retrieved_at is not None
        assert record.quality.coverage
        assert record.quality.freshness
        assert record.limitations


def test_providers_satisfy_the_protocol() -> None:
    assert isinstance(UnavailableEvidenceProvider(), EvidenceProvider)
    assert isinstance(FixtureEvidenceProvider({}), EvidenceProvider)


def test_runtime_defaults_to_the_unavailable_provider() -> None:
    provider = select_evidence_provider(settings_for("production"))
    assert isinstance(provider, UnavailableEvidenceProvider)
    assert provider.is_fixture is False


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_fixture_provider_cannot_be_activated_outside_development(environment: str) -> None:
    with pytest.raises(FixtureProviderNotAllowedError):
        select_evidence_provider(
            settings_for(environment), FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES)
        )


def test_fixture_provider_is_allowed_in_tests() -> None:
    provider = select_evidence_provider(
        settings_for("test"), FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES)
    )
    assert provider.is_fixture is True


def test_fixture_provider_is_not_importable_from_the_application_package() -> None:
    import importlib
    import pkgutil

    import app

    offenders: list[str] = []
    for info in pkgutil.walk_packages(app.__path__, prefix=f"{app.__name__}."):
        module = importlib.import_module(info.name)
        source = getattr(module, "__file__", None)
        if source is None:
            continue
        with open(source, encoding="utf-8") as handle:
            if "FixtureEvidenceProvider" in handle.read():
                offenders.append(info.name)

    assert not offenders, f"fixture provider referenced by production modules: {offenders}"


async def test_confidence_is_unavailable_when_nothing_was_retrieved() -> None:
    snapshot = await UnavailableEvidenceProvider().collect(REQUESTS)
    confidence = calculate_evidence_confidence(snapshot)

    assert confidence.score is None
    assert confidence.label == "tidak_tersedia"
    assert set(confidence.missing) == set(REQUIRED_EVIDENCE_METRICS)


async def test_confidence_uses_the_documented_weighted_mean() -> None:
    snapshot = await FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES).collect(REQUESTS)
    confidence = calculate_evidence_confidence(snapshot)

    # 0.25*0.8 + 0.20*1.0 + 0.20*0.7 + 0.20*0.6 + 0.15*0.5 = 0.735
    assert confidence.score == 0.74
    assert confidence.label == "sedang"
    assert confidence.missing == []


async def test_missing_metrics_lower_confidence_rather_than_being_filled_in() -> None:
    partial = {
        "competitor_count": COMPLETE_FIXTURE_VALUES["competitor_count"],
        "population_count": COMPLETE_FIXTURE_VALUES["population_count"],
    }
    snapshot = await FixtureEvidenceProvider(partial).collect(REQUESTS)
    confidence = calculate_evidence_confidence(snapshot)

    assert confidence.score == 0.37
    assert confidence.label == "rendah"
    assert set(confidence.missing) == {
        "comparable_price_median_idr",
        "comparable_price_sample_size",
    }
