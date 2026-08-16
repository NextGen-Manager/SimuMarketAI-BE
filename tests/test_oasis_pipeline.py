"""The four-agent path: success, every failure mode, and the honest fallback.

Every test here runs with no network, no broker, and no provider key. The fake
adapter walks the same councils, the same validation, and the same persistence
as the live one, so what these tests prove about the pipeline holds for a real
run too — except for the model responses themselves, which nothing here claims
to have verified.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.domain.agents import (
    OasisBudgetExceededError,
    OasisTimeoutError,
    OasisUnavailableError,
)
from app.integrations.oasis.fake import FakeOasisAdapter
from app.persistence.models import (
    AgentArtifact,
    AgentInstance,
    AgentRun,
    AgentTraceArtifact,
    AnalysisRun,
)
from tests.support.api import (
    client,
    complete_required_education,
    create_analysis,
    register,
    use_evidence_provider,
    use_oasis_adapter,
)
from tests.support.evidence import COMPLETE_FIXTURE_VALUES, FixtureEvidenceProvider


@pytest.fixture(autouse=True)
def isolate_traces(database_app: FastAPI, tmp_path: Path) -> None:
    """Never write a trace outside the test's own directory."""
    database_app.state.test_settings.oasis_trace_root = str(tmp_path / "traces")


async def _owner_with_run(
    database_app: FastAPI,
    *,
    adapter: FakeOasisAdapter | None = None,
    complete_evidence: bool = True,
) -> tuple[str, object]:
    if complete_evidence:
        use_evidence_provider(database_app, FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES))
    if adapter is not None:
        use_oasis_adapter(database_app, adapter)
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await create_analysis(database_app, owner)
        report = (await owner.get(f"/v1/analyses/{analysis_id}/report")).json()
        detail = (await owner.get(f"/v1/analyses/{analysis_id}")).json()
    return analysis_id, (report, detail)


# --------------------------------------------------------------------- success


async def test_fake_four_agent_run_produces_typed_artifacts(database_app: FastAPI) -> None:
    analysis_id, payloads = await _owner_with_run(database_app, adapter=FakeOasisAdapter())
    report, detail = payloads  # type: ignore[misc]

    assert detail["status"] == "completed"
    assert detail["progress"]["skipped_stages"] == []
    assert "simulating" in detail["progress"]["completed_stages"]

    simulation = report["synthetic_simulation"]
    assert simulation["status"] == "experimental"
    assert simulation["cohort_size"] == 16
    assert simulation["metrics"]["activated_persona_count"] == 16
    assert simulation["quotes"]
    assert all(quote["label"] == "respons sintetis" for quote in simulation["quotes"])

    review = report["agent_review"]
    assert review["status"] == "available"
    assert review["manifest"]["model_id"] == "gemini-3.1-flash-lite"
    assert review["manifest"]["prompt_version"] == "oasis-council-v2"
    assert review["manifest"]["cohort_version"] == "jabodetabek-fnb-v1"
    assert review["market_observations"]
    assert review["finance_critiques"]
    assert review["narrative_sections"]

    async with database_app.state.test_session_factory() as session:
        runs = list(
            await session.scalars(
                select(AgentRun).where(AgentRun.analysis_run_id == UUID(analysis_id))
            )
        )
        artifacts = list(
            await session.scalars(
                select(AgentArtifact).where(AgentArtifact.analysis_run_id == UUID(analysis_id))
            )
        )
        instances = list(await session.scalars(select(AgentInstance)))

    assert {run.agent_type for run in runs} == {
        "market_analyst",
        "customer_persona",
        "finance",
        "report",
    }
    assert {artifact.artifact_type for artifact in artifacts} == {
        "MarketAssessment",
        "CustomerSimulationResult",
        "FinanceReview",
        "ReportNarrative",
    }
    assert all(artifact.checksum for artifact in artifacts)
    assert all(artifact.validation_status == "valid" for artifact in artifacts)
    # The report council cites the artifacts it synthesised from.
    narrative = next(item for item in artifacts if item.artifact_type == "ReportNarrative")
    assert len(narrative.source_artifact_ids) == 3
    # 3 market + 16 persona + 4 finance + 3 report personality instances.
    assert len(instances) == 26


async def test_finance_numbers_come_only_from_the_engine(database_app: FastAPI) -> None:
    _, payloads = await _owner_with_run(database_app, adapter=FakeOasisAdapter())
    report, _ = payloads  # type: ignore[misc]

    # The agent critique cites tool calls; it never restates a figure of its own.
    assert report["finance"]["rule_version"] == "finance-v1"
    assert report["finance"]["bep_units_month"] == 715
    for critique in report["agent_review"]["finance_critiques"]:
        assert critique["tool_call_ids"]
        assert all(item.startswith("finance-volume-") for item in critique["tool_call_ids"])
    assert report["readiness"]["rule_version"] == "lrs-v0.2-unvalidated"


async def test_trace_and_manifest_are_unique_per_run(database_app: FastAPI) -> None:
    use_evidence_provider(database_app, FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES))
    use_oasis_adapter(database_app, FakeOasisAdapter())
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        first = await create_analysis(database_app, owner, headers={"Idempotency-Key": "a"})
        second = await create_analysis(database_app, owner, headers={"Idempotency-Key": "b"})

    async with database_app.state.test_session_factory() as session:
        traces = list(await session.scalars(select(AgentTraceArtifact)))

    assert first != second
    assert len(traces) == 2
    assert len({trace.object_key for trace in traces}) == 2
    assert len({trace.environment_id for trace in traces}) == 2
    for trace in traces:
        assert Path(trace.object_key).exists()
        assert trace.checksum
        assert trace.byte_size and trace.byte_size > 0
        assert trace.access_scope == "owner_only"
        assert trace.retention_until is not None
        # No secret may be stored in a manifest.
        rendered = str(trace.manifest)
        assert "api_key" not in rendered
        assert database_app.state.test_settings.jwt_secret not in rendered


# -------------------------------------------------------------------- failures


@pytest.mark.parametrize(
    "error",
    [
        OasisUnavailableError(OasisUnavailableError.reason),
        OasisTimeoutError(OasisTimeoutError.reason),
        OasisBudgetExceededError(OasisBudgetExceededError.reason),
    ],
    ids=["unavailable", "timeout", "budget"],
)
async def test_oasis_failure_still_yields_a_deterministic_partial_report(
    database_app: FastAPI, error: Exception
) -> None:
    adapter = FakeOasisAdapter(error=error)  # type: ignore[arg-type]
    _, payloads = await _owner_with_run(database_app, adapter=adapter)
    report, detail = payloads  # type: ignore[misc]

    assert detail["status"] == "partial"
    assert detail["failure_code"] is None
    # The stage stays in the plan: it was attempted, not skipped.
    assert detail["progress"]["skipped_stages"] == []
    assert "simulating" in detail["progress"]["completed_stages"]
    assert "simulation_failed" in {warning["code"] for warning in detail["warnings"]}

    # Deterministic output survives intact.
    assert report["finance"]["bep_units_month"] == 715
    assert report["readiness"]["score"] == 78
    assert report["evidence"]

    # The simulation section stays in place and states why it is empty.
    assert report["synthetic_simulation"]["status"] == "unavailable"
    assert report["synthetic_simulation"]["reason"]
    assert report["agent_review"]["status"] == "unavailable"
    assert any("Simulasi persona" in item for item in report["limitations"])


async def test_a_schema_invalid_artifact_fails_only_its_own_council(
    database_app: FastAPI,
) -> None:
    adapter = FakeOasisAdapter(invalid_roles=("market_analyst",))
    analysis_id, payloads = await _owner_with_run(database_app, adapter=adapter)
    report, detail = payloads  # type: ignore[misc]

    assert detail["status"] == "partial"
    assert "simulation_partial" in {warning["code"] for warning in detail["warnings"]}

    # The persona council still ran, so its section is present and labelled.
    assert report["synthetic_simulation"]["status"] == "experimental"
    assert report["agent_review"]["status"] == "partial"
    assert report["agent_review"]["market_observations"] == []
    assert report["agent_review"]["narrative_sections"]

    async with database_app.state.test_session_factory() as session:
        runs = list(
            await session.scalars(
                select(AgentRun).where(AgentRun.analysis_run_id == UUID(analysis_id))
            )
        )
    failed = next(run for run in runs if run.agent_type == "market_analyst")
    assert failed.status == "failed"
    assert failed.failure_code == "oasis_schema_invalid"
    assert failed.schema_failures == 1


async def test_a_partial_agent_failure_never_becomes_completed(
    database_app: FastAPI,
) -> None:
    adapter = FakeOasisAdapter(failing_roles=("finance",))
    _, payloads = await _owner_with_run(database_app, adapter=adapter)
    _, detail = payloads  # type: ignore[misc]

    assert detail["status"] == "partial"


async def test_missing_evidence_is_never_filled_in_by_an_agent(
    database_app: FastAPI,
) -> None:
    # Runtime provider, so no market metric is available at all.
    _, payloads = await _owner_with_run(
        database_app, adapter=FakeOasisAdapter(), complete_evidence=False
    )
    report, detail = payloads  # type: ignore[misc]

    assert detail["status"] == "partial"
    assert report["market"]["competitor_count"] is None
    assert report["missing_evidence"]
    assert report["readiness"]["score"] is None

    # The market council may only speak in uncertainties when nothing is known.
    for observation in report["agent_review"]["market_observations"]:
        assert observation["stance"] == "uncertainty"
        assert observation["evidence_metrics"] == []
    assert report["agent_review"]["evidence_gaps"]


async def test_narrative_with_an_unknown_number_is_rejected(
    database_app: FastAPI,
) -> None:
    # 987654 appears in no evidence record, finance result, score, or input.
    adapter = FakeOasisAdapter(narrative_extra_number=987_654)
    use_evidence_provider(database_app, FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES))
    use_oasis_adapter(database_app, adapter)

    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await create_analysis(database_app, owner)
        detail = (await owner.get(f"/v1/analyses/{analysis_id}")).json()
        report = await owner.get(f"/v1/analyses/{analysis_id}/report")

    assert detail["status"] == "failed"
    assert detail["failure_code"] == "report_validation_failed"
    # A report that failed validation is never stored, so there is nothing to read.
    assert report.status_code == 404


async def test_budget_limits_actually_stop_a_run(database_app: FastAPI) -> None:
    # 26 instances at 320 tokens each cannot fit in a 1000-token budget.
    database_app.state.test_settings.oasis_token_budget = 1_000
    _, payloads = await _owner_with_run(database_app, adapter=FakeOasisAdapter())
    _, detail = payloads  # type: ignore[misc]

    assert detail["status"] == "partial"
    assert "simulation_failed" in {warning["code"] for warning in detail["warnings"]}


async def test_round_and_persona_limits_are_never_exceeded(database_app: FastAPI) -> None:
    """Rounds and cohort size are capped by construction, not by hope.

    Unlike the token and wall-clock budgets, these two cannot be exceeded at
    run time: the roster is built from the manifest and the round index is
    clamped. This asserts the cap holds rather than injecting a failure.
    """
    database_app.state.test_settings.oasis_cohort_size = 12
    database_app.state.test_settings.oasis_round_limit = 2

    analysis_id, payloads = await _owner_with_run(database_app, adapter=FakeOasisAdapter())
    report, _ = payloads  # type: ignore[misc]

    assert report["synthetic_simulation"]["cohort_size"] == 12
    assert report["synthetic_simulation"]["rounds"] == 2
    assert report["agent_review"]["manifest"]["persona_count"] == 12
    assert report["agent_review"]["manifest"]["round_limit"] == 2

    async with database_app.state.test_session_factory() as session:
        runs = list(
            await session.scalars(
                select(AgentRun).where(AgentRun.analysis_run_id == UUID(analysis_id))
            )
        )
        instances = list(await session.scalars(select(AgentInstance)))
        traces = list(await session.scalars(select(AgentTraceArtifact)))

    persona_run = next(run for run in runs if run.agent_type == "customer_persona")
    persona_instances = [
        instance for instance in instances if instance.agent_run_id == persona_run.id
    ]
    assert len(persona_instances) == 12
    assert all(run.round_limit == 2 for run in runs)

    # No interaction in the trace claims a round beyond the limit.
    connection = sqlite3.connect(traces[0].object_key)
    try:
        highest = connection.execute("SELECT MAX(round_index) FROM interactions").fetchone()[0]
    finally:
        connection.close()
    assert highest is not None and highest <= 1


async def test_wall_clock_limit_actually_stops_a_run(database_app: FastAPI) -> None:
    database_app.state.test_settings.oasis_wall_clock_seconds = 1
    adapter = FakeOasisAdapter(stage_delay_seconds=0.6)
    _, payloads = await _owner_with_run(database_app, adapter=adapter)
    report, detail = payloads  # type: ignore[misc]

    assert detail["status"] == "partial"
    assert report["synthetic_simulation"]["status"] == "unavailable"


# ---------------------------------------------------------------- provenance


async def test_artifact_sources_name_only_what_a_council_actually_read(
    database_app: FastAPI,
) -> None:
    """Provenance has to describe the run, not the protocol diagram.

    The Report council really is handed the three upstream artifacts, so it
    really does record three sources. The other councils are handed none, and a
    plausible-looking list of IDs on them would be a false audit trail.
    """
    analysis_id, _ = await _owner_with_run(database_app, adapter=FakeOasisAdapter())

    async with database_app.state.test_session_factory() as session:
        artifacts = list(
            await session.scalars(
                select(AgentArtifact).where(AgentArtifact.analysis_run_id == UUID(analysis_id))
            )
        )

    by_type = {artifact.artifact_type: artifact for artifact in artifacts}
    assert set(by_type) == {
        "MarketAssessment",
        "CustomerSimulationResult",
        "FinanceReview",
        "ReportNarrative",
    }
    for artifact_type in ("MarketAssessment", "CustomerSimulationResult", "FinanceReview"):
        assert by_type[artifact_type].source_artifact_ids == []

    narrative = by_type["ReportNarrative"]
    upstream = {
        str(by_type[artifact_type].id)
        for artifact_type in ("MarketAssessment", "CustomerSimulationResult", "FinanceReview")
    }
    assert set(narrative.source_artifact_ids) == upstream


async def test_a_council_that_failed_is_not_listed_as_a_narrative_source(
    database_app: FastAPI,
) -> None:
    adapter = FakeOasisAdapter(invalid_roles=("market_analyst",))
    analysis_id, payloads = await _owner_with_run(database_app, adapter=adapter)
    report, _ = payloads  # type: ignore[misc]

    async with database_app.state.test_session_factory() as session:
        artifacts = list(
            await session.scalars(
                select(AgentArtifact).where(AgentArtifact.analysis_run_id == UUID(analysis_id))
            )
        )

    by_type = {artifact.artifact_type: artifact for artifact in artifacts}
    assert "MarketAssessment" not in by_type

    narrative = by_type["ReportNarrative"]
    assert len(narrative.source_artifact_ids) == 2
    # And the narrative itself never claims the artifact it never saw.
    cited = {
        source
        for section in report["agent_review"]["narrative_sections"]
        for source in section["source_artifact_types"]
    }
    assert "MarketAssessment" not in cited


# ------------------------------------------------------------ trace retention


async def test_a_failed_simulation_still_records_its_trace_for_retention(
    database_app: FastAPI,
) -> None:
    """The directory is created before the adapter runs, so it is owed a record.

    Without one, a failed run leaves a directory on disk that no maintenance job
    knows about — the cleanup requirement in docs/11 would quietly leak.
    """
    adapter = FakeOasisAdapter(error=OasisTimeoutError(OasisTimeoutError.reason))
    analysis_id, payloads = await _owner_with_run(database_app, adapter=adapter)
    _, detail = payloads  # type: ignore[misc]

    assert detail["status"] == "partial"

    async with database_app.state.test_session_factory() as session:
        traces = list(
            await session.scalars(
                select(AgentTraceArtifact).where(
                    AgentTraceArtifact.analysis_run_id == UUID(analysis_id)
                )
            )
        )

    assert len(traces) == 1
    assert traces[0].retention_until is not None
    assert traces[0].access_scope == "owner_only"
    assert traces[0].object_key


# ------------------------------------------------------------------ terminal


async def test_a_terminal_run_is_never_processed_twice(database_app: FastAPI) -> None:
    """Duplicate Celery delivery must be a no-op, not a second run."""
    use_evidence_provider(database_app, FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES))
    use_oasis_adapter(database_app, FakeOasisAdapter())

    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await create_analysis(database_app, owner)
        first = (await owner.get(f"/v1/analyses/{analysis_id}")).json()

        from tests.support.api import run_worker

        # Redelivery of the same task, twice.
        await run_worker(database_app, analysis_id)
        await run_worker(database_app, analysis_id)

        second = (await owner.get(f"/v1/analyses/{analysis_id}")).json()

    assert first["status"] == second["status"] == "completed"
    assert first["completed_at"] == second["completed_at"]

    async with database_app.state.test_session_factory() as session:
        runs = list(
            await session.scalars(
                select(AgentRun).where(AgentRun.analysis_run_id == UUID(analysis_id))
            )
        )
        traces = list(await session.scalars(select(AgentTraceArtifact)))
        stored = await session.scalar(
            select(AnalysisRun).where(AnalysisRun.id == UUID(analysis_id))
        )

    # One set of everything, not three.
    assert len(runs) == 4
    assert len(traces) == 1
    assert stored is not None and stored.status == "completed"
