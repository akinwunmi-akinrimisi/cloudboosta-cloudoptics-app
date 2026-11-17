"""AWS credits runway (PRD US-2.4, FR-ANA-11)."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from httpx import AsyncClient
from test_anomalies import cost
from test_budgets import workspace
from test_digest import member

from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.reporting import digest

AUG2, SEP1, SEP30 = date(2026, 8, 2), date(2026, 9, 1), date(2026, 9, 30)


async def test_runway_from_the_bills_and_the_entered_balance(
    client: AsyncClient, login, owner_sql
) -> None:
    tenant, admin, connection = await workspace(client, login, owner_sql)
    tid = tenant["id"]
    url = f"/v1/tenants/{tid}/costs/credits"
    assert (await client.get(url, headers=admin)).json()["status"] == "no_credits"

    # USD 20 a day of EC2. Credits: 5 a day in August, 15 a day in September.
    day = AUG2
    while day <= SEP30:
        cost(owner_sql, tenant, connection, day, 20)
        cost(owner_sql, tenant, connection, day, -(15 if day >= SEP1 else 5), category="Credit")
        day += timedelta(days=1)

    c = (await client.get(url, headers=admin)).json()
    assert c["status"] == "no_balance"  # credits applied, but no balance to count down
    assert (c["dataThrough"], c["burnUsdDay"], c["burnDays"]) == ("2026-09-30", 15, 30)
    assert c["appliedMonthUsd"] == 450 and c["spendBeforeCreditsUsdMonth"] == 600
    assert c["remainingUsd"] is None and len(c["daily"]) == 30
    assert c["daily"][-1] == {"date": "2026-09-30", "amount": 15}

    viewer_email, viewer = await member(client, login, owner_sql, tenant, "viewer")
    balance = f"{url}/balance"
    body = {"amountUsd": 1500, "asOf": "2026-09-20", "expiresOn": "2027-06-30"}
    assert (await client.put(balance, json=body, headers=viewer)).status_code == 403
    r = await client.put(balance, json={**body, "note": " Activate "}, headers=admin)
    c = r.json()
    # 1,500 less 10 days at 15 since 20 Sep = 1,350, 90 days at 15 a day.
    assert (c["usedSinceBalanceUsd"], c["remainingUsd"], c["daysLeft"]) == (150, 1350, 90)
    assert (c["depletionDate"], c["status"], c["expiresFirst"]) == ("2026-12-29", "ok", False)
    assert c["balance"]["note"] == "Activate" and c["balance"]["enteredBy"]

    c = (await client.put(balance, json={**body, "amountUsd": 1000}, headers=admin)).json()
    assert (c["remainingUsd"], c["daysLeft"], c["status"]) == (850, 56, "ending_soon")
    assert c["depletionDate"] == "2026-11-25"
    # Expiring at the end of October, before they would run out.
    c = (
        await client.put(balance, json={**body, "amountUsd": 1000, "expiresOn": "2026-10-31"},
                         headers=admin)
    ).json()  # fmt: skip
    assert (c["depletionDate"], c["daysLeft"], c["expiresFirst"]) == ("2026-10-31", 31, True)

    # The weekly digest and the monthly report say so.
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=uuid.UUID(tid))
        built = await digest.build(session, uuid.UUID(tid), date(2026, 9, 21))
    assert built is not None
    _, text_, _ = digest.render(built, "https://app.example")
    # As of the week's last day, 27 Sep: 1,000 - 7 x 15 = 895 left; burn over 29 Aug to
    # 27 Sep is (3 x 5 + 27 x 15) / 30 = 14 a day.
    assert (
        "Ending soon. USD 895.00 of credits left at USD 14.00 a day: they expire around "
        "31 Oct 2026 (about 34 days); the bill is then about USD 600.00 a month." in text_
    )
    report = (await client.get(f"/v1/tenants/{tid}/reports/2026-09", headers=viewer)).json()
    assert report["report"]["credits"].startswith("Ending soon. USD 850.00 of credits left")

    c = (await client.put(balance, json={"amountUsd": 100, "asOf": "2026-09-20"},
                          headers=admin)).json()  # fmt: skip
    assert (c["status"], c["remainingUsd"]) == ("used_up", 0)

    for bad in (
        {"amountUsd": -1},
        {"amountUsd": 10, "asOf": "2099-01-01"},
        {"amountUsd": 10, "asOf": "2026-09-20", "expiresOn": "2026-09-01"},
    ):
        assert (await client.put(balance, json=bad, headers=admin)).status_code == 422
    assert owner_sql(
        "SELECT count(*) FROM audit_log WHERE tenant_id = %s AND action = 'credits.balance_set'",
        tid,
    ) == [(4,)]
    assert viewer_email  # the viewer reads the runway too
    assert (await client.get(url, headers=viewer)).json()["status"] == "used_up"
