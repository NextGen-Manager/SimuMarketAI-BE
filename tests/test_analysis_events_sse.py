"""The live progress stream: event contract, reconnect, isolation, and fallback."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI

from app.domain.analysis_events import EVENT_SCHEMA_VERSION
from app.integrations.oasis.fake import FakeOasisAdapter
from app.services.analysis_events import RedisEventPublisher, channel_for
from tests.support.api import (
    analysis_payload,
    client,
    complete_required_education,
    create_analysis,
    register,
    use_evidence_provider,
    use_oasis_adapter,
)
from tests.support.evidence import COMPLETE_FIXTURE_VALUES, FixtureEvidenceProvider

EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "analysis_id",
    "status",
    "current_stage",
    "completed_stages",
    "skipped_stages",
    "percent",
    "message",
    "warnings",
    "correlation_id",
    "occurred_at",
}


def parse_events(body: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


def event_ids(body: str) -> list[str]:
    return [line[len("id: ") :] for line in body.splitlines() if line.startswith("id: ")]


async def test_stream_carries_every_contract_field_and_ends_at_terminal(
    database_app: FastAPI, tmp_path: Any
) -> None:
    database_app.state.test_settings.oasis_trace_root = str(tmp_path / "traces")
    use_evidence_provider(database_app, FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES))
    use_oasis_adapter(database_app, FakeOasisAdapter())

    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await create_analysis(database_app, owner)

        response = await owner.get(f"/v1/analyses/{analysis_id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"

    events = parse_events(response.text)
    assert events, response.text
    for event in events:
        assert set(event) == EVENT_FIELDS
        assert event["schema_version"] == EVENT_SCHEMA_VERSION
        assert event["analysis_id"] == analysis_id
        assert 0 <= event["percent"] <= 100
        assert event["message"]
        assert event["correlation_id"]

    # Every stage the run executed appears, including simulating.
    stages = [event["current_stage"] for event in events]
    assert "simulating" in stages
    assert stages == sorted(
        stages,
        key=lambda stage: [
            "queued",
            "collecting_evidence",
            "building_context",
            "simulating",
            "calculating_finance",
            "scoring",
            "composing_report",
            "validating_report",
        ].index(stage),
    )

    # The stream stops as soon as the run is terminal.
    assert events[-1]["status"] == "completed"
    assert events[-1]["percent"] == 100

    ids = event_ids(response.text)
    assert ids == [str(index + 1) for index in range(len(ids))]


async def test_last_event_id_resumes_without_replaying(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await create_analysis(database_app, owner)

        full = await owner.get(f"/v1/analyses/{analysis_id}/events")
        all_ids = event_ids(full.text)
        assert len(all_ids) > 2

        resumed = await owner.get(
            f"/v1/analyses/{analysis_id}/events",
            headers={"Last-Event-ID": all_ids[1]},
        )
        # A malformed header must not break the stream.
        garbled = await owner.get(
            f"/v1/analyses/{analysis_id}/events",
            headers={"Last-Event-ID": "not-a-number"},
        )

    assert event_ids(resumed.text) == all_ids[2:]
    assert event_ids(garbled.text) == all_ids


async def test_a_queued_run_streams_its_queued_state(database_app: FastAPI) -> None:
    """A stream opened before the worker starts still says something true."""
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        created = await owner.post("/v1/analyses", json=analysis_payload())
        analysis_id = created.json()["analysis_id"]

        response = await owner.get(f"/v1/analyses/{analysis_id}/events")

    events = parse_events(response.text)
    assert events[0]["status"] == "queued"
    assert events[0]["current_stage"] == "queued"
    # Not terminal, so the stream ran to its timeout and said so.
    assert ": stream-timeout" in response.text


async def test_stream_is_tenant_isolated(database_app: FastAPI) -> None:
    async with client(database_app) as first, client(database_app) as second:
        await register(first, "first@example.com", "Pemilik Satu")
        await register(second, "second@example.com", "Pemilik Dua")
        await complete_required_education(database_app, first)
        analysis_id = await create_analysis(database_app, first)

        other = await second.get(f"/v1/analyses/{analysis_id}/events")
        anonymous_response = await second.get("/v1/analyses/not-a-uuid/events")

    assert other.status_code == 404
    assert other.json()["error"]["code"] == "NOT_FOUND"
    assert anonymous_response.status_code == 422


async def test_stream_reads_postgres_when_redis_is_unavailable(
    database_app: FastAPI,
) -> None:
    """Redis is transport, never the authority on whether a run finished."""

    class BrokenRedis:
        async def publish(self, channel: str, message: str) -> int:
            raise ConnectionError("redis is down")

    database_app.state.test_publisher = RedisEventPublisher(BrokenRedis())  # type: ignore[arg-type]

    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        analysis_id = await create_analysis(database_app, owner)
        response = await owner.get(f"/v1/analyses/{analysis_id}/events")
        detail = await owner.get(f"/v1/analyses/{analysis_id}")

    # The run completed and the whole transition history is still streamable.
    assert detail.json()["status"] == "partial"
    events = parse_events(response.text)
    assert len(events) >= 6
    assert events[-1]["status"] == "partial"


def test_channel_name_is_scoped_per_analysis() -> None:
    from uuid import UUID as _UUID

    analysis_id = _UUID("8ff7d369-924a-4d6e-ac0e-4c94aa868d0a")
    assert channel_for(analysis_id) == f"analysis:events:{analysis_id}"
