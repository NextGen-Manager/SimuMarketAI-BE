"""Queueing, idempotency under concurrency, and the Celery task itself."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from sqlalchemy import select

from app.core.config import Settings
from app.persistence.models import AnalysisReportRecord, AnalysisRun, EvidenceItem
from app.repositories.analysis import AnalysisRepository
from app.workers.celery_app import create_celery_app
from tests.support.api import (
    analysis_payload,
    client,
    complete_required_education,
    register,
    run_worker,
)


async def test_repeated_requests_with_one_key_execute_one_run(
    database_app: FastAPI,
) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        headers = {"Idempotency-Key": "draft-repeat"}

        responses = [
            await owner.post("/v1/analyses", json=analysis_payload(), headers=headers)
            for _ in range(4)
        ]
        listing = await owner.get("/v1/analyses")

    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()["analysis_id"] for response in responses}) == 1
    assert len(listing.json()) == 1
    # Only the request that actually created the run is allowed to queue work.
    assert len(database_app.state.test_dispatcher.dispatched) == 1


async def test_a_lost_idempotency_race_returns_the_winning_run(
    database_app: FastAPI, monkeypatch: Any
) -> None:
    """The unique constraint, not the lookup, is what makes the key safe.

    Two concurrent callers can both miss the lookup, because the winner has not
    committed yet when the loser reads. Only one insert then survives
    `uq_analysis_user_idempotency`, and the loser must recover by returning the
    winning run rather than surfacing a database error.

    Tests here run on in-memory SQLite, where every session shares one
    connection and a genuine `asyncio.gather` race cannot be reproduced
    faithfully. So the losing side is reproduced exactly: the lookup is forced
    to miss once, which drops the second request into the constraint.
    """
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        headers = {"Idempotency-Key": "draft-race"}

        winner = await owner.post("/v1/analyses", json=analysis_payload(), headers=headers)
        assert winner.status_code == 202

        original = AnalysisRepository.find_by_idempotency_key
        missed = {"once": False}

        async def miss_once(self: AnalysisRepository, key: str) -> Any:
            if not missed["once"]:
                missed["once"] = True
                return None
            return await original(self, key)

        monkeypatch.setattr(AnalysisRepository, "find_by_idempotency_key", miss_once)

        loser = await owner.post("/v1/analyses", json=analysis_payload(), headers=headers)
        listing = await owner.get("/v1/analyses")

    assert missed["once"] is True
    assert loser.status_code == 202, loser.text
    assert loser.json()["analysis_id"] == winner.json()["analysis_id"]
    assert len(listing.json()) == 1
    # The loser recovered the existing run and did not queue a second execution.
    assert len(database_app.state.test_dispatcher.dispatched) == 1


async def test_duplicate_delivery_does_not_duplicate_evidence_or_reports(
    database_app: FastAPI,
) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        created = await owner.post("/v1/analyses", json=analysis_payload())
        analysis_id = created.json()["analysis_id"]

        # The broker redelivers the same task three times.
        await asyncio.gather(
            run_worker(database_app, analysis_id),
            run_worker(database_app, analysis_id),
            run_worker(database_app, analysis_id),
        )

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
        run = await session.scalar(select(AnalysisRun).where(AnalysisRun.id == UUID(analysis_id)))

    assert len(reports) == 1
    assert run is not None and run.status == "partial"
    # The unavailable runtime provider returns no records; the point is that
    # three deliveries did not write three copies of whatever there was.
    assert len(evidence) == len({item.metric for item in evidence})


async def test_a_worker_for_an_unknown_run_is_a_no_op(database_app: FastAPI) -> None:
    await run_worker(database_app, str(uuid4()))

    async with database_app.state.test_session_factory() as session:
        runs = list(await session.scalars(select(AnalysisRun)))
    assert runs == []


def test_celery_task_is_registered_and_needs_no_broker() -> None:
    """The task can be constructed and inspected without touching a network.

    Building the app opens no connection; Celery connects lazily on first
    publish. This is what lets CI verify the wiring without a running Redis.
    """
    settings = Settings(
        environment="test",
        jwt_secret="test-secret-with-at-least-thirty-two-characters",
        celery_task_always_eager=True,
        redis_url="redis://localhost:6379/0",
    )
    assert settings.broker_url == "redis://localhost:6379/0"
    assert settings.result_backend_url == "redis://localhost:6379/0"

    app = create_celery_app()
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.accept_content == ["json"]

    from app.workers.analysis import run_analysis

    assert run_analysis.name == "analysis.run"
    assert run_analysis.max_retries == 3
