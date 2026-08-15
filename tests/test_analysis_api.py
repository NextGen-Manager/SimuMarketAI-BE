"""Analysis pipeline through the API: state machine, tenancy, and report shape."""

from __future__ import annotations

from uuid import UUID

from fastapi import FastAPI
from sqlalchemy import select

from app.persistence.models import AnalysisRun, EvidenceItem, InputSnapshot
from tests.support.api import (
    analysis_payload,
    client,
    complete_required_education,
    create_business,
    join_as_cashier,
    register,
    use_evidence_provider,
)
from tests.support.evidence import COMPLETE_FIXTURE_VALUES, FixtureEvidenceProvider


async def test_run_completes_without_any_llm_and_reports_partial_honestly(
    database_app: FastAPI,
) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)

        created = await owner.post("/v1/analyses", json=analysis_payload())
        assert created.status_code == 202, created.text
        analysis_id = created.json()["analysis_id"]
        assert created.json()["status_url"] == f"/v1/analyses/{analysis_id}"
        assert created.json()["events_url"] == f"/v1/analyses/{analysis_id}/events"

        events = await owner.get(created.json()["events_url"])
        assert events.status_code == 200
        assert events.headers["content-type"].startswith("text/event-stream")
        assert "event: status" in events.text

        detail = await owner.get(f"/v1/analyses/{analysis_id}")
        report = await owner.get(f"/v1/analyses/{analysis_id}/report")

        body = detail.json()
        assert body["status"] == "partial"
        assert body["progress"]["current_stage"] == "validating_report"
        assert body["progress"]["percent"] == 100
        assert body["progress"]["skipped_stages"] == ["simulating"]
        assert "simulating" not in body["progress"]["completed_stages"]
        assert body["correlation_id"]
        assert body["failure_code"] is None

        codes = {warning["code"] for warning in body["warnings"]}
        assert "simulation_skipped" in codes
        assert "evidence_missing" in codes
        assert "score_unavailable" in codes

        payload = report.json()
        assert report.status_code == 200
        assert payload["status"] == "partial"
        assert payload["rule_version"] == "lrs-v0.2-unvalidated"
        assert payload["readiness"]["status"] == "unavailable"
        assert payload["readiness"]["score"] is None
        assert payload["readiness"]["validation_status"] == "unvalidated"
        assert payload["evidence_confidence"]["label"] == "tidak_tersedia"
        assert payload["synthetic_simulation"]["status"] == "unavailable"
        assert payload["synthetic_simulation"]["reason"]
        assert payload["finance"]["bep_units_month"] == 715
        assert payload["missing_evidence"]
        assert payload["limitations"]
        assert (
            payload["disclaimer"]
            == "Hasil adalah alat bantu keputusan, bukan jaminan keberhasilan usaha."
        )


async def test_complete_evidence_produces_a_completed_run_with_a_score(
    database_app: FastAPI,
) -> None:
    use_evidence_provider(database_app, FixtureEvidenceProvider(COMPLETE_FIXTURE_VALUES))
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)

        created = await owner.post("/v1/analyses", json=analysis_payload())
        analysis_id = created.json()["analysis_id"]
        detail = await owner.get(f"/v1/analyses/{analysis_id}")
        report = await owner.get(f"/v1/analyses/{analysis_id}/report")

        assert detail.json()["status"] == "completed"
        assert detail.json()["score"] == 78
        assert detail.json()["interpretation"] == "Layak dengan mitigasi"
        assert report.json()["readiness"]["score"] == 78
        assert report.json()["evidence"][0]["source"] == "fixture"
        assert report.json()["evidence"][0]["observed_at"]

        async with database_app.state.test_session_factory() as session:
            stored = list(
                await session.scalars(
                    select(EvidenceItem).where(EvidenceItem.analysis_run_id == UUID(analysis_id))
                )
            )
        assert {item.metric for item in stored} == set(COMPLETE_FIXTURE_VALUES)


async def test_input_snapshot_is_frozen_at_creation(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        created = await owner.post("/v1/analyses", json=analysis_payload())
        analysis_id = created.json()["analysis_id"]

        report = await owner.get(f"/v1/analyses/{analysis_id}/report")
        async with database_app.state.test_session_factory() as session:
            run = await session.scalar(
                select(AnalysisRun).where(AnalysisRun.id == UUID(analysis_id))
            )
            assert run is not None and run.input_snapshot_id is not None
            snapshot = await session.scalar(
                select(InputSnapshot).where(InputSnapshot.id == run.input_snapshot_id)
            )

        assert snapshot is not None
        assert snapshot.payload["concept_name"] == "Rice Bowl Sambal"
        assert snapshot.payload == report.json()["input_snapshot"]


async def test_idempotency_key_returns_the_same_run(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        headers = {"Idempotency-Key": "draft-8f2c"}

        first = await owner.post("/v1/analyses", json=analysis_payload(), headers=headers)
        second = await owner.post("/v1/analyses", json=analysis_payload(), headers=headers)
        listing = await owner.get("/v1/analyses")

        assert first.json()["analysis_id"] == second.json()["analysis_id"]
        assert len(listing.json()) == 1


async def test_idempotency_key_rejects_a_different_input(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        headers = {"Idempotency-Key": "draft-8f2c"}

        first = await owner.post("/v1/analyses", json=analysis_payload(), headers=headers)
        changed = await owner.post(
            "/v1/analyses",
            json=analysis_payload(concept_name="Konsep berbeda"),
            headers=headers,
        )

        assert first.status_code == 202
        assert changed.status_code == 409
        assert changed.json()["error"]["code"] == "CONFLICT"


async def test_history_is_scoped_to_the_owner(database_app: FastAPI) -> None:
    async with client(database_app) as first, client(database_app) as second:
        await register(first, "first@example.com", "Pemilik Satu")
        await register(second, "second@example.com", "Pemilik Dua")
        await complete_required_education(database_app, first)

        created = await first.post("/v1/analyses", json=analysis_payload())
        analysis_id = created.json()["analysis_id"]

        assert (await second.get("/v1/analyses")).json() == []
        assert (await second.get(f"/v1/analyses/{analysis_id}")).status_code == 404
        assert (await second.get(f"/v1/analyses/{analysis_id}/report")).status_code == 404


async def test_cashier_cannot_reach_analyses(database_app: FastAPI) -> None:
    async with client(database_app) as owner, client(database_app) as cashier:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        business_id = await create_business(owner)
        created = await owner.post("/v1/analyses", json=analysis_payload())
        analysis_id = created.json()["analysis_id"]

        await register(cashier, "cashier@example.com", "Kasir")
        await join_as_cashier(owner, cashier, business_id)

        assert (await cashier.get("/v1/analyses")).status_code == 404
        assert (await cashier.get(f"/v1/analyses/{analysis_id}")).status_code == 404
        assert (await cashier.get(f"/v1/analyses/{analysis_id}/report")).status_code == 404
        assert (await cashier.post("/v1/analyses", json=analysis_payload())).status_code == 404


async def test_unauthenticated_requests_are_rejected(database_app: FastAPI) -> None:
    async with client(database_app) as anonymous:
        assert (await anonymous.get("/v1/analyses")).status_code == 401
        assert (await anonymous.post("/v1/analyses", json=analysis_payload())).status_code == 401


async def test_invalid_input_is_rejected_before_a_run_is_created(
    database_app: FastAPI,
) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        payload = analysis_payload()
        payload["operations"] = {
            **payload["operations"],
            "volume_units_day": {"min": 60, "base": 10, "max": 20},
        }

        response = await owner.post("/v1/analyses", json=payload)

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_FAILED"
        assert (await owner.get("/v1/analyses")).json() == []


async def test_money_in_the_report_is_always_an_integer(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        created = await owner.post("/v1/analyses", json=analysis_payload())
        report = await owner.get(f"/v1/analyses/{created.json()['analysis_id']}/report")

        offenders: list[str] = []

        def walk(node: object, path: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    walk(value, f"{path}[{index}]")
            elif path.endswith("_idr") and not isinstance(node, int) and node is not None:
                offenders.append(path)

        walk(report.json(), "report")
        assert not offenders, offenders


async def test_report_is_not_available_before_a_run_exists(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await complete_required_education(database_app, owner)
        missing = "8ff7d369-924a-4d6e-ac0e-4c94aa868d0a"

        assert (await owner.get(f"/v1/analyses/{missing}")).status_code == 404
        assert (await owner.get(f"/v1/analyses/{missing}/report")).status_code == 404
