from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import FastAPI
from sqlalchemy import select

from app.persistence.models import AnalysisReportRecord, AnalysisRun, User
from app.services.exports import DISCLAIMER
from app.workers.artifacts import execute_export
from tests.support.api import client, register


async def test_analysis_pdf_export_is_async_private_and_idempotent(
    database_app: FastAPI,
) -> None:
    analysis_id = uuid4()
    async with client(database_app) as owner:
        await register(owner, "export-owner@example.com", "Pemilik")
        async with database_app.state.test_session_factory() as session:
            user = await session.scalar(
                select(User).where(User.email == "export-owner@example.com")
            )
            assert user is not None
            session.add(
                AnalysisRun(
                    id=analysis_id,
                    user_id=user.id,
                    status="partial",
                    current_stage="partial",
                    concept_name="Kedai Uji",
                    area_name="Tebet",
                    score=68,
                    interpretation="layak_dengan_mitigasi",
                    rule_version="lrs-v0.2-unvalidated",
                )
            )
            session.add(
                AnalysisReportRecord(
                    analysis_run_id=analysis_id,
                    report_version="report-v1",
                    payload={
                        "status": "partial",
                        "readiness": {
                            "score": 68,
                            "rule_version": "lrs-v0.2-unvalidated",
                        },
                        "evidence_confidence": {
                            "label": "rendah",
                            "missing": ["traffic observation"],
                        },
                        "evidence": [],
                        "disclaimer": DISCLAIMER,
                    },
                )
            )
            await session.commit()

        first = await owner.post(
            f"/v1/analyses/{analysis_id}/exports",
            headers={"Idempotency-Key": "analysis-export-test-001"},
            json={"format": "pdf"},
        )
        repeated = await owner.post(
            f"/v1/analyses/{analysis_id}/exports",
            headers={"Idempotency-Key": "analysis-export-test-001"},
            json={"format": "pdf"},
        )
        assert first.status_code == 202, first.text
        assert first.json()["status"] == "queued"
        assert repeated.json()["export_id"] == first.json()["export_id"]

        export_id = first.json()["export_id"]
        await execute_export(
            UUID(export_id),
            session_factory=database_app.state.test_session_factory,
            settings=database_app.state.test_settings,
            storage=database_app.state.test_object_storage,
        )
        ready = await owner.get(f"/v1/exports/{export_id}")
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        download_url = ready.json()["download"]["url"]
        object_key = download_url.removeprefix("memory://download/")
        pdf = await database_app.state.test_object_storage.read(object_key)
        assert pdf.body.startswith(b"%PDF-")

    async with client(database_app) as stranger:
        await register(stranger, "export-stranger@example.com", "Orang Lain")
        hidden = await stranger.get(f"/v1/exports/{export_id}")
        assert hidden.status_code == 404
