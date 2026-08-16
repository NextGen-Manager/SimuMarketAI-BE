from httpx import AsyncClient

from app.core.security import REFRESH_COOKIE

REGISTER_PAYLOAD = {
    "email": "raka@example.com",
    "display_name": "Raka Pratama",
    "password": "rahasia-yang-aman",
}


async def test_register_sets_http_only_session_and_returns_profile(
    auth_client: AsyncClient,
) -> None:
    response = await auth_client.post("/v1/auth/register", json=REGISTER_PAYLOAD)

    assert response.status_code == 201
    assert response.json()["user"]["email"] == "raka@example.com"
    assert response.json()["memberships"] == []
    cookies = response.headers.get_list("set-cookie")
    assert all("HttpOnly" in cookie for cookie in cookies)
    assert all("SameSite=lax" in cookie for cookie in cookies)

    me = await auth_client.get("/v1/me")
    assert me.status_code == 200
    assert me.json()["user"]["display_name"] == "Raka Pratama"


async def test_login_rejects_wrong_password_without_revealing_account(
    auth_client: AsyncClient,
) -> None:
    await auth_client.post("/v1/auth/register", json=REGISTER_PAYLOAD)
    await auth_client.post("/v1/auth/logout")

    response = await auth_client.post(
        "/v1/auth/login",
        json={"email": REGISTER_PAYLOAD["email"], "password": "salah"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Email atau kata sandi tidak sesuai."


async def test_refresh_token_is_rotated_and_old_token_cannot_be_reused(
    auth_client: AsyncClient,
) -> None:
    await auth_client.post("/v1/auth/register", json=REGISTER_PAYLOAD)
    old_refresh = auth_client.cookies.get(REFRESH_COOKIE)
    assert old_refresh is not None

    refreshed = await auth_client.post("/v1/auth/refresh")
    assert refreshed.status_code == 200
    assert auth_client.cookies.get(REFRESH_COOKIE) != old_refresh

    auth_client.cookies.set(REFRESH_COOKIE, old_refresh, path="/v1/auth")
    reused = await auth_client.post("/v1/auth/refresh")
    assert reused.status_code == 401


async def test_logout_revokes_access_session(auth_client: AsyncClient) -> None:
    await auth_client.post("/v1/auth/register", json=REGISTER_PAYLOAD)

    response = await auth_client.post("/v1/auth/logout")
    me = await auth_client.get("/v1/me")

    assert response.status_code == 200
    assert me.status_code == 401
