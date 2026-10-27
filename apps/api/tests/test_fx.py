"""Exchange rates: staff entry, lookup rules, the reference feed and FX decomposition.

``fx_rate`` is global and shared by the test session, so each test uses its own currency
or date range.
"""

import dataclasses
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import AsyncClient

from cloudoptics.fx import feed, service
from cloudoptics.identity.auth import DevVerifier
from cloudoptics.platform.settings import get_settings

STAFF = "rates-desk@cloudoptics.example"


@pytest.fixture
def staff_list(app) -> Iterator[None]:  # type: ignore[no-untyped-def]
    settings = get_settings()
    settings.platform_admin_emails = f"someone@else.example, {STAFF.upper()}"
    yield
    settings.platform_admin_emails = ""


async def workspace(client: AsyncClient, login, country: str) -> tuple[str, dict[str, str]]:  # type: ignore[no-untyped-def]
    headers = await login()
    r = await client.post(
        "/v1/tenants", json={"name": "W", "countryCode": country}, headers=headers
    )
    return r.json()["id"], headers


async def current(client: AsyncClient, tenant: str, headers: dict[str, str], day: str) -> dict:  # type: ignore[type-arg]
    r = await client.get(f"/v1/tenants/{tenant}/fx/current", params={"day": day}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def test_decomposition_splits_usage_and_fx_effects() -> None:
    result = service.decompose(prev_usd=1000, prev_rate=1500, cur_usd=1100, cur_rate=1600)
    assert result.usage_effect == 150_000  # 100 USD more at the old rate
    assert result.fx_effect == 110_000  # this month's USD at the rate change
    assert result.total == 1100 * 1600 - 1000 * 1500


async def test_only_staff_can_set_rates(client: AsyncClient, login, staff_list) -> None:
    body = {"currency": "GHS", "rate": "15.5", "rateDate": "2026-04-01", "reason": "back-fill"}
    r = await client.post("/v1/admin/fx/rates", json=body, headers=await login())
    assert r.status_code == 403

    staff = await login(STAFF)
    r = await client.post("/v1/admin/fx/rates", json={**body, "currency": "USD"}, headers=staff)
    assert r.status_code == 422
    # A staff address whose owner has not confirmed it is not staff: anyone can sign up with it.
    verify = DevVerifier.verify

    async def unconfirmed(self: DevVerifier, token: str) -> Any:
        return dataclasses.replace(await verify(self, token), email_verified=False)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(DevVerifier, "verify", unconfirmed)
        r = await client.post("/v1/admin/fx/rates", json=body, headers=staff)
        assert r.status_code == 403
    r = await client.post("/v1/admin/fx/rates", json={**body, "reason": None}, headers=staff)
    assert r.status_code == 422, "a back-dated rate needs a reason"


async def test_manual_rates_carry_forward_five_days(
    client: AsyncClient, login, owner_sql, staff_list
) -> None:
    staff = await login(STAFF)
    tenant, headers = await workspace(client, login, "NG")
    assert (await current(client, tenant, headers, "2026-05-01"))["quote"] is None

    r = await client.post(
        "/v1/admin/fx/rates",
        json={
            "currency": "NGN",
            "rate": "1530.25",
            "rateDate": "2026-05-01",
            "sourceNote": "CBN closing",
            "reason": "back-fill",
        },
        headers=staff,
    )
    assert r.status_code == 201, r.text

    same_day = await current(client, tenant, headers, "2026-05-01")
    assert same_day == {
        "currency": "NGN",
        "basis": "official",
        "quote": {
            "currency": "NGN",
            "rate": 1530.25,
            "rateDate": "2026-05-01",
            "basis": "official",
            "source": "manual",
            "estimated": False,
        },
    }
    carried = (await current(client, tenant, headers, "2026-05-06"))["quote"]
    assert carried["rateDate"] == "2026-05-01"
    assert carried["estimated"] is True
    assert (await current(client, tenant, headers, "2026-05-07"))["quote"] is None

    # A move over 5% needs confirming; the confirmed change is audited on the platform chain.
    big = {"currency": "NGN", "rate": "1650", "rateDate": "2026-05-02", "reason": "devaluation"}
    r = await client.post("/v1/admin/fx/rates", json=big, headers=staff)
    assert r.status_code == 409
    assert "+7.8%" in r.json()["title"]
    r = await client.post("/v1/admin/fx/rates", json={**big, "confirm": True}, headers=staff)
    assert r.status_code == 201
    assert (await current(client, tenant, headers, "2026-05-03"))["quote"]["rate"] == 1650

    audit = owner_sql(
        "SELECT actor_type, after->>'second_approval_required', after->>'change' "
        "FROM audit_log WHERE tenant_id IS NULL AND action = 'fx_rate.set' "
        "AND target_id = 'NGN:official:2026-05-02'"
    )
    assert audit == [("staff", "false", "0.0783")]

    history = await client.get(
        "/v1/fx/rates",
        params={"currency": "NGN", "from": "2026-05-01", "to": "2026-05-31"},
        headers=headers,
    )
    assert history.status_code == 200
    entries = history.json()
    assert [(e["rateDate"], e["rate"], e["enteredBy"]) for e in entries] == [
        ("2026-05-01", 1530.25, STAFF),
        ("2026-05-02", 1650.0, STAFF),
    ]


async def test_feed_is_used_only_without_manual_rates(
    client: AsyncClient, login, monkeypatch, staff_list
) -> None:
    tenant, headers = await workspace(client, login, "KE")
    today = datetime.now(UTC).date()

    async def fake_fetch(url: str) -> dict[str, float]:
        assert url == "https://rates.example/latest/USD"
        return {"KES": 129.4, "TZS": 2600.0}

    settings = get_settings()
    monkeypatch.setattr(feed, "fetch_feed", fake_fetch)
    assert await feed.fetch(0) == 0, "no feed configured"
    monkeypatch.setattr(settings, "fx_feed_url", "https://rates.example/latest/USD")
    assert await feed.fetch(0) == 2
    assert await feed.fetch(0) == 0, "one feed row per currency and day"

    quote = (await current(client, tenant, headers, today.isoformat()))["quote"]
    assert quote["source"] == "feed"
    assert quote["estimated"] is True
    assert quote["rate"] == 129.4

    # Once staff have set any KES rate, the feed is no longer used for KES.
    staff = await login(STAFF)
    past = today - timedelta(days=30)
    r = await client.post(
        "/v1/admin/fx/rates",
        json={"currency": "KES", "rate": "128", "rateDate": past.isoformat(), "reason": "x"},
        headers=staff,
    )
    assert r.status_code == 201, r.text
    assert (await current(client, tenant, headers, today.isoformat()))["quote"] is None

    # A rate close to both the previous rate and the feed needs no confirmation.
    r = await client.post(
        "/v1/admin/fx/rates", json={"currency": "KES", "rate": "129"}, headers=staff
    )
    assert r.status_code == 201, r.text
    # With no manual history, a new rate is checked against the feed.
    r = await client.post(
        "/v1/admin/fx/rates", json={"currency": "TZS", "rate": "2900"}, headers=staff
    )
    assert r.status_code == 409
    assert "feed rate (2600)" in r.json()["title"]


def test_parse_feed() -> None:
    body = {"result": "success", "base_code": "USD", "rates": {"NGN": 1531.2, "JPY": 150, "EUR": 0}}
    assert feed.parse_feed(body) == {"NGN": 1531.2}
    with pytest.raises(feed.FeedError):
        feed.parse_feed({"base_code": "EUR", "rates": {}})
    with pytest.raises(feed.FeedError):
        feed.parse_feed(["not", "a", "dict"])


async def test_fetch_feed_reads_json() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"base": "USD", "rates": {"ZAR": 18.1}})
    )
    http = httpx.AsyncClient(transport=transport)  # fetch_feed opens and closes it
    assert await feed.fetch_feed("https://rates.example/x", http) == {"ZAR": 18.1}


async def test_usd_is_always_one(client: AsyncClient, login) -> None:
    headers = await login()
    r = await client.post(
        "/v1/tenants",
        json={"name": "W", "countryCode": "NG", "displayCurrency": "USD"},
        headers=headers,
    )
    body = await current(client, r.json()["id"], headers, "2026-01-01")
    assert body["quote"]["rate"] == 1
