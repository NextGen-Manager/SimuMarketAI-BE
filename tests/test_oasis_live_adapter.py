"""Live OASIS adapter.

The parts that can be checked without a provider are checked here: roster
construction, profile generation, JSON extraction, and the refusal to reuse a
trace path. The part that cannot — whether Gemini actually returns
schema-valid council output within budget — is skipped, loudly, rather than
asserted from a stub that would prove nothing about the real provider.

Nothing in this file has been run against Gemini. `GEMINI_API_KEY` was not
available while Phase 4 was implemented, so the live integration and the Phase 0
benchmark in `Docs/docs/14` both remain open.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core.config import Settings
from app.domain.agents import (
    ConceptCard as Concept,
)
from app.domain.agents import (
    FinanceBounds,
    OasisUnavailableError,
    SimulationBudget,
    SimulationRequest,
    TraceArtifact,
    build_cohort_manifest,
)
from app.integrations.oasis.live import (
    LiveOasisAdapter,
    _extract_json,
    _usage_tokens,
    build_profiles,
    build_roster,
)
from app.integrations.oasis.runtime import build_manifest

LIVE_SKIP_REASON = (
    "GEMINI_API_KEY tidak tersedia; run live OASIS dan benchmark Fase 0 belum dijalankan."
)

requires_live_provider = pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY"), reason=LIVE_SKIP_REASON
)


def _request(size: int = 16) -> SimulationRequest:
    return SimulationRequest(
        analysis_ref="a" * 32,
        correlation_ref="b" * 32,
        concept=Concept(
            business_type="food_stall",
            concept_name="Rice Bowl Sambal",
            area_id="jabodetabek-tebet",
            analysis_radius_m=1500,
            price_idr=18_000,
            variable_cost_per_unit_idr=11_000,
            channels=["takeaway"],
            value_proposition="Makan siang cepat",
        ),
        evidence=[],
        missing_evidence_metrics=["competitor_count"],
        finance_bounds=FinanceBounds(
            volume_units_day_min=25,
            volume_units_day_base=40,
            volume_units_day_max=55,
            variable_cost_per_unit_idr=11_000,
        ),
        finance_rule_version="finance-v1",
        budget=SimulationBudget(
            persona_count=size,
            round_limit=4,
            token_budget=120_000,
            max_output_tokens_per_stage=1_024,
            concurrency_limit=4,
            wall_clock_seconds=240,
            retry_limit=1,
        ),
        cohort=build_cohort_manifest(cohort_version="jabodetabek-fnb-v1", size=size),
        seed=42,
    )


def test_roster_covers_every_council_in_a_stable_order() -> None:
    roster = build_roster(_request())
    roles = [role for role, _ in roster]

    # 3 market + 16 persona + 4 finance + 3 report.
    assert len(roster) == 26
    assert roles[:3] == ["market_analyst"] * 3
    assert roles[3:19] == ["customer_persona"] * 16
    assert roles[19:23] == ["finance"] * 4
    assert roles[23:] == ["report"] * 3

    # Stable across calls, so a manifest reproduces the same agent indices.
    assert [member.agent_id for _, member in build_roster(_request())] == [
        member.agent_id for _, member in roster
    ]


def test_profiles_invent_no_demographic_attribute() -> None:
    profiles = build_profiles(build_roster(_request()))

    assert len(profiles) == 26
    assert len({profile["username"] for profile in profiles}) == 26
    for profile in profiles:
        assert profile["gender"] == "N/A"
        assert profile["age"] == "N/A"
        assert profile["mbti"] == "N/A"
        assert profile["persona"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('Berikut hasilnya:\n```json\n{"a": 1}\n```', {"a": 1}),
        ('prefix {"a": {"b": 2}} suffix', {"a": {"b": 2}}),
    ],
)
def test_json_is_recovered_from_a_wrapped_response(raw: str, expected: dict[str, int]) -> None:
    assert _extract_json(raw) == expected


@pytest.mark.parametrize("raw", ["tidak ada json", "", "[1, 2, 3]", "{rusak"])
def test_malformed_output_is_a_schema_error_not_a_crash(raw: str) -> None:
    from app.domain.agents import OasisSchemaError

    with pytest.raises(OasisSchemaError):
        _extract_json(raw)


def test_token_usage_defaults_to_zero_when_the_provider_omits_it() -> None:
    assert _usage_tokens({"usage": {"total_tokens": 120}}) == 120
    assert _usage_tokens({"usage": {}}) == 0
    assert _usage_tokens({}) == 0
    assert _usage_tokens(None) == 0


async def test_an_existing_trace_path_is_refused(tmp_path: Path) -> None:
    """Two runs must never share one environment, even by accident."""
    settings = Settings(
        environment="test",
        jwt_secret="test-secret-with-at-least-thirty-two-characters",
        oasis_trace_root=str(tmp_path),
    )
    directory = tmp_path / "env-1"
    directory.mkdir()
    trace = directory / "trace.db"
    trace.write_bytes(b"an earlier run wrote this")

    manifest = build_manifest(
        settings,
        adapter_id="oasis-live",
        environment="env-1",
        cohort=build_cohort_manifest(cohort_version="jabodetabek-fnb-v1", size=16),
        budget=_request().budget,
        trace=TraceArtifact(object_key=str(trace), retention_days=30),
        evidence_snapshot_version="evidence-snapshot-fixture-v1",
        snapshot_hash="0" * 64,
    )
    adapter = LiveOasisAdapter(api_key="unused", model_id="gemini-3.1-flash-lite")

    with pytest.raises(OasisUnavailableError):
        await adapter.simulate(
            _request(),
            finance_tool=lambda _: (_ for _ in ()).throw(AssertionError("must not be called")),
            manifest=manifest,
        )

    # The earlier run's trace is untouched.
    assert trace.read_bytes() == b"an earlier run wrote this"


def test_a_missing_package_degrades_instead_of_crashing_the_import() -> None:
    """Importing the adapter must never require camel-oasis to be installed."""
    import app.integrations.oasis.live as live

    assert live.LiveOasisAdapter.adapter_id == "oasis-live"
    assert live.LiveOasisAdapter.is_fake is False


@requires_live_provider
async def test_live_four_agent_run_against_gemini() -> None:
    """Not executed. Recorded so the gap is visible rather than assumed closed.

    When a key is available this must assert: four councils complete, artifacts
    validate, the trace file exists with a checksum, token usage lands under
    budget, and the measured numbers are written to `Docs/docs/14` with the date
    they were measured.
    """
    pytest.fail("Live run harness belum ditulis; jangan mengklaim hasil yang belum diukur.")


@requires_live_provider
def test_live_benchmark_numbers_are_recorded() -> None:
    """Not executed. Phase 0 exit criteria stay open until this runs for real."""
    pytest.fail("Benchmark Gemini belum dijalankan; angka tidak boleh dikarang.")
