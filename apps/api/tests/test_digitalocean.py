"""The DigitalOcean provider (ADR-021) against the fake API: token checks, connect, replace
and disconnect, invoices and month to date in the cost tables, reconciliation, the scan, the
rules, and that the token never leaves the encrypted column."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import psycopg
import pytest
from fake_digitalocean import (
    IDLE_DROPLET,
    READ_SCOPES,
    FakeDigitalOcean,
    Team,
    prev_month,
    secret_token,
)
from httpx import AsyncClient
from test_data_deletion import member, workspace

from cloudoptics import providers
from cloudoptics.analysis import registry
from cloudoptics.ingestion import jobs as ingestion_jobs
from cloudoptics.providers.digitalocean import billing, connect, inventory
from cloudoptics.providers.digitalocean import client as do_client
from cloudoptics.providers.digitalocean.client import Client, DigitalOceanError, RateLimiter

Sql = Callable[..., list[tuple[Any, ...]]]
TODAY = datetime.now(UTC).date()


@pytest.fixture
def fake_do() -> Iterator[FakeDigitalOcean]:
    fake = FakeDigitalOcean(today=TODAY)
    do_client.set_transport(fake.transport())
    yield fake
    do_client.set_transport(None)


def _client(token: str, **kw: Any) -> Client:
    return Client(token, limiter=RateLimiter(sleep=lambda s: None), sleep=lambda s: None, **kw)


async def _connect(
    http: AsyncClient, tenant: str, headers: dict[str, str], token: str
) -> httpx.Response:
    return await http.post(
        f"/v1/tenants/{tenant}/connections/digitalocean", json={"token": token}, headers=headers
    )


# --- pure parts ---------------------------------------------------------------------------


def test_registered_with_costs_first_capabilities() -> None:
    do = providers.get("digitalocean")
    assert do.label == "DigitalOcean" and do.account_noun == "account"
    assert do.billing_currencies == frozenset({"USD"})
    assert do.valid_account_id(str(uuid.uuid4())) and do.valid_account_id("5df3e3004a17e242b7c2")
    assert not do.valid_account_id("team/../x") and not do.valid_account_id("")
    for name in ("connect", "billing_source", "reconcile", "inventory"):
        assert do.capability(name) is not None, name
    for name in ("credentials", "accounts", "metrics", "prices", "commitments", "native_recs"):
        assert do.capability(name) is None, name
    assert {s.provider for s in registry.rules_for("digitalocean")} == {"digitalocean"}
    assert len(registry.rules_for("digitalocean")) == 5


@pytest.mark.parametrize(
    ("raw", "want"),
    [("$3.11", "3.11"), ("-$1.00", "-1.00"), ("($2.50)", "-2.50"), ("1,234.56", "1234.56"),
     ("", None), ("n/a", None)],
)  # fmt: skip
def test_parse_usd(raw: str, want: str | None) -> None:
    assert billing.parse_usd(raw) == (None if want is None else Decimal(want))


def test_split_by_day_sums_exactly_and_stays_in_the_month() -> None:
    period = date(2026, 10, 1)
    start = datetime(2026, 9, 28, tzinfo=UTC)
    end = datetime(2026, 10, 16, 12, tzinfo=UTC)
    parts = billing.split_by_day(Decimal("10.00"), start, end, period)
    assert sum(a for _, a, _ in parts) == Decimal("10.00")
    assert parts[0][0] == period and parts[-1][0] == date(2026, 10, 16)
    assert parts[-1][2] == Decimal("12.000000")
    assert billing.split_by_day(Decimal("1"), None, None, period) == [(period, Decimal("1"), 0)]


def test_map_invoice_rows_match_the_invoice_and_keep_no_names() -> None:
    team = Team(str(uuid.uuid4()), "Acme", TODAY)
    period = prev_month(TODAY)
    inv = billing.Invoice(team.invoice_uuid(period), period, team.invoice_amount())
    loaded = billing.map_invoice(
        billing.InvoiceData(inv, team.csv(period), team.summary(period)), team.uuid, "Acme"
    )
    assert loaded.ok
    assert sum(r.billed_cost for r in loaded.rows) == team.invoice_amount()
    assert {c.id: c.status for c in loaded.checks}["invoice_total"] == "ok"
    services = {r.service_name: r.service_category for r in loaded.rows}
    assert services["Droplets"] == "Compute" and services["Volumes"] == "Storage"
    assert services["Load Balancers"] == "Networking"
    assert services["Managed Databases"] == "Databases"
    charges = {r.charge_category for r in loaded.rows}
    assert {"Usage", "Tax", "Credit"} <= charges
    assert {r.usage_type for r in loaded.rows if r.service_name == "Droplets"} == {
        "s-2vcpu-4gb", "s-1vcpu-2gb"}  # fmt: skip
    assert {"do:project": "Production"} in loaded.tag_sets.values()
    stored = repr(loaded.rows)
    assert "web-1" not in stored and "203.0.113.7" not in stored
    assert loaded.accounts == {team.uuid: "Acme"}
    assert loaded.manifest.export_version == inv.uuid


def test_map_invoice_checks() -> None:
    period = date(2026, 9, 1)
    inv = billing.Invoice(str(uuid.uuid4()), period, Decimal("5.00"))
    bad = billing.map_invoice(billing.InvoiceData(inv, "product,USD\nDroplets,$1\n"), "t", None)
    assert not bad.ok and bad.rows == []
    body = (
        "product,start,end,USD\nDroplets,2026-09-01 00:00:00 +0000,2026-10-01 00:00:00 +0000,$4\n"
    )
    off = billing.map_invoice(billing.InvoiceData(inv, body), "t", None)
    assert off.ok and {c.id: c.status for c in off.checks}["invoice_total"] == "warning"
    unreadable = body.replace("$4", "four")
    assert not billing.map_invoice(billing.InvoiceData(inv, unreadable), "t", None).ok


def test_overages_are_added_only_when_the_lines_miss_them() -> None:
    team = Team(str(uuid.uuid4()), "Acme", TODAY)
    period = prev_month(TODAY)
    summary = {**team.summary(period), "overages": {"name": "Overages", "amount": "4.00"}}
    with_overage = billing.Invoice(team.invoice_uuid(period), period,
                                   team.invoice_amount() + Decimal("4.00"))  # fmt: skip
    loaded = billing.map_invoice(billing.InvoiceData(with_overage, team.csv(period), summary),
                                 team.uuid, None)  # fmt: skip
    assert sum(r.billed_cost for r in loaded.rows) == with_overage.amount
    assert "Overages" in {r.service_name for r in loaded.rows}
    # Already in the lines (the invoice equals lines + taxes + credits): not added twice.
    plain = billing.Invoice(team.invoice_uuid(period), period, team.invoice_amount())
    again = billing.map_invoice(billing.InvoiceData(plain, team.csv(period), summary), "t", None)
    assert "Overages" not in {r.service_name for r in again.rows}


def test_sku_and_category_maps() -> None:
    assert billing.sku_product("1-DO-DROP-0109") == "Droplets"
    assert billing.sku_product("1-KS-K8SWN-00109") == "Kubernetes"
    assert billing.sku_product("ZZZ") == "Other"
    assert billing.service_category("GenAI Agents") == "AI and Machine Learning"
    assert billing.service_category("Something new") == "Other"


def test_cpu_usage() -> None:
    team = Team(str(uuid.uuid4()), "x", TODAY)
    now = int(datetime.now(UTC).timestamp())
    avg, peak, days = inventory.cpu_usage(team.cpu(str(IDLE_DROPLET), now - 14 * 86400, now)) or (
        0, 0, 0)  # fmt: skip
    assert 0.5 < avg < 1.5 and peak < 2 and days > 13
    assert inventory.cpu_usage({"data": {"result": []}}) is None


def test_tags_of() -> None:
    assert inventory.tags_of(["env:prod", "web"]) == {"env": "prod", "web": ""}


# --- the client ---------------------------------------------------------------------------


def test_client_retries_429_and_5xx_then_gives_up(fake_do: FakeDigitalOcean) -> None:
    token = fake_do.add_token()
    waits: list[float] = []
    fake_do.throttle = 2
    with Client(token, limiter=RateLimiter(sleep=lambda s: None), sleep=waits.append) as c:
        assert c.get("/v2/account")["account"]["team"]["uuid"]
        assert waits == [1.0, 1.0]
        fake_do.unavailable = 1
        c.get("/v2/account")
        assert waits[-1] == 1.0  # 2 ** 0
        fake_do.unavailable = 10
        with pytest.raises(DigitalOceanError) as err:
            c.get("/v2/account")
        assert err.value.status == 503 and err.value.code == "service_unavailable"
        assert token not in str(err.value) and token not in repr(c)


def test_client_refuses_foreign_next_links() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"droplets": [], "links": {"pages": {
            "next": "https://evil.example/v2/droplets?page=2"}}})  # fmt: skip

    do_client.set_transport(httpx.MockTransport(handler))
    try:
        with _client(secret_token()) as c, pytest.raises(DigitalOceanError, match="foreign"):
            list(c.paged("/v2/droplets", "droplets"))
    finally:
        do_client.set_transport(None)


def test_paging_follows_next(fake_do: FakeDigitalOcean) -> None:
    token = fake_do.add_token()
    with _client(token) as c:
        assert len(list(c.paged("/v2/reserved_ips", "reserved_ips", {"per_page": 1}))) == 2
        assert c.calls == 2


def test_rate_limiter_holds_past_the_minute_and_honours_reset() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    limiter = RateLimiter(per_minute=2, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.acquire("k")
    assert slept == [60.0]
    limiter.block("k", 5)
    limiter.acquire("k")
    assert slept[-1] == 5.0


def test_limits_headers_block_the_token(fake_do: FakeDigitalOcean) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, headers={"ratelimit-remaining": "0",
                                                     "ratelimit-reset": "1030"})  # fmt: skip

    do_client.set_transport(httpx.MockTransport(handler))
    limiter = RateLimiter(clock=lambda: 0.0, sleep=lambda s: None)
    try:
        with Client(secret_token(), limiter=limiter, clock=lambda: 1000.0) as c:
            c.get("/v2/account")
        assert limiter._blocked_until[c._key] == 30.0
    finally:
        do_client.set_transport(None)


def test_token_format() -> None:
    assert do_client.valid_token(secret_token()) and do_client.valid_token("a" * 64)
    assert not do_client.valid_token("dop_v1_short") and not do_client.valid_token("x" * 71)


# --- inspecting a token --------------------------------------------------------------------


def test_inspect_read_only_token(fake_do: FakeDigitalOcean) -> None:
    token = fake_do.add_token()
    with _client(token) as c:
        result = connect.inspect_token(c, TODAY)
    assert result.status == "healthy" and result.team_uuid == fake_do.team_for(token).uuid
    assert {c.id for c in result.checks} >= {"account", "billing", "invoices", "read_only"}
    assert not result.can_write
    assert ("POST", "/v2/droplets", True) in fake_do.requests


@pytest.mark.parametrize(
    ("scopes", "status", "failed"),
    [
        (READ_SCOPES | {"droplet:create"}, "failed", "read_only"),
        (READ_SCOPES | {"tag:create"}, "failed", "read_only"),
        (READ_SCOPES - {"billing:read"}, "failed", "billing"),
        (READ_SCOPES - {"monitoring:read"}, "degraded", "monitoring"),
        (READ_SCOPES - {"droplet:read", "block_storage:read"}, "degraded", "droplets"),
    ],
)
def test_inspect_scopes(
    fake_do: FakeDigitalOcean, scopes: frozenset[str], status: str, failed: str
) -> None:
    token = fake_do.add_token(scopes=scopes)
    with _client(token) as c:
        result = connect.inspect_token(c, TODAY)
    assert result.status == status
    check = next(c for c in result.checks if c.id == failed)
    assert check.status == ("error" if status == "failed" else "warning")


def test_inspect_refused_token(fake_do: FakeDigitalOcean) -> None:
    with _client(secret_token()) as c:
        result = connect.inspect_token(c, TODAY)
    assert result.status == "failed" and result.team_uuid is None
    assert result.checks[0].message == "DigitalOcean did not accept this token."
    assert all(c.status == "skipped" for c in result.checks[1:])


# --- through the API ----------------------------------------------------------------------


async def test_connect_loads_costs_scans_and_recommends(
    client: AsyncClient,
    login: Any,
    owner_sql: Sql,
    fake_do: FakeDigitalOcean,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    tenant, headers = await workspace(client, login, "Ocean Ltd")
    token = fake_do.add_token()
    team = fake_do.team_for(token)
    r = await _connect(client, tenant, headers, token)
    assert r.status_code == 201, r.text
    body = r.json()
    cid = body["id"]
    assert body["provider"] == "digitalocean" and body["awsAccountId"] == team.uuid
    assert body["status"] == "healthy"
    assert {c["id"] for c in body["permissionChecks"]} >= {"account", "billing", "read_only"}

    # Closed months: both invoices, to the cent; reconciled against the invoice.
    rows = owner_sql(
        "SELECT billing_period, sum(billed_cost_usd), min(provider) FROM cost_daily "
        "WHERE connection_id = %s GROUP BY billing_period ORDER BY 1",
        cid,
    )
    assert [r[0] for r in rows] == sorted(team.periods())
    assert all(r[1] == team.invoice_amount() and r[2] == "digitalocean" for r in rows)
    recon = owner_sql("SELECT status FROM cost_reconciliation WHERE connection_id = %s", cid)
    assert [r[0] for r in recon] == ["ok", "ok"]
    # This month: provisional, from the balance and the daily insights.
    snap = owner_sql(
        "SELECT provider, billed_cost_usd, jsonb_array_length(daily) FROM cost_snapshot_ce "
        "WHERE connection_id = %s",
        cid,
    )
    assert snap[0][0] == "digitalocean" and snap[0][1] == team.mtd_total()
    # The team is an account with its name; projects are a tag.
    assert owner_sql(
        "SELECT name, resource_access FROM cloud_account WHERE connection_id = %s", cid
    ) == [("Acme Engineering", True)]
    assert (
        owner_sql(
            "SELECT count(*) FROM tag_set WHERE tenant_id = %s AND tags ? 'do:project'", tenant
        )[0][0]
        == 2
    )

    summary = await client.get(f"/v1/tenants/{tenant}/costs/summary", headers=headers)
    assert summary.status_code == 200, summary.text
    months = await client.get(f"/v1/tenants/{tenant}/costs/months", headers=headers)
    assert months.status_code == 200

    # The scan and the five rules.
    kinds = dict(owner_sql(
        "SELECT resource_type, count(*) FROM resource WHERE connection_id = %s "
        "AND provider = 'digitalocean' GROUP BY 1", cid))  # fmt: skip
    assert kinds == {"do_droplet": 2, "do_volume": 1, "do_snapshot": 1, "do_reserved_ip": 2,
                     "do_load_balancer": 1, "do_database": 1}  # fmt: skip
    recs = owner_sql(
        "SELECT rule_id, fingerprint, fix FROM recommendation WHERE tenant_id = %s "
        "AND provider = 'digitalocean' ORDER BY rule_id",
        tenant,
    )
    assert [r[0] for r in recs] == [
        "DO1_unattached_volume", "DO2_unassigned_reserved_ip", "DO3_old_snapshot",
        "DO4_idle_droplet", "DO5_idle_load_balancer"]  # fmt: skip
    assert all(r[1].startswith(f"digitalocean:{r[0]}:{team.uuid}:") for r in recs)
    assert all(step.startswith("doctl ") for r in recs for step in r[2]["steps"]
               if not step.startswith("doctl compute load-balancer get"))  # fmt: skip
    assert recs[3][1].endswith(f":{IDLE_DROPLET}")
    listed = await client.get(f"/v1/tenants/{tenant}/recommendations", headers=headers)
    assert (
        listed.status_code == 200
        and len([x for x in listed.json() if x.get("provider") == "digitalocean"]) == 5
    )

    # The token is nowhere but the ciphertext.
    stored = owner_sql("SELECT external_id_enc FROM cloud_connection WHERE id = %s", cid)[0][0]
    assert token.encode() not in bytes(stored)
    everything = owner_sql(
        "SELECT coalesce(string_agg(t::text, ''), '') FROM (SELECT row_to_json(a) t FROM "
        "audit_log a WHERE tenant_id = %s UNION ALL SELECT row_to_json(s) FROM sync_run s "
        "WHERE tenant_id = %s UNION ALL SELECT row_to_json(c) FROM cloud_connection c "
        "WHERE tenant_id = %s) x",
        tenant, tenant, tenant,
    )[0][0]  # fmt: skip
    assert token not in everything and token[7:] not in everything
    assert token not in caplog.text and token[7:] not in caplog.text
    for path in ("", f"/{cid}"):
        got = await client.get(f"/v1/tenants/{tenant}/connections{path}", headers=headers)
        assert token not in got.text
    actions = [r[0] for r in owner_sql(
        "SELECT action FROM audit_log WHERE tenant_id = %s ORDER BY id", tenant)]  # fmt: skip
    assert "connection.created" in actions

    # Idempotent: the next poll loads nothing new.
    again = await billing.ingest_connection(uuid.UUID(tenant), uuid.UUID(cid))
    assert again.status == "up_to_date"
    assert owner_sql("SELECT count(*) FROM ingest_batch WHERE connection_id = %s", cid)[0][0] == 2


async def test_a_team_id_that_is_not_a_uuid_and_no_credentials_kept(
    client: AsyncClient, login: Any, owner_sql: Sql, fake_do: FakeDigitalOcean
) -> None:
    """The API reference's example team ``uuid`` is 40 hex digits; the database list carries a
    password; tags need tag:read."""
    tenant, headers = await workspace(client, login)
    token = fake_do.add_token(
        team="5df3e3004a17e242b7c20ca6c9fc25b701a47ece", scopes=READ_SCOPES - {"tag:read"}
    )
    r = await _connect(client, tenant, headers, token)
    assert r.status_code == 201, r.text
    assert r.json()["awsAccountId"] == "5df3e3004a17e242b7c20ca6c9fc25b701a47ece"
    assert r.json()["status"] == "degraded"
    cid = r.json()["id"]
    assert owner_sql("SELECT count(*) FROM cost_snapshot_ce WHERE connection_id = %s", cid) == [
        (1,)
    ]
    configs = owner_sql(
        "SELECT string_agg(config::text || tags::text, '') FROM resource WHERE connection_id = %s",
        cid,
    )[0][0]
    assert "fake-db-password" not in configs and "doadmin" not in configs
    assert owner_sql(
        "SELECT tags FROM resource WHERE connection_id = %s AND resource_type = 'do_droplet'", cid
    ) == [({},), ({},)]


async def test_refused_tokens_are_not_stored(
    client: AsyncClient, login: Any, owner_sql: Sql, fake_do: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    cases = [
        ("not-a-token-but-long-enough", "token_format"),
        (secret_token(), "token_refused"),  # unknown to DigitalOcean
        (fake_do.add_token(scopes=READ_SCOPES | {"droplet:create"}), "token_can_write"),
        (fake_do.add_token(scopes=READ_SCOPES - {"billing:read"}), "token_refused"),
        (fake_do.add_token(scopes=READ_SCOPES - {"account:read"}), "token_refused"),
    ]
    for token, code in cases:
        r = await _connect(client, tenant, headers, token)
        assert r.status_code == 422, (code, r.text)
        assert r.json().get("code") == code
        assert token not in r.text
    assert (
        owner_sql("SELECT count(*) FROM cloud_connection WHERE tenant_id = %s", tenant)[0][0] == 0
    )
    short = await _connect(client, tenant, headers, "short")
    assert short.status_code == 422 and "short" not in short.text


async def test_only_admins_connect(
    client: AsyncClient, login: Any, owner_sql: Sql, fake_do: FakeDigitalOcean
) -> None:
    tenant, _ = await workspace(client, login)
    viewer = await member(client, login, owner_sql, tenant, "member")
    r = await _connect(client, tenant, viewer, fake_do.add_token())
    assert r.status_code == 403


async def test_degraded_token_still_loads_costs(
    client: AsyncClient, login: Any, owner_sql: Sql, fake_do: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    token = fake_do.add_token(scopes=READ_SCOPES - {"monitoring:read", "snapshot:read"})
    team = fake_do.team_for(token)
    fake_do.forbidden_paths.add(f"/v2/billing/do:team:{team.uuid}/insights/")
    r = await _connect(client, tenant, headers, token)
    assert r.status_code == 201 and r.json()["status"] == "degraded"
    cid = r.json()["id"]
    assert (
        owner_sql(
            "SELECT count(DISTINCT billing_period) FROM cost_daily WHERE connection_id = %s", cid
        )[0][0]
        == 2
    )
    assert owner_sql(
        "SELECT billed_cost_usd, daily FROM cost_snapshot_ce WHERE connection_id = %s", cid
    ) == [(team.mtd_total(), [])]
    rules = {r[0] for r in owner_sql(
        "SELECT rule_id FROM recommendation WHERE tenant_id = %s", tenant)}  # fmt: skip
    assert "DO4_idle_droplet" not in rules and "DO3_old_snapshot" not in rules
    assert "DO1_unattached_volume" in rules
    scope = owner_sql(
        "SELECT scope FROM sync_run WHERE connection_id = %s AND job_type = 'inventory.scan'", cid
    )[0][0]
    assert scope["noAccess"] == ["snapshot:read"] and scope["cpu"] == "no_access"


async def test_one_workspace_per_team_and_no_duplicates(
    client: AsyncClient, login: Any, fake_do: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    token = fake_do.add_token()
    assert (await _connect(client, tenant, headers, token)).status_code == 201
    assert (await _connect(client, tenant, headers, token)).status_code == 409
    other, other_headers = await workspace(client, login)
    second = fake_do.add_token(team=fake_do.team_for(token).uuid)
    r = await _connect(client, other, other_headers, second)
    assert r.status_code == 409 and "another Cloudoptics workspace" in r.text
    # And the other workspace cannot see the first one's connection.
    listed = await client.get(f"/v1/tenants/{other}/connections", headers=other_headers)
    assert listed.json() == []


async def test_replace_disconnect_and_reconnect(
    client: AsyncClient, login: Any, owner_sql: Sql, fake_do: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    token = fake_do.add_token()
    team = fake_do.team_for(token).uuid
    cid = (await _connect(client, tenant, headers, token)).json()["id"]
    base = f"/v1/tenants/{tenant}/connections/{cid}"
    before = owner_sql("SELECT external_id_enc FROM cloud_connection WHERE id = %s", cid)[0][0]

    other_team = fake_do.add_token()
    r = await client.put(f"{base}/token", json={"token": other_team}, headers=headers)
    assert r.status_code == 409 and "different DigitalOcean team" in r.text
    writer = fake_do.add_token(team=team, scopes=READ_SCOPES | {"tag:create"})
    r = await client.put(f"{base}/token", json={"token": writer}, headers=headers)
    assert r.status_code == 422
    rotated = fake_do.add_token(team=team)
    r = await client.put(f"{base}/token", json={"token": rotated}, headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "healthy" and rotated not in r.text
    after = owner_sql("SELECT external_id_enc FROM cloud_connection WHERE id = %s", cid)[0][0]
    assert bytes(after) != bytes(before)

    # Check again (inline validation dispatches to the plug-in).
    r = await client.post(f"{base}/validate", headers=headers)
    assert r.status_code == 200 and r.json()["provider"] == "digitalocean"
    # The AWS stack page and Scan now.
    assert (await client.get(f"{base}/setup", headers=headers)).status_code == 409
    scan = await client.post(f"{base}/scan", headers=headers)
    assert scan.status_code == 200 and scan.json()["status"] == "succeeded"

    assert (await client.delete(base, headers=headers)).status_code == 204
    assert owner_sql("SELECT status, external_id_enc FROM cloud_connection WHERE id = %s", cid) == [
        ("disconnected", None)
    ]
    assert (await billing.ingest_connection(uuid.UUID(tenant), uuid.UUID(cid))).status == "skipped"
    assert await inventory.scan_connection(uuid.UUID(tenant), uuid.UUID(cid)) == "skipped"
    r = await client.put(f"{base}/token", json={"token": rotated}, headers=headers)
    assert r.status_code == 409

    again = await _connect(client, tenant, headers, rotated)
    assert again.status_code == 201 and again.json()["id"] == cid
    actions = [r[0] for r in owner_sql(
        "SELECT action FROM audit_log WHERE tenant_id = %s ORDER BY id", tenant)]  # fmt: skip
    for action in ("connection.token_replaced", "connection.disconnected",
                   "connection.reconnected"):  # fmt: skip
        assert action in actions
    texts = owner_sql(
        "SELECT string_agg(row_to_json(a)::text, '') FROM audit_log a WHERE tenant_id = %s",
        tenant,
    )[0][0]
    for t in (token, rotated, writer, other_team):
        assert t not in texts


async def test_jobs_dispatch_to_the_plug_in(
    client: AsyncClient, login: Any, owner_sql: Sql, fake_do: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    cid = (await _connect(client, tenant, headers, fake_do.add_token())).json()["id"]
    assert await ingestion_jobs.ingest(tenant, cid) == "up_to_date"
    assert await ingestion_jobs.snapshot(tenant, cid) == "fresh"
    period = prev_month(TODAY).isoformat()
    assert await ingestion_jobs.reconcile(tenant, cid, period) == "ok"
    from cloudoptics.connectors import jobs as connector_jobs

    assert await connector_jobs.check_connection(tenant, cid) == "healthy"
    reloaded = await billing.reload_connection(uuid.UUID(tenant), uuid.UUID(cid), None)
    assert reloaded.status == "succeeded" and len(reloaded.reloaded) == 2
    assert (
        owner_sql(
            "SELECT count(DISTINCT billing_period) FROM cost_daily WHERE connection_id = %s", cid
        )[0][0]
        == 2
    )


async def test_reconciliation_flags_a_mismatch(
    client: AsyncClient, login: Any, owner_sql: Sql, fake_do: FakeDigitalOcean
) -> None:
    tenant, headers = await workspace(client, login)
    cid = (await _connect(client, tenant, headers, fake_do.add_token())).json()["id"]
    period = prev_month(TODAY)
    owner_sql(
        "UPDATE cost_daily SET billed_cost_usd = billed_cost_usd + 50 WHERE connection_id = %s "
        "AND billing_period = %s AND charge_category = 'Tax'",
        cid, period,
    )  # fmt: skip
    assert await billing.reconcile_period(uuid.UUID(tenant), uuid.UUID(cid), period) == "mismatch"
    old = prev_month(prev_month(prev_month(TODAY)))
    assert await billing.reconcile_period(uuid.UUID(tenant), uuid.UUID(cid), old) == "skipped"


async def test_dead_api_fails_softly(
    client: AsyncClient,
    login: Any,
    owner_sql: Sql,
    fake_do: FakeDigitalOcean,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant, headers = await workspace(client, login)
    cid = (await _connect(client, tenant, headers, fake_do.add_token())).json()["id"]
    t, c = uuid.UUID(tenant), uuid.UUID(cid)
    fake_do.unavailable = 1000
    monkeypatch.setattr(do_client, "MAX_RETRIES", 0)
    assert (await billing.refresh_snapshot(t, c, force=True)) == "failed"
    assert (await billing.reload_connection(t, c, None)).status == "failed"
    assert (await billing.reconcile_period(t, c, prev_month(TODAY))) == "failed"
    assert await inventory.scan_connection(t, c) == "partial"
    result = await billing.ingest_connection(t, c, TODAY + timedelta(days=62))
    assert result.status == "failed"
    assert (
        owner_sql(
            "SELECT count(*) FROM resource WHERE connection_id = %s AND deleted_at IS NULL", cid
        )[0][0]
        == 8
    )


def test_secret_check_constraint(owner_sql: Sql) -> None:
    tenant = str(uuid.uuid4())
    owner_sql(
        "INSERT INTO tenant (id, name, country_code, display_currency) VALUES "
        "(%s, 'x', 'NG', 'NGN')",
        tenant,
    )
    for provider, account, state in (("aws", "123456789012", "disconnected"),
                                     ("digitalocean", str(uuid.uuid4()), "healthy")):  # fmt: skip
        with pytest.raises(psycopg.errors.CheckViolation):
            owner_sql(
                "INSERT INTO cloud_connection (id, tenant_id, provider, provider_account_id, "
                "account_type, role_arn, external_id_enc, stack_name, status) VALUES "
                "(%s, %s, %s, %s, 'standalone', '', NULL, 's', %s)",
                str(uuid.uuid4()), tenant, provider, account, state,
            )  # fmt: skip
    owner_sql(
        "INSERT INTO cloud_connection (id, tenant_id, provider, provider_account_id, "
        "account_type, role_arn, external_id_enc, stack_name, status) VALUES "
        "(%s, %s, 'digitalocean', %s, 'standalone', '', NULL, 's', 'disconnected')",
        str(uuid.uuid4()), tenant, str(uuid.uuid4()),
    )  # fmt: skip
    owner_sql("DELETE FROM tenant WHERE id = %s", tenant)
