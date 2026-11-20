"""DigitalOcean savings rules (ADR-021), run by the shared engine over DigitalOcean resources.

- DO1, a volume attached to no Droplet: snapshot, then delete (the data stays in the snapshot).
- DO2, a reserved IP assigned to no Droplet: release it (the address cannot be got back).
- DO3, a snapshot older than 90 days: delete it (it may be the only copy).
- DO4, a running Droplet with low CPU over the tenant's idle period: snapshot, then destroy
  (rebuilt from the snapshot).
- DO5, a load balancer with no Droplets behind it: save its settings, then delete (a new one
  gets a new IP).

Costs are DigitalOcean's list prices (USD a month) or, for Droplets, the price DigitalOcean
returns with the size. A powered-off Droplet is still billed, so DO4 recommends a snapshot
and destroy, not power off. Rollback steps are commands to run, with ``<placeholders>`` for
what DigitalOcean returns when the fix runs.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

from cloudoptics.analysis.registry import RuleSpec, check_unique
from cloudoptics.analysis.rules import (
    Candidate,
    Confidence,
    Context,
    Resource,
    env_factor,
    parse_time,
)

# List prices, USD a month (digitalocean.com/pricing, November 2026).
VOLUME_GB_MONTH = 0.10
SNAPSHOT_GB_MONTH = 0.06
RESERVED_IP_UNASSIGNED_MONTH = 5.00
LOAD_BALANCER_NODE_MONTH = 12.00
OLD_SNAPSHOT_DAYS = 90
RETAIN_TAGS = ("retain", "retention", "compliance", "legal-hold", "do-not-delete", "backup")
CONSOLE = "https://cloud.digitalocean.com"


def _cli(*args: str) -> str:
    return " ".join(shlex.quote(a) for a in args)


def _age_days(now: datetime, since: datetime | None) -> float | None:
    return None if since is None else (now - since) / timedelta(days=1)


def _console(path: str, steps: list[str]) -> dict[str, Any]:
    return {"url": f"{CONSOLE}/{path}", "steps": steps}


def _retained(r: Resource) -> bool:
    return any(k.lower() in RETAIN_TAGS for k in r.tags)


# --- DO1 unattached volume ----------------------------------------------------------------


def unattached_volumes(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "do_volume" or r.state != "available":
            continue
        size = float(r.config.get("sizeGiB") or 0)
        cost = size * VOLUME_GB_MONTH
        if cost <= 0:
            continue
        unattached = _age_days(ctx.now, r.state_since) or 0
        confidence: Confidence = (
            "high" if unattached >= 30 else "medium" if unattached >= 7 else "low"
        )
        factors = env_factor(r.tags)
        if unattached < 7:
            factors.append({"factor": "recently_detached", "points": 10})
        name = r.name or r.resource_id
        yield Candidate(
            rule_id="DO1_unattached_volume",
            rule_version=1,
            type="delete_unattached_volume",
            resource=r,
            title=f"Delete unattached volume {name} ({size:g} GiB)",
            current_config={"sizeGiB": size, "attached": False},
            recommended_config={"action": "snapshot_then_delete"},
            cost_now=cost,
            cost_after=size * SNAPSHOT_GB_MONTH,
            basis="list_price",
            effort="S",
            reversibility="with_snapshot",
            confidence=confidence,
            risk_factors=factors,
            evidence=[{"kind": "state", "value": "available", "sinceDays": round(unattached, 1)}],
            rule_config={"mediumDays": 7, "highDays": 30},
            fix={
                "steps": [
                    _cli(
                        "doctl",
                        "compute",
                        "volume",
                        "snapshot",
                        r.resource_id,
                        "--snapshot-name",
                        f"cloudoptics-backup-{r.resource_id}",
                    ),
                    _cli("doctl", "compute", "volume", "delete", r.resource_id, "--force"),
                ],
                "rollback": [
                    _cli(
                        "doctl",
                        "compute",
                        "volume",
                        "create",
                        name,
                        "--region",
                        r.region_id,
                        "--snapshot",
                        "<snapshot-id>",
                        "--size",
                        f"{size:g}GiB",
                    ),
                ],
                "notes": "The snapshot costs USD 0.06 per GiB a month; delete it once you are "
                "sure the data is not needed.",
                "console": _console(
                    "volumes",
                    [
                        "Open Volumes and find the volume.",
                        "Choose More, then Take snapshot, and wait for it to finish.",
                        "Choose More, then Destroy, and confirm.",
                    ],
                ),
            },
        )


# --- DO2 unassigned reserved IP -----------------------------------------------------------


def unassigned_reserved_ips(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "do_reserved_ip" or r.state != "unassigned":
            continue
        days = _age_days(ctx.now, r.state_since) or 0
        factors = env_factor(r.tags)
        factors.append({"factor": "address_not_recoverable", "points": 20})
        if r.config.get("locked"):
            continue
        yield Candidate(
            rule_id="DO2_unassigned_reserved_ip",
            rule_version=1,
            type="release_address",
            resource=r,
            title=f"Release reserved IP {r.resource_id} (assigned to no Droplet)",
            current_config={"assigned": False},
            recommended_config={"action": "release"},
            cost_now=RESERVED_IP_UNASSIGNED_MONTH,
            cost_after=0.0,
            basis="list_price",
            effort="S",
            reversibility="irreversible",
            confidence="high" if days >= 7 else "medium",
            risk_factors=factors,
            evidence=[{"kind": "state", "value": "unassigned", "sinceDays": round(days, 1)}],
            fix={
                "steps": [
                    _cli("doctl", "compute", "reserved-ip", "delete", r.resource_id, "--force")
                ],
                "rollback": [
                    _cli("doctl", "compute", "reserved-ip", "create", "--region", r.region_id)
                ],
                "notes": "A released address is gone: a new reserved IP gets a different one. "
                "Check no DNS record or allow list still names this address.",
                "console": _console(
                    "networking/reserved_ips",
                    [
                        "Open Networking, then Reserved IPs.",
                        "Find the address, choose More, then Release, and confirm.",
                    ],
                ),
            },
        )


# --- DO3 old snapshot ---------------------------------------------------------------------


def old_snapshots(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "do_snapshot" or _retained(r):
            continue
        age = _age_days(ctx.now, parse_time(_iso(r.config.get("createdAt"))))
        size = float(r.config.get("sizeGiB") or 0)
        cost = size * SNAPSHOT_GB_MONTH
        if age is None or age < OLD_SNAPSHOT_DAYS or cost <= 0:
            continue
        factors = env_factor(r.tags)
        factors.append({"factor": "may_be_only_backup", "points": 25})
        yield Candidate(
            rule_id="DO3_old_snapshot",
            rule_version=1,
            type="delete_snapshot",
            resource=r,
            title=f"Delete snapshot {r.name or r.resource_id} ({age:.0f} days old, {size:g} GiB)",
            current_config={"sizeGiB": size, "ageDays": round(age)},
            recommended_config={"action": "delete"},
            cost_now=cost,
            cost_after=0.0,
            basis="list_price",
            effort="S",
            reversibility="irreversible",
            confidence="high" if age >= 180 else "medium",
            risk_factors=factors,
            evidence=[
                {"kind": "created", "value": r.config.get("createdAt")},
                {"kind": "source", "value": r.config.get("resourceType")},
            ],
            rule_config={"minDays": OLD_SNAPSHOT_DAYS},
            fix={
                "steps": [_cli("doctl", "compute", "snapshot", "delete", r.resource_id, "--force")],
                "rollback": [],
                "notes": "Deleting a snapshot cannot be undone. Keep it if it is the only copy of "
                "data you may need; tag it 'retain' and Cloudoptics stops suggesting this.",
                "console": _console(
                    "images/snapshots/droplets",
                    [
                        "Open Backups & Snapshots, then Snapshots.",
                        "Find the snapshot, choose More, then Delete, and confirm.",
                    ],
                ),
            },
        )


def _iso(value: Any) -> str | None:
    return str(value).replace("Z", "+00:00") if value else None


# --- DO4 idle Droplet ---------------------------------------------------------------------


def idle_droplets(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    limit = ctx.settings.idle_cpu_percent
    for r in resources:
        if r.resource_type != "do_droplet" or r.state != "active":
            continue
        avg, peak, days = (r.config.get(k) for k in ("cpuAvgPct", "cpuMaxPct", "cpuDays"))
        price = float(r.config.get("priceMonthly") or 0)
        if avg is None or peak is None or days is None or price <= 0:
            continue  # no CPU data (monitoring:read not granted, or no agent): stay quiet
        if float(days) < min(ctx.settings.idle_days, 14) or float(avg) >= limit:
            continue
        if float(peak) >= max(limit * 5, 10.0):
            continue  # short busy spells: not idle
        disk = float(r.config.get("diskGiB") or 0)
        factors = env_factor(r.tags)
        factors.append({"factor": "server_removed", "points": 20})
        name = r.name or r.resource_id
        yield Candidate(
            rule_id="DO4_idle_droplet",
            rule_version=1,
            type="delete_idle_instance",
            resource=r,
            title=f"Snapshot and destroy idle Droplet {name} ({r.config.get('sizeSlug')})",
            current_config={
                "sizeSlug": r.config.get("sizeSlug"),
                "cpuAvgPct": avg,
                "cpuMaxPct": peak,
            },
            recommended_config={"action": "snapshot_then_destroy"},
            cost_now=price,
            cost_after=disk * SNAPSHOT_GB_MONTH,
            basis="list_price",
            effort="M",
            reversibility="with_snapshot",
            confidence="high" if float(days) >= 14 and float(peak) < limit * 2 else "medium",
            risk_factors=factors,
            evidence=[{"kind": "cpu", "averagePct": avg, "peakPct": peak, "days": days}],
            rule_config={"idleCpuPercent": limit, "days": 14},
            fix={
                "steps": [
                    _cli(
                        "doctl",
                        "compute",
                        "droplet-action",
                        "snapshot",
                        r.resource_id,
                        "--snapshot-name",
                        f"cloudoptics-backup-{r.resource_id}",
                        "--wait",
                    ),
                    _cli("doctl", "compute", "droplet", "delete", r.resource_id, "--force"),
                ],
                "rollback": [
                    _cli(
                        "doctl",
                        "compute",
                        "droplet",
                        "create",
                        name,
                        "--region",
                        r.region_id,
                        "--size",
                        str(r.config.get("sizeSlug") or "<size>"),
                        "--image",
                        "<snapshot-id>",
                        "--wait",
                    ),
                ],
                "notes": "A powered-off Droplet is still billed, so the saving needs the Droplet "
                "destroyed. A rebuilt Droplet gets a new IP address unless it uses a reserved IP.",
                "console": _console(
                    f"droplets/{r.resource_id}",
                    [
                        "Open the Droplet, choose Snapshots and take a snapshot.",
                        "When it is done, choose Destroy and confirm.",
                    ],
                ),
            },
        )


# --- DO5 idle load balancer ---------------------------------------------------------------


def idle_load_balancers(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "do_load_balancer" or r.state not in (None, "active", "new"):
            continue
        if r.config.get("dropletIds") or r.config.get("dropletTag"):
            continue
        nodes = int(r.config.get("sizeUnit") or 1)
        days = _age_days(ctx.now, r.state_since) or 0
        factors = env_factor(r.tags)
        factors.append({"factor": "address_not_recoverable", "points": 10})
        name = r.name or r.resource_id
        yield Candidate(
            rule_id="DO5_idle_load_balancer",
            rule_version=1,
            type="delete_idle_load_balancer",
            resource=r,
            title=f"Delete load balancer {name} (no Droplets behind it)",
            current_config={"nodes": nodes, "droplets": 0},
            recommended_config={"action": "delete"},
            cost_now=nodes * LOAD_BALANCER_NODE_MONTH,
            cost_after=0.0,
            basis="list_price",
            effort="S",
            reversibility="irreversible",
            confidence="high" if days >= 7 else "medium",
            risk_factors=factors,
            evidence=[{"kind": "targets", "value": 0, "sinceDays": round(days, 1)}],
            fix={
                "steps": [
                    _cli(
                        "doctl",
                        "compute",
                        "load-balancer",
                        "get",
                        r.resource_id,
                        "--output",
                        "json",
                    )
                    + " > "
                    + shlex.quote(f"lb-{r.resource_id}.json"),
                    _cli("doctl", "compute", "load-balancer", "delete", r.resource_id, "--force"),
                ],
                "rollback": [
                    _cli(
                        "doctl",
                        "compute",
                        "load-balancer",
                        "create",
                        "--name",
                        name,
                        "--region",
                        r.region_id,
                        "--forwarding-rules",
                        "<rules from the saved JSON>",
                    ),
                ],
                "notes": "A new load balancer gets a new IP address; update DNS if anything "
                "pointed at this one.",
                "console": _console(
                    "networking/load_balancers",
                    [
                        "Open Networking, then Load Balancers.",
                        "Open the load balancer, choose Settings, then Destroy, and confirm.",
                    ],
                ),
            },
        )


def _spec(rid: str, rule_id: str, kind: str, fn: Any) -> RuleSpec:
    return RuleSpec(
        id=rid,
        provider="digitalocean",
        rule_ids=(rule_id,),
        resource_types=frozenset({kind}),
        version=1,
        evaluate=fn,
    )


DO_RULES: tuple[RuleSpec, ...] = check_unique(
    (
        _spec("DO1", "DO1_unattached_volume", "do_volume", unattached_volumes),
        _spec("DO2", "DO2_unassigned_reserved_ip", "do_reserved_ip", unassigned_reserved_ips),
        _spec("DO3", "DO3_old_snapshot", "do_snapshot", old_snapshots),
        _spec("DO4", "DO4_idle_droplet", "do_droplet", idle_droplets),
        _spec("DO5", "DO5_idle_load_balancer", "do_load_balancer", idle_load_balancers),
    )
)
RULE_IDS = tuple(r for spec in DO_RULES for r in spec.rule_ids)
