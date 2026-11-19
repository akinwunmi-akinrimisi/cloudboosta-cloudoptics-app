"""First-party analytics (SITE-15, docs/14 §8.4): the site beacon, the server funnel events and
the staff read API."""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from httpx import AsyncClient
from test_billing_rules import NOW, SEPT
from test_identity import create_tenant
from test_ingestion import bucket, ingest  # noqa: F401 - pytest fixture
from test_rate_limits import an_ip, assert_rate_limited

from cloudoptics.analysis import engine
from cloudoptics.analytics.funnel import Attribution, clean_path, clean_text
from cloudoptics.platform.settings import get_settings
from cur_fixture import generate, write_export

VISITS = "/v1/site/visits"
BROWSER = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 Chrome/131.0 Mobile Safari/537.36"


def marker() -> str:
    """A unique campaign name, so each test finds only its own rows."""
    return f"t{secrets.token_hex(6)}"


async def send(client: AsyncClient, body: Any, **headers: str) -> Any:
    raw = body if isinstance(body, (str, bytes)) else json.dumps(body)
    return await client.post(
        VISITS,
        content=raw,
        headers={"content-type": "text/plain;charset=UTF-8", "user-agent": BROWSER, **headers},
    )


def site_rows(owner_sql, campaign: str) -> list[tuple[Any, ...]]:  # type: ignore[no-untyped-def]
    return owner_sql(  # type: ignore[no-any-return]
        "SELECT kind, path, landing, referrer_host, cta, utm_source, utm_medium, utm_content, "
        "utm_term, ref FROM site_event WHERE utm_campaign = %s ORDER BY id",
        campaign,
    )


# --- The beacon ----------------------------------------------------------------------------


async def test_a_page_view_and_a_click_are_stored_without_identifiers(
    client: AsyncClient, owner_sql
) -> None:
    campaign = marker()
    ip = "198.51.100.77"
    view = {
        "kind": "page_view",
        "path": "/how-it-works/",
        "landing": True,
        "referrerHost": "WWW.LinkedIn.com.",
        "utmSource": "LinkedIn",
        "utmMedium": "Social",
        "utmCampaign": campaign,
        "utmContent": "ada@example.com",  # an email is dropped, never stored
        "ref": "Partner-Blog",
    }
    r = await send(client, view, **{"do-connecting-ip": ip})
    assert r.status_code == 204, r.text
    assert r.content == b""
    click = {"kind": "cta_click", "path": "/", "cta": "start", "utmCampaign": campaign}
    assert (await send(client, click)).status_code == 204

    assert site_rows(owner_sql, campaign) == [
        ("page_view", "/how-it-works", True, "www.linkedin.com", None, "linkedin", "social",
         None, None, "partner-blog"),
        ("cta_click", "/", False, None, "start", None, None, None, None, None),
    ]  # fmt: skip
    # Nothing that identifies a visitor exists in the table, and the hour is all that is kept.
    columns = {
        r[0]
        for r in owner_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'site_event'"
        )
    }
    assert columns == {
        "id", "occurred_at", "kind", "path", "landing", "referrer_host", "cta", "utm_source",
        "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref",
    }  # fmt: skip
    stored = owner_sql(
        "SELECT row_to_json(e)::text, occurred_at = date_trunc('hour', occurred_at) "
        "FROM site_event e WHERE utm_campaign = %s",
        campaign,
    )
    for text_row, on_the_hour in stored:
        assert ip not in text_row and "Mozilla" not in text_row and "@" not in text_row
        assert on_the_hour is True


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "purchase", "path": "/"},
        {"kind": "page_view"},
        {"kind": "page_view", "path": "https://evil.example/"},
        {"kind": "page_view", "path": "/<script>"},
        {"kind": "page_view", "path": "/" + "a" * 200},
        {"kind": "page_view", "path": "/", "utmSource": "x" * 101},
        {"kind": "page_view", "path": "/", "referrerHost": "not a host"},
        {"kind": "cta_click", "path": "/", "cta": "Start Free!"},
        {"kind": "page_view", "path": "/", "landing": "maybe"},
        "not json",
        "[]",
    ],
)
async def test_invalid_beacons_are_refused(client: AsyncClient, body: Any) -> None:
    r = await send(client, body)
    assert r.status_code == 422, r.text
    assert r.headers["content-type"].startswith("application/problem+json")


async def test_an_oversized_beacon_is_refused_before_parsing(client: AsyncClient) -> None:
    body = json.dumps({"kind": "page_view", "path": "/", "pad": "x" * 3000})
    r = await send(client, body)
    assert r.status_code == 413
    assert r.json()["code"] == "too_large"


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/", {"dnt": "1"}),
        ("/", {"sec-gpc": "1"}),
        ("/", {"user-agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"}),
        ("/", {"user-agent": ""}),
        ("/demo", {}),
        ("/demo/costs", {}),
        ("/app/overview", {}),
        ("/api/v1/me", {}),
    ],
)
async def test_untracked_visits_get_the_same_answer_and_are_not_stored(
    client: AsyncClient, owner_sql, path: str, headers: dict[str, str]
) -> None:
    campaign = marker()
    r = await send(client, {"kind": "page_view", "path": path, "utmCampaign": campaign}, **headers)
    assert r.status_code == 204
    assert site_rows(owner_sql, campaign) == []


async def test_the_app_role_cannot_read_or_delete_recent_visits(
    client: AsyncClient, database, owner_sql
) -> None:
    campaign = marker()
    assert (await send(client, {"kind": "page_view", "path": "/", "utmCampaign": campaign})).status_code == 204  # fmt: skip
    url = database["app"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        assert conn.execute("SELECT count(*) FROM site_event").fetchone() == (0,)
        assert conn.execute("SELECT count(*) FROM funnel_event").fetchone() == (0,)
        conn.execute("SELECT set_config('app.analytics_read', 'on', true)")
        assert conn.execute("DELETE FROM site_event WHERE utm_campaign = %s", (campaign,)).rowcount == 0  # fmt: skip
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE site_event SET path = '/x'")
    assert len(site_rows(owner_sql, campaign)) == 1


@pytest.fixture
def site_limit(app) -> Iterator[None]:  # type: ignore[no-untyped-def]
    settings = get_settings()
    saved = (settings.rate_limit_enabled, settings.rate_limit_site_per_minute, settings.client_ip_header)  # fmt: skip
    settings.rate_limit_enabled = True
    settings.rate_limit_site_per_minute = 3
    settings.client_ip_header = "do-connecting-ip"
    yield
    settings.rate_limit_enabled, settings.rate_limit_site_per_minute, settings.client_ip_header = saved  # fmt: skip


async def test_the_beacon_is_limited_per_client_address(
    client: AsyncClient, owner_sql, site_limit
) -> None:
    campaign, ip = marker(), an_ip()
    view = {"kind": "page_view", "path": "/", "utmCampaign": campaign}
    for _ in range(3):
        assert (await send(client, view, **{"do-connecting-ip": ip})).status_code == 204
    assert_rate_limited(await send(client, view, **{"do-connecting-ip": ip}), "ip-site")
    assert (await send(client, view, **{"do-connecting-ip": an_ip()})).status_code == 204
    assert len(site_rows(owner_sql, campaign)) == 4
    # The counter keeps a hash of the address, not the address.
    assert owner_sql(
        "SELECT count(*) FROM rate_limit_counter WHERE bucket LIKE %s", f"%{ip.split(':')[2]}%"
    ) == [(0,)]


def test_cleaning() -> None:
    assert clean_text("  Spring\n\x00Sale  ", 100) == "Spring Sale"
    assert clean_text("x" * 150, 100) == "x" * 100
    assert clean_text("mail me: ada@example.com", 100) is None
    assert clean_text(42, 100) is None
    assert clean_path("/how-it-works?utm_source=x#faq") == "/how-it-works"
    assert clean_path("//evil.example") == "//evil.example"  # a path, stored as text only
    assert clean_path("javascript:alert(1)") is None
    a = Attribution.model_validate(
        {"source": " LinkedIn ", "campaign": "x" * 300, "term": "a@b.co", "landingPath": "nope"}
    )
    assert (a.source, len(a.campaign or ""), a.term, a.landing_path) == ("linkedin", 100, None, None)


# --- Funnel events -------------------------------------------------------------------------


def funnel(owner_sql, tenant: str) -> dict[str, tuple[Any, ...]]:  # type: ignore[no-untyped-def]
    rows = owner_sql(
        "SELECT event, detail, source, medium, campaign, ref, landing_path, user_id IS NOT NULL "
        "FROM funnel_event WHERE tenant_id = %s",
        tenant,
    )
    return {r[0]: r[1:] for r in rows}


async def test_sign_up_is_recorded_once_per_user(client: AsyncClient, login, owner_sql) -> None:
    email = f"new-{secrets.token_hex(3)}@example.com"
    headers = await login(email)
    for _ in range(2):
        assert (await client.get("/v1/me", headers=headers)).status_code == 200
    me = (await client.get("/v1/me", headers=headers)).json()
    assert owner_sql(
        "SELECT event, tenant_id FROM funnel_event WHERE user_id = %s", me["id"]
    ) == [("signup_completed", None)]


async def test_workspace_creation_stores_the_first_touch_campaign(
    client: AsyncClient, login, owner_sql
) -> None:
    headers = await login()
    attribution = {
        "source": "LinkedIn",
        "medium": "social",
        "campaign": "beta-launch",
        "term": "ada@example.com",
        "ref": None,
        "landingPath": "/security",
        "unknown": "ignored",
    }
    tenant = await create_tenant(client, headers, attribution=attribution)
    assert funnel(owner_sql, tenant["id"]) == {
        "workspace_created": (None, "linkedin", "social", "beta-launch", None, "/security", True)
    }
    # Without attribution (or with junk) the workspace is still created.
    plain = await create_tenant(client, headers, attribution={"source": 5, "landingPath": 7})
    assert funnel(owner_sql, plain["id"])["workspace_created"][1:3] == (None, None)
    bare = await create_tenant(client, headers)
    assert "workspace_created" in funnel(owner_sql, bare["id"])


async def test_each_step_is_recorded_once_from_its_own_code_path(
    client: AsyncClient,
    login,  # type: ignore[no-untyped-def]
    owner_sql,
    fake_aws,
    bucket: Path,  # noqa: F811
) -> None:
    headers = await login()
    tenant = (await create_tenant(client, headers))["id"]
    for _ in range(2):
        r = await client.post(f"/v1/tenants/{tenant}/connections/intents", headers=headers)
        assert r.status_code == 201, r.text
    account = "".join(secrets.choice("0123456789") for _ in range(12))
    r = await client.post(
        f"/v1/tenants/{tenant}/connections", json={"awsAccountId": account}, headers=headers
    )
    assert r.status_code == 201, r.text
    connection = r.json()["connection"]["id"]
    for _ in range(2):
        r = await client.post(
            f"/v1/tenants/{tenant}/connections/{connection}/validate", headers=headers
        )
        assert r.status_code in (200, 202), r.text
    owner_sql(
        "UPDATE cloud_connection SET status = 'healthy', cur_bucket = 'b', "
        "cur_prefix = 'cloudoptics' WHERE id = %s",
        connection,
    )
    write_export(generate(SEPT), bucket / "cloudoptics")
    assert (await ingest(client, tenant, connection, headers))["status"] == "succeeded"
    for _ in range(2):
        await engine.run_analysis(uuid.UUID(tenant), now=NOW)

    steps = funnel(owner_sql, tenant)
    assert set(steps) == {
        "workspace_created",
        "connect_started",
        "connected",
        "first_costs",
        "first_recommendation",
    }
    assert steps["first_costs"][0] == "bills"
    counts = owner_sql(
        "SELECT event, count(*) FROM funnel_event WHERE tenant_id = %s GROUP BY event", tenant
    )
    assert all(n == 1 for _, n in counts)


async def test_funnel_rows_follow_workspace_isolation(
    client: AsyncClient, login, database
) -> None:
    headers = await login()
    mine = (await create_tenant(client, headers))["id"]
    other = (await create_tenant(client, await login()))["id"]
    url = database["app"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, true)", (mine,))
        seen = conn.execute("SELECT DISTINCT tenant_id::text FROM funnel_event").fetchall()
        assert seen == [(mine,)]
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO funnel_event (event, tenant_id) VALUES ('connected', %s)", (other,)
            )


# --- Staff read API ------------------------------------------------------------------------


@pytest.fixture
def staff_email(app) -> Iterator[str]:  # type: ignore[no-untyped-def]
    email = f"growth-{secrets.token_hex(3)}@cloudoptics.example"
    settings = get_settings()
    settings.platform_admin_emails = email
    yield email
    settings.platform_admin_emails = ""


async def test_only_staff_with_mfa_read_the_funnel(
    client: AsyncClient, login, staff_email: str
) -> None:
    for path in ("/v1/admin/analytics/funnel", "/v1/admin/analytics/site"):
        assert (await client.get(path, headers=await login(mfa=True))).status_code == 403
        r = await client.get(path, headers=await login(staff_email))
        assert r.status_code == 401 and r.json()["code"] == "mfa_required"
        assert (await client.get(path)).status_code == 401


async def test_the_funnel_counts_by_source(
    client: AsyncClient, login, staff_email: str
) -> None:
    source = marker()
    for kind, extra in (
        ("page_view", {"landing": True}),
        ("page_view", {"landing": True}),
        ("page_view", {}),
        ("cta_click", {"cta": "start"}),
    ):
        body = {"kind": kind, "path": "/", "utmSource": source, "utmCampaign": "c", **extra}
        assert (await send(client, body)).status_code == 204
    headers = await login()
    tenant = (
        await create_tenant(client, headers, attribution={"source": source, "landingPath": "/"})
    )["id"]
    assert (
        await client.post(f"/v1/tenants/{tenant}/connections/intents", headers=headers)
    ).status_code == 201

    staff = await login(staff_email, mfa=True)
    r = await client.get("/v1/admin/analytics/funnel", headers=staff)
    assert r.status_code == 200, r.text
    body = r.json()
    row = next(s for s in body["bySource"] if s["source"] == source)
    assert row == {
        "source": source,
        "landings": 2,
        "startClicks": 1,
        "steps": {
            "workspace_created": 1,
            "connect_started": 1,
            "connected": 0,
            "first_costs": 0,
            "first_recommendation": 0,
        },
    }
    assert body["signups"] >= 1 and body["signupsWithWorkspace"] >= 1
    assert body["steps"]["workspace_created"] >= 1
    assert body["site"]["landings"] >= 2

    r = await client.get("/v1/admin/analytics/site", headers=staff)
    assert r.status_code == 200, r.text
    assert {"name": f"{source} / c", "count": 2} in [
        c for c in r.json()["campaigns"] if c["name"].startswith(source)
    ]
    r = await client.get(
        "/v1/admin/analytics/funnel", params={"since": "2026-02-01", "until": "2026-01-01"},
        headers=staff,
    )  # fmt: skip
    assert r.status_code == 422
