"""ETags, 304s and the query cache keyed by the tenant's data version (WBS API-8)."""

from __future__ import annotations

import uuid
from datetime import date

import psycopg
import pytest
from httpx import AsyncClient
from test_budgets import ACCOUNT, spend, workspace
from test_identity import create_tenant

from cloudoptics.platform import conditional
from cloudoptics.platform.conditional import QueryCache, etag_matches, query_cache
from cloudoptics.platform.settings import get_settings

TODAY = date(2026, 3, 20)
SUMMARY = "/v1/tenants/{}/costs/summary?today=2026-03-20"
HEAVY = (
    "/costs/summary",
    "/costs/daily",
    "/costs/breakdown",
    "/costs/months",
    "/costs/credits",
    "/costs/forecast",
    "/costs/changes",
    "/recommendations",
    "/recommendations/summary",
    "/reports",
    "/resources/summary",
)
# Tenant tables whose changes do not change any ETagged response (migration 0031).
UNVERSIONED = {
    "audit_log",
    "membership",
    "invite",
    "connection_intent",
    "notification",
    "notification_preference",
    "resource_metric",
    "aws_api_usage",
    "tenant_data_version",
    "data_version_pending",
    "export_job",  # a user's export files; downloads re-check access, no cached read
    "data_deletion",  # deletion requests and verification records (no cached read uses them)
    "support_access_grant",  # staff break-glass grants (no cached read uses them)
    "support_access_request",  # requests made under those grants
    "funnel_event",  # first-time funnel steps for staff analytics (no cached read uses them)
    "gcp_connection",  # a Google Cloud connection's export settings (read by its setup page only)
    "azure_connection",  # Azure connection settings (ADR-020); only uncached admin routes read them
    "oauth_state",  # 10-minute DigitalOcean sign-in states (ADR-021); no cached read uses them
}


def version(owner_sql, tenant_id: str) -> int:  # type: ignore[no-untyped-def]
    rows = owner_sql("SELECT version FROM tenant_data_version WHERE tenant_id = %s", tenant_id)
    return int(rows[0][0]) if rows else 0  # type: ignore[call-overload]


async def test_heavy_reads_send_a_weak_etag_and_answer_304(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, headers, _ = await workspace(client, login, owner_sql)
    for path in HEAVY:
        r = await client.get(f"/v1/tenants/{tenant['id']}{path}", headers=headers)
        assert r.status_code == 200, (path, r.text)
        etag = r.headers["etag"]
        assert etag.startswith('W/"'), path
        assert r.headers["cache-control"] == "private, no-cache"
        assert "Authorization" in r.headers["vary"]
        again = await client.get(
            f"/v1/tenants/{tenant['id']}{path}", headers={**headers, "if-none-match": etag}
        )
        assert again.status_code == 304, path
        assert again.content == b""
        assert again.headers["etag"] == etag
        # A strong form of the same tag and a list containing it match too (weak comparison).
        strong = etag.removeprefix("W/")
        listed = await client.get(
            f"/v1/tenants/{tenant['id']}{path}",
            headers={**headers, "if-none-match": f'"other", {strong}'},
        )
        assert listed.status_code == 304, path


async def test_new_bills_change_the_etag_and_the_cached_figures(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, headers, connection = await workspace(client, login, owner_sql)
    url = SUMMARY.format(tenant["id"])
    spend(owner_sql, tenant, connection, date(2026, 3, 10), ACCOUNT, 40.0)
    first = await client.get(url, headers=headers)
    total = first.json()["monthToDate"]["effective"]
    assert total == pytest.approx(40.0)
    cached = await client.get(url, headers=headers)
    assert cached.json() == first.json() and cached.headers["etag"] == first.headers["etag"]

    spend(owner_sql, tenant, connection, date(2026, 3, 11), ACCOUNT, 2.5)
    r = await client.get(url, headers={**headers, "if-none-match": first.headers["etag"]})
    assert r.status_code == 200
    assert r.headers["etag"] != first.headers["etag"]
    assert r.json()["monthToDate"]["effective"] == pytest.approx(42.5)


async def test_settings_and_status_changes_bump_the_version(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, headers, _ = await workspace(client, login, owner_sql)
    url = f"/v1/tenants/{tenant['id']}/recommendations"
    before = (await client.get(url, headers=headers)).headers["etag"]
    v = version(owner_sql, tenant["id"])
    r = await client.put(
        f"/v1/tenants/{tenant['id']}/reports/settings",
        json={"emails": ["cfo@example.com"]},
        headers=headers,
    )
    assert r.status_code == 200
    # One transaction, one bump, even though it wrote a setting and an audit row.
    assert version(owner_sql, tenant["id"]) == v + 1
    assert (await client.get(url, headers=headers)).headers["etag"] != before

    r = await client.patch(f"/v1/tenants/{tenant['id']}", json={"name": "Renamed"}, headers=headers)
    assert r.status_code == 200
    assert version(owner_sql, tenant["id"]) == v + 2


async def test_reads_and_rolled_back_writes_do_not_bump(
    client: AsyncClient, login, owner_sql, database
) -> None:
    tenant, headers, connection = await workspace(client, login, owner_sql)
    v = version(owner_sql, tenant["id"])
    await client.get(SUMMARY.format(tenant["id"]), headers=headers)
    await client.get(f"/v1/tenants/{tenant['id']}/audit-log", headers=headers)
    assert version(owner_sql, tenant["id"]) == v
    url = database["owner"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        conn.execute("UPDATE cloud_connection SET status = status WHERE id = %s", (connection,))
        conn.rollback()
    assert version(owner_sql, tenant["id"]) == v


async def test_etags_never_cross_users_or_tenants(client: AsyncClient, login, owner_sql) -> None:
    tenant, headers, connection = await workspace(client, login, owner_sql)
    spend(owner_sql, tenant, connection, date(2026, 3, 10), ACCOUNT, 40.0)
    mine = await client.get(SUMMARY.format(tenant["id"]), headers=headers)

    other_headers = await login()
    other = await create_tenant(client, other_headers)
    theirs = await client.get(
        SUMMARY.format(other["id"]),
        headers={**other_headers, "if-none-match": mine.headers["etag"]},
    )
    assert theirs.status_code == 200
    assert theirs.json()["monthToDate"] is None  # their own (empty) data, not the cached one
    # An outsider presenting the tenant's ETag gets 404, not 304.
    r = await client.get(
        SUMMARY.format(tenant["id"]),
        headers={**other_headers, "if-none-match": mine.headers["etag"]},
    )
    assert r.status_code == 404


async def test_exchange_rates_change_the_forecast_etag(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, headers, _ = await workspace(client, login, owner_sql)
    url = f"/v1/tenants/{tenant['id']}/costs/forecast?today=2026-03-20"
    before = (await client.get(url, headers=headers)).headers["etag"]
    # Any rate (XTS: the ISO code for tests, so other tests' NGN rates are untouched).
    owner_sql(
        "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source) "
        "VALUES (%s, 'XTS', '2026-03-19', 'official', 1500, 'feed')",
        str(uuid.uuid4()),
    )
    r = await client.get(url, headers={**headers, "if-none-match": before})
    assert r.status_code == 200 and r.headers["etag"] != before


async def test_errors_carry_no_etag(client: AsyncClient, login, owner_sql) -> None:
    tenant, headers, _ = await workspace(client, login, owner_sql)
    r = await client.get(
        f"/v1/tenants/{tenant['id']}/costs/summary?today=2026-03-20&month=2027-01",
        headers=headers,
    )
    assert r.status_code == 422
    assert "etag" not in r.headers


def test_every_tenant_table_is_versioned_or_deliberately_left_out(owner_sql) -> None:
    tables = {
        r[0]
        for r in owner_sql(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name = 'tenant_id'"
        )
    }
    versioned = {
        r[0]
        for r in owner_sql(
            "SELECT DISTINCT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE t.tgname IN ('data_version_ins', 'data_version_upd', 'data_version_del')"
        )
    }
    assert tables - UNVERSIONED - versioned == set()
    assert "tenant" in versioned


def test_app_role_cannot_write_versions(database) -> None:
    url = database["app"].replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url) as conn:
        for sql in (
            "UPDATE tenant_data_version SET version = 0",
            "UPDATE fx_data_version SET version = 0",
            "SELECT * FROM data_version_pending",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
            conn.rollback()


def test_weak_comparison() -> None:
    assert etag_matches('W/"abc"', 'W/"abc"')
    assert etag_matches('"abc"', 'W/"abc"')
    assert etag_matches('"x", W/"abc"', 'W/"abc"')
    assert etag_matches("*", 'W/"abc"')
    assert not etag_matches('W/"abd"', 'W/"abc"')
    assert not etag_matches(None, 'W/"abc"')


def test_query_cache_is_a_bounded_lru(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "query_cache_entries", 2)
    cache = QueryCache()
    cache.put(("a",), 1)
    cache.put(("b",), 2)
    assert cache.get(("a",)) == 1  # a is now the most recent
    cache.put(("c",), 3)
    assert cache.get(("b",)) is None and len(cache) == 2
    monkeypatch.setattr(settings, "query_cache_ttl_seconds", -1)
    cache.put(("d",), 4)
    assert cache.get(("d",)) is None
    monkeypatch.setattr(settings, "query_cache_entries", 0)
    cache.put(("e",), 5)
    assert cache.get(("e",)) is None
    cache.clear()
    assert len(cache) == 0
    assert conditional.query_cache is query_cache
