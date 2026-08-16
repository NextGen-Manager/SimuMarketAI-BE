from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


async def _client(app: FastAPI) -> AsyncClient:
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


async def test_owner_and_cashier_are_scoped_per_business(database_app: FastAPI) -> None:
    async with (
        await _client(database_app) as owner,
        await _client(database_app) as cashier,
        await _client(database_app) as other_owner,
    ):
        await _register(owner, "owner@example.com", "Pemilik Satu")
        created = await owner.post(
            "/v1/businesses",
            json={"name": "Kopi Tebet", "location_name": "Tebet, Jakarta Selatan"},
        )
        assert created.status_code == 201
        business_id = created.json()["id"]

        invite = await owner.post(f"/v1/businesses/{business_id}/invites")
        assert invite.status_code == 201
        assert len(invite.json()["code"]) == 8
        invite_id = invite.json()["id"]
        invite_status = await owner.get(f"/v1/businesses/{business_id}/invites/{invite_id}")
        assert invite_status.status_code == 200
        assert invite_status.json()["status"] == "active"
        assert "code" not in invite_status.json()

        await _register(cashier, "cashier@example.com", "Kasir Satu")
        redeemed = await cashier.post("/v1/invites/redeem", json={"code": invite.json()["code"]})
        assert redeemed.status_code == 200
        assert redeemed.json()["role"] == "cashier"

        redeemed_status = await owner.get(f"/v1/businesses/{business_id}/invites/{invite_id}")
        assert redeemed_status.json()["status"] == "redeemed"
        cashier_invite_status = await cashier.get(
            f"/v1/businesses/{business_id}/invites/{invite_id}"
        )
        assert cashier_invite_status.status_code == 404

        cashier_update = await cashier.put(
            f"/v1/businesses/{business_id}",
            json={"name": "Nama Curian", "location_name": "Lokasi Curian"},
        )
        assert cashier_update.status_code == 404

        await _register(other_owner, "other@example.com", "Pemilik Dua")
        other_business = await other_owner.post(
            "/v1/businesses",
            json={"name": "Warung Depok", "location_name": "Depok"},
        )
        other_business_id = other_business.json()["id"]

        cross_tenant = await owner.put(
            f"/v1/businesses/{other_business_id}",
            json={"name": "Nama Curian", "location_name": "Lokasi Curian"},
        )
        assert cross_tenant.status_code == 404

        me = await cashier.get("/v1/me")
        assert me.json()["memberships"] == [
            {
                "business_id": business_id,
                "business_name": "Kopi Tebet",
                "location_name": "Tebet, Jakarta Selatan",
                "role": "cashier",
            }
        ]


async def test_invite_is_single_use(database_app: FastAPI) -> None:
    async with (
        await _client(database_app) as owner,
        await _client(database_app) as cashier_one,
        await _client(database_app) as cashier_two,
    ):
        await _register(owner, "owner@example.com", "Pemilik")
        business = await owner.post(
            "/v1/businesses",
            json={"name": "Kopi Tebet", "location_name": "Tebet"},
        )
        invite = await owner.post(f"/v1/businesses/{business.json()['id']}/invites")
        code = invite.json()["code"]

        await _register(cashier_one, "one@example.com", "Kasir Satu")
        await _register(cashier_two, "two@example.com", "Kasir Dua")
        first = await cashier_one.post("/v1/invites/redeem", json={"code": code})
        second = await cashier_two.post("/v1/invites/redeem", json={"code": code})

        assert first.status_code == 200
        assert second.status_code == 422
