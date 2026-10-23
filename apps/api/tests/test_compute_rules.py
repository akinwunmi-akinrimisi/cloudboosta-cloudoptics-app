"""R4 idle EC2/RDS and R6 previous-generation/Graviton rules, on hand-built inputs."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from cloudoptics.analysis import compute, engine
from cloudoptics.analysis.pricing import PriceBook
from cloudoptics.analysis.rules import Context, Resource

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=90)

PRICES = {
    ("*", "ec2_m4_large_hour"): 0.10,
    ("*", "ec2_m5_large_hour"): 0.096,
    ("*", "ec2_m6i_large_hour"): 0.096,
    ("*", "ec2_m7g_large_hour"): 0.0816,
    ("*", "ec2_t2_large_hour"): 0.0928,
    ("*", "ec2_t3_large_hour"): 0.0832,
    ("*", "ec2_t4g_large_hour"): 0.0672,
    ("*", "ec2_m1_large_hour"): 0.175,
    ("*", "rds_postgres_m5_large_hour"): 0.178,
    ("*", "ebs_gp3_gb_month"): 0.08,
    ("*", "ebs_gp3_iops_month"): 0.005,
    ("*", "ebs_gp3_throughput_month"): 0.04,
}


def resource(kind: str, rid: str, state: str, config: dict[str, Any], **tags: str) -> Resource:
    return Resource(
        id=uuid.uuid4(),
        account_id="123456789012",
        region_id="eu-west-1",
        resource_type=kind,
        resource_id=rid,
        name=None,
        state=state,
        config=config,
        tags=tags,
        state_since=LONG_AGO,
        first_seen_at=LONG_AGO,
    )


def server(kind: str = "m5.large", **tags: str) -> Resource:
    config = {"instanceType": kind, "launchTime": LONG_AGO.isoformat(), "volumeIds": ["vol-1"]}
    return resource("ec2_instance", "i-1", "running", config, **tags)


def database(**config: Any) -> Resource:
    base = {"instanceClass": "db.m5.large", "engine": "postgres", "multiAz": True}
    return resource("rds_instance", "orders", "available", {**base, **config})


def context() -> Context:
    return Context(now=NOW, prices=PriceBook(list_prices=dict(PRICES)))


def fill(ctx: Context, r: Resource, metric: str, value: float, days: int = 33) -> None:
    series = ctx.usage.setdefault(r.id, {}).setdefault(metric, {})
    for i in range(days + 1):
        series[NOW.date() - timedelta(days=i)] = [value] * 24


def quiet_server(ctx: Context, r: Resource, days: int = 33) -> None:
    fill(ctx, r, "cpu_max", 2.0, days)
    fill(ctx, r, "network_in", 1000.0, days)
    fill(ctx, r, "network_out", 1000.0, days)


def quiet_database(ctx: Context, r: Resource, days: int = 33) -> None:
    fill(ctx, r, "connections", 0.0, days)
    fill(ctx, r, "cpu", 1.5, days)
    fill(ctx, r, "read_iops", 0.2, days)
    fill(ctx, r, "write_iops", 0.5, days)


def run(rule: Any, ctx: Context, *resources: Resource) -> list[Any]:
    return list(rule(ctx, list(resources)))


def test_idle_server_includes_its_disks_and_is_high_confidence_over_32_days() -> None:
    ctx = context()
    r = server()
    volume = resource(
        "ebs_volume", "vol-1", "in-use", {"volumeType": "gp3", "sizeGiB": 100, "iops": 3000}
    )
    engine.build_context([r, volume], ctx)
    quiet_server(ctx, r)
    [c] = run(compute.idle_instances, ctx, r, volume)
    assert c.rule_id == "R4_idle_ec2" and c.confidence == "high"
    assert c.cost_now == pytest.approx(0.096 * 730 + 8.0)
    assert c.fix["steps"] == ["aws ec2 stop-instances --instance-ids i-1 --region eu-west-1"]
    assert "stop_saves_compute_only" in c.confidence_notes
    assert "list_price_us_east_1" in c.confidence_notes
    assert engine.score(c).risk_level == "low"  # unknown environment + stop

    ctx = context()
    quiet_server(ctx, r, days=20)  # idle, but only 20 days of history
    assert run(compute.idle_instances, ctx, r)[0].confidence == "medium"


@pytest.mark.parametrize(
    "change",
    ["busy_cpu", "busy_network", "missing_day", "autoscaling", "new", "stopped"],
)
def test_servers_that_are_not_idle_or_are_excluded(change: str) -> None:
    ctx = context()
    tags = {
        "autoscaling": {"aws:autoscaling:groupName": "web"},
    }.get(change, {})
    r = server(**tags)
    if change == "new":
        r = resource("ec2_instance", "i-1", "running", {**r.config, "launchTime": NOW})
    if change == "stopped":
        r = resource("ec2_instance", "i-1", "stopped", r.config)
    quiet_server(ctx, r)
    if change == "busy_cpu":
        ctx.usage[r.id]["cpu_max"][NOW.date() - timedelta(days=3)][5] = 40.0
    if change == "busy_network":
        fill(ctx, r, "network_in", 1024.0 * 1024)
    if change == "missing_day":
        del ctx.usage[r.id]["cpu_max"][NOW.date() - timedelta(days=4)]
    assert run(compute.idle_instances, ctx, r) == []


def test_idle_database_has_snapshot_delete_and_stop_alternative() -> None:
    ctx = context()
    r = database()
    quiet_database(ctx, r)
    [c] = run(compute.idle_databases, ctx, r)
    assert c.rule_id == "R4_idle_rds" and c.confidence == "high"
    assert c.cost_now == pytest.approx(0.178 * 2 * 730)  # Multi-AZ
    assert "--final-db-snapshot-identifier orders-cloudoptics-final" in c.fix["steps"][0]
    assert "7 days" in c.fix["alternative"]["title"]
    assert c.reversibility == "with_snapshot"

    ctx = context()
    quiet_database(ctx, r)
    fill(ctx, r, "connections", 3.0)
    fill(ctx, r, "connections", 0.0, days=10)  # no connections for 10 days, not a month
    assert run(compute.idle_databases, ctx, r)[0].confidence == "medium"


@pytest.mark.parametrize(
    "change", ["connection", "busy_cpu", "busy_disk", "replica", "aurora", "oracle"]
)
def test_databases_in_use_or_out_of_scope(change: str) -> None:
    ctx = context()
    r = database(
        **{
            "replica": {"readReplicaOf": "main"},
            "aurora": {"clusterId": "cluster-1"},
            "oracle": {"engine": "oracle-ee"},
        }.get(change, {})
    )
    quiet_database(ctx, r)
    if change == "connection":
        ctx.usage[r.id]["connections"][NOW.date() - timedelta(days=2)][9] = 1.0
    if change == "busy_cpu":
        fill(ctx, r, "cpu", 30.0)
    if change == "busy_disk":
        fill(ctx, r, "write_iops", 50.0)
    assert run(compute.idle_databases, ctx, r) == []


def test_previous_generation_offers_newer_type_and_graviton() -> None:
    ctx = context()
    r = server("m4.large", env="prod")
    found = {c.rule_id: c for c in run(compute.previous_generation, ctx, r)}
    newer, arm = found["R6_newer_generation"], found["R6_graviton"]
    assert newer.recommended_config == {"instanceType": "m6i.large"}
    assert newer.savings == pytest.approx((0.10 - 0.096) * 730)
    assert "needs_nitro_drivers" in newer.confidence_notes
    assert newer.fix["steps"][2].endswith("--instance-type Value=m6i.large --region eu-west-1")
    assert "Value=m4.large" in newer.fix["rollback"][2]
    assert arm.recommended_config["instanceType"] == "m7g.large" and arm.effort == "L"
    assert arm.fix["checklist"] and engine.score(arm).risk_level == "medium"

    # m5 costs the same as m6i, so only Graviton saves money.
    assert [c.rule_id for c in run(compute.previous_generation, ctx, server())] == ["R6_graviton"]
    # Windows cannot run on Graviton.
    windows = resource("ec2_instance", "i-2", "running", {"instanceType": "t2.large",
                                                          "platform": "Windows"})  # fmt: skip
    found = run(compute.previous_generation, ctx, windows)
    assert [(c.rule_id, c.recommended_config["instanceType"]) for c in found] == [
        ("R6_newer_generation", "t3.large")
    ]
    assert "t3_unlimited_by_default" in found[0].confidence_notes
    # No m6i.small or m7g.small exists; Graviton types already are current.
    assert run(compute.previous_generation, ctx, server("m1.small")) == []
    assert run(compute.previous_generation, ctx, server("m7g.large")) == []
    assert run(compute.previous_generation, ctx, server("x9.weird")) == []


def test_sizes_and_types() -> None:
    assert compute.size_factor("nano") == 0.0625
    assert compute.size_factor("xlarge") == 2
    assert compute.size_factor("12xlarge") == 24
    assert compute.size_factor("metal") is None
    assert compute.split_type("db.r6g.2xlarge") == ("r6g", "2xlarge")
    assert compute.split_type("weird") is None
    assert compute.percentile([1.0, 2.0, 3.0, 100.0], 50) in (2.0, 3.0)


def busy_server(ctx: Context, r: Resource, cpu: float, days: int = 33) -> None:
    fill(ctx, r, "cpu_max", cpu, days)
    fill(ctx, r, "network_in", 1e9, days)
    fill(ctx, r, "network_out", 1e9, days)


def test_rightsizing_one_size_down_with_headroom() -> None:
    ctx = context()
    r = server("m5.xlarge")
    busy_server(ctx, r, 20.0)
    [c] = run(compute.rightsizing, ctx, r)
    assert c.rule_id == "R5_rightsize_ec2"
    assert c.recommended_config == {"instanceType": "m5.large"}
    assert c.savings == pytest.approx(0.096 * 730)
    assert c.confidence == "medium" and "no_memory_data" in c.confidence_notes
    projected = next(e for e in c.evidence if e["kind"] == "projectedCpuPercent")
    assert projected["value"] == pytest.approx(52.0)  # p95 20% x 2 x 1.3 headroom
    assert "Value=m5.large" in c.fix["steps"][2]

    ctx = context()
    busy_server(ctx, r, 20.0, days=20)
    [c] = run(compute.rightsizing, ctx, r)
    assert c.confidence == "low" and "short_history" in c.confidence_notes

    # Production uses p99.5 with 20% headroom: 20 x 2 x 1.2 = 48%.
    ctx = context()
    prod = server("m5.xlarge", env="prod")
    busy_server(ctx, prod, 20.0)
    assert run(compute.rightsizing, ctx, prod)[0].rule_config["percentile"] == 99.5


@pytest.mark.parametrize(
    "change", ["too_busy", "spike", "optimized", "smallest", "burstable", "idle", "history"]
)
def test_rightsizing_is_cautious(change: str) -> None:
    ctx = context()
    kind = {"smallest": "m5.large", "burstable": "t3.xlarge"}.get(change, "m5.xlarge")
    r = server(kind)
    busy_server(
        ctx, r, 30.0 if change == "too_busy" else 20.0, days=10 if change == "history" else 33
    )
    if change == "spike":
        ctx.usage[r.id]["cpu_max"][NOW.date() - timedelta(days=5)][3] = 50.0
    if change == "optimized":
        r.config["computeOptimizer"] = {"finding": "Optimized", "options": []}
    if change == "idle":
        quiet_server(ctx, r)
    assert run(compute.rightsizing, ctx, r) == []


def test_rightsizing_shows_compute_optimizer_side_by_side() -> None:
    ctx = context()
    r = server("m5.xlarge")
    r.config["computeOptimizer"] = {
        "finding": "Overprovisioned",
        "options": [{"instanceType": "m6g.large", "performanceRisk": 1.0, "rank": 1}],
    }
    busy_server(ctx, r, 10.0)
    [c] = run(compute.rightsizing, ctx, r)
    assert "compute_optimizer_agrees" in c.confidence_notes
    optimizer = next(e for e in c.evidence if e["kind"] == "computeOptimizer")
    assert optimizer["options"][0]["instanceType"] == "m6g.large"


def test_settings_turn_rules_off_hide_tagged_resources_and_set_thresholds() -> None:
    from cloudoptics.analysis.settings import RuleSettings

    r = server("m4.large")
    hidden = server("m4.large", **{"Keep-Me": "yes"})
    ctx = context()
    quiet_server(ctx, r)
    found = {s.candidate.rule_id for s in engine.evaluate(ctx, [r])}
    assert found == {"R4_idle_ec2", "R6_newer_generation", "R6_graviton"}

    ctx = context()
    ctx.settings = RuleSettings.from_json(
        {"disabledRules": ["R6_graviton", "unknown"], "ignoreTag": "keep-me", "idleCpuPercent": 1}
    )
    quiet_server(ctx, r)  # peak CPU 2%: idle at 5%, not at 1%
    quiet_server(ctx, hidden)
    found = {
        (s.candidate.resource.id, s.candidate.rule_id) for s in engine.evaluate(ctx, [r, hidden])
    }
    assert found == {(r.id, "R6_newer_generation")}
    assert ctx.settings.to_json()["disabledRules"] == ["R6_graviton"]

    ctx = context()
    ctx.settings = RuleSettings.from_json({"idleDays": 30})
    quiet_server(ctx, r, days=20)  # not 30 days of history yet
    assert run(compute.idle_instances, ctx, r) == []
