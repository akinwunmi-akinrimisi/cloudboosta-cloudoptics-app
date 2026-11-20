"""Google Cloud savings rules (ADR-019): the starter set, as ``RuleSpec``s for the registry.

| Rule | Finds | Fix |
|---|---|---|
| GCP1 | running VMs whose hourly CPU stayed under the idle threshold | stop the VM |
| GCP2 | persistent disks attached to nothing | snapshot, then delete |
| GCP3 | reserved external IP addresses not in use | release the address |
| GCP4 | snapshots 90+ days old of disks that are gone | delete the snapshot |
| GCP5 | Recommender's machine-type changes for VMs | change the machine type |
| GCP6 | Recommender's idle Cloud SQL instances | stop the instance |
| GCP7 | Recommender's over-provisioned Cloud SQL instances | smaller tier |

GCP1-GCP4 price with Google's public list prices (us-central1, Nov 2026; ``LIST_PRICES``):
an estimate, shown with the ``list_price`` basis. GCP5-GCP7 take Recommender's own savings,
which use the customer's prices (``effective_rate``), converted to USD at today's rate.
Every fix is a ``gcloud`` command with its rollback and the console route.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from cloudoptics.analysis.compute import complete_days, percentile
from cloudoptics.analysis.pricing import HOURS_PER_MONTH
from cloudoptics.analysis.registry import RuleSpec, check_unique
from cloudoptics.analysis.rules import (
    RETAIN_TAGS,
    Candidate,
    Confidence,
    Context,
    Resource,
    env_factor,
    parse_time,
)

CONSOLE = "https://console.cloud.google.com"
#: USD list prices (us-central1). Disks per GB-month, addresses per hour, snapshots per
#: GB-month, VMs per vCPU-hour and GB-hour by machine family.
LIST_PRICES: dict[str, float] = {
    "pd-standard": 0.04,
    "pd-balanced": 0.10,
    "pd-ssd": 0.17,
    "pd-extreme": 0.125,
    "hyperdisk-balanced": 0.08,
    "hyperdisk-throughput": 0.05,
    "hyperdisk-extreme": 0.125,
    "static_ip_hour": 0.01,
    "snapshot_gb_month": 0.05,
}
VCPU_HOUR = {"e2": 0.021811, "n1": 0.031611, "n2": 0.031611, "n2d": 0.027502, "c2": 0.03398,
             "c3": 0.03465, "t2d": 0.027502, "n4": 0.030}  # fmt: skip
GB_HOUR = {"e2": 0.002923, "n1": 0.004237, "n2": 0.004237, "n2d": 0.003686, "c2": 0.00455,
           "c3": 0.004650, "t2d": 0.003686, "n4": 0.0040}  # fmt: skip
SHARED_CORE_MONTH = {"e2-micro": 6.11, "e2-small": 12.23, "e2-medium": 24.46,
                     "f1-micro": 3.88, "g1-small": 13.23}  # fmt: skip
MEMORY_PER_VCPU = {"standard": 4.0, "highmem": 8.0, "highcpu": 1.0}
SNAPSHOT_MIN_AGE_DAYS = 90


def _cli(*args: str) -> str:
    return " ".join(shlex.quote(a) for a in args)


def _n(r: Resource) -> str:
    """The resource's own name (``resource_id`` is ``disks/<name>``, ADR-019)."""
    return r.name or r.resource_id.rsplit("/", 1)[-1]


def zonal(location: str) -> bool:
    return re.fullmatch(r".+-[a-z]", location) is not None


def location_args(r: Resource) -> list[str]:
    if r.region_id == "global":
        return ["--global"]
    return ["--zone", r.region_id] if zonal(r.region_id) else ["--region", r.region_id]


def _project(r: Resource) -> list[str]:
    return ["--project", r.account_id]


def console_link(r: Resource) -> str:
    p, name, loc = quote(r.account_id), quote(_n(r)), quote(r.region_id)
    kind = "zones" if zonal(r.region_id) else "regions"
    pages = {
        "gce_instance": f"compute/instancesDetail/zones/{loc}/instances/{name}",
        "gce_disk": f"compute/disksDetail/{kind}/{loc}/disks/{name}",
        "gce_address": "networking/addresses/list",
        "gce_snapshot": f"compute/snapshotsDetail/projects/{p}/global/snapshots/{name}",
        "cloudsql_instance": f"sql/instances/{name}/overview",
    }
    return f"{CONSOLE}/{pages.get(r.resource_type, 'home/dashboard')}?project={p}"


def machine_month_usd(machine_type: str | None) -> float | None:
    """Monthly list price of a machine type, from its name (``e2-standard-4``,
    ``n2-custom-4-8192``); None for a type the table cannot price."""
    if not machine_type:
        return None
    if machine_type in SHARED_CORE_MONTH:
        return SHARED_CORE_MONTH[machine_type]
    parts = machine_type.split("-")
    family = parts[0] if parts[0] != "custom" else "n1"
    if family not in VCPU_HOUR:
        return None
    custom = re.search(r"custom-(\d+)-(\d+)", machine_type)
    if custom:
        vcpus, memory = float(custom.group(1)), float(custom.group(2)) / 1024
    elif len(parts) >= 3 and parts[1] in MEMORY_PER_VCPU and parts[2].isdigit():
        vcpus = float(parts[2])
        per = 3.75 if family == "n1" and parts[1] == "standard" else MEMORY_PER_VCPU[parts[1]]
        memory = vcpus * per
    else:
        return None
    return round(HOURS_PER_MONTH * (vcpus * VCPU_HOUR[family] + memory * GB_HOUR[family]), 2)


def disk_month_usd(r: Resource) -> float:
    price = LIST_PRICES.get(str(r.config.get("type") or "pd-standard"), LIST_PRICES["pd-balanced"])
    return float(r.config.get("sizeGb") or 0) * price * (2 if r.config.get("regional") else 1)


def _days(ctx: Context, since: Any) -> float:
    when = parse_time(since)
    return 0.0 if when is None else (ctx.now - when) / timedelta(days=1)


def _labels(r: Resource) -> dict[str, str]:
    return {k.lower(): v for k, v in r.tags.items()}


def _console(r: Resource, steps: list[str]) -> dict[str, Any]:
    return {"url": console_link(r), "steps": steps}


# --- GCP1: idle VMs -----------------------------------------------------------------------


def idle_instances(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    days = ctx.settings.idle_days
    threshold = ctx.settings.idle_cpu_percent
    for r in resources:
        if r.resource_type != "gce_instance" or r.state != "RUNNING":
            continue
        cpu = complete_days(ctx, r, "cpu", days)
        if not cpu:
            continue
        p95 = percentile(cpu, 95)
        if p95 >= threshold:
            continue
        cost = machine_month_usd(r.config.get("machineType"))
        if not cost:
            continue
        confidence: Confidence = "high" if percentile(cpu, 99) < threshold else "medium"
        factors = env_factor(_labels(r))
        if r.config.get("externalIp"):
            factors.append({"factor": "ephemeral_ip_released_on_stop", "points": 10})
        args = [_n(r), *location_args(r), *_project(r)]
        yield Candidate(
            rule_id="GCP1_idle_vm",
            rule_version=1,
            type="stop_idle_instance",
            resource=r,
            title=f"Stop idle VM {_n(r)} ({r.config.get('machineType')})",
            current_config={"machineType": r.config.get("machineType"), "status": "RUNNING"},
            recommended_config={"action": "stop"},
            cost_now=cost,
            cost_after=0.0,
            basis="list_price",
            effort="S",
            reversibility="reversible",
            confidence=confidence,
            confidence_notes=["disks_and_static_ips_still_billed"],
            risk_factors=factors,
            evidence=[
                {"kind": "cpuP95Percent", "value": round(p95, 2), "days": days},
                {"kind": "machineType", "value": r.config.get("machineType")},
            ],
            rule_config={"idleCpuPercent": threshold, "idleDays": days},
            fix={
                "steps": [_cli("gcloud", "compute", "instances", "stop", *args)],
                "rollback": [_cli("gcloud", "compute", "instances", "start", *args)],
                "notes": "Its disks and any static IP are still billed while it is stopped. "
                "An ephemeral external IP changes when it starts again.",
                "console": _console(
                    r, ["Open the VM in the Google Cloud console.", "Choose Stop and confirm."]
                ),
            },
        )


# --- GCP2: unattached persistent disks ------------------------------------------------------


def unattached_disks(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "gce_disk" or r.state != "unattached":
            continue
        cost = disk_month_usd(r)
        if cost <= 0:
            continue
        since = r.config.get("lastDetachTimestamp") or r.state_since
        idle = _days(ctx, since)
        confidence: Confidence = "high" if idle >= 32 else ("medium" if idle >= 7 else "low")
        factors = [*env_factor(_labels(r)), {"factor": "no_recent_snapshot", "points": 30}]
        if any(t in _labels(r) for t in RETAIN_TAGS):
            factors.append({"factor": "retention_label", "points": 30})
        backup = f"{_n(r)[:50]}-cloudoptics-backup"
        disk_loc = (
            ["--source-disk-zone", r.region_id]
            if zonal(r.region_id)
            else ["--source-disk-region", r.region_id]
        )
        yield Candidate(
            rule_id="GCP2_unattached_disk",
            rule_version=1,
            type="delete_unattached_volume",
            resource=r,
            title=f"Delete unattached {r.config.get('type')} disk {_n(r)} "
            f"({r.config.get('sizeGb')} GB)",
            current_config={"type": r.config.get("type"), "sizeGb": r.config.get("sizeGb")},
            recommended_config={"action": "snapshot_then_delete"},
            cost_now=cost,
            cost_after=0.0,
            basis="list_price",
            effort="S",
            reversibility="with_snapshot",
            confidence=confidence,
            risk_factors=factors,
            evidence=[
                {"kind": "users", "value": []},
                {"kind": "unattachedDays", "value": round(idle, 1)},
            ],
            rule_config={"mediumDays": 7, "highDays": 32},
            fix={
                "steps": [
                    _cli("gcloud", "compute", "snapshots", "create", backup, "--source-disk",
                         _n(r), *disk_loc, *_project(r)),
                    _cli("gcloud", "compute", "disks", "delete", _n(r),
                         *location_args(r), *_project(r), "--quiet"),
                ],
                "rollback": [
                    _cli("gcloud", "compute", "disks", "create", _n(r),
                         "--source-snapshot", backup, "--type", str(r.config.get("type")),
                         *location_args(r), *_project(r)),
                ],
                "notes": "Keep the snapshot until you are sure the data is not needed; it costs "
                "far less than the disk.",
                "console": _console(
                    r,
                    [
                        "Open the disk in the Google Cloud console.",
                        "Choose Create snapshot and wait until it is ready.",
                        "Choose Delete and confirm.",
                    ],
                ),
            },
        )  # fmt: skip


# --- GCP3: reserved external addresses not in use --------------------------------------------


def unused_addresses(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "gce_address" or r.state != "RESERVED":
            continue
        if r.config.get("addressType", "EXTERNAL") != "EXTERNAL":
            continue  # internal addresses are free
        idle = (ctx.now - r.state_since) / timedelta(days=1)
        confidence: Confidence = "high" if idle >= 7 else ("medium" if idle >= 3 else "low")
        cost = LIST_PRICES["static_ip_hour"] * HOURS_PER_MONTH
        args = [_n(r), *location_args(r), *_project(r)]
        yield Candidate(
            rule_id="GCP3_unused_address",
            rule_version=1,
            type="release_idle_address",
            resource=r,
            title=f"Release unused static IP {r.config.get('address') or _n(r)}",
            current_config={"address": r.config.get("address"), "status": "RESERVED"},
            recommended_config={"action": "release"},
            cost_now=cost,
            cost_after=0.0,
            basis="list_price",
            effort="S",
            reversibility="irreversible",
            confidence=confidence,
            risk_factors=[
                *env_factor(_labels(r)),
                {"factor": "address_cannot_be_recovered", "points": 20},
            ],
            evidence=[{"kind": "status", "value": "RESERVED", "sinceDays": round(idle, 1)}],
            rule_config={"mediumDays": 3, "highDays": 7},
            fix={
                "steps": [_cli("gcloud", "compute", "addresses", "delete", *args, "--quiet")],
                "rollback": [_cli("gcloud", "compute", "addresses", "create", *args)],
                "notes": "A released address goes back to Google: the new one will differ. Check "
                "that no DNS record or allow-list still names it.",
                "console": _console(
                    r,
                    [
                        "Open IP addresses in the Google Cloud console.",
                        f"Tick {_n(r)}, choose Release static address and confirm.",
                    ],
                ),
            },
        )


# --- GCP4: old snapshots of deleted disks ----------------------------------------------------


def old_snapshots(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    disks = {(r.account_id, _n(r)) for r in resources if r.resource_type == "gce_disk"}
    for r in resources:
        if r.resource_type != "gce_snapshot" or r.state != "READY":
            continue
        age = _days(ctx, r.config.get("creationTimestamp"))
        if age < SNAPSHOT_MIN_AGE_DAYS or r.config.get("autoCreated"):
            continue  # snapshot schedules manage their own retention
        if any(t in _labels(r) for t in RETAIN_TAGS):
            continue
        source = r.config.get("sourceDisk")
        if source and (r.account_id, source) in disks:
            continue
        size_gb = float(r.config.get("storageBytes") or 0) / 1024**3
        cost = size_gb * LIST_PRICES["snapshot_gb_month"]
        if cost < 0.01:
            continue
        yield Candidate(
            rule_id="GCP4_old_snapshot",
            rule_version=1,
            type="delete_old_snapshot",
            resource=r,
            title=f"Delete {int(age)}-day-old snapshot {_n(r)} of a deleted disk",
            current_config={"storageGb": round(size_gb, 2)},
            recommended_config={"action": "delete"},
            cost_now=cost,
            cost_after=0.0,
            basis="list_price",
            effort="S",
            reversibility="irreversible",
            # Snapshots are incremental: deleting one can move data to the next.
            confidence="medium",
            confidence_notes=["savings_upper_bound_incremental"],
            risk_factors=[*env_factor(_labels(r)), {"factor": "data_loss", "points": 30}],
            evidence=[
                {"kind": "created", "value": r.config.get("creationTimestamp")},
                {"kind": "sourceDisk", "value": source, "exists": False},
            ],
            rule_config={"minAgeDays": SNAPSHOT_MIN_AGE_DAYS},
            fix={
                "steps": [
                    _cli("gcloud", "compute", "snapshots", "delete", _n(r),
                         *_project(r), "--quiet")
                ],
                "rollback": [],
                "notes": "Deleting a snapshot cannot be undone. To keep it for less, move it to "
                "an archive snapshot instead.",
                "console": _console(
                    r, ["Open the snapshot in the Google Cloud console.", "Choose Delete."]
                ),
            },
        )  # fmt: skip


# --- GCP5-GCP7: Recommender ----------------------------------------------------------------

MACHINE_TYPE = "google.compute.instance.MachineTypeRecommender"
SQL_IDLE = "google.cloudsql.instance.IdleRecommender"
SQL_OVERPROVISIONED = "google.cloudsql.instance.OverprovisionedRecommender"


def _recommendations(r: Resource, recommender: str) -> Iterator[dict[str, Any]]:
    for rec in r.config.get("recommendations") or []:
        if rec.get("recommender") == recommender and (rec.get("savingsUsdMonth") or 0) > 0:
            yield rec


def _evidence(rec: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"kind": "recommender", "value": rec.get("recommender")},
        {"kind": "recommendation", "value": rec.get("name")},
        {"kind": "description", "value": rec.get("description")},
        {
            "kind": "savingsNative",
            "value": rec.get("savingsMonthly"),
            "currency": rec.get("currency"),
        },
    ]


def rightsize_instances(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "gce_instance":
            continue
        for rec in _recommendations(r, MACHINE_TYPE):
            current, suggested = r.config.get("machineType"), rec.get("suggested")
            if not suggested:
                continue
            savings = float(rec["savingsUsdMonth"])
            now = max(machine_month_usd(current) or 0.0, savings)
            args = [_n(r), *location_args(r), *_project(r)]
            yield Candidate(
                rule_id="GCP5_rightsize_vm",
                rule_version=1,
                type="rightsize_instance",
                resource=r,
                title=f"Change VM {_n(r)} from {current} to {suggested}",
                current_config={"machineType": current},
                recommended_config={"machineType": suggested},
                cost_now=now,
                cost_after=now - savings,
                basis="effective_rate",
                effort="M",
                reversibility="reversible",
                confidence="high" if rec.get("priority") in ("P1", "P2") else "medium",
                restart_required=True,
                risk_factors=[*env_factor(_labels(r)), {"factor": "restart", "points": 15}],
                evidence=_evidence(rec),
                fix={
                    "steps": [
                        _cli("gcloud", "compute", "instances", "stop", *args),
                        _cli("gcloud", "compute", "instances", "set-machine-type", *args,
                             "--machine-type", str(suggested)),
                        _cli("gcloud", "compute", "instances", "start", *args),
                    ],
                    "rollback": [
                        _cli("gcloud", "compute", "instances", "stop", *args),
                        _cli("gcloud", "compute", "instances", "set-machine-type", *args,
                             "--machine-type", str(current)),
                        _cli("gcloud", "compute", "instances", "start", *args),
                    ],
                    "notes": "The VM restarts. Recommender based this on its last 8 days of use.",
                    "console": _console(
                        r,
                        [
                            "Open the VM, choose Stop, then Edit.",
                            f"Under Machine configuration choose {suggested}, save, then Start.",
                        ],
                    ),
                },
            )  # fmt: skip


def idle_sql(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "cloudsql_instance" or r.state != "RUNNABLE":
            continue
        for rec in _recommendations(r, SQL_IDLE):
            savings = float(rec["savingsUsdMonth"])
            yield Candidate(
                rule_id="GCP6_idle_cloudsql",
                rule_version=1,
                type="stop_idle_database",
                resource=r,
                title=f"Stop idle Cloud SQL instance {_n(r)}",
                current_config={"tier": r.config.get("tier"), "activationPolicy": "ALWAYS"},
                recommended_config={"activationPolicy": "NEVER"},
                cost_now=savings,
                cost_after=0.0,
                basis="effective_rate",
                effort="S",
                reversibility="reversible",
                confidence="medium",
                confidence_notes=["storage_still_billed"],
                risk_factors=[*env_factor(_labels(r)), {"factor": "database", "points": 20}],
                evidence=_evidence(rec),
                fix={
                    "steps": [
                        _cli("gcloud", "sql", "instances", "patch", _n(r),
                             "--activation-policy", "NEVER", *_project(r))
                    ],
                    "rollback": [
                        _cli("gcloud", "sql", "instances", "patch", _n(r),
                             "--activation-policy", "ALWAYS", *_project(r))
                    ],
                    "notes": "Storage and backups are still billed while it is stopped.",
                    "console": _console(
                        r, ["Open the instance in the Google Cloud console.", "Choose Stop."]
                    ),
                },
            )  # fmt: skip


def rightsize_sql(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "cloudsql_instance" or r.state != "RUNNABLE":
            continue
        for rec in _recommendations(r, SQL_OVERPROVISIONED):
            current, suggested = r.config.get("tier"), rec.get("suggested")
            if not suggested:
                continue
            savings = float(rec["savingsUsdMonth"])
            yield Candidate(
                rule_id="GCP7_rightsize_cloudsql",
                rule_version=1,
                type="rightsize_database",
                resource=r,
                title=f"Change Cloud SQL instance {_n(r)} from {current} to {suggested}",
                current_config={"tier": current},
                recommended_config={"tier": suggested},
                cost_now=savings,
                cost_after=0.0,
                basis="effective_rate",
                effort="M",
                reversibility="reversible",
                confidence="medium",
                restart_required=True,
                risk_factors=[*env_factor(_labels(r)), {"factor": "restart", "points": 15},
                              {"factor": "database", "points": 20}],
                evidence=_evidence(rec),
                fix={
                    "steps": [
                        _cli("gcloud", "sql", "instances", "patch", _n(r), "--tier",
                             str(suggested), *_project(r))
                    ],
                    "rollback": [
                        _cli("gcloud", "sql", "instances", "patch", _n(r), "--tier",
                             str(current), *_project(r))
                    ],
                    "notes": "Changing the tier restarts the instance (a few minutes of downtime).",
                    "console": _console(
                        r,
                        ["Open the instance, choose Edit.",
                         f"Under Machine configuration choose {suggested} and save."],
                    ),
                },
            )  # fmt: skip


def _spec(rid: str, rule_id: str, types: set[str], fn: Any) -> RuleSpec:
    return RuleSpec(
        id=rid,
        provider="gcp",
        rule_ids=(rule_id,),
        resource_types=frozenset(types),
        version=1,
        evaluate=fn,
    )


GCP_RULES: tuple[RuleSpec, ...] = check_unique(
    (
        _spec("GCP1", "GCP1_idle_vm", {"gce_instance"}, idle_instances),
        _spec("GCP2", "GCP2_unattached_disk", {"gce_disk"}, unattached_disks),
        _spec("GCP3", "GCP3_unused_address", {"gce_address"}, unused_addresses),
        _spec("GCP4", "GCP4_old_snapshot", {"gce_snapshot", "gce_disk"}, old_snapshots),
        _spec("GCP5", "GCP5_rightsize_vm", {"gce_instance"}, rightsize_instances),
        _spec("GCP6", "GCP6_idle_cloudsql", {"cloudsql_instance"}, idle_sql),
        _spec("GCP7", "GCP7_rightsize_cloudsql", {"cloudsql_instance"}, rightsize_sql),
    )
)
