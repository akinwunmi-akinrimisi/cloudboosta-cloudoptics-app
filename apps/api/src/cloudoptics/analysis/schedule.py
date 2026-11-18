"""R7 non-production scheduling (PRD FR-ANA-07, WBS AN-9).

EC2 instances and RDS DB instances that are tagged or named as non-production (the tenant's
pattern and tag keys, settings ``nonprodPattern`` / ``nonprodTagKeys``) and run around the
clock get a schedule: weekdays, office hours, in the tenant's time zone. "Around the clock"
comes from the hourly CloudWatch values the metrics puller stores: a stopped server reports
no CPU, so an hour without a value is an hour it was off.

The fix is one tag (``cloudoptics:schedule = 07:00-20:00;MO-FR;Africa/Lagos``) that the
scheduler stack in infra/cloudformation/scheduler/ acts on. The customer deploys that stack
in their own account; Cloudoptics stays read-only. Savings = hours off x hourly rate.

Idle servers and databases are left to R4 (stopping them for good saves more), and
resources another scheduler already handles (a ``Schedule`` tag) are skipped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from datetime import timedelta
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cloudoptics.analysis import compute
from cloudoptics.analysis.pricing import HOURS_PER_MONTH
from cloudoptics.analysis.rules import (
    PROD,
    Candidate,
    Confidence,
    Context,
    Resource,
    _cli,
    _region_args,
)

SCHEDULE_TAG = "cloudoptics:schedule"
# A resource with one of these tags is already scheduled (ours, or AWS Instance Scheduler).
SCHEDULED_TAGS = {SCHEDULE_TAG, "schedule"}
SCHEDULE_DAYS = "MO-FR"
WORK_DAYS = 5
HOURS_PER_WEEK = 168
MIN_DAYS = 7
HIGH_CONFIDENCE_DAYS = 14
# CloudWatch can miss the odd hour; a server off for a night is well under this.
MIN_RUNNING_SHARE = 0.95
STACK_NAME = "cloudoptics-scheduler"
TEMPLATE_PATH = "scheduler/cloudoptics-scheduler.yaml"

Source = Literal["tag", "name"]


@lru_cache(maxsize=32)
def _pattern(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def non_production(ctx: Context, r: Resource) -> tuple[Source, str] | None:
    """How the resource is known to be non-production (``tag`` or ``name``) and the
    matching text, or None (production, or nothing matches)."""
    settings = ctx.settings
    keys = {k.lower() for k in settings.nonprod_tag_keys}
    tagged = [(k, v) for k, v in r.tags.items() if k.lower() in keys]
    if any(v.strip().lower() in PROD for _, v in tagged):
        return None
    pattern = _pattern(settings.nonprod_pattern)
    for key, value in tagged:
        if pattern.search(value[:256]):
            return "tag", f"{key}={value}"
    for name in (r.name, r.resource_id):
        if name and pattern.search(name[:256]):
            return "name", name
    return None


def running_share(ctx: Context, r: Resource, days: int) -> float | None:
    """Share of the hours of the last ``days`` complete UTC days with a CPU value (it was
    running), or None when any of those days has not been pulled."""
    series = ctx.usage.get(r.id, {}).get("cpu", {})
    today = ctx.now.date()
    wanted = [today - timedelta(days=i) for i in range(1, days + 1)]
    if any(d not in series for d in wanted):
        return None
    hours = sum(1 for d in wanted for v in series[d][:24] if v is not None)
    return hours / (24 * days)


def time_zone(ctx: Context) -> str:
    try:
        ZoneInfo(ctx.time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        return "UTC"
    return ctx.time_zone


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def hours_on_per_week(start: str, end: str) -> float:
    return WORK_DAYS * (_minutes(end) - _minutes(start)) / 60


def tag_value(start: str, end: str, tz: str, *, rds: bool) -> str:
    """``07:00-20:00;MO-FR;Africa/Lagos``. RDS tag values may not contain ``;``, so RDS
    gets spaces instead; the scheduler reads both."""
    sep = " " if rds else ";"
    return sep.join((f"{start}-{end}", SCHEDULE_DAYS, tz))


def _already_scheduled(r: Resource) -> bool:
    return any(k.lower() in SCHEDULED_TAGS for k in r.tags)


def _quick_create_url(ctx: Context, region: str) -> str | None:
    if not ctx.scheduler_quick_create_template_url:
        return None
    params = {
        "templateURL": ctx.scheduler_quick_create_template_url,
        "stackName": STACK_NAME,
    }
    return (
        f"https://{region}.console.aws.amazon.com/cloudformation/home"
        f"?region={region}#/stacks/create/review?{urlencode(params)}"
    )


def _city(tz: str) -> str:
    return tz.rsplit("/", 1)[-1].replace("_", " ")


def _common(
    ctx: Context,
    r: Resource,
    *,
    rds: bool,
    hourly: float,
    fallback: bool,
    source: tuple[Source, str],
    share: float,
) -> dict[str, Any]:
    """What EC2 and RDS share: costs, confidence, risk, evidence, schedule and tag."""
    settings = ctx.settings
    tz = time_zone(ctx)
    start, end = settings.schedule_start, settings.schedule_end
    on_week = hours_on_per_week(start, end)
    off_month = (HOURS_PER_WEEK - on_week) * HOURS_PER_MONTH / HOURS_PER_WEEK
    long_share = running_share(ctx, r, HIGH_CONFIDENCE_DAYS)
    long_enough = long_share is not None and long_share >= MIN_RUNNING_SHARE
    by_tag = source[0] == "tag"
    confidence: Confidence = (
        "high" if by_tag and long_enough else "medium" if by_tag or long_enough else "low"
    )
    days = HIGH_CONFIDENCE_DAYS if long_enough else MIN_DAYS
    shown_share = long_share if long_enough and long_share is not None else share
    factors: list[dict[str, Any]] = [{"factor": "stops_out_of_hours", "points": 15}]
    if not by_tag:
        factors.append({"factor": "environment_from_name", "points": 20})
    if rds:
        factors.append({"factor": "database_stops_daily", "points": 10})
    value = tag_value(start, end, tz, rds=rds)
    notes = compute._price_notes(fallback)
    if rds:
        notes = ["storage_not_included", "rds_restarts_after_7_days", *notes]
    else:
        notes = ["schedule_saves_compute_only", *notes]
    return {
        "tz": tz,
        "value": value,
        "cost_now": hourly * HOURS_PER_MONTH,
        "cost_after": hourly * (HOURS_PER_MONTH - off_month),
        "confidence": confidence,
        "notes": notes,
        "factors": factors,
        "evidence": [
            {"kind": "nonProduction", "value": f"{source[0]} {source[1]}"},
            {"kind": "runningHoursPercent", "value": round(100 * shown_share, 1), "days": days},
            {"kind": "scheduledHoursPerWeek", "value": round(on_week, 2)},
            {"kind": "hoursOffPerMonth", "value": round(off_month, 1)},
            {"kind": "hourlyRateUsd", "value": round(hourly, 4)},
        ],
        "rule_config": {
            "nonprodPattern": settings.nonprod_pattern,
            "nonprodTagKeys": list(settings.nonprod_tag_keys),
            "start": start,
            "end": end,
            "days": SCHEDULE_DAYS,
            "timeZone": tz,
            "minRunningPercent": round(100 * MIN_RUNNING_SHARE),
            "minDays": MIN_DAYS,
            "highConfidenceDays": HIGH_CONFIDENCE_DAYS,
        },
        "recommended": {
            "tagKey": SCHEDULE_TAG,
            "schedule": value,
            "start": start,
            "end": end,
            "days": SCHEDULE_DAYS,
            "timeZone": tz,
        },
        "schedule": {
            "tagKey": SCHEDULE_TAG,
            "tagValue": value,
            "start": start,
            "end": end,
            "days": SCHEDULE_DAYS,
            "timeZone": tz,
            "hoursOnPerWeek": round(on_week, 2),
            "stackName": STACK_NAME,
            "templateUrl": ctx.scheduler_template_url,
            "quickCreateUrl": _quick_create_url(ctx, r.region_id),
            "rdsAutoStart": rds,
        },
        "window": f"{start}-{end} weekdays, {_city(tz)} time",
    }


def _eligible(ctx: Context, r: Resource) -> tuple[tuple[Source, str], float] | None:
    if _already_scheduled(r):
        return None
    source = non_production(ctx, r)
    if source is None:
        return None
    share = running_share(ctx, r, MIN_DAYS)
    if share is None or share < MIN_RUNNING_SHARE:
        return None
    return source, share


def _idle(ctx: Context, r: Resource, rule_id: str) -> bool:
    """R4 would flag it as idle: stopping it for good beats a schedule, so R7 stays out."""
    if rule_id in ctx.settings.disabled_rules:
        return False
    rule = compute.idle_instances if rule_id == "R4_idle_ec2" else compute.idle_databases
    return next(rule(ctx, [r]), None) is not None


def schedule_instances(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "ec2_instance" or r.state != "running":
            continue
        if compute.ASG_TAG in r.tags or r.config.get("lifecycle") == "spot":
            continue  # the group would replace it; spot cannot be stopped and started
        eligible = _eligible(ctx, r)
        if eligible is None or _idle(ctx, r, "R4_idle_ec2"):
            continue
        kind = str(r.config.get("instanceType") or "")
        split = compute.split_type(kind)
        price = split and compute.hourly_price(ctx.prices, r.region_id, "ec2_", *split)
        if not price:
            continue
        c = _common(
            ctx, r, rds=False, hourly=price[0], fallback=price[1], source=eligible[0],
            share=eligible[1],
        )  # fmt: skip
        notes = c["notes"]
        if "linux" not in str(r.config.get("platform") or "Linux/UNIX").lower():
            notes.append("priced_as_linux")
        region = _region_args(r)
        tags = json.dumps([{"Key": SCHEDULE_TAG, "Value": c["value"]}])
        name = r.name or r.resource_id
        yield Candidate(
            rule_id="R7_schedule_ec2",
            rule_version=1,
            type="schedule_instance",
            console_steps=[
                f"Once per account and region: deploy the {STACK_NAME} stack from the link "
                "above. It runs in your account; Cloudoptics stays read-only.",
                "Open the server in the AWS console.",
                "Choose the Tags tab, then Manage tags, then Add new tag.",
                f"Enter the key {SCHEDULE_TAG} and the value {c['value']}, then choose Save.",
            ],
            resource=r,
            title=f"Run non-production server {name} ({kind}) only in office hours: "
            f"{c['window']}",
            current_config={"instanceType": kind, "runs": "24/7"},
            recommended_config=c["recommended"],
            cost_now=c["cost_now"],
            cost_after=c["cost_after"],
            basis="list_price",
            effort="M",
            reversibility="reversible",
            confidence=c["confidence"],
            confidence_notes=notes,
            risk_factors=c["factors"],
            evidence=c["evidence"],
            rule_config=c["rule_config"],
            restart_required=True,
            fix={
                "steps": [
                    _cli("aws", "ec2", "create-tags", "--resources", r.resource_id, "--tags",
                         tags, *region),
                ],
                "rollback": [
                    _cli("aws", "ec2", "delete-tags", "--resources", r.resource_id, "--tags",
                         f"Key={SCHEDULE_TAG}", *region),
                    _cli("aws", "ec2", "start-instances", "--instance-ids", r.resource_id,
                         *region),
                ],
                "schedule": c["schedule"],
                "notes": "The scheduler stops the server at the end of the day and starts it "
                "in the morning; its disks stay, so nothing on them is lost. Anything only in "
                "memory is, and a public IP that is not an Elastic IP changes at each start.",
            },
        )  # fmt: skip


def schedule_databases(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    for r in resources:
        if r.resource_type != "rds_instance" or r.state != "available":
            continue
        if r.config.get("readReplicaOf") or r.config.get("clusterId"):
            continue  # replicas and Aurora members cannot be stopped on their own
        eligible = _eligible(ctx, r)
        if eligible is None or _idle(ctx, r, "R4_idle_rds"):
            continue
        price = compute._db_hourly(ctx, r)
        if price is None:
            continue
        c = _common(
            ctx, r, rds=True, hourly=price[0], fallback=price[1], source=eligible[0],
            share=eligible[1],
        )  # fmt: skip
        region = _region_args(r)
        arn = r.arn or f"arn:aws:rds:{r.region_id}:{r.account_id}:db:{r.resource_id}"
        tags = json.dumps([{"Key": SCHEDULE_TAG, "Value": c["value"]}])
        name = r.resource_id
        yield Candidate(
            rule_id="R7_schedule_rds",
            rule_version=1,
            type="schedule_database",
            console_steps=[
                f"Once per account and region: deploy the {STACK_NAME} stack from the link "
                "above. It runs in your account; Cloudoptics stays read-only.",
                "Open the database in the AWS console.",
                "Choose the Tags tab, then Add tags.",
                f"Enter the key {SCHEDULE_TAG} and the value {c['value']}, then choose Add.",
            ],
            resource=r,
            title=f"Run non-production database {name} ({r.config.get('instanceClass')}) only "
            f"in office hours: {c['window']}",
            current_config={
                "instanceClass": r.config.get("instanceClass"),
                "engine": r.config.get("engine"),
                "multiAz": bool(r.config.get("multiAz")),
                "runs": "24/7",
            },
            recommended_config=c["recommended"],
            cost_now=c["cost_now"],
            cost_after=c["cost_after"],
            basis="list_price",
            effort="M",
            reversibility="reversible",
            confidence=c["confidence"],
            confidence_notes=c["notes"],
            risk_factors=c["factors"],
            evidence=c["evidence"],
            rule_config=c["rule_config"],
            restart_required=True,
            fix={
                "steps": [
                    _cli("aws", "rds", "add-tags-to-resource", "--resource-name", arn,
                         "--tags", tags, *region),
                ],
                "rollback": [
                    _cli("aws", "rds", "remove-tags-from-resource", "--resource-name", arn,
                         "--tag-keys", SCHEDULE_TAG, *region),
                    _cli("aws", "rds", "start-db-instance", "--db-instance-identifier", name,
                         *region),
                ],
                "schedule": c["schedule"],
                # The 7-day automatic restart is shown from schedule.rdsAutoStart.
                "notes": "A stopped database keeps its data and settings but cannot be "
                "reached until it starts again; its storage and backups are still charged.",
            },
        )  # fmt: skip


SCHEDULE_RULES: list[Callable[[Context, list[Resource]], Iterator[Candidate]]] = [
    schedule_instances,
    schedule_databases,
]
