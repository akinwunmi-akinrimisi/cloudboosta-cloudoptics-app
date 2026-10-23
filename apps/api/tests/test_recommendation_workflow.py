"""Assigning, commenting, history, filters, bulk changes and rule settings."""

import uuid
from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from test_inventory import setup, world  # noqa: F401 - fixture

from cloudoptics.analysis import engine
from cloudoptics.inventory import scanner


async def analysed(client: AsyncClient, login, owner_sql, world):  # type: ignore[no-untyped-def]  # noqa: F811
    tenant, connection, _, headers = await setup(client, login, owner_sql)
    world.no_access = {"555555555555"}
    await scanner.scan_connection(uuid.UUID(tenant), uuid.UUID(connection))
    await engine.run_analysis(uuid.UUID(tenant), now=datetime.now(UTC) + timedelta(days=40))
    return tenant, headers, f"/v1/tenants/{tenant}/recommendations"


async def teammate(
    client: AsyncClient, login, owner_sql, tenant: str, role: str
) -> tuple[str, dict[str, str]]:  # type: ignore[no-untyped-def]
    email = f"{role}-{uuid.uuid4().hex[:6]}@example.com"
    headers = await login(email)
    await client.get("/v1/me", headers=headers)
    owner_sql(
        "INSERT INTO membership (tenant_id, user_id, role) SELECT %s, id, %s FROM app_user "
        "WHERE email = %s",
        tenant,
        role,
        email,
    )
    user = owner_sql("SELECT id::text FROM app_user WHERE email = %s", email)[0][0]
    return user, headers


async def test_assign_comment_and_history(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tenant, headers, base = await analysed(client, login, owner_sql, world)
    sam, sam_headers = await teammate(client, login, owner_sql, tenant, "member")
    rec = (await client.get(base, headers=headers)).json()[0]

    r = await client.patch(f"{base}/{rec['id']}", json={"assigneeId": sam}, headers=headers)
    assert r.status_code == 200
    assert (r.json()["assigneeId"], r.json()["status"]) == (sam, "assigned")

    mine = (await client.get(base, params={"assignee": "me"}, headers=sam_headers)).json()
    assert [m["id"] for m in mine] == [rec["id"]]
    assert (await client.get(base, params={"assignee": "me"}, headers=headers)).json() == []

    r = await client.post(
        f"{base}/{rec['id']}/comments", json={"body": "  On it this week "}, headers=sam_headers
    )
    assert r.status_code == 201
    r = await client.patch(
        f"{base}/{rec['id']}", json={"status": "in_progress"}, headers=sam_headers
    )
    assert r.json()["status"] == "in_progress"

    events = (await client.get(f"{base}/{rec['id']}/events", headers=headers)).json()
    assert [(e["kind"], e["toStatus"], e["body"]) for e in events] == [
        ("assigned", None, None),
        ("status_changed", "assigned", None),
        ("commented", None, "On it this week"),
        ("status_changed", "in_progress", None),
    ]
    assert events[0]["assigneeId"] == sam and events[0]["assigneeName"] == "Test User"
    assert events[2]["actorId"] == sam

    # Unassigning an assigned one puts it back to new; strangers cannot be assigned.
    other = await client.get(base, headers=headers)
    second = other.json()[1]
    await client.patch(f"{base}/{second['id']}", json={"assigneeId": sam}, headers=headers)
    r = await client.patch(f"{base}/{second['id']}", json={"assigneeId": None}, headers=headers)
    assert (r.json()["assigneeId"], r.json()["status"]) == (None, "new")
    stranger = str(uuid.uuid4())
    r = await client.patch(f"{base}/{second['id']}", json={"assigneeId": stranger}, headers=headers)
    assert r.status_code == 422
    assert (
        await client.patch(f"{base}/{second['id']}", json={}, headers=headers)
    ).status_code == 422
    missing = f"{base}/{uuid.uuid4()}"
    assert (
        await client.patch(missing, json={"status": "done"}, headers=headers)
    ).status_code == 404
    assert (await client.get(f"{missing}/events", headers=headers)).status_code == 404

    # Viewers can read the history but not comment.
    _, viewer = await teammate(client, login, owner_sql, tenant, "viewer")
    assert (await client.get(f"{base}/{rec['id']}/events", headers=viewer)).status_code == 200
    r = await client.post(f"{base}/{rec['id']}/comments", json={"body": "hi"}, headers=viewer)
    assert r.status_code == 403


async def test_filters_and_bulk_changes(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tenant, headers, base = await analysed(client, login, owner_sql, world)
    recs = (await client.get(base, headers=headers)).json()
    risks = {r["riskLevel"] for r in recs}
    for level in risks:
        only = (await client.get(base, params={"risk": level}, headers=headers)).json()
        assert only and {r["riskLevel"] for r in only} == {level}
    assert (await client.get(base, params={"assignee": "nope"}, headers=headers)).status_code == 422

    ids = [r["id"] for r in recs[:3]]
    bad = await client.post(
        f"{base}/bulk", json={"ids": ids, "status": "dismissed"}, headers=headers
    )
    assert bad.status_code == 422
    r = await client.post(
        f"{base}/bulk",
        json={
            "ids": [*ids, ids[0], str(uuid.uuid4())],
            "status": "dismissed",
            "reason": "Test kit",
        },
        headers=headers,
    )
    assert r.json() == {"updated": 3}
    left = {x["id"] for x in (await client.get(base, headers=headers)).json()}
    assert not left & set(ids)
    assert owner_sql(
        "SELECT count(*) FROM recommendation_event WHERE body = 'Test kit' AND tenant_id = %s",
        tenant,
    ) == [(3,)]


async def test_rule_settings_rerun_the_rules(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tenant, headers, base = await analysed(client, login, owner_sql, world)
    url = f"/v1/tenants/{tenant}/rule-settings"
    view = (await client.get(url, headers=headers)).json()
    assert "R4_idle_ec2" in view["rules"]
    assert view["settings"] == {
        "disabledRules": [],
        "ignoreTag": "cloudoptics:ignore",
        "idleCpuPercent": 5.0,
        "idleDays": 14,
    }
    before = {r["ruleId"] for r in (await client.get(base, headers=headers)).json()}
    assert "R2_idle_eip" in before

    r = await client.put(
        url,
        json={
            "disabledRules": ["R2_idle_eip"],
            "ignoreTag": "keep",
            "idleCpuPercent": 3,
            "idleDays": 21,
        },
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["settings"]["disabledRules"] == ["R2_idle_eip"]
    after = {r["ruleId"] for r in (await client.get(base, headers=headers)).json()}
    assert "R2_idle_eip" not in after  # turned off: resolved straight away
    assert (await client.get(url, headers=headers)).json()["settings"]["idleDays"] == 21
    assert owner_sql(
        "SELECT action FROM audit_log WHERE action = 'rule_settings.updated' AND tenant_id = %s",
        tenant,
    ) == [("rule_settings.updated",)]

    for body in (
        {"disabledRules": ["R99"]},
        {"idleCpuPercent": 50},
        {"idleDays": 2},
        {"ignoreTag": ""},
    ):
        assert (await client.put(url, json=body, headers=headers)).status_code == 422
    _, member = await teammate(client, login, owner_sql, tenant, "member")
    assert (await client.get(url, headers=member)).status_code == 200
    assert (await client.put(url, json={}, headers=member)).status_code == 403
