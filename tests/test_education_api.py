"""Education modules, versioned progress, knowledge check, and the F-09 gate."""

from __future__ import annotations

from fastapi import FastAPI

from tests.support.api import (
    client,
    create_business,
    join_as_cashier,
    register,
    seed_education_module,
)


async def test_module_list_is_empty_when_no_content_is_published(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await seed_education_module(database_app, published=False)

        response = await owner.get("/v1/education/modules")

        assert response.status_code == 200
        assert response.json() == []


async def test_prerequisites_block_when_no_module_is_published(
    database_app: FastAPI,
) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")

        response = await owner.get(
            "/v1/education/prerequisites", params={"business_type": "food_stall"}
        )

        body = response.json()
        assert response.status_code == 200
        assert body["rule_version"] == "education-gate-v1"
        assert body["satisfied"] is False
        assert body["content_available"] is False
        assert body["required"] == []
        assert body["note"]


async def test_module_detail_never_returns_the_answer_key(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        module_id, _ = await seed_education_module(database_app)

        response = await owner.get(f"/v1/education/modules/{module_id}")

        body = response.json()
        assert response.status_code == 200
        assert body["content_version"] == "v1"
        assert len(body["questions"]) == 2
        for question in body["questions"]:
            assert set(question) == {"id", "position", "prompt", "options"}
        assert "correct_index" not in response.text


async def test_progress_records_the_content_version_it_was_earned_on(
    database_app: FastAPI,
) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        module_id, answers = await seed_education_module(database_app)

        completion = await owner.post(
            f"/v1/education/modules/{module_id}/complete",
            json={"content_version": "v1", "answers": answers},
        )
        listing = await owner.get("/v1/education/modules")

        body = completion.json()
        assert completion.status_code == 200
        assert body["passed"] is True
        assert body["correct_answers"] == 2
        assert body["total_questions"] == 2
        assert body["content_version"] == "v1"
        assert listing.json()[0]["progress"]["content_version"] == "v1"
        assert listing.json()[0]["progress"]["passed"] is True


async def test_failed_knowledge_check_does_not_complete_the_module(
    database_app: FastAPI,
) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        module_id, answers = await seed_education_module(database_app)
        wrong = [(answer + 1) % 3 for answer in answers]

        completion = await owner.post(
            f"/v1/education/modules/{module_id}/complete",
            json={"content_version": "v1", "answers": wrong},
        )

        assert completion.status_code == 200
        assert completion.json()["passed"] is False
        assert completion.json()["completed_at"] is None


async def test_stale_content_version_is_rejected(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        module_id, answers = await seed_education_module(database_app)

        response = await owner.post(
            f"/v1/education/modules/{module_id}/complete",
            json={"content_version": "v0", "answers": answers},
        )

        assert response.status_code == 409


async def test_answer_count_mismatch_is_rejected(database_app: FastAPI) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        module_id, _ = await seed_education_module(database_app)

        response = await owner.post(
            f"/v1/education/modules/{module_id}/complete",
            json={"content_version": "v1", "answers": [0]},
        )

        assert response.status_code == 422


async def test_published_module_without_questions_cannot_complete_the_gate(
    database_app: FastAPI,
) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        module_id, _ = await seed_education_module(database_app, questions=0)

        response = await owner.post(
            f"/v1/education/modules/{module_id}/complete",
            json={"content_version": "v1", "answers": []},
        )

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EDUCATION_CONTENT_INVALID"


async def test_analysis_is_blocked_at_the_api_until_prerequisites_are_met(
    database_app: FastAPI,
) -> None:
    from tests.support.api import analysis_payload

    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        module_id, answers = await seed_education_module(database_app)

        blocked = await owner.post("/v1/analyses", json=analysis_payload())
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "EDUCATION_PREREQUISITE_NOT_MET"
        assert "Menghitung HPP" in blocked.json()["error"]["message"]

        listing = await owner.get("/v1/analyses")
        assert listing.json() == []

        await owner.post(
            f"/v1/education/modules/{module_id}/complete",
            json={"content_version": "v1", "answers": answers},
        )
        allowed = await owner.post("/v1/analyses", json=analysis_payload())
        assert allowed.status_code == 202


async def test_gate_only_requires_modules_mapped_to_the_business_type(
    database_app: FastAPI,
) -> None:
    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")
        await seed_education_module(database_app, business_types=["coffee_shop"])

        response = await owner.get(
            "/v1/education/prerequisites", params={"business_type": "food_stall"}
        )

        assert response.status_code == 200
        assert response.json()["satisfied"] is False
        assert response.json()["content_available"] is False
        assert response.json()["required"] == []


async def test_analysis_is_unavailable_when_prerequisite_content_is_missing(
    database_app: FastAPI,
) -> None:
    from tests.support.api import analysis_payload

    async with client(database_app) as owner:
        await register(owner, "owner@example.com", "Pemilik")

        response = await owner.post("/v1/analyses", json=analysis_payload())

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EDUCATION_CONTENT_UNAVAILABLE"
        assert response.json()["error"]["retryable"] is True


async def test_cashier_cannot_reach_education(database_app: FastAPI) -> None:
    async with client(database_app) as owner, client(database_app) as cashier:
        await register(owner, "owner@example.com", "Pemilik")
        business_id = await create_business(owner)
        module_id, _ = await seed_education_module(database_app)

        await register(cashier, "cashier@example.com", "Kasir")
        await join_as_cashier(owner, cashier, business_id)

        assert (await cashier.get("/v1/education/modules")).status_code == 404
        assert (await cashier.get(f"/v1/education/modules/{module_id}")).status_code == 404
        assert (
            await cashier.get("/v1/education/prerequisites", params={"business_type": "food_stall"})
        ).status_code == 404
        assert (
            await cashier.post(
                f"/v1/education/modules/{module_id}/complete",
                json={"content_version": "v1", "answers": [0, 0]},
            )
        ).status_code == 404
