from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.persistence.models import AnalysisRun, User


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def _register(client: AsyncClient, email: str, name: str) -> None:
    response = await client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "display_name": name,
            "password": "kata-sandi-yang-aman",
        },
    )
    assert response.status_code == 201


async def _business(client: AsyncClient) -> str:
    response = await client.post(
        "/v1/businesses",
        json={"name": "Kopi Tebet", "location_name": "Tebet, Jakarta Selatan"},
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _product(client: AsyncClient, business_id: str, name: str = "Kopi Susu") -> dict:
    response = await client.post(
        "/v1/products",
        json={
            "business_id": business_id,
            "name": name,
            "selling_price_idr": 18_000,
            "hpp_idr": 9_000,
        },
    )
    assert response.status_code == 201
    return response.json()


async def test_cashier_product_payload_excludes_cost_and_analytics_is_hidden(
    database_app: FastAPI,
) -> None:
    async with _client(database_app) as owner, _client(database_app) as cashier:
        await _register(owner, "owner@example.com", "Pemilik")
        business_id = await _business(owner)
        await _product(owner, business_id)
        invite = await owner.post(f"/v1/businesses/{business_id}/invites")

        await _register(cashier, "cashier@example.com", "Kasir")
        await cashier.post("/v1/invites/redeem", json={"code": invite.json()["code"]})

        products = await cashier.get("/v1/products", params={"business_id": business_id})
        raw_product = products.json()[0]
        assert products.status_code == 200
        assert raw_product["selling_price_idr"] == 18_000
        assert "hpp_idr" not in raw_product
        assert "margin_idr" not in raw_product

        analytics = await cashier.get(
            "/v1/transaction-analytics", params={"business_id": business_id}
        )
        assert analytics.status_code == 404


async def test_transaction_total_is_calculated_and_client_reference_is_idempotent(
    database_app: FastAPI,
) -> None:
    async with _client(database_app) as owner:
        await _register(owner, "owner@example.com", "Pemilik")
        business_id = await _business(owner)
        product = await _product(owner, business_id)
        payload = {
            "business_id": business_id,
            "occurred_at": "2026-08-14T05:00:00Z",
            "channel": "takeaway",
            "client_reference": "device-transaction-001",
            "items": [
                {
                    "product_id": product["id"],
                    "quantity": 3,
                    "unit_price_idr": 17_000,
                }
            ],
        }

        first = await owner.post("/v1/transactions", json=payload)
        second = await owner.post("/v1/transactions", json=payload)

        assert first.status_code == 201
        assert first.json()["gross_total_idr"] == 51_000
        assert first.json()["items"][0]["line_total_idr"] == 51_000
        assert second.json()["id"] == first.json()["id"]


async def test_analytics_gate_and_dashboard_states_are_backend_owned(
    database_app: FastAPI,
) -> None:
    async with _client(database_app) as owner:
        await _register(owner, "owner@example.com", "Pemilik")
        empty_dashboard = await owner.get("/v1/dashboard")
        assert empty_dashboard.json()["keadaan"] == "belum_ada_data"

        async with database_app.state.test_session_factory() as session:
            user = await session.scalar(select(User).where(User.email == "owner@example.com"))
            assert user is not None
            session.add(
                AnalysisRun(
                    id=uuid4(),
                    user_id=user.id,
                    status="completed",
                    concept_name="Kedai Analisis",
                    area_name="Tebet",
                    score=68,
                    interpretation="Layak dengan mitigasi",
                    rule_version="lrs-v0.2-unvalidated",
                )
            )
            await session.commit()

        analyzed_dashboard = await owner.get("/v1/dashboard")
        assert analyzed_dashboard.json()["keadaan"] == "sudah_menganalisis"

        business_id = await _business(owner)
        product = await _product(owner, business_id)
        collecting_dashboard = await owner.get("/v1/dashboard")
        assert collecting_dashboard.json()["keadaan"] == "usaha_berjalan_data_kurang"

        start = datetime(2026, 8, 14, 5, tzinfo=UTC)
        for offset in range(7):
            occurred_at = start + timedelta(days=offset)
            response = await owner.post(
                "/v1/transactions",
                json={
                    "business_id": business_id,
                    "occurred_at": occurred_at.isoformat(),
                    "channel": "takeaway",
                    "client_reference": f"day-{offset}",
                    "items": [
                        {
                            "product_id": product["id"],
                            "quantity": 1,
                            "unit_price_idr": 18_000,
                        }
                    ],
                },
            )
            assert response.status_code == 201

        analytics = await owner.get(
            "/v1/transaction-analytics", params={"business_id": business_id}
        )
        ready_dashboard = await owner.get("/v1/dashboard")

        assert analytics.status_code == 200
        assert analytics.json()["status"] == "available"
        assert analytics.json()["days_recorded"] == 7
        assert ready_dashboard.json()["keadaan"] == "usaha_berjalan_data_cukup"
