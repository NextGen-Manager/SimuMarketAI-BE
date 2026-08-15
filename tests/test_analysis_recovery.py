"""Recovery for runs that no worker is executing.

Two failures used to leave a run stuck forever, and neither needs a provider to
reproduce:

- the API committed a run and the broker rejected the dispatch, so nothing was
  ever queued;
- a worker claimed a run and died, and Celery's redelivery found nothing to
  claim because the run was no longer `queued`.

docs/11 requires that both are found in PostgreSQL and requeued idempotently.
These tests drive the reconciler directly, so "recoverable" is a property that
is checked rather than assumed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.core.config import Settings
from app.domain.analysis_state import WORKER_LOST_FAILURE_CODE
from app.integrations.oasis.fake import FakeOasisAdapter
from app.persistence.models import (
    AgentRun,
    AnalysisEventRecord,
    AnalysisReportRecord,
    AnalysisRun,
    EvidenceItem,
)
from app.repositories.analysis_worker import AnalysisWorkerRepository
from app.services.analysis_queue import RecordingDispatcher
from app.services.analysis_recovery import recover_stuck_runs
from tests.support.api import (
    analysis_payload,
    client,
    complete_required_education,
    register,
    run_worker,
    use_evidence_provider,
    use_oasis_adapter,
)
from tests.support.evidence import COMPLETE_FIXTURE_VALUES, FixtureEvidenceProvider


def test_recovery_lease_must_outlive_the_celery_hard_timeout() -> None:
    with pytest.raises(ValueError, match="CELERY_ANALYSIS_TIME_LIMIT_SECONDS"):
        Settings(
            environment="test",
            jwt_secret="test-secret-with-at-least-thirty-two-characters",
            celery_analysis_time_limit_seconds=900,
            analysis_lease_seconds=900,
        )


async def _queue_analysis(app: FastAPI, owner: Any) -> str:
    response = await owner.post("/v1/analyses", json=analysis_payload())
    assert response.status_code == 202, response.text
    return str(response.json()["analysis_id"])


async def _load(app: FastAPI, analysis_id: str) -> AnalysisRun:
    async with app.state.test_session_factory() as session:
        run = await session.get(AnalysisRun, UUID(analysis_id))
        assert run is not None
        return run


async def _age_run(app: FastAPI, analysis_id: str, **values: Any) -> None:
    async with app.state.test_session_factory() as session:
        run = await session.get(AnalysisRun, UUID(analysis_id))
        assert run is not None
        for field, value in values.items():
            setattr(run, field, value)
        await session.commit()


async def _recover(app: FastAPI) -> Any:
    dispatcher = RecordingDispatcher()
    async with app.state.test_session_factory() as session:
        report = await recover_stuck_runs(
            session,
            settings=app.state.test_settings,
            dispatcher=dispatcher,
            publisher=app.state.test_publisher,
        )
    return report, dispatcher


# ------------------------------------------------------- dispatch never landed


async def test_a_run_whose_dispatch_failed_is_still_accepted_and_recovered(
    database_app: FastAPI, monkeypatch: Any
) -> None:
    """A broker outage must not lose a run that is already durable.

    The run is committed before dispatch, so failing the request would report an
    error for work that is in fact queued. It stays `queued` with no lease and
    the reconciler picks it up.
    """

    def explode(analysis_id: UUID, correlation_id: UUID) -> None:
        raise ConnectionError("broker unreachable")

    monkeypatch.setattr(database_app.state.test_dispatcher, "dispatch", explode)

    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        response = await owner.post("/v1/analyses", json=analysis_payload())

    assert response.status_code == 202, response.text
    analysis_id = str(response.json()["analysis_id"])

    run = await _load(database_app, analysis_id)
    assert run.status == "queued"
    assert run.lease_expires_at is None

    # Inside the grace period nothing is touched: a run queued a second ago is
    # not stuck, it is new.
    report, dispatcher = await _recover(database_app)
    assert report.total == 0
    assert dispatcher.dispatched == []

    grace = database_app.state.test_settings.analysis_queue_grace_seconds
    await _age_run(
        database_app,
        analysis_id,
        created_at=datetime.now(UTC) - timedelta(seconds=grace + 60),
    )

    report, dispatcher = await _recover(database_app)
    assert report.requeued == [analysis_id]
    assert [str(item[0]) for item in dispatcher.dispatched] == [analysis_id]


# ------------------------------------------------------------- worker crashed


async def test_a_crashed_worker_leaves_a_run_that_the_reconciler_requeues(
    database_app: FastAPI,
) -> None:
    """A claimed run whose lease lapsed is being executed by nobody."""
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await _queue_analysis(database_app, owner)

    # The worker claimed the run and then died mid-stage.
    await _age_run(
        database_app,
        analysis_id,
        status="simulating",
        current_stage="simulating",
        completed_stages=["queued", "collecting_evidence", "building_context"],
        attempt_count=1,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    report, dispatcher = await _recover(database_app)

    assert report.requeued == [analysis_id]
    run = await _load(database_app, analysis_id)
    assert run.status == "queued"
    assert run.current_stage == "queued"
    # While queued this is the deadline for the recovery dispatch to be claimed.
    # If that dispatch is lost too, the next reconciliation may try again after
    # the deadline instead of redispatching on every beat tick.
    assert run.lease_expires_at is not None
    # SQLite drops timezone metadata while PostgreSQL preserves it.
    deadline = run.lease_expires_at
    comparison_now = (
        datetime.now(UTC) if deadline.tzinfo is not None else datetime.now(UTC).replace(tzinfo=None)
    )
    assert deadline > comparison_now
    assert [str(item[0]) for item in dispatcher.dispatched] == [analysis_id]


async def test_a_live_lease_is_left_alone(database_app: FastAPI) -> None:
    """A healthy long-running simulation must not be restarted underneath itself."""
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await _queue_analysis(database_app, owner)

    await _age_run(
        database_app,
        analysis_id,
        status="simulating",
        current_stage="simulating",
        attempt_count=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=4),
    )

    report, dispatcher = await _recover(database_app)

    assert report.total == 0
    assert dispatcher.dispatched == []
    assert (await _load(database_app, analysis_id)).status == "simulating"


async def test_a_terminal_run_is_never_requeued(database_app: FastAPI) -> None:
    use_evidence_provider(database_app, FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES))
    use_oasis_adapter(database_app, FakeOasisAdapter())

    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await _queue_analysis(database_app, owner)
        await run_worker(database_app, analysis_id)

    run = await _load(database_app, analysis_id)
    assert run.status in {"completed", "partial"}
    # A finished run releases its lease, so it cannot look stuck.
    assert run.lease_expires_at is None

    report, dispatcher = await _recover(database_app)
    assert report.total == 0
    assert dispatcher.dispatched == []


# ------------------------------------------------------------- discarded work


async def test_requeueing_discards_the_dead_attempt_rows(database_app: FastAPI) -> None:
    """A rerun must not append a second evidence set to the same analysis."""
    use_evidence_provider(database_app, FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES))
    use_oasis_adapter(database_app, FakeOasisAdapter())

    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await _queue_analysis(database_app, owner)
        await run_worker(database_app, analysis_id)

    async with database_app.state.test_session_factory() as session:
        before = len(
            list(
                await session.scalars(
                    select(EvidenceItem).where(EvidenceItem.analysis_run_id == UUID(analysis_id))
                )
            )
        )
    assert before > 0

    # Pretend the run never finished and its worker died.
    await _age_run(
        database_app,
        analysis_id,
        status="scoring",
        current_stage="scoring",
        completed_at=None,
        attempt_count=1,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    await _recover(database_app)
    await run_worker(database_app, analysis_id)

    async with database_app.state.test_session_factory() as session:
        evidence = list(
            await session.scalars(
                select(EvidenceItem).where(EvidenceItem.analysis_run_id == UUID(analysis_id))
            )
        )
        reports = list(
            await session.scalars(
                select(AnalysisReportRecord).where(
                    AnalysisReportRecord.analysis_run_id == UUID(analysis_id)
                )
            )
        )
        agent_runs = list(
            await session.scalars(
                select(AgentRun).where(AgentRun.analysis_run_id == UUID(analysis_id))
            )
        )

    # Exactly one attempt's worth of rows, not two.
    assert len(evidence) == before
    assert len(reports) == 1
    assert len(agent_runs) == 4


# --------------------------------------------------------------- give-up path


async def test_a_run_that_keeps_dying_fails_instead_of_looping_forever(
    database_app: FastAPI,
) -> None:
    """An endless retry loop would hide a defect behind a spinner."""
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await _queue_analysis(database_app, owner)

    attempts = database_app.state.test_settings.analysis_max_attempts
    await _age_run(
        database_app,
        analysis_id,
        status="simulating",
        current_stage="simulating",
        attempt_count=attempts,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    report, dispatcher = await _recover(database_app)

    assert report.failed == [analysis_id]
    assert dispatcher.dispatched == []
    run = await _load(database_app, analysis_id)
    assert run.status == "failed"
    assert run.failure_code == WORKER_LOST_FAILURE_CODE
    assert run.completed_at is not None
    assert any(warning["code"] == "worker_lost" for warning in run.warnings)

    async with database_app.state.test_session_factory() as session:
        events = list(
            await session.scalars(
                select(AnalysisEventRecord)
                .where(AnalysisEventRecord.analysis_run_id == UUID(analysis_id))
                .order_by(AnalysisEventRecord.sequence)
            )
        )
    # The client learns why, over the same stream it was already watching.
    assert events[-1].status == "failed"
    assert events[-1].failure_code == WORKER_LOST_FAILURE_CODE


async def test_recovery_is_idempotent_when_run_twice(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await _queue_analysis(database_app, owner)

    await _age_run(
        database_app,
        analysis_id,
        status="collecting_evidence",
        current_stage="collecting_evidence",
        attempt_count=1,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )

    first, first_dispatcher = await _recover(database_app)
    second, second_dispatcher = await _recover(database_app)

    assert first.requeued == [analysis_id]
    assert len(first_dispatcher.dispatched) == 1
    # Back in `queued` with a fresh dispatch deadline, the run is inside the
    # grace period again and is not queued a second time.
    assert second.total == 0
    assert second_dispatcher.dispatched == []


async def test_requeue_loses_safely_when_run_becomes_terminal(database_app: FastAPI) -> None:
    """A stale recovery snapshot must never delete a report that just finished."""
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await _queue_analysis(database_app, owner)

    expired = datetime.now(UTC) - timedelta(minutes=5)
    await _age_run(
        database_app,
        analysis_id,
        status="simulating",
        current_stage="simulating",
        attempt_count=1,
        lease_expires_at=expired,
    )
    stale = await _load(database_app, analysis_id)

    # The worker finishes after the recovery scan but before its conditional
    # transition. This is the race that used to delete attempt rows first.
    await _age_run(
        database_app,
        analysis_id,
        status="completed",
        completed_at=datetime.now(UTC),
        lease_expires_at=None,
    )

    async with database_app.state.test_session_factory() as session:
        repository = AnalysisWorkerRepository(session)
        requeued = await repository.requeue(
            stale,
            queue_grace_seconds=database_app.state.test_settings.analysis_queue_grace_seconds,
        )

    assert requeued is False
    assert (await _load(database_app, analysis_id)).status == "completed"


async def test_recovery_fences_the_previous_worker_attempt(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await _queue_analysis(database_app, owner)

    await _age_run(
        database_app,
        analysis_id,
        status="simulating",
        current_stage="simulating",
        attempt_count=1,
        lease_expires_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    await _recover(database_app)

    async with database_app.state.test_session_factory() as session:
        repository = AnalysisWorkerRepository(session)
        old_worker_still_owns_run = await repository.renew_lease(
            UUID(analysis_id),
            attempt_count=1,
            lease_seconds=database_app.state.test_settings.analysis_lease_seconds,
        )
        claimed = await repository.claim(
            UUID(analysis_id),
            first_stage="collecting_evidence",
            lease_seconds=database_app.state.test_settings.analysis_lease_seconds,
            max_attempts=database_app.state.test_settings.analysis_max_attempts,
        )

    assert old_worker_still_owns_run is False
    assert claimed is not None
    assert claimed.attempt_count == 2


# --------------------------------------------------------------- lease upkeep


async def test_a_running_pipeline_renews_its_lease_and_clears_it_at_the_end(
    database_app: FastAPI,
) -> None:
    use_evidence_provider(database_app, FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES))
    use_oasis_adapter(database_app, FakeOasisAdapter())

    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await _queue_analysis(database_app, owner)

    before = await _load(database_app, analysis_id)
    assert before.attempt_count == 0
    assert before.lease_expires_at is None

    await run_worker(database_app, analysis_id)

    after = await _load(database_app, analysis_id)
    assert after.attempt_count == 1
    assert after.lease_expires_at is None
    assert after.status in {"completed", "partial"}
