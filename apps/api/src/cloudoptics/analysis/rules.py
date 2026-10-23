"""Inventory rules: R1 unused storage, R2 idle network, R3 gp2 to gp3 (usage-based rules
are in ``compute``).

Each rule turns inventory rows into candidates with costs, risk factors, confidence and a
fix (PRD FR-ANA-01/01b/02/02b/03, FR-REC-02/05). Rules are pure: the engine loads the
inputs, scores the candidates (``scoring``) and stores them.
"""

from __future__ import annotations

import shlex
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Literal

from cloudoptics.analysis.pricing import HOURS_PER_MONTH, Basis, PriceBook
from cloudoptics.analysis.settings import RuleSettings

Confidence = Literal["high", "medium", "low"]
Reversibility = Literal["reversible", "with_snapshot", "irreversible"]

PROD = {"prod", "production", "prd", "live"}
NONPROD = {"dev", "development", "test", "testing", "qa", "staging", "stage", "sandbox", "uat"}
ENV_TAGS = ("env", "environment", "stage")
RETAIN_TAGS = ("retain", "retention", "compliance", "legal-hold", "do-not-delete")
LB_ITEMS = {"application": "alb_hour", "network": "nlb_hour", "gateway": "gwlb_hour"}


@dataclass(frozen=True)
class Resource:
    id: uuid.UUID
    account_id: str
    region_id: str
    resource_type: str
    resource_id: str
    name: str | None
    state: str | None
    config: dict[str, Any]
    tags: dict[str, str]
    state_since: datetime
    first_seen_at: datetime
    arn: str | None = None


@dataclass
class Context:
    now: datetime
    prices: PriceBook
    settings: RuleSettings = field(default_factory=RuleSettings)
    # (account, region) -> {volume id -> newest completed snapshot start}
    snapshots: dict[tuple[str, str], dict[str, datetime]] = field(default_factory=dict)
    # (account, region) -> {instance id -> Resource}
    instances: dict[tuple[str, str], dict[str, Resource]] = field(default_factory=dict)
    live_volumes: set[tuple[str, str, str]] = field(default_factory=set)
    # (account, region, volume id) -> Resource
    volumes: dict[tuple[str, str, str], Resource] = field(default_factory=dict)
    # resource uuid -> metric -> UTC day -> 24 hourly values (None where no data)
    usage: dict[uuid.UUID, dict[str, dict[date, list[float | None]]]] = field(default_factory=dict)


@dataclass
class Candidate:
    rule_id: str
    rule_version: int
    type: str
    resource: Resource
    title: str
    current_config: dict[str, Any]
    recommended_config: dict[str, Any]
    cost_now: float
    cost_after: float
    basis: Basis
    effort: Literal["S", "M", "L"]
    reversibility: Reversibility
    confidence: Confidence
    risk_factors: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    fix: dict[str, Any]
    confidence_notes: list[str] = field(default_factory=list)
    rule_config: dict[str, Any] = field(default_factory=dict)
    restart_required: bool = False

    @property
    def savings(self) -> float:
        return self.cost_now - self.cost_after


def parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def environment(tags: dict[str, str]) -> Literal["prod", "nonprod", "unknown"]:
    lowered = {k.lower(): v.strip().lower() for k, v in tags.items()}
    for key in ENV_TAGS:
        value = lowered.get(key)
        if value in PROD:
            return "prod"
        if value in NONPROD:
            return "nonprod"
    return "unknown"


def env_factor(tags: dict[str, str]) -> list[dict[str, Any]]:
    env = environment(tags)
    if env == "prod":
        return [{"factor": "production", "points": 25}]
    if env == "unknown":
        return [{"factor": "environment_unknown", "points": 15}]
    return []


def _age_days(ctx: Context, since: datetime | None) -> float | None:
    return None if since is None else (ctx.now - since) / timedelta(days=1)


def _cli(*args: str) -> str:
    return " ".join(shlex.quote(a) for a in args)


def _region_args(r: Resource) -> list[str]:
    return ["--region", r.region_id]


def volume_month_cost(prices: PriceBook, r: Resource, volume_type: str) -> tuple[float, Basis]:
    """Monthly cost of a volume as configured, at the tenant's rate where known."""
    c = r.config
    size = float(c.get("sizeGiB") or 0)
    item = {"io1": "ebs_io1_gb_month", "io2": "ebs_io2_gb_month"}.get(
        volume_type, f"ebs_{volume_type}_gb_month"
    )
    storage = prices.price(r.account_id, r.region_id, item)
    total, basis = size * storage.usd, storage.basis
    iops = float(c.get("iops") or 0)
    if volume_type in ("io1", "io2") and iops:
        total += iops * prices.price(r.account_id, r.region_id, "ebs_io1_iops_month").usd
    if volume_type == "gp3":
        extra_iops = max(0.0, iops - 3000)
        extra_tp = max(0.0, float(c.get("throughput") or 125) - 125)
        total += extra_iops * prices.price(r.account_id, r.region_id, "ebs_gp3_iops_month").usd
        total += extra_tp * prices.price(r.account_id, r.region_id, "ebs_gp3_throughput_month").usd
    return total, basis


# --- R1: unattached EBS volumes (FR-ANA-01) -----------------------------------------------


def unattached_volumes(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "ebs_volume" or r.state != "available":
            continue
        volume_type = str(r.config.get("volumeType") or "gp2")
        if volume_type not in {"gp2", "gp3", "io1", "io2", "st1", "sc1", "standard"}:
            continue
        unattached = _age_days(ctx, r.state_since) or 0
        created = parse_time(r.config.get("createTime"))
        age = _age_days(ctx, created)
        notes: list[str] = []
        first_sight = abs((r.state_since - r.first_seen_at) / timedelta(minutes=1)) < 5
        if unattached >= 32:
            confidence: Confidence = "high"
        elif unattached >= 7:
            confidence = "medium"
        elif first_sight and age is not None and age >= 7:
            # Unattached when first scanned: the detach time is unknown.
            confidence = "low"
            notes.append("detach_time_unknown")
        else:
            continue
        cost, basis = volume_month_cost(ctx.prices, r, volume_type)
        if cost <= 0:
            continue
        last_snap = ctx.snapshots.get((r.account_id, r.region_id), {}).get(r.resource_id)
        factors = env_factor(r.tags)
        if last_snap is None or (_age_days(ctx, last_snap) or 0) > 30:
            factors.append({"factor": "no_recent_snapshot", "points": 30})
        if age is not None and age < 30:
            factors.append({"factor": "recently_created", "points": 10})
        region = _region_args(r)
        zone = str(r.config.get("availabilityZone") or f"{r.region_id}a")
        yield Candidate(
            rule_id="R1_unattached_ebs",
            rule_version=1,
            type="delete_unattached_volume",
            resource=r,
            title=f"Delete unattached {volume_type} volume {r.name or r.resource_id} "
            f"({r.config.get('sizeGiB')} GiB)",
            current_config={"volumeType": volume_type, "sizeGiB": r.config.get("sizeGiB")},
            recommended_config={"action": "snapshot_then_delete"},
            cost_now=cost,
            cost_after=0.0,
            basis=basis,
            effort="S",
            reversibility="with_snapshot",
            confidence=confidence,
            confidence_notes=notes,
            risk_factors=factors,
            evidence=[
                {"kind": "state", "value": "available", "sinceDays": round(unattached, 1)},
                {"kind": "created", "value": created.isoformat() if created else None},
                {"kind": "lastSnapshot", "value": last_snap.isoformat() if last_snap else None},
            ],
            rule_config={"mediumDays": 7, "highDays": 32},
            fix={
                "steps": [
                    _cli(
                        "aws", "ec2", "create-snapshot", "--volume-id", r.resource_id,
                        "--description", f"CloudOptics backup of {r.resource_id}", *region,
                    ),
                    _cli(
                        "aws", "ec2", "wait", "snapshot-completed", "--filters",
                        f"Name=volume-id,Values={r.resource_id}", *region,
                    ),
                    _cli("aws", "ec2", "delete-volume", "--volume-id", r.resource_id, *region),
                ],
                "rollback": [
                    _cli(
                        "aws", "ec2", "create-volume", "--snapshot-id", "<snapshot-id>",
                        "--availability-zone", zone, "--volume-type", volume_type, *region,
                    )
                ],
                "notes": "Keep the snapshot until you are sure the data is not needed.",
            },
        )  # fmt: skip


def _protected_snapshot(r: Resource) -> bool:
    c = r.config
    tag_keys = {k.lower() for k in r.tags}
    description = str(c.get("description") or "").lower()
    return bool(
        c.get("usedByAmi")
        or str(c.get("storageTier") or "standard") != "standard"
        or any(k.startswith("aws:backup") or k.startswith("aws:dlm") for k in tag_keys)
        or "dlm:managed" in tag_keys
        or any(t in tag_keys for t in RETAIN_TAGS)
        or "aws backup" in description
        or "created by createimage" in description
        or "dlm" in description.split()
    )


def old_snapshots(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    """FR-ANA-01b: completed snapshots 90+ days old whose source volume is gone."""
    for r in resources:
        if r.resource_type != "ebs_snapshot" or r.state != "completed":
            continue
        started = parse_time(r.config.get("startTime"))
        age = _age_days(ctx, started)
        if age is None or age < 90 or _protected_snapshot(r):
            continue
        volume = r.config.get("volumeId")
        if volume and (r.account_id, r.region_id, volume) in ctx.live_volumes:
            continue
        size = float(r.config.get("volumeSizeGiB") or 0)
        price = ctx.prices.price(r.account_id, r.region_id, "ebs_snapshot_gb_month")
        cost = size * price.usd
        if cost <= 0:
            continue
        archive = ctx.prices.price(r.account_id, r.region_id, "ebs_snapshot_archive_gb_month")
        region = _region_args(r)
        yield Candidate(
            rule_id="R1_old_snapshot",
            rule_version=1,
            type="delete_old_snapshot",
            resource=r,
            title=f"Delete {int(age)}-day-old snapshot {r.name or r.resource_id} "
            f"of a deleted volume",
            current_config={"sizeGiB": size, "storageTier": "standard"},
            recommended_config={"action": "delete", "alternative": "archive"},
            cost_now=cost,
            cost_after=0.0,
            basis=price.basis,
            effort="S",
            reversibility="irreversible",
            # Snapshots are incremental: the bill drops by at most this much.
            confidence="medium",
            confidence_notes=["savings_upper_bound_incremental"],
            risk_factors=[*env_factor(r.tags), {"factor": "data_loss", "points": 30}],
            evidence=[
                {"kind": "started", "value": started.isoformat() if started else None},
                {"kind": "sourceVolume", "value": volume, "exists": False},
            ],
            rule_config={"minAgeDays": 90},
            fix={
                "steps": [
                    _cli("aws", "ec2", "delete-snapshot", "--snapshot-id", r.resource_id, *region)
                ],
                "alternative": {
                    "title": "Archive instead (about 75% cheaper, 90-day minimum)",
                    "monthlyCostUsd": round(size * archive.usd, 2),
                    "steps": [
                        _cli(
                            "aws", "ec2", "modify-snapshot-tier", "--snapshot-id",
                            r.resource_id, "--storage-tier", "archive", *region,
                        )
                    ],
                },
                "rollback": [],
                "notes": "Deleting a snapshot cannot be undone.",
            },
        )  # fmt: skip


# --- R2: idle Elastic IPs and load balancers (FR-ANA-02/02b) ------------------------------


def idle_addresses(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "eip":
            continue
        notes: list[str] = []
        since = _age_days(ctx, r.state_since) or 0
        first_sight = abs((r.state_since - r.first_seen_at) / timedelta(minutes=1)) < 5
        if r.state == "unassociated":
            if since >= 7:
                confidence: Confidence = "high"
            elif since >= 3:
                confidence = "medium"
            elif first_sight:
                confidence = "low"
                notes.append("idle_since_unknown")
            else:
                continue
            reason = "not attached to anything"
        else:
            instance_id = r.config.get("instanceId")
            instance = ctx.instances.get((r.account_id, r.region_id), {}).get(
                str(instance_id or "")
            )
            if instance is None or instance.state != "stopped":
                continue
            stopped = _age_days(ctx, instance.state_since) or 0
            if stopped < 7:
                continue
            confidence = "medium"
            reason = f"attached to instance {instance_id}, stopped {int(stopped)} days"
        price = ctx.prices.price(r.account_id, r.region_id, "public_ipv4_hour")
        ip = r.config.get("publicIp") or r.resource_id
        region = _region_args(r)
        yield Candidate(
            rule_id="R2_idle_eip",
            rule_version=1,
            type="release_idle_ip",
            resource=r,
            title=f"Release idle Elastic IP {ip} ({reason})",
            current_config={"publicIp": ip, "state": r.state},
            recommended_config={"action": "release"},
            cost_now=HOURS_PER_MONTH * price.usd,
            cost_after=0.0,
            basis=price.basis,
            effort="S",
            reversibility="irreversible",
            confidence=confidence,
            confidence_notes=notes,
            risk_factors=env_factor(r.tags),
            evidence=[{"kind": "state", "value": r.state, "sinceDays": round(since, 1)}],
            rule_config={"mediumDays": 3, "highDays": 7, "stoppedDays": 7},
            fix={
                "steps": [
                    *(
                        [_cli("aws", "ec2", "disassociate-address", "--public-ip", str(ip),
                              *region)]
                        if r.state == "associated" else []
                    ),
                    _cli("aws", "ec2", "release-address", "--allocation-id", r.resource_id,
                         *region),
                ],
                "rollback": [],
                "notes": "A released address cannot be got back; update DNS or allow-lists "
                "that use it first.",
            },
        )  # fmt: skip


def idle_load_balancers(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "elb" or r.state != "idle":
            continue
        kind = str(r.config.get("type") or "application")
        created = parse_time(r.config.get("createdTime"))
        age = _age_days(ctx, created)
        if age is not None and age < 7:
            continue
        idle = _age_days(ctx, r.state_since) or 0
        confidence: Confidence = "high" if idle >= 7 else ("medium" if idle >= 3 else "low")
        item = LB_ITEMS.get(kind, "clb_hour")
        price = ctx.prices.price(r.account_id, r.region_id, item)
        region = _region_args(r)
        if kind == "classic":
            delete = _cli(
                "aws",
                "elb",
                "delete-load-balancer",
                "--load-balancer-name",
                r.resource_id,
                *region,
            )
        else:
            arn = r.arn or f"<arn of {r.resource_id}>"
            delete = _cli(
                "aws", "elbv2", "delete-load-balancer", "--load-balancer-arn", str(arn), *region
            )
        yield Candidate(
            rule_id="R2_idle_lb",
            rule_version=1,
            type="delete_idle_load_balancer",
            resource=r,
            title=f"Delete idle {kind} load balancer {r.resource_id} (no healthy targets)",
            current_config={
                "type": kind,
                "targets": r.config.get("targets"),
                "healthyTargets": r.config.get("healthyTargets"),
            },
            recommended_config={"action": "delete"},
            cost_now=HOURS_PER_MONTH * price.usd,
            cost_after=0.0,
            basis=price.basis,
            effort="S",
            reversibility="irreversible",
            confidence=confidence,
            confidence_notes=["hourly_charge_only_lcu_excluded"],
            risk_factors=env_factor(r.tags),
            evidence=[
                {"kind": "healthyTargets", "value": r.config.get("healthyTargets")},
                {"kind": "idleDays", "value": round(idle, 1)},
            ],
            rule_config={"minAgeDays": 7, "highDays": 7},
            fix={
                "steps": [delete],
                "rollback": [],
                "notes": "Check DNS records and listeners pointing at it before deleting.",
            },
        )  # fmt: skip


# --- R3: gp2 to gp3 (FR-ANA-03) -----------------------------------------------------------


def gp3_target(size_gib: float) -> tuple[int, int]:
    """IOPS and throughput that match or beat what gp2 gives a volume of this size."""
    iops = int(min(16000, max(3000, 3 * size_gib)))
    throughput = 250 if size_gib >= 170 else 125
    return iops, throughput


def gp2_to_gp3(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "ebs_volume" or r.config.get("volumeType") != "gp2":
            continue
        if r.state not in ("in-use", "available"):
            continue
        size = float(r.config.get("sizeGiB") or 0)
        if size <= 0:
            continue
        iops, throughput = gp3_target(size)
        now_cost, basis = volume_month_cost(ctx.prices, r, "gp2")
        target = replace(r, config={**r.config, "iops": iops, "throughput": throughput})
        after, _ = volume_month_cost(ctx.prices, target, "gp3")
        if now_cost - after <= 0.01:
            continue
        factors = env_factor(r.tags)
        if size > 1024:
            factors.append({"factor": "large_volume", "points": 20})
        region = _region_args(r)
        yield Candidate(
            rule_id="R3_gp2_gp3",
            rule_version=1,
            type="modify_volume_gp3",
            resource=r,
            title=f"Change volume {r.name or r.resource_id} from gp2 to gp3 ({int(size)} GiB)",
            current_config={"volumeType": "gp2", "sizeGiB": size},
            recommended_config={"volumeType": "gp3", "iops": iops, "throughput": throughput},
            cost_now=now_cost,
            cost_after=after,
            basis=basis,
            effort="S",
            reversibility="reversible",
            confidence="high",
            risk_factors=factors,
            evidence=[{"kind": "volumeType", "value": "gp2", "sizeGiB": size}],
            rule_config={"maxIops": 16000, "throughputAt170GiB": 250},
            fix={
                "steps": [
                    _cli(
                        "aws", "ec2", "modify-volume", "--volume-id", r.resource_id,
                        "--volume-type", "gp3", "--iops", str(iops), "--throughput",
                        str(throughput), *region,
                    )
                ],
                "rollback": [
                    _cli(
                        "aws", "ec2", "modify-volume", "--volume-id", r.resource_id,
                        "--volume-type", "gp2", *region,
                    )
                ],
                "notes": "Online change, no restart. A volume can be modified once every "
                "6 hours.",
            },
        )  # fmt: skip


RULES: list[Callable[[Context, list[Resource]], Iterator[Candidate]]] = [
    unattached_volumes,
    old_snapshots,
    idle_addresses,
    idle_load_balancers,
    gp2_to_gp3,
]
