"""Azure starter rules (ADR-020), run by the shared engine over Azure resources only.

- AZ1: unattached managed disks. Fix: snapshot, then delete (data loss without the snapshot).
- AZ2: public IP addresses associated with nothing. Fix: delete (the address cannot be got
  back).
- AZ3: VMs stopped but not deallocated, still billed for compute. Fix: deallocate (a dynamic
  public IP may change on the next start).
- AZ4: snapshots 90+ days old whose source disk is gone. Fix: delete (irreversible).
- AZ5: underused VMs: Azure Advisor's cost advice, or CPU under the idle threshold. Fix:
  resize to Advisor's size, or deallocate (restart needed).

Costs come from the bill (``monthlyCostUsd``, the last 30 days of the resource's billed cost in
USD: basis ``effective_rate``) when the export has the resource; otherwise from small list
prices below (basis ``list_price``, confidence lowered).
"""

from __future__ import annotations

import shlex
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

from cloudoptics.analysis.pricing import Basis
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

PROVIDER = "azure"
# USD per GiB-month (pay-as-you-go, typical region) when the bill has no line for the disk.
DISK_GB_MONTH = {
    "standard_lrs": 0.045,
    "standardssd_lrs": 0.075,
    "standardssd_zrs": 0.094,
    "premium_lrs": 0.15,
    "premium_zrs": 0.19,
    "premiumv2_lrs": 0.12,
    "ultrassd_lrs": 0.12,
}
SNAPSHOT_GB_MONTH = 0.05
PUBLIC_IP_MONTH = 3.65
PORTAL = "https://portal.azure.com/#@/resource"


def _cli(*args: str) -> str:
    return " ".join(shlex.quote(a) for a in args)


def _arm(r: Resource) -> str:
    return str(r.config.get("armId") or r.resource_id)


def _days(ctx: Context, value: Any) -> float | None:
    when = parse_time(value)
    if when is None:
        return None
    if when.tzinfo is None:
        return None
    return (ctx.now - when) / timedelta(days=1)


def _cost(r: Resource, estimate: float) -> tuple[float, Basis, bool]:
    billed = r.config.get("monthlyCostUsd")
    if billed is not None and float(billed) > 0:
        return float(billed), "effective_rate", True
    return estimate, "list_price", False


def _portal_steps(r: Resource, *steps: str) -> list[str]:
    return [f"Open {PORTAL}{_arm(r)} in the Azure portal.", *steps]


def _retained(r: Resource) -> bool:
    keys = {k.lower() for k in r.tags}
    return any(t in keys for t in RETAIN_TAGS)


# --- AZ1: unattached managed disks ---------------------------------------------------------


def unattached_disks(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "azure_disk" or (r.state or "").lower() != "unattached":
            continue
        size = float(r.config.get("sizeGiB") or 0)
        sku = str(r.config.get("sku") or "Standard_LRS")
        cost, basis, billed = _cost(r, size * DISK_GB_MONTH.get(sku.lower(), 0.075))
        if cost <= 0:
            continue
        since = (ctx.now - r.state_since) / timedelta(days=1)
        confidence: Confidence = "high" if since >= 30 else ("medium" if since >= 7 else "low")
        factors = [*env_factor(r.tags), {"factor": "no_snapshot_known", "points": 20}]
        if _retained(r):
            factors.append({"factor": "retain_tag", "points": 30})
        group = str(r.config.get("resourceGroup") or "<resource-group>")
        backup = f"{(r.name or 'disk')[:60]}-cloudoptics-backup"
        yield Candidate(
            rule_id="AZ1_unattached_disk",
            rule_version=1,
            type="delete_unattached_volume",
            resource=r,
            title=f"Delete unattached disk {r.name or r.resource_id} ({size:g} GiB {sku})",
            current_config={"sku": sku, "sizeGiB": size, "diskState": r.state},
            recommended_config={"action": "snapshot_then_delete"},
            cost_now=cost,
            cost_after=0.0,
            basis=basis,
            effort="S",
            reversibility="with_snapshot",
            confidence=confidence if billed else "low",
            confidence_notes=[] if billed else ["cost_estimated"],
            risk_factors=factors,
            evidence=[
                {"kind": "state", "value": r.state, "sinceDays": round(since, 1)},
                {"kind": "created", "value": r.config.get("timeCreated")},
            ],
            rule_config={"mediumDays": 7, "highDays": 30},
            console_steps=_portal_steps(
                r, "Choose Create snapshot and wait for it to finish.", "Choose Delete."
            ),
            fix={
                "steps": [
                    _cli("az", "snapshot", "create", "--resource-group", group, "--name", backup,
                         "--source", _arm(r)),
                    _cli("az", "disk", "delete", "--ids", _arm(r), "--yes"),
                ],
                "rollback": [
                    _cli("az", "disk", "create", "--resource-group", group, "--name",
                         r.name or "restored-disk", "--source", backup, "--sku", sku),
                ],
                "notes": "Keep the snapshot until you are sure the data is not needed; it "
                "costs far less than the disk.",
            },
        )  # fmt: skip


# --- AZ2: unassociated public IP addresses -------------------------------------------------


def idle_public_ips(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "azure_public_ip" or r.state != "unassociated":
            continue
        cost, basis, billed = _cost(r, PUBLIC_IP_MONTH)
        since = (ctx.now - r.state_since) / timedelta(days=1)
        factors = [*env_factor(r.tags), {"factor": "address_not_recoverable", "points": 25}]
        if r.config.get("allocation") == "Static":
            factors.append({"factor": "static_address", "points": 10})
        group = str(r.config.get("resourceGroup") or "<resource-group>")
        yield Candidate(
            rule_id="AZ2_idle_public_ip",
            rule_version=1,
            type="release_idle_ip",
            resource=r,
            title=f"Delete unused public IP {r.name or r.resource_id}",
            current_config={"sku": r.config.get("sku"), "ipAddress": r.config.get("ipAddress")},
            recommended_config={"action": "delete"},
            cost_now=cost,
            cost_after=0.0,
            basis=basis,
            effort="S",
            reversibility="irreversible",
            confidence=("high" if since >= 7 else "medium") if billed else "low",
            confidence_notes=[] if billed else ["cost_estimated"],
            risk_factors=factors,
            evidence=[{"kind": "state", "value": r.state, "sinceDays": round(since, 1)}],
            console_steps=_portal_steps(r, "Check nothing uses the address, then choose Delete."),
            fix={
                "steps": [_cli("az", "network", "public-ip", "delete", "--ids", _arm(r))],
                "rollback": [
                    _cli("az", "network", "public-ip", "create", "--resource-group", group,
                         "--name", r.name or "restored-ip", "--sku",
                         str(r.config.get("sku") or "Standard"), "--allocation-method", "Static"),
                ],
                "notes": "A new address is different: update DNS records and allow-lists that "
                "name the old one.",
            },
        )  # fmt: skip


# --- AZ3: stopped but not deallocated VMs --------------------------------------------------


def stopped_not_deallocated(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "azure_vm" or r.state != "stopped":
            continue
        cost, basis, billed = _cost(r, 0.0)
        if cost <= 0:
            continue
        since = (ctx.now - r.state_since) / timedelta(days=1)
        yield Candidate(
            rule_id="AZ3_stopped_vm",
            rule_version=1,
            type="deallocate_stopped_vm",
            resource=r,
            title=f"Deallocate stopped VM {r.name or r.resource_id}: it is still billed",
            current_config={"powerState": "stopped", "vmSize": r.config.get("vmSize")},
            recommended_config={"powerState": "deallocated"},
            cost_now=cost,
            cost_after=0.0,
            basis=basis,
            effort="S",
            reversibility="reversible",
            confidence="high" if since >= 1 else "medium",
            confidence_notes=[] if billed else ["cost_estimated"],
            risk_factors=[*env_factor(r.tags), {"factor": "dynamic_ip_may_change", "points": 10}],
            evidence=[{"kind": "powerState", "value": "stopped", "sinceDays": round(since, 1)}],
            console_steps=_portal_steps(
                r, "Choose Stop: the portal deallocates the VM (Stopped (deallocated))."
            ),
            fix={
                "steps": [_cli("az", "vm", "deallocate", "--ids", _arm(r))],
                "rollback": [_cli("az", "vm", "start", "--ids", _arm(r))],
                "notes": "Stopping from inside the VM keeps it allocated and billed; deallocating "
                "stops compute charges. Disks are still billed.",
            },
        )


# --- AZ4: old snapshots of deleted disks ---------------------------------------------------


def old_snapshots(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    live = {r.resource_id for r in resources if r.resource_type == "azure_disk"}
    for r in resources:
        if r.resource_type != "azure_snapshot" or _retained(r):
            continue
        age = _days(ctx, r.config.get("timeCreated"))
        if age is None or age < 90:
            continue
        source = str(r.config.get("sourceResourceId") or "")
        if source and source in live:
            continue
        size = float(r.config.get("sizeGiB") or 0)
        cost, basis, billed = _cost(r, size * SNAPSHOT_GB_MONTH)
        if cost <= 0:
            continue
        yield Candidate(
            rule_id="AZ4_old_snapshot",
            rule_version=1,
            type="delete_old_snapshot",
            resource=r,
            title=f"Delete {age:.0f}-day-old snapshot {r.name or r.resource_id}",
            current_config={"sizeGiB": size, "ageDays": round(age)},
            recommended_config={"action": "delete"},
            cost_now=cost,
            cost_after=0.0,
            basis=basis,
            effort="S",
            reversibility="irreversible",
            confidence=("high" if age >= 180 else "medium") if billed else "low",
            confidence_notes=[] if billed else ["cost_estimated"],
            risk_factors=[*env_factor(r.tags), {"factor": "irreversible", "points": 30}],
            evidence=[
                {"kind": "created", "value": r.config.get("timeCreated")},
                {"kind": "sourceDisk", "value": source or None, "exists": False},
            ],
            rule_config={"minAgeDays": 90},
            console_steps=_portal_steps(r, "Check it is not a backup you must keep, then Delete."),
            fix={
                "steps": [_cli("az", "snapshot", "delete", "--ids", _arm(r))],
                "rollback": [],
                "notes": "Deleting a snapshot cannot be undone. Keep any snapshot a retention "
                "policy or audit needs (tag it retain to hide it here).",
            },
        )


# --- AZ5: underused VMs (Advisor or CPU) ---------------------------------------------------


def _cpu_max(ctx: Context, r: Resource) -> tuple[float | None, int]:
    days = ctx.usage.get(r.id, {}).get("cpu_max", {})
    values = [v for hours in days.values() for v in hours if v is not None]
    return (max(values) if values else None), len(days)


def underused_vms(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    threshold = ctx.settings.idle_cpu_percent
    for r in resources:
        if r.resource_type != "azure_vm" or r.state != "running":
            continue
        advice = next(
            (a for a in r.config.get("advisor") or [] if a.get("savingsMonthly") is not None),
            None,
        )
        peak, days = _cpu_max(ctx, r)
        idle = peak is not None and days >= ctx.settings.idle_days and peak < threshold
        if advice is None and not idle:
            continue
        cost, basis, billed = _cost(r, 0.0)
        size = str(r.config.get("vmSize") or "")
        target = str(advice.get("targetSku") or "") if advice else ""
        if advice and target and target.lower() not in ("shutdown", size.lower()):
            saving = float(advice["savingsMonthly"])
            now_cost = cost if billed else saving
            after = max(0.0, now_cost - saving)
            steps = [_cli("az", "vm", "resize", "--ids", _arm(r), "--size", target)]
            rollback = [_cli("az", "vm", "resize", "--ids", _arm(r), "--size", size)]
            title = f"Resize underused VM {r.name or r.resource_id} from {size} to {target}"
            recommended: dict[str, Any] = {"vmSize": target}
            kind = "rightsize_instance"
        else:
            now_cost = cost if billed else (float(advice["savingsMonthly"]) if advice else 0.0)
            after = 0.0
            steps = [_cli("az", "vm", "deallocate", "--ids", _arm(r))]
            rollback = [_cli("az", "vm", "start", "--ids", _arm(r))]
            title = f"Deallocate idle VM {r.name or r.resource_id}"
            recommended = {"powerState": "deallocated"}
            kind = "stop_idle_instance"
        if now_cost - after <= 0:
            continue
        evidence: list[dict[str, Any]] = [
            {"kind": "cpuMax", "value": peak, "days": days, "threshold": threshold}
        ]
        if advice:
            evidence.append(
                {"kind": "azureAdvisor", "problem": advice.get("problem"),
                 "solution": advice.get("solution"), "savingsMonthly": advice.get("savingsMonthly"),
                 "currency": advice.get("currency")}
            )  # fmt: skip
        yield Candidate(
            rule_id="AZ5_underused_vm",
            rule_version=1,
            type=kind,
            resource=r,
            title=title,
            current_config={"vmSize": size, "powerState": "running"},
            recommended_config=recommended,
            cost_now=now_cost,
            cost_after=after,
            basis=basis,
            effort="M",
            reversibility="reversible",
            restart_required=True,
            confidence="high" if (advice and idle) else "medium",
            confidence_notes=([] if billed else ["cost_estimated"])
            + (["azure_advisor"] if advice else []),
            risk_factors=[*env_factor(r.tags), {"factor": "restart_required", "points": 15}],
            evidence=evidence,
            rule_config={"idleCpuPercent": threshold, "idleDays": ctx.settings.idle_days},
            console_steps=_portal_steps(
                r, "Open Size and choose the smaller size, or Stop to deallocate."
            ),
            fix={
                "steps": steps,
                "rollback": rollback,
                "notes": "The VM restarts. Check peak load (month end, batch jobs) first.",
            },
        )


def _spec(rule_id: str, fn: Any, types: set[str]) -> RuleSpec:
    return RuleSpec(
        id=rule_id.split("_", 1)[0],
        provider=PROVIDER,
        rule_ids=(rule_id,),
        resource_types=frozenset(types),
        version=1,
        evaluate=fn,
    )


AZURE_RULES: tuple[RuleSpec, ...] = check_unique(
    (
        _spec("AZ1_unattached_disk", unattached_disks, {"azure_disk"}),
        _spec("AZ2_idle_public_ip", idle_public_ips, {"azure_public_ip"}),
        _spec("AZ3_stopped_vm", stopped_not_deallocated, {"azure_vm"}),
        _spec("AZ4_old_snapshot", old_snapshots, {"azure_snapshot", "azure_disk"}),
        _spec("AZ5_underused_vm", underused_vms, {"azure_vm"}),
    )
)
RULE_IDS = [rule for spec in AZURE_RULES for rule in spec.rule_ids]
