"""Verification and realised savings (PRD US-5.1/5.2, FR-REC-07): per-rule checks, the
status changes, the 72-hour deadline, before/after bills, assignment emails and jobs."""

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pyarrow.compute as pc
import pytest
from httpx import AsyncClient
from test_ingestion_pipeline import SEPT, focus_of
from test_inventory import setup, world  # noqa: F401 - fixture
from test_notifications import mailer  # noqa: F401 - fixture
from test_recommendation_workflow import teammate

from cloudoptics.analysis import engine
from cloudoptics.analysis.settings import RuleSettings
from cloudoptics.ingestion.aggregate import DailyAggregator
from cloudoptics.inventory import scanner
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.recommendations import jobs, verification
from cloudoptics.recommendations.verification import Check, Item, Observed
from cur_fixture import generate

LATER = datetime.now(UTC) + timedelta(days=40)
NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)


async def scanned(client: AsyncClient, login, owner_sql, world):  # type: ignore[no-untyped-def]  # noqa: F811
    tenant, connection, account, headers = await setup(client, login, owner_sql)
    world.no_access = {"555555555555"}
    tid = uuid.UUID(tenant)
    await scanner.scan_connection(tid, uuid.UUID(connection))
    await engine.run_analysis(tid, now=LATER)
    base = f"/v1/tenants/{tenant}/recommendations"
    recs = (await client.get(f"{base}?alternatives=true", headers=headers)).json()
    return tid, connection, account, headers, base, recs


def pick(recs: list[dict[str, Any]], rule: str, region: str = "eu-west-1") -> dict[str, Any]:
    return next(r for r in recs if r["ruleId"] == rule and r["regionId"] == region)


async def verify_at(tid: uuid.UUID, now: datetime) -> dict[str, int]:
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        return await verification.verify(session, tid, now)


def status_of(owner_sql, rec_id: str) -> str:  # type: ignore[no-untyped-def]
    return str(owner_sql("SELECT status FROM recommendation WHERE id = %s", rec_id)[0][0])


# Per-rule checks and the decision, without a database.


def item(
    rule: str,
    *,
    status: str = "done",
    state: str | None = "available",
    config: dict[str, Any] | None = None,
    deleted: bool = False,
    missing: bool = False,
    stale: bool = False,
    suppressed: bool = False,
    done_at: datetime | None = None,
    recommended: dict[str, Any] | None = None,
    fix: dict[str, Any] | None = None,
) -> Item:
    seen = NOW - timedelta(hours=1)
    resource = (
        None
        if missing
        else Observed(
            resource_type="x",
            state=state,
            config=config or {},
            tags={},
            arn=None,
            state_since=NOW - timedelta(days=2),
            last_seen_at=seen,
            deleted_at=seen if deleted else None,
        )
    )
    return Item(
        id=uuid.uuid4(),
        rule_id=rule,
        status=status,
        account_id="111122223333",
        resource_id="r-1",
        current_config={},
        recommended_config=recommended or {},
        cost_now_usd_month=10.0,
        estimate_usd_month=10.0,
        fix=fix or {},
        done_at=done_at,
        stale=stale,
        suppressed=suppressed,
        resource=resource,
    )


def run_check(i: Item) -> Check:
    return verification.check_for(i.rule_id)(i)


def test_rule_checks() -> None:
    assert run_check(item("R1_unattached_ebs", deleted=True)).code == "volume_deleted"
    assert run_check(item("R1_unattached_ebs")).outcome == "not_implemented"
    assert run_check(item("R2_idle_eip", missing=True)).code == "address_released"
    assert run_check(item("R2_idle_lb", deleted=True)).code == "load_balancer_deleted"
    archived = run_check(
        item(
            "R1_old_snapshot",
            config={"storageTier": "archive"},
            fix={"alternative": {"monthlyCostUsd": 2.5}},
        )
    )
    assert (archived.code, archived.estimate_usd_month) == ("snapshot_archived", 7.5)
    assert run_check(item("R1_old_snapshot", deleted=True)).code == "snapshot_deleted"
    assert run_check(item("R1_old_snapshot")).outcome == "not_implemented"
    assert run_check(item("R3_gp2_gp3", config={"volumeType": "gp3"})).code == "volume_type_gp3"
    assert run_check(item("R3_gp2_gp3", deleted=True)).outcome == "gone"
    assert run_check(item("R3_gp2_gp3", config={"volumeType": "gp2"})).code == "unchanged"
    assert run_check(item("R4_idle_ec2", state="stopped")).code == "instance_stopped"
    assert run_check(item("R4_idle_ec2", state="terminated")).code == "instance_terminated"
    assert run_check(item("R4_idle_ec2", missing=True)).code == "instance_terminated"
    assert run_check(item("R4_idle_ec2", state="running")).outcome == "not_implemented"
    assert run_check(item("R4_idle_rds", state="stopped")).code == "database_stopped"
    assert run_check(item("R4_idle_rds", state="deleting")).code == "database_deleted"
    assert run_check(item("R4_idle_rds")).outcome == "not_implemented"
    for rule in ("R5_rightsize_ec2", "R6_newer_generation", "R6_graviton"):
        target = {"instanceType": "m7g.large"}
        changed = item(rule, config={"instanceType": "m7g.large"}, recommended=target)
        assert run_check(changed).code == "instance_type_changed"
        other = item(rule, config={"instanceType": "m6i.large"}, recommended=target)
        assert run_check(other).outcome == "not_implemented"
        assert run_check(item(rule, state="terminated", recommended=target)).outcome == "gone"


def test_rules_without_a_check_use_the_default() -> None:
    # A future rule (R7, R8, R10 ...) is verified when its resource goes or it stops firing.
    assert verification.check_for("R99_future") is verification.default_check
    assert run_check(item("R99_future", deleted=True)).code == "resource_gone"
    assert run_check(item("R99_future", stale=True)).code == "rule_no_longer_applies"
    # Turned off or tagged to be ignored: no longer firing says nothing.
    assert run_check(item("R99_future", stale=True, suppressed=True)).code == "unchanged"
    assert run_check(item("R99_future")).code == "unchanged"


def test_decide() -> None:
    done = NOW - timedelta(hours=80)
    fixed = Check("implemented", "volume_deleted")
    unchanged = verification.UNCHANGED
    assert verification.decide(item("R1", status="done", done_at=done), fixed, NOW) == "verified"
    assert verification.decide(item("R1", status="new"), fixed, NOW) == "verified"
    assert verification.decide(item("R1", status="new", deleted=True), fixed, NOW) == (
        "resolved_externally"
    )
    assert verification.decide(item("R1", status="new", stale=True), unchanged, NOW) == (
        "resolved_externally"
    )
    assert verification.decide(item("R1", status="assigned"), unchanged, NOW) is None
    assert verification.decide(item("R1", done_at=done), unchanged, NOW) == "not_verified"
    recent = NOW - timedelta(hours=10)
    assert verification.decide(item("R1", done_at=recent), unchanged, NOW) is None
    # No scan since it was marked done: wait.
    late = NOW - timedelta(minutes=10)
    assert (
        verification.decide(item("R1", done_at=late), unchanged, late + timedelta(days=4)) is None
    )
    assert verification.decide(item("R1", status="not_verified"), unchanged, NOW) is None
    assert verification.decide(item("R1", status="not_verified"), fixed, NOW) == "verified"
    assert verification.decide(item("R1", status="dismissed"), fixed, NOW) is None


def test_bill_only_items_are_not_gone() -> None:
    """R6b/R10 (and R8) have no inventory resource: only the rule no longer firing counts."""
    from dataclasses import replace

    open_item = replace(item("R10_idle_nat", status="new", missing=True), bill_only=True)
    assert not open_item.gone
    result = verification.default_check(open_item)
    assert result == verification.UNCHANGED
    assert verification.decide(open_item, result, NOW) is None

    stopped = replace(open_item, stale=True)
    result = verification.default_check(stopped)
    assert (result.outcome, result.code) == ("implemented", "rule_no_longer_applies")
    assert verification.decide(stopped, result, NOW) == "verified"

    off = replace(stopped, suppressed=True)
    assert verification.default_check(off) == verification.UNCHANGED

    done = replace(open_item, status="done", done_at=NOW - timedelta(days=4))
    assert verification.decide(done, verification.UNCHANGED, NOW) == "not_verified"


def test_compare_before_and_after() -> None:
    change = date(2026, 10, 1)
    daily = {change - timedelta(days=d): 2.0 for d in range(1, 41)}
    daily[change] = 5.0  # the change day counts in neither window
    daily.update({change + timedelta(days=d): 0.5 for d in range(1, 11)})
    m = verification.compare(daily, change, change + timedelta(days=10))
    assert (m.before_days, m.after_days) == (30, 10)
    assert m.realised_usd_month == pytest.approx((2.0 - 0.5) * 30)
    # A resource younger than 30 days: averaged over the days it existed.
    young = {change - timedelta(days=d): 3.0 for d in range(1, 6)}
    m = verification.compare(young, change, change + timedelta(days=8))
    assert (m.before_days, m.after_days, m.realised_usd_month) == (5, 8, 90.0)
    # No cost before the change: nothing to compare.
    assert verification.compare({}, change, change + timedelta(days=9)).realised_usd_month is None
    assert verification.compare(young, change, change).realised_usd_month is None
    assert verification.resource_keys("eipalloc-1", None, {"publicIp": "203.0.113.5"}) == [
        "eipalloc-1",
        "203.0.113.5",
    ]


def test_resource_costs_from_the_cur() -> None:
    focus = focus_of(generate(SEPT).table)
    agg = DailyAggregator()
    agg.update(focus)
    costs = agg.resource_rows()
    assert costs and all(c.resource_id for c in costs)
    usage = focus.filter(
        pc.and_(pc.equal(focus["ChargeCategory"], "Usage"), pc.is_valid(focus["ResourceId"]))
    )
    total = pc.sum(usage["EffectiveCost"]).as_py()
    assert float(sum(c.effective_cost for c in costs)) == pytest.approx(total, abs=0.01)
    keys = {
        (
            c.usage_date,
            c.account_id,
            c.service_code,
            c.region_id,
            c.usage_type,
            c.resource_id,
            c.tag_set_hash,
        )
        for c in costs
    }
    assert len(keys) == len(costs)


# Against the database.


async def test_done_is_verified_with_events_and_audit(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tid, _, _, headers, base, recs = await scanned(client, login, owner_sql, world)
    lb = pick(recs, "R2_idle_lb")
    r = await client.patch(f"{base}/{lb['id']}", json={"status": "done"}, headers=headers)
    assert r.json()["status"] == "done" and r.json()["doneAt"] is not None
    summary = (await client.get(f"{base}/summary", headers=headers)).json()
    assert summary["awaitingVerificationCount"] == 1

    # Not changed yet, and less than 72 hours: stays done; the check is kept.
    await engine.run_analysis(tid, now=LATER)
    detail = (await client.get(f"{base}/{lb['id']}", headers=headers)).json()
    assert detail["status"] == "done"
    assert detail["verification"]["check"] == "unchanged"

    owner_sql(
        "UPDATE resource SET deleted_at = now() WHERE resource_id = 'idle' "
        "AND region_id = 'eu-west-1' AND tenant_id = %s",
        str(tid),
    )
    result = await engine.run_analysis(tid, now=LATER)
    assert result["verified"] >= 1
    detail = (await client.get(f"{base}/{lb['id']}", headers=headers)).json()
    assert detail["status"] == "verified" and detail["verifiedAt"] is not None
    assert detail["verification"]["check"] == "load_balancer_deleted"
    assert detail["changedAt"] <= detail["verifiedAt"]
    assert detail["realisedSavingsUsdMonth"] is None  # bills not in yet: estimated
    events = (await client.get(f"{base}/{lb['id']}/events", headers=headers)).json()
    last = events[-1]
    assert (last["kind"], last["toStatus"], last["actorId"]) == ("status_changed", "verified", None)
    assert last["body"] == "Checked in AWS. The load balancer is deleted."
    assert owner_sql(
        "SELECT actor_type, after->>'status' FROM audit_log WHERE target_id = %s "
        "AND actor_type = 'system'",
        lb["id"],
    ) == [("system", "verified")]

    summary = (await client.get(f"{base}/summary", headers=headers)).json()
    assert summary["verifiedCount"] == 1 and summary["awaitingVerificationCount"] == 0
    assert summary["estimatedRealisedUsdMonth"] == pytest.approx(lb["savingsUsdMonth"], abs=0.01)
    assert summary["realisedSavingsUsdMonth"] == 0

    # Reopening forgets the verification.
    r = await client.patch(f"{base}/{lb['id']}", json={"status": "new"}, headers=headers)
    body = r.json()
    assert (body["status"], body["verifiedAt"], body["verification"], body["doneAt"]) == (
        "new",
        None,
        None,
        None,
    )


async def test_changes_seen_without_done_and_resources_removed_outside(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tid, _, _, headers, base, recs = await scanned(client, login, owner_sql, world)
    gp3 = pick(recs, "R3_gp2_gp3")
    snapshot = pick(recs, "R1_old_snapshot")
    unattached = pick(recs, "R1_unattached_ebs")

    # The volume is changed to gp3 in AWS without anyone marking it done: verified and
    # credited. It is still unattached, so R1 stays open.
    owner_sql(
        "UPDATE resource SET config = jsonb_set(config, '{volumeType}', '\"gp3\"') "
        "WHERE resource_id = 'vol-loose' AND region_id = 'eu-west-1' AND tenant_id = %s",
        str(tid),
    )
    # The snapshot is deleted outside Cloudoptics: resolved externally.
    owner_sql(
        "UPDATE resource SET deleted_at = now() WHERE resource_id = 'snap-1' "
        "AND region_id = 'eu-west-1' AND tenant_id = %s",
        str(tid),
    )
    await engine.run_analysis(tid, now=LATER)
    assert status_of(owner_sql, gp3["id"]) == "verified"
    assert status_of(owner_sql, unattached["id"]) == "new"
    assert status_of(owner_sql, snapshot["id"]) == "resolved_externally"
    events = (await client.get(f"{base}/{gp3['id']}/events", headers=headers)).json()
    assert events[-1]["body"].startswith("Cloudoptics found the change was made without")
    events = (await client.get(f"{base}/{snapshot['id']}/events", headers=headers)).json()
    assert events[-1]["body"] == "The resource was removed outside Cloudoptics."


async def test_not_verified_after_72_hours_then_verified(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tid, _, _, headers, base, recs = await scanned(client, login, owner_sql, world)
    eip = pick(recs, "R2_idle_eip")
    await client.patch(f"{base}/{eip['id']}", json={"status": "done"}, headers=headers)
    owner_sql(
        "UPDATE recommendation SET done_at = now() - interval '4 days' WHERE id = %s", eip["id"]
    )
    result = await engine.run_analysis(tid, now=LATER)
    assert result["not_verified"] == 1
    detail = (await client.get(f"{base}/{eip['id']}", headers=headers)).json()
    assert detail["status"] == "not_verified" and detail["changedAt"] is None
    summary = (await client.get(f"{base}/summary", headers=headers)).json()
    assert summary["notVerifiedCount"] == 1

    # Released later: the next check verifies it.
    owner_sql(
        "UPDATE resource SET deleted_at = now() WHERE resource_id = 'eipalloc-free' "
        "AND region_id = 'eu-west-1' AND tenant_id = %s",
        str(tid),
    )
    result = await verify_at(tid, datetime.now(UTC))
    assert result["verified"] == 1
    assert status_of(owner_sql, eip["id"]) == "verified"


async def test_rule_settings_do_not_count_as_fixes(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tid, _, _, _, _, recs = await scanned(client, login, owner_sql, world)
    eip = pick(recs, "R2_idle_eip")
    off = RuleSettings(disabled_rules=frozenset({"R2_idle_eip"}))
    await engine.run_analysis(tid, now=LATER, settings=off)
    assert status_of(owner_sql, eip["id"]) == "resolved_externally"


async def test_realised_savings_from_the_bills(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
) -> None:
    tid, connection, account, headers, base, recs = await scanned(client, login, owner_sql, world)
    tenant = str(tid)
    lb = pick(recs, "R2_idle_lb")
    eip = pick(recs, "R2_idle_eip")
    snapshot = pick(recs, "R1_old_snapshot")
    pending = pick(recs, "R2_idle_lb", "af-south-1")
    for rec in (lb, eip, pending):
        await client.patch(f"{base}/{rec['id']}", json={"status": "done"}, headers=headers)
    owner_sql(
        "UPDATE resource SET deleted_at = now() WHERE tenant_id = %s AND ((region_id = "
        "'eu-west-1' AND resource_id IN ('idle', 'eipalloc-free', 'snap-1')) OR (region_id = "
        "'af-south-1' AND resource_id = 'idle'))",
        tenant,
    )
    await engine.run_analysis(tid, now=LATER)
    assert status_of(owner_sql, snapshot["id"]) == "resolved_externally"

    today = datetime.now(UTC).date()
    change = today - timedelta(days=10)
    owner_sql(
        "UPDATE recommendation SET changed_at = %s WHERE id IN (%s, %s, %s)",
        datetime(change.year, change.month, change.day, 12, tzinfo=UTC),
        lb["id"],
        eip["id"],
        snapshot["id"],
    )
    owner_sql(
        "UPDATE recommendation SET changed_at = now() - interval '3 days' WHERE id = %s",
        pending["id"],
    )
    # The bills run to yesterday; the load balancer cost 1.00 a day (by ARN) before the
    # change and nothing after; the snapshot (by ARN suffix) 0.40 a day, then 0.10.
    owner_sql(
        "INSERT INTO cost_daily (tenant_id, connection_id, usage_date, billing_period, "
        "account_id, service_code, service_name, service_category, charge_category, "
        "billed_cost_usd, effective_cost_usd, net_cost_usd, data_version) VALUES "
        "(%s, %s, %s, %s, %s, 'AmazonEC2', 'EC2', 'Compute', 'Usage', 1, 1, 1, 'v1')",
        tenant,
        connection,
        today - timedelta(days=1),
        today.replace(day=1),
        account,
    )
    snap_arn = f"arn:aws:ec2:eu-west-1:{account}:snapshot/snap-1"
    for d in range(1, 21):
        day = change + timedelta(days=d - 10)
        if day == change or day >= today:
            continue
        before = day < change
        rows = [("arn:lb/app/idle", 1.0 if before else 0.0), (snap_arn, 0.4 if before else 0.1)]
        for rid, cost in rows:
            owner_sql(
                "INSERT INTO cost_resource_daily (tenant_id, connection_id, usage_date, "
                "billing_period, account_id, service_code, usage_type, resource_id, "
                "effective_cost_usd, usage_quantity) "
                "VALUES (%s, %s, %s, %s, %s, 'AWSELB', 'LoadBalancerUsage', %s, %s, 24)",
                tenant,
                connection,
                day,
                day.replace(day=1),
                account,
                rid,
                cost,
            )

    result = await verify_at(tid, datetime.now(UTC))
    assert result["measured"] == 3  # lb and snapshot from the bills, eip estimated
    lb_now = (await client.get(f"{base}/{lb['id']}", headers=headers)).json()
    assert lb_now["realisedBasis"] == "cur"
    assert lb_now["realisedSavingsUsdMonth"] == pytest.approx(30.0)
    m = lb_now["verification"]["measurement"]
    assert (m["beforeDays"], m["afterDays"], m["beforeUsdDay"], m["afterUsdDay"]) == (
        9, 9, 1.0, 0.0
    )  # fmt: skip
    eip_now = (await client.get(f"{base}/{eip['id']}", headers=headers)).json()
    assert eip_now["realisedBasis"] == "estimate"
    assert eip_now["realisedSavingsUsdMonth"] == pytest.approx(eip["savingsUsdMonth"])
    snap_now = (await client.get(f"{base}/{snapshot['id']}", headers=headers)).json()
    assert snap_now["realisedBasis"] == "cur"
    assert snap_now["realisedSavingsUsdMonth"] == pytest.approx(9.0)
    pending_now = (await client.get(f"{base}/{pending['id']}", headers=headers)).json()
    assert pending_now["realisedSavingsUsdMonth"] is None  # under 7 days of bills after

    summary = (await client.get(f"{base}/summary", headers=headers)).json()
    assert summary["realisedSavingsUsdMonth"] == pytest.approx(39.0)
    assert summary["estimatedRealisedUsdMonth"] == pytest.approx(
        eip["savingsUsdMonth"] + pending["savingsUsdMonth"], abs=0.01
    )
    events = (await client.get(f"{base}/{lb['id']}/events", headers=headers)).json()
    assert events[-1]["kind"] == "savings_measured"
    assert owner_sql(
        "SELECT count(*) FROM audit_log WHERE action = 'recommendation.savings_measured' "
        "AND tenant_id = %s",
        tenant,
    ) == [(3,)]

    # Measured again next day: updated, but no second timeline event.
    await verify_at(tid, datetime.now(UTC))
    events = (await client.get(f"{base}/{lb['id']}/events", headers=headers)).json()
    assert [e["kind"] for e in events].count("savings_measured") == 1

    # The digest and the monthly report count what was verified in the period.
    from cloudoptics.recommendations import queries

    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        period = await queries.period(
            session, datetime.now(UTC) - timedelta(days=7), datetime.now(UTC) + timedelta(days=1)
        )
    assert period.verified_count == 3
    assert period.realised_usd_month == pytest.approx(
        30.0 + eip["savingsUsdMonth"] + pending["savingsUsdMonth"], abs=0.01
    )


async def test_assignees_get_an_email(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
    mailer,  # noqa: F811
) -> None:
    tid, _, _, headers, base, recs = await scanned(client, login, owner_sql, world)
    sam, sam_headers = await teammate(client, login, owner_sql, str(tid), "member")
    first, second, third = recs[0], recs[1], recs[2]
    await client.patch(f"{base}/{first['id']}", json={"assigneeId": sam}, headers=headers)
    [email] = mailer.sent
    assert email.tag == "assignment" and len(email.to) == 1
    assert email.subject.endswith(first["title"])
    assert f"/recommendations/{first['id']}" in email.text

    # Bulk: one email listing them; assigning yourself sends nothing.
    r = await client.post(
        f"{base}/bulk", json={"ids": [second["id"], third["id"]], "assigneeId": sam},
        headers=headers,
    )  # fmt: skip
    assert r.json() == {"updated": 2}
    assert len(mailer.sent) == 2 and "assigned you 2 recommendations" in mailer.sent[1].subject
    await client.patch(f"{base}/{first['id']}", json={"assigneeId": None}, headers=headers)
    await client.patch(f"{base}/{first['id']}", json={"assigneeId": sam}, headers=sam_headers)
    assert len(mailer.sent) == 2
    assert owner_sql(
        "SELECT count(*) FROM notification WHERE kind = 'recommendation_assigned' "
        "AND tenant_id = %s",
        str(tid),
    ) == [(2,)]


async def test_jobs(
    client: AsyncClient,
    login,
    owner_sql,
    world,  # noqa: F811
    monkeypatch,
    mailer,  # noqa: F811
) -> None:
    import cloudoptics.platform.jobs as platform_jobs

    tid, _, _, headers, base, recs = await scanned(client, login, owner_sql, world)
    assert await jobs.run(str(tid)) == 0
    queued: list[dict[str, str]] = []

    async def fake_defer(app, name, lock, **kwargs):  # type: ignore[no-untyped-def]
        queued.append({"name": name, **kwargs})

    monkeypatch.setattr(jobs, "defer_once", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    assert await jobs.poll(0) >= 1
    assert {"name": jobs.VERIFY_TASK, "tenant_id": str(tid)} in queued

    sam, _ = await teammate(client, login, owner_sql, str(tid), "member")
    me = owner_sql(
        "SELECT user_id::text FROM membership WHERE tenant_id = %s AND user_id <> %s",
        str(tid),
        sam,
    )[0][0]
    owner_sql("UPDATE recommendation SET assignee_id = %s WHERE id = %s", sam, recs[0]["id"])
    sent = await jobs.assignment_email(str(tid), sam, me, recs[0]["id"], "k1")
    assert sent == "sent" and len(mailer.sent) == 1
    assert await jobs.assignment_email(str(tid), sam, me, recs[0]["id"], "k1") == "skipped"
    assert await jobs.assignment_email(str(tid), sam, sam, recs[0]["id"], "k2") == "skipped"

    # Queue mode defers the email instead of sending it in the request.
    from cloudoptics.platform.settings import get_settings

    monkeypatch.setattr(get_settings(), "jobs_mode", "queue")
    await client.patch(f"{base}/{recs[1]['id']}", json={"assigneeId": sam}, headers=headers)
    assert queued[-1]["name"] == jobs.ASSIGNED_TASK
    assert queued[-1]["recommendation_ids"] == recs[1]["id"]
