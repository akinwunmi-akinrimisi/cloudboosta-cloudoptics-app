import uuid

import pytest
from httpx import AsyncClient


async def create_tenant(client: AsyncClient, headers: dict[str, str], **body: object) -> dict:
    payload = {"name": "Acme Pay", "countryCode": "NG", **body}
    r = await client.post("/v1/tenants", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


async def test_requires_a_bearer_token(client: AsyncClient) -> None:
    r = await client.get("/v1/me")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_rejects_a_forged_token(client: AsyncClient) -> None:
    r = await client.get("/v1/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


async def test_first_request_provisions_the_user(client: AsyncClient, login) -> None:
    headers = await login("ada@example.com")
    r = await client.get("/v1/me", headers=headers)
    assert r.status_code == 200
    me = r.json()
    assert me["email"] == "ada@example.com"
    assert me["preferredCurrencyView"] == "both"
    assert me["memberships"] == []


async def test_create_workspace_uses_country_defaults(client: AsyncClient, login) -> None:
    headers = await login()
    tenant = await create_tenant(client, headers, name="  Lagos Ledger  ")
    assert tenant["name"] == "Lagos Ledger"
    assert tenant["displayCurrency"] == "NGN"
    assert tenant["timeZone"] == "Africa/Lagos"
    assert tenant["role"] == "admin"

    kenya = await create_tenant(client, headers, name="Nairobi", countryCode="KE")
    assert (kenya["displayCurrency"], kenya["timeZone"]) == ("KES", "Africa/Nairobi")

    me = (await client.get("/v1/me", headers=headers)).json()
    assert {m["tenantName"] for m in me["memberships"]} == {"Lagos Ledger", "Nairobi"}


async def test_validation_errors_are_problem_json(client: AsyncClient, login) -> None:
    headers = await login()
    r = await client.post(
        "/v1/tenants",
        json={"name": "X", "countryCode": "ng", "timeZone": "Mars/Base"},
        headers=headers,
    )
    assert r.status_code == 422
    fields = {e["field"] for e in r.json()["errors"]}
    assert {"countryCode", "timeZone"} <= fields


async def test_other_users_cannot_see_a_workspace(client: AsyncClient, login) -> None:
    owner = await login()
    tenant = await create_tenant(client, owner)
    stranger = await login()
    for path in ("", "/members", "/audit-log"):
        r = await client.get(f"/v1/tenants/{tenant['id']}{path}", headers=stranger)
        assert r.status_code == 404, path
    r = await client.get(f"/v1/tenants/{uuid.uuid4()}", headers=stranger)
    assert r.status_code == 404


async def test_roles_are_enforced(client: AsyncClient, login, owner_sql) -> None:
    admin = await login()
    tenant = await create_tenant(client, admin)
    viewer_email = "viewer@example.com"
    viewer = await login(viewer_email)
    await client.get("/v1/me", headers=viewer)  # provisions the user
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) "
        "SELECT %s, id, 'viewer' FROM app_user WHERE email = %s",
        tenant["id"],
        viewer_email,
    )
    r = await client.get(f"/v1/tenants/{tenant['id']}", headers=viewer)
    assert r.status_code == 200 and r.json()["role"] == "viewer"
    r = await client.patch(f"/v1/tenants/{tenant['id']}", json={"name": "x"}, headers=viewer)
    assert r.status_code == 403
    r = await client.get(f"/v1/tenants/{tenant['id']}/audit-log", headers=viewer)
    assert r.status_code == 403
    members = (await client.get(f"/v1/tenants/{tenant['id']}/members", headers=viewer)).json()
    assert sorted(m["role"] for m in members) == ["admin", "viewer"]


async def test_admin_update_is_audited(client: AsyncClient, login) -> None:
    headers = await login()
    tenant = await create_tenant(client, headers)
    r = await client.patch(
        f"/v1/tenants/{tenant['id']}",
        json={"displayCurrency": "USD", "mfaRequired": True},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["displayCurrency"] == "USD" and r.json()["mfaRequired"] is True
    log = (await client.get(f"/v1/tenants/{tenant['id']}/audit-log", headers=headers)).json()
    assert [e["action"] for e in log] == ["tenant.updated", "tenant.created"]
    assert log[0]["before"] == {"display_currency": "NGN", "mfa_required": False}


async def test_patch_me_currency_view(client: AsyncClient, login) -> None:
    headers = await login()
    r = await client.patch("/v1/me", json={"preferredCurrencyView": "local"}, headers=headers)
    assert r.status_code == 200 and r.json()["preferredCurrencyView"] == "local"
    r = await client.patch("/v1/me", json={"preferredCurrencyView": "eur"}, headers=headers)
    assert r.status_code == 422


@pytest.mark.parametrize("env", ["staging", "production"])
def test_dev_auth_is_refused_outside_dev(env: str) -> None:
    from cloudoptics.platform.settings import Settings

    with pytest.raises(ValueError, match="auth_mode=dev"):
        Settings(environment=env, auth_mode="dev")
    with pytest.raises(ValueError, match="jobs_mode"):
        Settings(environment=env, auth_mode="oidc")
    with pytest.raises(ValueError, match="data key"):
        Settings(environment=env, auth_mode="oidc", jobs_mode="queue")


def test_digitalocean_style_database_urls_get_the_psycopg_driver() -> None:
    from cloudoptics.platform.settings import Settings

    s = Settings(
        database_url="postgresql://doadmin:x@db.example:25060/cloudoptics?sslmode=require",
        app_database_url="postgres://cloudoptics_app:y@db.example:25060/cloudoptics",
    )
    assert s.database_url.startswith("postgresql+psycopg://doadmin:")
    assert s.database_url.endswith("?sslmode=require")
    assert s.app_database_url.startswith("postgresql+psycopg://cloudoptics_app:")


async def test_oidc_without_configuration_is_a_clear_503(client: AsyncClient) -> None:
    from cloudoptics.platform.settings import get_settings

    settings = get_settings()
    settings.auth_mode = "oidc"
    try:
        r = await client.get("/v1/me", headers={"Authorization": "Bearer x.y.z"})
    finally:
        settings.auth_mode = "dev"
    assert r.status_code == 503
    assert r.json()["title"] == "Sign-in is not configured yet"


async def test_auth_config_tells_the_web_app_which_sign_in_to_show(client: AsyncClient) -> None:
    from cloudoptics.platform.settings import get_settings

    assert (await client.get("/v1/auth/config")).json() == {
        "mode": "dev",
        "configured": True,
        "provider": None,
        "issuer": None,
        "clientId": None,
    }
    settings = get_settings()
    settings.auth_mode = "oidc"
    try:
        body = (await client.get("/v1/auth/config")).json()
    finally:
        settings.auth_mode = "dev"
    assert body["mode"] == "oidc" and body["configured"] is False
