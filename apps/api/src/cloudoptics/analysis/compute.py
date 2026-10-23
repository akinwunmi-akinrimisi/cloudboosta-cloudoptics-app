"""Usage-based rules: R4 idle EC2 and RDS, R5 rightsizing, R6 previous-generation and
Graviton.

R4 (PRD FR-ANA-04/04b) and R5 (FR-ANA-05) read the hourly CloudWatch values the metrics
puller stores. R6 (FR-ANA-06) needs only the inventory. Prices are on-demand list prices
per family, scaled by size (migration 0013), so savings are labelled ``list_price``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

from cloudoptics.analysis.pricing import HOURS_PER_MONTH, PriceBook
from cloudoptics.analysis.rules import (
    Candidate,
    Confidence,
    Context,
    Resource,
    _age_days,
    _cli,
    _region_args,
    env_factor,
    environment,
    parse_time,
    volume_month_cost,
)

ASG_TAG = "aws:autoscaling:groupName"
IDLE_NETWORK_BYTES_DAY = 5 * 1024 * 1024
HIGH_CONFIDENCE_DAYS = 32
DB_IDLE_CONNECTION_DAYS = 7
DB_HIGH_CONFIDENCE_DAYS = 30
DB_IDLE_IOPS = 20.0

SIZES = {"nano": 0.0625, "micro": 0.125, "small": 0.25, "medium": 0.5, "large": 1.0}
BURSTABLE_SIZES = {"nano", "micro", "small", "medium", "large", "xlarge", "2xlarge"}
GENERAL_SIZES = {"large", "xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge", "16xlarge"}

# Previous generation -> current generation on the same architecture (FR-ANA-06). Only
# moves that cost less are listed: m5, c5 and r5 cost the same as m6i, c6i and r6i.
NEWER_GENERATION = {
    "t1": "t3",
    "t2": "t3",
    "m1": "m6i",
    "m3": "m6i",
    "m4": "m6i",
    "c3": "c6i",
    "c4": "c6i",
    "r3": "r6i",
    "r4": "r6i",
}
# Xen-based families: moving to a Nitro type needs the ENA and NVMe drivers.
XEN_FAMILIES = {"t1", "t2", "m1", "m3", "m4", "c3", "c4", "r3", "r4"}
GRAVITON = {"t": "t4g", "m": "m7g", "c": "c7g", "r": "r7g"}
RDS_ENGINES = {"postgres": "postgres", "mysql": "mysql", "mariadb": "mysql"}


def size_factor(size: str) -> float | None:
    if size in SIZES:
        return SIZES[size]
    m = re.fullmatch(r"(\d*)xlarge", size)
    if m is None:
        return None
    return 2.0 * int(m.group(1) or 1)


def split_type(instance_type: str | None) -> tuple[str, str] | None:
    """``m5.large`` or ``db.m5.large`` -> ``("m5", "large")``."""
    parts = str(instance_type or "").removeprefix("db.").split(".")
    return (parts[0], parts[1]) if len(parts) == 2 else None


def hourly_price(
    prices: PriceBook, region: str, item_prefix: str, family: str, size: str
) -> tuple[float, bool] | None:
    """On-demand $/hour for a family and size, and whether the price is the fallback."""
    factor = size_factor(size)
    if factor is None:
        return None
    try:
        large, fallback = prices.list_price(region, f"{item_prefix}{family}_large_hour")
    except KeyError:
        return None
    return large * factor, fallback


def complete_days(ctx: Context, r: Resource, metric: str, days: int) -> list[float] | None:
    """Every stored hourly value of the last ``days`` complete UTC days, or None when any
    of those days is missing (too little history to judge)."""
    series = ctx.usage.get(r.id, {}).get(metric, {})
    today = ctx.now.date()
    wanted = [today - timedelta(days=i) for i in range(1, days + 1)]
    if any(d not in series for d in wanted):
        return None
    return [v for d in wanted for v in series[d] if v is not None]


def daily_totals(ctx: Context, r: Resource, metrics: tuple[str, ...], days: int) -> list[float]:
    today = ctx.now.date()
    totals = []
    for i in range(1, days + 1):
        day = today - timedelta(days=i)
        totals.append(
            sum(
                v
                for m in metrics
                for v in ctx.usage.get(r.id, {}).get(m, {}).get(day, [])
                if v is not None
            )
        )
    return totals


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
    return ordered[index]


def _old_enough(ctx: Context, r: Resource, created_key: str) -> bool:
    created = parse_time(r.config.get(created_key)) or r.first_seen_at
    return (_age_days(ctx, created) or 0) >= ctx.settings.idle_days


def _price_notes(fallback: bool) -> list[str]:
    return ["list_price_us_east_1"] if fallback else []


# --- R4: idle EC2 instances (FR-ANA-04) ---------------------------------------------------


def _idle_ec2(ctx: Context, r: Resource, days: int) -> tuple[float, float] | None:
    """(peak CPU %, busiest day's network bytes) when idle over ``days``, else None."""
    cpu = complete_days(ctx, r, "cpu_max", days)
    if not cpu:
        return None
    if complete_days(ctx, r, "network_in", days) is None:
        return None
    if complete_days(ctx, r, "network_out", days) is None:
        return None
    peak = max(cpu)
    network = max(daily_totals(ctx, r, ("network_in", "network_out"), days))
    if peak >= ctx.settings.idle_cpu_percent or network >= IDLE_NETWORK_BYTES_DAY:
        return None
    return peak, network


def idle_instances(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "ec2_instance" or r.state != "running":
            continue
        if ASG_TAG in r.tags or not _old_enough(ctx, r, "launchTime"):
            continue
        idle = _idle_ec2(ctx, r, ctx.settings.idle_days)
        if idle is None:
            continue
        kind = str(r.config.get("instanceType") or "")
        split = split_type(kind)
        price = split and hourly_price(ctx.prices, r.region_id, "ec2_", *split)
        if not price:
            continue
        compute = price[0] * HOURS_PER_MONTH
        storage = 0.0
        for volume_id in r.config.get("volumeIds") or []:
            volume = ctx.volumes.get((r.account_id, r.region_id, str(volume_id)))
            if volume is not None:
                storage += volume_month_cost(
                    ctx.prices, volume, str(volume.config.get("volumeType") or "gp2")
                )[0]
        confidence: Confidence = (
            "high" if _idle_ec2(ctx, r, HIGH_CONFIDENCE_DAYS) is not None else "medium"
        )
        notes = ["stop_saves_compute_only", *_price_notes(price[1])]
        if "linux" not in str(r.config.get("platform") or "Linux/UNIX").lower():
            notes.append("priced_as_linux")
        peak, network = idle
        region = _region_args(r)
        name = r.name or r.resource_id
        yield Candidate(
            rule_id="R4_idle_ec2",
            rule_version=1,
            type="stop_idle_instance",
            console_steps=[
                "Open the server in the AWS console.",
                "Choose Instance state, then Stop instance.",
                "If nobody misses it after 14 days: choose Actions, Image and templates, "
                "Create image, then Instance state, Terminate instance.",
            ],
            resource=r,
            title=f"Stop idle server {name} ({kind}): almost no CPU or network for "
            f"{ctx.settings.idle_days} days",
            current_config={"instanceType": kind, "state": "running"},
            recommended_config={"action": "stop_then_terminate"},
            cost_now=compute + storage,
            cost_after=0.0,
            basis="list_price",
            effort="S",
            reversibility="reversible",
            confidence=confidence,
            confidence_notes=notes,
            risk_factors=[*env_factor(r.tags), {"factor": "stops_running_server", "points": 15}],
            evidence=[
                {"kind": "peakCpuPercent", "value": round(peak, 2), "days": ctx.settings.idle_days},
                {"kind": "busiestDayNetworkMb", "value": round(network / 1024 / 1024, 2),
                 "days": ctx.settings.idle_days},
                {"kind": "stopSavesUsdMonth", "value": round(compute, 2)},
                {"kind": "volumesUsdMonth", "value": round(storage, 2)},
            ],
            rule_config={
                "maxCpuPercent": ctx.settings.idle_cpu_percent,
                "maxNetworkMbPerDay": 5,
                "days": ctx.settings.idle_days,
                "highConfidenceDays": HIGH_CONFIDENCE_DAYS,
            },
            restart_required=True,
            fix={
                "steps": [
                    _cli("aws", "ec2", "stop-instances", "--instance-ids", r.resource_id,
                         *region),
                ],
                "rollback": [
                    _cli("aws", "ec2", "start-instances", "--instance-ids", r.resource_id,
                         *region),
                ],
                "alternative": {
                    "title": "After 14 days stopped with no complaints: keep a backup image "
                    "and remove it for good",
                    "steps": [
                        _cli("aws", "ec2", "create-image", "--instance-id", r.resource_id,
                             "--name", f"cloudoptics-final-{r.resource_id}", *region),
                        _cli("aws", "ec2", "terminate-instances", "--instance-ids",
                             r.resource_id, *region),
                    ],
                },
                "notes": "Stopping keeps the disks, so you can start it again at any time. "
                "Its disks and any Elastic IP still cost money until it is removed.",
            },
        )  # fmt: skip


# --- R4: idle RDS DB instances (FR-ANA-04b) -----------------------------------------------


def _db_hourly(ctx: Context, r: Resource) -> tuple[float, bool] | None:
    engine = RDS_ENGINES.get(str(r.config.get("engine") or ""))
    split = split_type(str(r.config.get("instanceClass") or ""))
    if engine is None or split is None:
        return None
    price = hourly_price(ctx.prices, r.region_id, f"rds_{engine}_", *split)
    if price is None:
        return None
    return price[0] * (2 if r.config.get("multiAz") else 1), price[1]


def idle_databases(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "rds_instance" or r.state != "available":
            continue
        c = r.config
        if c.get("readReplicaOf") or c.get("clusterId"):
            continue
        if not _old_enough(ctx, r, "createTime"):
            continue
        idle_days = ctx.settings.idle_days
        connections = complete_days(ctx, r, "connections", DB_IDLE_CONNECTION_DAYS)
        cpu = complete_days(ctx, r, "cpu", ctx.settings.idle_days)
        reads = complete_days(ctx, r, "read_iops", ctx.settings.idle_days)
        writes = complete_days(ctx, r, "write_iops", ctx.settings.idle_days)
        if not connections or not cpu or reads is None or writes is None:
            continue
        if max(connections) > 0:
            continue
        cpu_p95 = percentile(cpu, 95)
        iops_p95 = (percentile(reads, 95) if reads else 0.0) + (
            percentile(writes, 95) if writes else 0.0
        )
        if cpu_p95 >= ctx.settings.idle_cpu_percent or iops_p95 >= DB_IDLE_IOPS:
            continue
        price = _db_hourly(ctx, r)
        if price is None:
            continue
        month = complete_days(ctx, r, "connections", DB_HIGH_CONFIDENCE_DAYS)
        confidence: Confidence = "high" if month and max(month) == 0 else "medium"
        name = r.resource_id
        snapshot = f"{name}-cloudoptics-final"
        region = _region_args(r)
        yield Candidate(
            rule_id="R4_idle_rds",
            rule_version=1,
            type="delete_idle_database",
            console_steps=[
                "Open the database in the AWS console.",
                "Choose Actions, then Delete.",
                "Keep Create final snapshot ticked, type delete me, and choose Delete.",
            ],
            resource=r,
            title=f"Remove unused database {name} ({c.get('instanceClass')}): no connections "
            f"for {DB_IDLE_CONNECTION_DAYS} days",
            current_config={
                "instanceClass": c.get("instanceClass"),
                "engine": c.get("engine"),
                "multiAz": bool(c.get("multiAz")),
            },
            recommended_config={"action": "snapshot_then_delete"},
            cost_now=price[0] * HOURS_PER_MONTH,
            cost_after=0.0,
            basis="list_price",
            effort="S",
            reversibility="with_snapshot",
            confidence=confidence,
            confidence_notes=["storage_not_included", *_price_notes(price[1])],
            risk_factors=[*env_factor(r.tags), {"factor": "deletes_database", "points": 20}],
            evidence=[
                {"kind": "maxConnections", "value": 0, "days": DB_IDLE_CONNECTION_DAYS},
                {"kind": "cpuP95Percent", "value": round(cpu_p95, 2), "days": idle_days},
                {"kind": "iopsP95", "value": round(iops_p95, 2), "days": idle_days},
            ],
            rule_config={
                "connectionDays": DB_IDLE_CONNECTION_DAYS,
                "maxCpuP95Percent": ctx.settings.idle_cpu_percent,
                "maxIopsP95": DB_IDLE_IOPS,
            },
            fix={
                "steps": [
                    _cli("aws", "rds", "delete-db-instance", "--db-instance-identifier", name,
                         "--final-db-snapshot-identifier", snapshot, *region),
                ],
                "rollback": [
                    _cli("aws", "rds", "restore-db-instance-from-db-snapshot",
                         "--db-instance-identifier", name, "--db-snapshot-identifier",
                         snapshot, *region),
                ],
                "alternative": {
                    "title": "Stop it instead (AWS starts it again by itself after 7 days)",
                    "steps": [
                        _cli("aws", "rds", "stop-db-instance", "--db-instance-identifier",
                             name, *region),
                    ],
                },
                "notes": "AWS keeps a final snapshot, so the database can be restored from it.",
            },
        )  # fmt: skip


# --- R6: previous generation and Graviton (FR-ANA-06) -------------------------------------


def _fits(family: str, size: str) -> bool:
    if family.startswith("t"):
        return size in BURSTABLE_SIZES
    return size in GENERAL_SIZES or (family.endswith("g") and size == "medium")


def _change_type(r: Resource, target: str) -> dict[str, Any]:
    region = _region_args(r)
    current = str(r.config.get("instanceType"))
    stop = _cli("aws", "ec2", "stop-instances", "--instance-ids", r.resource_id, *region)
    wait = _cli("aws", "ec2", "wait", "instance-stopped", "--instance-ids", r.resource_id, *region)
    start = _cli("aws", "ec2", "start-instances", "--instance-ids", r.resource_id, *region)

    def modify(kind: str) -> str:
        return _cli(
            "aws", "ec2", "modify-instance-attribute", "--instance-id", r.resource_id,
            "--instance-type", f"Value={kind}", *region,
        )  # fmt: skip

    return {
        "steps": [stop, wait, modify(target), start],
        "rollback": [stop, wait, modify(current), start],
    }


def change_type_steps(target: str) -> list[str]:
    return [
        "Open the server in the AWS console.",
        "Choose Instance state, then Stop instance, and wait until it shows Stopped.",
        f"Choose Actions, Instance settings, Change instance type, pick {target} and apply.",
        "Choose Instance state, then Start instance.",
    ]


def _instance_candidates(ctx: Context, r: Resource) -> Iterator[tuple[str, str, float, bool]]:
    """(rule, target type, monthly saving, fallback price) for each cheaper option."""
    kind = str(r.config.get("instanceType") or "")
    split = split_type(kind)
    if split is None:
        return
    family, size = split
    now = hourly_price(ctx.prices, r.region_id, "ec2_", family, size)
    if now is None:
        return
    options = []
    if family in NEWER_GENERATION:
        options.append(("R6_newer_generation", NEWER_GENERATION[family]))
    linux = "linux" in str(r.config.get("platform") or "Linux/UNIX").lower()
    if linux and not family.endswith("g") and family[0] in GRAVITON:
        options.append(("R6_graviton", GRAVITON[family[0]]))
    for rule, target in options:
        if not _fits(target, size):
            continue
        after = hourly_price(ctx.prices, r.region_id, "ec2_", target, size)
        if after is None or after[0] >= now[0] * 0.99:
            continue
        yield rule, f"{target}.{size}", now[0] - after[0], now[1] or after[1]


def previous_generation(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "ec2_instance" or r.state != "running":
            continue
        if ASG_TAG in r.tags:
            continue
        kind = str(r.config.get("instanceType"))
        split = split_type(kind)
        now = split and hourly_price(ctx.prices, r.region_id, "ec2_", *split)
        if not split or not now:
            continue
        family = split[0]
        name = r.name or r.resource_id
        for rule, target, saving, fallback in _instance_candidates(ctx, r):
            notes = ["check_type_in_region", *_price_notes(fallback)]
            if rule == "R6_newer_generation":
                if family in XEN_FAMILIES:
                    notes.append("needs_nitro_drivers")
                if target.startswith("t3"):
                    notes.append("t3_unlimited_by_default")
                yield Candidate(
                    rule_id=rule,
                    rule_version=1,
                    type="modernise_instance",
                    console_steps=change_type_steps(target),
                    resource=r,
                    title=f"Move server {name} from {kind} to the newer {target}",
                    current_config={"instanceType": kind},
                    recommended_config={"instanceType": target},
                    cost_now=now[0] * HOURS_PER_MONTH,
                    cost_after=(now[0] - saving) * HOURS_PER_MONTH,
                    basis="list_price",
                    effort="S",
                    reversibility="reversible",
                    confidence="medium" if family in XEN_FAMILIES else "high",
                    confidence_notes=notes,
                    risk_factors=[*env_factor(r.tags), {"factor": "restart_needed", "points": 10}],
                    evidence=[{"kind": "previousGeneration", "value": family}],
                    rule_config={"map": NEWER_GENERATION},
                    restart_required=True,
                    fix={
                        **_change_type(r, target),
                        "notes": "The server stops for a few minutes while its type changes.",
                    },
                )
            else:
                yield Candidate(
                    rule_id=rule,
                    rule_version=1,
                    type="move_to_graviton",
                    console_steps=[],
                    resource=r,
                    title=f"Move server {name} from {kind} to Graviton {target} (Arm)",
                    current_config={"instanceType": kind, "architecture": "x86_64"},
                    recommended_config={"instanceType": target, "architecture": "arm64"},
                    cost_now=now[0] * HOURS_PER_MONTH,
                    cost_after=(now[0] - saving) * HOURS_PER_MONTH,
                    basis="list_price",
                    effort="L",
                    reversibility="reversible",
                    confidence="medium",
                    confidence_notes=["arm64_build_needed", *notes],
                    risk_factors=[
                        *env_factor(r.tags),
                        {"factor": "architecture_change", "points": 35},
                    ],
                    evidence=[{"kind": "architecture", "value": "x86_64"}],
                    rule_config={"map": GRAVITON},
                    restart_required=True,
                    fix={
                        "steps": [],
                        "checklist": [
                            "Check that your software and its libraries have Arm (arm64) versions.",
                            "Build or pick an arm64 image (AMI) for the server.",
                            f"Start a test server on {target} from that image and test it.",
                            "Switch traffic to the new server, then stop the old one.",
                        ],
                        "rollback": [],
                        "notes": "Graviton uses Arm processors, so the server needs software "
                        "built for Arm. Keep the old server until the new one is proven.",
                    },
                )


# --- R5: EC2 rightsizing (FR-ANA-05) -----------------------------------------------------

SIZE_ORDER = ["medium", "large", "xlarge", "2xlarge", "4xlarge", "8xlarge", "12xlarge",
              "16xlarge", "24xlarge", "32xlarge", "48xlarge"]  # fmt: skip
RIGHTSIZE_MIN_DAYS = 14
RIGHTSIZE_DAYS = 32
MAX_PROJECTED_PERCENTILE = 60.0
MAX_PROJECTED_PEAK = 85.0


def smaller_size(family: str, size: str) -> str | None:
    if size not in SIZE_ORDER or SIZE_ORDER.index(size) == 0:
        return None
    target = SIZE_ORDER[SIZE_ORDER.index(size) - 1]
    if target == "medium" and not family.endswith("g"):
        return None  # x86 m/c/r families start at large
    return target


def rightsizing(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "ec2_instance" or r.state != "running":
            continue
        if ASG_TAG in r.tags:
            continue
        kind = str(r.config.get("instanceType"))
        split = split_type(kind)
        # Burstable types (t2/t3/t4g) depend on CPU credits, not a fixed share: left out.
        if split is None or split[0].startswith("t"):
            continue
        family, size = split
        target_size = smaller_size(family, size)
        if target_size is None:
            continue
        days = RIGHTSIZE_DAYS
        cpu = complete_days(ctx, r, "cpu_max", RIGHTSIZE_DAYS)
        if not cpu:
            days, cpu = RIGHTSIZE_MIN_DAYS, complete_days(ctx, r, "cpu_max", RIGHTSIZE_MIN_DAYS)
        if not cpu or _idle_ec2(ctx, r, ctx.settings.idle_days) is not None:
            continue  # too little history, or idle (R4 covers it)
        optimizer = r.config.get("computeOptimizer") or {}
        if optimizer.get("finding") in ("Optimized", "Underprovisioned"):
            continue  # AWS disagrees: never recommend against its own finding
        prod = environment(r.tags) == "prod"
        pct, headroom = (99.5, 1.2) if prod else (95.0, 1.3)
        ratio = (size_factor(size) or 1) / (size_factor(target_size) or 1)
        projected = percentile(cpu, pct) * ratio * headroom
        peak = max(cpu) * ratio
        if projected > MAX_PROJECTED_PERCENTILE or peak > MAX_PROJECTED_PEAK:
            continue
        now = hourly_price(ctx.prices, r.region_id, "ec2_", family, size)
        after = hourly_price(ctx.prices, r.region_id, "ec2_", family, target_size)
        if now is None or after is None:
            continue
        target = f"{family}.{target_size}"
        # No memory data (the CloudWatch agent is optional): confidence is at most medium.
        confidence: Confidence = "medium" if days == RIGHTSIZE_DAYS else "low"
        notes = ["no_memory_data", *_price_notes(now[1])]
        if days < RIGHTSIZE_DAYS:
            notes.append("short_history")
        evidence: list[dict[str, Any]] = [
            {"kind": "cpuPercentile", "percentile": pct, "value": round(percentile(cpu, pct), 2),
             "days": days},
            {"kind": "peakCpuPercent", "value": round(max(cpu), 2), "days": days},
            {"kind": "projectedCpuPercent", "value": round(projected, 2), "headroom": headroom},
        ]  # fmt: skip
        if optimizer.get("finding"):
            notes.append("compute_optimizer_agrees")
            evidence.append(
                {"kind": "computeOptimizer", "value": optimizer.get("finding"),
                 "options": optimizer.get("options", [])}
            )  # fmt: skip
        name = r.name or r.resource_id
        yield Candidate(
            rule_id="R5_rightsize_ec2",
            rule_version=1,
            type="rightsize_instance",
            console_steps=change_type_steps(target),
            resource=r,
            title=f"Make server {name} one size smaller: {kind} to {target}",
            current_config={"instanceType": kind},
            recommended_config={"instanceType": target},
            cost_now=now[0] * HOURS_PER_MONTH,
            cost_after=after[0] * HOURS_PER_MONTH,
            basis="list_price",
            effort="S",
            reversibility="reversible",
            confidence=confidence,
            confidence_notes=notes,
            risk_factors=[
                *env_factor(r.tags),
                {"factor": "restart_needed", "points": 10},
                {"factor": "less_headroom", "points": 15},
            ],
            evidence=evidence,
            rule_config={
                "percentile": pct,
                "headroom": headroom,
                "maxProjectedPercentile": MAX_PROJECTED_PERCENTILE,
                "maxProjectedPeak": MAX_PROJECTED_PEAK,
                "days": days,
            },
            restart_required=True,
            fix={
                **_change_type(r, target),
                "notes": "The server stops for a few minutes while its size changes. Watch it "
                "for a few days afterwards; going back is the same change in reverse.",
            },
        )


COMPUTE_RULES: list[Callable[[Context, list[Resource]], Iterator[Candidate]]] = [
    idle_instances,
    idle_databases,
    rightsizing,
    previous_generation,
]
