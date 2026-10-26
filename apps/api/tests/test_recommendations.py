"""Recommendation engine: prices, rules R1-R3, scoring, storage and the API."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from test_inventory import FakeFactory, World, setup

from cloudoptics.analysis import engine, rules
from cloudoptics.analysis.pricing import PriceBook, usage_item
from cloudoptics.connectors import service as connectors
from cloudoptics.inventory import scanner

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
LIST = {
    ("*", "ebs_gp2_gb_month"): 0.10,
    ("*", "ebs_gp3_gb_month"): 0.08,
    ("*", "ebs_gp3_iops_month"): 0.005,
    ("*", "ebs_gp3_throughput_month"): 0.04,
    ("*", "ebs_io1_gb_month"): 0.125,
    ("*", "ebs_io1_iops_month"): 0.065,
    ("*", "ebs_snapshot_gb_month"): 0.05,
    ("*", "ebs_snapshot_archive_gb_month"): 0.0125,
    ("*", "public_ipv4_hour"): 0.005,
    ("*", "alb_hour"): 0.0225,
    ("*", "clb_hour"): 0.025,
    ("eu-west-1", "ebs_gp2_gb_month"): 0.11,
}


@pytest.fixture
def world():  # type: ignore[no-untyped-def]
    w = World()
    connectors.set_session_factory(FakeFactory(w))
    yield w
    connectors.set_session_factory(None)


def res(
    resource_type: str,
    resource_id: str,
    state: str,
    config: dict[str, Any],
    *,
    tags: dict[str, str] | None = None,
    since_days: float = 40,
    seen_days: float = 60,
    region: str = "us-east-2",
) -> rules.Resource:
    return rules.Resource(
        id=uuid.uuid4(),
        account_id="111122223333",
        region_id=region,
        resource_type=resource_type,
        resource_id=resource_id,
        name=None,
        state=state,
        config=config,
        tags=tags or {},
        state_since=NOW - timedelta(days=since_days),
        first_seen_at=NOW - timedelta(days=seen_days),
    )


def run(resources: list[rules.Resource], book: PriceBook | None = None) -> list[engine.Scored]:
    ctx = rules.Context(now=NOW, prices=book or PriceBook(list_prices=LIST))
    return engine.evaluate(ctx, resources)


def test_prices_prefer_the_tenants_own_rate() -> None:
    assert usage_item("EUW1-EBS:VolumeUsage.gp2") == "ebs_gp2_gb_month"
    assert usage_item("EBS:SnapshotUsage") == "ebs_snapshot_gb_month"
    assert usage_item("USE2-BoxUsage:t3.micro") is None

    book = PriceBook(
        list_prices=LIST, effective={("111122223333", "us-east-2", "ebs_gp2_gb_month"): 0.08}
    )
    assert book.price("111122223333", "us-east-2", "ebs_gp2_gb_month").basis == "effective_rate"
    # gp3 has no usage of its own: list price scaled by the 20% discount seen on gp2.
    gp3 = book.price("111122223333", "us-east-2", "ebs_gp3_gb_month")
    assert gp3.usd == pytest.approx(0.064) and gp3.basis == "effective_rate"
    other = book.price("999999999999", "eu-west-1", "ebs_gp2_gb_month")
    assert (other.usd, other.basis, other.fallback_region) == (0.11, "list_price", False)
    assert book.price("999999999999", "ap-south-1", "public_ipv4_hour").fallback_region


def test_rates_against_fallback_prices_keep_the_region_price_level() -> None:
    # Staging, 26 Oct 2026: London gp3 at its list price of 0.0928 against the us-east-1
    # fallback (0.08) was capped as a "discount" of 1, so gp2 came out at 0.10, not 0.116.
    own = {("111122223333", "ap-south-2", "ebs_gp3_gb_month"): 0.0928}
    gp2 = PriceBook(list_prices=LIST, effective=own).price(
        "111122223333", "ap-south-2", "ebs_gp2_gb_month"
    )
    assert gp2.usd == pytest.approx(0.116) and gp2.basis == "effective_rate"
    # With the region's own list prices the ratio is a discount again, never above 1.
    regional = {**LIST, ("ap-south-2", "ebs_gp2_gb_month"): 0.11,
                ("ap-south-2", "ebs_gp3_gb_month"): 0.09}  # fmt: skip
    gp2 = PriceBook(list_prices=regional, effective=own).price(
        "111122223333", "ap-south-2", "ebs_gp2_gb_month"
    )
    assert gp2.usd == pytest.approx(0.11)


def test_unattached_volume_confidence_risk_and_fix() -> None:
    volume = {"volumeType": "gp3", "sizeGiB": 200, "iops": 4000, "throughput": 125,
              "createTime": (NOW - timedelta(days=90)).isoformat(),
              "availabilityZone": "us-east-2b"}  # fmt: skip
    [s] = run([res("ebs_volume", "vol-1", "available", volume, tags={"env": "dev"})])
    c = s.candidate
    assert c.rule_id == "R1_unattached_ebs" and c.confidence == "high"
    assert c.cost_now == pytest.approx(200 * 0.08 + 1000 * 0.005)
    assert c.reversibility == "with_snapshot"
    # No snapshot of the volume: data-loss risk only (dev environment adds nothing).
    assert [f["factor"] for f in c.risk_factors] == ["no_recent_snapshot"]
    assert (s.risk_score, s.risk_level) == (30, "low")
    assert s.priority == pytest.approx(c.savings * 1.0 * 1.0 * 1.0)
    assert c.fix["steps"][0].startswith("aws ec2 create-snapshot --volume-id vol-1")
    assert "--availability-zone us-east-2b" in c.fix["rollback"][0]

    # A recent snapshot removes that factor; unknown env and a young volume add theirs.
    recent = (NOW - timedelta(days=2)).isoformat()
    snapshot = res(
        "ebs_snapshot", "snap-9", "completed", {"volumeId": "vol-1", "startTime": recent}
    )
    young = {**volume, "createTime": (NOW - timedelta(days=20)).isoformat()}
    [s] = [x for x in run([res("ebs_volume", "vol-1", "available", young, since_days=10), snapshot])
           if x.candidate.rule_id == "R1_unattached_ebs"]  # fmt: skip
    assert s.candidate.confidence == "medium"
    assert [f["factor"] for f in s.candidate.risk_factors] == [
        "environment_unknown",
        "recently_created",
    ]

    # Flagged from the first scan: unattached when first seen, the detach time is unknown.
    [s] = run([res("ebs_volume", "vol-1", "available", volume, since_days=0, seen_days=0)])
    assert s.candidate.confidence == "low"
    assert s.candidate.confidence_notes == ["detach_time_unknown"]
    # Detached two days ago: low confidence, and the recent detach adds risk.
    [s] = run([res("ebs_volume", "vol-1", "available", volume, since_days=2)])
    assert s.candidate.confidence == "low" and s.candidate.confidence_notes == []
    assert "recently_detached" in [f["factor"] for f in s.candidate.risk_factors]
    # A brand-new volume never attached is flagged too.
    fresh = {**volume, "createTime": NOW.isoformat()}
    [s] = run([res("ebs_volume", "vol-1", "available", fresh, since_days=0, seen_days=0)])
    assert s.candidate.confidence == "low"
    assert run([res("ebs_volume", "vol-1", "in-use", volume)]) == []


def test_old_snapshots_skip_protected_and_live_sources() -> None:
    old = (NOW - timedelta(days=200)).isoformat()
    base = {"volumeSizeGiB": 50, "startTime": old, "storageTier": "standard"}
    found = run(
        [
            res("ebs_snapshot", "snap-orphan", "completed", {**base, "volumeId": "vol-gone"}),
            res("ebs_snapshot", "snap-live", "completed", {**base, "volumeId": "vol-live"}),
            res("ebs_snapshot", "snap-ami", "completed", {**base, "usedByAmi": True}),
            res("ebs_snapshot", "snap-backup", "completed", base,
                tags={"aws:backup:source-resource": "x"}),
            res("ebs_snapshot", "snap-keep", "completed", base, tags={"Retain": "yes"}),
            res("ebs_snapshot", "snap-new", "completed",
                {**base, "startTime": (NOW - timedelta(days=30)).isoformat()}),
            res("ebs_volume", "vol-live", "in-use", {"volumeType": "gp3", "sizeGiB": 1}),
        ]
    )  # fmt: skip
    snaps = [s for s in found if s.candidate.rule_id == "R1_old_snapshot"]
    assert [s.candidate.resource.resource_id for s in snaps] == ["snap-orphan"]
    c = snaps[0].candidate
    assert c.cost_now == pytest.approx(2.5) and c.reversibility == "irreversible"
    assert c.fix["alternative"]["monthlyCostUsd"] == pytest.approx(0.62, abs=0.01)
    # Unknown environment + data loss: 45, medium risk.
    assert (snaps[0].risk_score, snaps[0].risk_level) == (45, "medium")


def test_idle_addresses_and_load_balancers() -> None:
    stopped = res("ec2_instance", "i-1", "stopped", {}, since_days=10)
    found = run(
        [
            res("eip", "eipalloc-1", "unassociated", {"publicIp": "203.0.113.1"}, since_days=8),
            res("eip", "eipalloc-2", "unassociated", {}, since_days=4),
            res("eip", "eipalloc-3", "unassociated", {}, since_days=1),
            res("eip", "eipalloc-4", "associated", {"instanceId": "i-1", "publicIp": "1.2.3.4"}),
            stopped,
            res("elb", "app-idle", "idle", {"type": "application"}, since_days=9),
            res("elb", "classic-idle", "idle", {"type": "classic"}, since_days=1),
            res("elb", "new-idle", "idle",
                {"type": "application", "createdTime": (NOW - timedelta(days=2)).isoformat()},
                since_days=2, seen_days=2),
            res("elb", "busy", "active", {"type": "application"}),
        ]
    )  # fmt: skip
    by_id = {s.candidate.resource.resource_id: s.candidate for s in found}
    # Everything unattached or idle is flagged from the first scan; confidence grows with time.
    assert set(by_id) == {
        "eipalloc-1", "eipalloc-2", "eipalloc-3", "eipalloc-4",
        "app-idle", "classic-idle", "new-idle",
    }  # fmt: skip
    assert [by_id[f"eipalloc-{i}"].confidence for i in (1, 2, 3)] == ["high", "medium", "low"]
    assert by_id["eipalloc-3"].confidence_notes == []
    assert by_id["new-idle"].confidence == "low"
    assert [f["factor"] for f in by_id["new-idle"].risk_factors] == [
        "environment_unknown",
        "recently_created",
    ]
    assert by_id["eipalloc-1"].cost_now == pytest.approx(730 * 0.005)
    assert by_id["eipalloc-4"].fix["steps"][0].startswith("aws ec2 disassociate-address")
    assert by_id["app-idle"].cost_now == pytest.approx(730 * 0.0225)
    assert by_id["app-idle"].confidence == "high"
    assert by_id["classic-idle"].fix["steps"][0].startswith("aws elb delete-load-balancer")
    assert by_id["classic-idle"].confidence == "low"


def test_gp2_to_gp3_matches_performance() -> None:
    assert rules.gp3_target(100) == (3000, 125)
    assert rules.gp3_target(2000) == (6000, 250)
    assert rules.gp3_target(6000) == (16000, 250)

    big = {"volumeType": "gp2", "sizeGiB": 2000}
    [s] = run([res("ebs_volume", "vol-big", "in-use", big, tags={"Environment": "Production"})])
    c = s.candidate
    assert c.recommended_config == {"volumeType": "gp3", "iops": 6000, "throughput": 250}
    assert c.cost_now == pytest.approx(200.0)
    assert c.cost_after == pytest.approx(160 + 3000 * 0.005 + 125 * 0.04)
    assert c.reversibility == "reversible" and c.confidence == "high"
    assert (s.risk_score, s.risk_level) == (45, "medium")  # production 25 + large 20
    assert "--volume-type gp3 --iops 6000 --throughput 250" in c.fix["steps"][0]


async def test_engine_stores_dedupes_and_resolves(
    client: AsyncClient, login, owner_sql, world
) -> None:
    tenant, connection, _, headers = await setup(client, login, owner_sql)
    world.no_access = {"555555555555"}
    tid = uuid.UUID(tenant)
    await scanner.scan_connection(tid, uuid.UUID(connection))

    # Forty days on: vol-loose (gp2, unattached) gets R1 and R3; snap-1 is old and orphaned;
    # eipalloc-free and the "idle" load balancer have been idle long enough.
    later = datetime.now(UTC) + timedelta(days=40)
    result = await engine.run_analysis(tid, now=later)
    assert result["recommendations"] == 2 * 5  # two regions

    base = f"/v1/tenants/{tenant}/recommendations"
    recs = (await client.get(base, headers=headers)).json()
    eu = [r for r in recs if r["regionId"] == "eu-west-1"]
    assert {r["ruleId"] for r in eu} == {
        "R1_unattached_ebs",
        "R1_old_snapshot",
        "R2_idle_eip",
        "R2_idle_lb",
    }
    assert recs == sorted(recs, key=lambda r: -r["priorityScore"])
    volume = next(r for r in eu if r["resourceId"] == "vol-loose")
    assert volume["ruleId"] == "R1_unattached_ebs" and volume["confidence"] == "high"
    # 100 GiB gp2 at the Ireland list price (0.11), not us-east-1 (0.10).
    assert volume["savingsUsdMonth"] == pytest.approx(11.0)
    assert volume["savingsBasis"] == "list_price"

    alternatives = (await client.get(f"{base}?alternatives=true", headers=headers)).json()
    gp3 = next(
        r for r in alternatives if r["ruleId"] == "R3_gp2_gp3" and r["regionId"] == "eu-west-1"
    )
    assert gp3["primaryOf"] == volume["id"]

    summary = (await client.get(f"{base}/summary", headers=headers)).json()
    assert summary["openCount"] == 8
    assert summary["savingsUsdMonth"] == pytest.approx(
        sum(r["savingsUsdMonth"] for r in recs), abs=0.01
    )
    detail = (await client.get(f"{base}/{volume['id']}", headers=headers)).json()
    assert (
        detail["fix"]["steps"][2]
        == "aws ec2 delete-volume --volume-id vol-loose --region eu-west-1"
    )
    assert (await client.get(f"{base}/{uuid.uuid4()}", headers=headers)).status_code == 404

    # The volume is attached again: its recommendations resolve; the rest keep their ids.
    owner_sql(
        "UPDATE resource SET state = 'in-use' WHERE resource_id = 'vol-loose' AND tenant_id = %s",
        tenant,
    )
    await engine.run_analysis(tid, now=later)
    statuses = dict(
        owner_sql("SELECT rule_id || ':' || region_id, status FROM recommendation "
                  "WHERE resource_id = 'vol-loose' AND tenant_id = %s", tenant)
    )  # fmt: skip
    assert statuses["R1_unattached_ebs:eu-west-1"] == "resolved_externally"
    assert statuses["R3_gp2_gp3:eu-west-1"] == "new"  # still gp2, now the primary
    again = (await client.get(base, headers=headers)).json()
    assert {r["id"] for r in again} >= {r["id"] for r in recs if r["resourceId"] != "vol-loose"}

    # Detached again: the same row reopens.
    owner_sql(
        "UPDATE resource SET state = 'available' WHERE resource_id = 'vol-loose' "
        "AND tenant_id = %s",
        tenant,
    )
    await engine.run_analysis(tid, now=later)
    assert owner_sql(
        "SELECT id, status FROM recommendation WHERE resource_id = 'vol-loose' "
        "AND region_id = 'eu-west-1' AND rule_id = 'R1_unattached_ebs' AND tenant_id = %s",
        tenant,
    ) == [(uuid.UUID(volume["id"]), "new")]


async def test_scan_job_queues_analysis(
    client: AsyncClient, login, owner_sql, monkeypatch, world
) -> None:
    import cloudoptics.platform.jobs as platform_jobs
    from cloudoptics.analysis import jobs as analysis_jobs
    from cloudoptics.inventory import jobs
    from cloudoptics.metrics import jobs as metrics_jobs

    tenant, connection, _, _ = await setup(client, login, owner_sql)
    world.no_access = {"555555555555"}
    queued: list[str] = []

    async def fake_pull(app, tenant_id, connection_id):  # type: ignore[no-untyped-def]
        queued.append(f"pull {connection_id}")

    async def fake_defer(app, tenant_id):  # type: ignore[no-untyped-def]
        queued.append(f"analysis {tenant_id}")

    monkeypatch.setattr(metrics_jobs, "defer_pull", fake_pull)
    monkeypatch.setattr(analysis_jobs, "defer_analysis", fake_defer)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    assert await jobs.scan(tenant, connection) == "succeeded"
    assert queued == [f"pull {connection}"]
    assert await metrics_jobs.pull(tenant, connection) == "succeeded"
    assert queued == [f"pull {connection}", f"analysis {tenant}"]
    assert await analysis_jobs.run(tenant) == 10


async def test_members_change_status_and_done_is_verified(
    client: AsyncClient, login, owner_sql, world
) -> None:
    tenant, connection, _, headers = await setup(client, login, owner_sql)
    world.no_access = {"555555555555"}
    tid = uuid.UUID(tenant)
    await scanner.scan_connection(tid, uuid.UUID(connection))
    later = datetime.now(UTC) + timedelta(days=40)
    await engine.run_analysis(tid, now=later)
    base = f"/v1/tenants/{tenant}/recommendations"
    recs = (await client.get(base, headers=headers)).json()
    eip = next(r for r in recs if r["ruleId"] == "R2_idle_eip")
    lb = next(r for r in recs if r["ruleId"] == "R2_idle_lb")

    bad = await client.patch(f"{base}/{lb['id']}", json={"status": "dismissed"}, headers=headers)
    assert bad.status_code == 422
    ok = await client.patch(
        f"{base}/{lb['id']}",
        json={"status": "dismissed", "reason": "Needed for DR"},
        headers=headers,
    )
    assert ok.status_code == 200 and ok.json()["status"] == "dismissed"
    assert (
        await client.patch(f"{base}/{eip['id']}", json={"status": "done"}, headers=headers)
    ).json()["status"] == "done"
    assert owner_sql(
        "SELECT action, after->>'reason' FROM audit_log WHERE target_id = %s", lb["id"]
    ) == [("recommendation.status_changed", "Needed for DR")]

    # The address is released: the done recommendation is verified; the dismissed one stays.
    owner_sql(
        "UPDATE resource SET deleted_at = now() WHERE resource_id = %s AND region_id = %s",
        eip["resourceId"],
        eip["regionId"],
    )
    await engine.run_analysis(tid, now=later)
    rows = dict(
        owner_sql(
            "SELECT id::text, status FROM recommendation WHERE id IN (%s, %s)", eip["id"], lb["id"]
        )
    )
    assert rows == {eip["id"]: "verified", lb["id"]: "dismissed"}
    open_ids = {r["id"] for r in (await client.get(base, headers=headers)).json()}
    assert eip["id"] not in open_ids and lb["id"] not in open_ids
