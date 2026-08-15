"""Small helpers shared by the API tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.domain.agents import OasisAdapter
from app.domain.evidence import EvidenceProvider
from app.persistence.models import EducationModule, EducationQuestion
from app.workers.analysis import execute_analysis

PASSWORD = "kata-sandi-yang-aman"


def client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def register(async_client: AsyncClient, email: str, name: str) -> None:
    response = await async_client.post(
        "/v1/auth/register",
        json={"email": email, "display_name": name, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text


async def create_business(async_client: AsyncClient, name: str = "Kopi Tebet") -> str:
    response = await async_client.post(
        "/v1/businesses",
        json={"name": name, "location_name": "Tebet, Jakarta Selatan"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def join_as_cashier(owner: AsyncClient, cashier: AsyncClient, business_id: str) -> None:
    invite = await owner.post(f"/v1/businesses/{business_id}/invites")
    assert invite.status_code == 201, invite.text
    redeem = await cashier.post("/v1/invites/redeem", json={"code": invite.json()["code"]})
    assert redeem.status_code == 200, redeem.text


def use_evidence_provider(app: FastAPI, provider: EvidenceProvider) -> None:
    """Point the worker at a test provider.

    Production wiring is untouched: `select_evidence_provider` still refuses a
    fixture outside development and test.
    """
    app.state.test_evidence_provider = provider


def use_oasis_adapter(app: FastAPI, adapter: OasisAdapter) -> None:
    """Point the worker at a test adapter.

    As with evidence, `select_oasis_adapter` still refuses a fake outside
    development and test, so this only reaches the injected instance.
    """
    app.state.test_oasis_adapter = adapter


async def run_worker(app: FastAPI, analysis_id: str) -> None:
    """Execute the queued run the way the Celery task would.

    Tests call this explicitly instead of relying on an eager broker, which is
    what makes "the API returned before the work happened" observable: between
    the POST and this call the run is still `queued`.
    """
    await execute_analysis(
        UUID(analysis_id),
        session_factory=app.state.test_session_factory,
        settings=app.state.test_settings,
        evidence_provider=app.state.test_evidence_provider,
        oasis_adapter=app.state.test_oasis_adapter,
        publisher=app.state.test_publisher,
    )


async def create_analysis(app: FastAPI, async_client: AsyncClient, /, **overrides: Any) -> str:
    """POST an analysis and run its worker. Returns the analysis ID."""
    headers = overrides.pop("headers", None)
    response = await async_client.post(
        "/v1/analyses", json=analysis_payload(**overrides), headers=headers
    )
    assert response.status_code == 202, response.text
    analysis_id = str(response.json()["analysis_id"])
    await run_worker(app, analysis_id)
    return analysis_id


async def seed_education_module(
    app: FastAPI,
    *,
    slug: str = "dasar-hpp",
    title: str = "Menghitung HPP dan marjin",
    content_version: str = "v1",
    business_types: list[str] | None = None,
    published: bool = True,
    is_required: bool = True,
    passing_score_percent: int = 70,
    questions: int = 2,
) -> tuple[UUID, list[int]]:
    """Insert a module for tests. Returns its ID and the correct answer keys."""
    module_id = uuid4()
    answers = [index % 3 for index in range(questions)]
    now = datetime.now(UTC)
    async with app.state.test_session_factory() as session:
        session.add(
            EducationModule(
                id=module_id,
                slug=slug,
                title=title,
                summary="Ringkasan modul uji.",
                topic="finansial",
                body="Isi modul uji.",
                content_version=content_version,
                business_types=business_types if business_types is not None else ["food_stall"],
                estimated_minutes=8,
                passing_score_percent=passing_score_percent,
                is_required=is_required,
                reviewed_at=now,
                published_at=now if published else None,
                position=0,
                created_at=now,
                updated_at=now,
            )
        )
        for index in range(questions):
            session.add(
                EducationQuestion(
                    id=uuid4(),
                    module_id=module_id,
                    position=index,
                    prompt=f"Pertanyaan {index + 1}?",
                    options=["Pilihan A", "Pilihan B", "Pilihan C"],
                    correct_index=answers[index],
                    explanation=None,
                )
            )
        await session.commit()
    return module_id, answers


async def complete_required_education(app: FastAPI, async_client: AsyncClient) -> None:
    """Publish and complete a unique prerequisite for an analysis API test."""
    module_id, answers = await seed_education_module(
        app,
        slug=f"analysis-prerequisite-{uuid4()}",
    )
    response = await async_client.post(
        f"/v1/education/modules/{module_id}/complete",
        json={"content_version": "v1", "answers": answers},
    )
    assert response.status_code == 200, response.text


def analysis_payload(**overrides: Any) -> dict[str, Any]:
    from tests.support.analysis_payload import GOLDEN_PAYLOAD

    payload = dict(GOLDEN_PAYLOAD)
    payload.update(overrides)
    return payload
