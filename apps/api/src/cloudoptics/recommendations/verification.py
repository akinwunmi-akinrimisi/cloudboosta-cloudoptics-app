"""Verification and realised savings (WBS AN-16, PRD US-5.1/5.2, FR-REC-07).

After each analysis run (so after each inventory scan) and once a day, every recommendation
that is open, marked done or not verified is checked against the resource as the latest
scan saw it:

- A per-rule check (``CHECKS``) says whether the recommended change was made: the volume or
  snapshot is gone (R1), the address released or the load balancer deleted (R2), the volume
  is gp3 (R3), the server or database stopped or removed (R4), the server has the
  recommended type (R5/R6). Rules without a check of their own (``default_check``) count
  the resource gone, or the rule no longer finding it, as done.
- Marked done and confirmed (or the resource is gone): ``verified``. Open and the change
  is seen (the fix was made without being marked done): ``verified`` too, and credited.
  Marked done but still unchanged on a scan 72 hours later: ``not_verified`` (a later scan
  can still verify it).
- Open and the resource gone, or no longer found by the rule without the change being
  seen: ``resolved_externally`` (FR-REC-07). It counts as realised savings only if the
  resource's own bills show the cost drop.

Realised savings (``measure``): once the bills cover 7 days after the change, the
resource's average daily cost in the 30 days before the change day against the days after
it (up to 30), normalised to 30 days. The resource's cost comes from the CUR's resource IDs
(``cost_resource_daily``); when the bills do not show the resource, a verified
recommendation is credited its estimate, labelled ``estimate``. Until the 7 days are in,
the app shows the estimate as "estimated realised savings".

Each change is a timeline event without an actor (Cloudoptics) and an audit entry by the
system.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.analysis.settings import RuleSettings, load_rule_settings
from cloudoptics.audit import log as audit
from cloudoptics.platform.ids import uuid7

OPEN = ("new", "assigned", "in_progress")
CHECKED = ("done", "not_verified")
VERIFY_WITHIN = timedelta(hours=72)
MIN_AFTER_DAYS = 7
WINDOW_DAYS = 30
# Measurements stop once the window after the change is full, or this long after it.
MEASURE_FOR = timedelta(days=90)

Outcome = Literal["implemented", "gone", "not_implemented"]
SYSTEM = audit.Actor(type="system")


@dataclass(frozen=True)
class Observed:
    """The resource as the latest scan saw it."""

    resource_type: str
    state: str | None
    config: dict[str, Any]
    tags: dict[str, str]
    arn: str | None
    state_since: datetime
    last_seen_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True)
class Item:
    id: uuid.UUID
    rule_id: str
    status: str
    account_id: str
    resource_id: str
    current_config: dict[str, Any]
    recommended_config: dict[str, Any]
    cost_now_usd_month: float
    estimate_usd_month: float
    fix: dict[str, Any]
    done_at: datetime | None
    # The rule no longer finds it (only known straight after an analysis run).
    stale: bool
    # The rule is off or the resource is tagged to be ignored: that it is no longer found
    # says nothing about a fix.
    suppressed: bool
    resource: Observed | None
    baseline: dict[str, Any] = field(default_factory=dict)
    # Found from the bill alone (R6b, R10) or not tied to one resource (R8): there is no
    # inventory resource to watch, so only the rule no longer finding it shows the fix.
    bill_only: bool = False

    @property
    def gone(self) -> bool:
        if self.bill_only:
            return False
        return self.resource is None or self.resource.deleted_at is not None

    @property
    def gone_at(self) -> datetime | None:
        return self.resource.deleted_at if self.resource is not None else None


@dataclass(frozen=True)
class Check:
    outcome: Outcome
    # What was seen, for the timeline: volume_deleted, instance_type_changed, ...
    code: str
    # When the change was seen, if the scans tell.
    at: datetime | None = None
    # A smaller saving than the recommendation's (for example archiving, not deleting).
    estimate_usd_month: float | None = None


CheckFn = Callable[[Item], Check]
CHECKS: dict[str, CheckFn] = {}

UNCHANGED = Check("not_implemented", "unchanged")


def check(*rule_ids: str) -> Callable[[CheckFn], CheckFn]:
    """Register the check for these rules."""

    def register(fn: CheckFn) -> CheckFn:
        for rule_id in rule_ids:
            CHECKS[rule_id] = fn
        return fn

    return register


def default_check(item: Item) -> Check:
    """Rules without a check of their own: gone, or no longer found by the rule."""
    if item.gone:
        return Check("implemented", "resource_gone", item.gone_at)
    if item.stale and not item.suppressed and item.resource is not None:
        return Check("implemented", "rule_no_longer_applies", item.resource.last_seen_at)
    if item.stale and not item.suppressed and item.bill_only:
        # The bills no longer show the charge (the rules treat it as already fixed).
        return Check("implemented", "rule_no_longer_applies")
    return UNCHANGED


def check_for(rule_id: str) -> CheckFn:
    return CHECKS.get(rule_id, default_check)


def _deleted(code: str) -> CheckFn:
    def run(item: Item) -> Check:
        return Check("implemented", code, item.gone_at) if item.gone else UNCHANGED

    return run


check("R1_unattached_ebs")(_deleted("volume_deleted"))
check("R2_idle_eip")(_deleted("address_released"))
check("R2_idle_lb")(_deleted("load_balancer_deleted"))


@check("R1_old_snapshot")
def _snapshot(item: Item) -> Check:
    if item.gone:
        return Check("implemented", "snapshot_deleted", item.gone_at)
    assert item.resource is not None
    if item.resource.config.get("storageTier") == "archive":
        archived = (item.fix.get("alternative") or {}).get("monthlyCostUsd")
        estimate = None
        if isinstance(archived, int | float):
            estimate = max(0.0, round(item.cost_now_usd_month - float(archived), 6))
        return Check("implemented", "snapshot_archived", item.resource.last_seen_at, estimate)
    return UNCHANGED


@check("R3_gp2_gp3")
def _gp3(item: Item) -> Check:
    if item.gone:
        return Check("gone", "resource_gone", item.gone_at)
    assert item.resource is not None
    if item.resource.config.get("volumeType") == "gp3":
        return Check("implemented", "volume_type_gp3", item.resource.last_seen_at)
    return UNCHANGED


@check("R4_idle_ec2")
def _instance_stopped(item: Item) -> Check:
    r = item.resource
    if item.gone or r is None:
        return Check("implemented", "instance_terminated", item.gone_at)
    if r.state in ("terminated", "shutting-down"):
        return Check("implemented", "instance_terminated", r.state_since)
    if r.state in ("stopped", "stopping"):
        return Check("implemented", "instance_stopped", r.state_since)
    return UNCHANGED


@check("R4_idle_rds")
def _database_stopped(item: Item) -> Check:
    r = item.resource
    if item.gone or r is None or r.state == "deleting":
        return Check("implemented", "database_deleted", item.gone_at)
    if r.state in ("stopped", "stopping"):
        return Check("implemented", "database_stopped", r.state_since)
    return UNCHANGED


@check("R5_rightsize_ec2", "R6_newer_generation", "R6_graviton")
def _instance_type(item: Item) -> Check:
    r = item.resource
    if item.gone or r is None or r.state in ("terminated", "shutting-down"):
        return Check("gone", "resource_gone", item.gone_at)
    target = item.recommended_config.get("instanceType")
    if target and r.config.get("instanceType") == target:
        return Check("implemented", "instance_type_changed", r.last_seen_at)
    return UNCHANGED


# What Cloudoptics says on the timeline, by check code.
SAID = {
    "volume_deleted": "The volume is deleted.",
    "snapshot_deleted": "The snapshot is deleted.",
    "snapshot_archived": "The snapshot is in the archive tier.",
    "address_released": "The address is released.",
    "load_balancer_deleted": "The load balancer is deleted.",
    "volume_type_gp3": "The volume is gp3 now.",
    "instance_stopped": "The server is stopped.",
    "instance_terminated": "The server is terminated.",
    "database_stopped": "The database is stopped.",
    "database_deleted": "The database is deleted.",
    "instance_type_changed": "The server has the recommended type now.",
    "resource_gone": "The resource no longer exists.",
    "rule_no_longer_applies": "The rule no longer finds anything to change.",
    "unchanged": "The latest scan shows no change yet.",
}


def decide(item: Item, result: Check, now: datetime) -> str | None:
    """The status the check moves the item to, if any."""
    if item.status in OPEN:
        # Removed without being marked done: resolved outside Cloudoptics (FR-REC-07).
        if item.gone:
            return "resolved_externally"
        if result.outcome == "implemented":
            return "verified"
        return "resolved_externally" if item.stale else None
    if item.status in CHECKED:
        if result.outcome in ("implemented", "gone"):
            return "verified"
        scanned = item.bill_only or (
            item.resource is not None
            and (item.done_at is None or item.resource.last_seen_at > item.done_at)
        )
        if (
            item.status == "done"
            and item.done_at is not None
            and now - item.done_at >= VERIFY_WITHIN
            and scanned
        ):
            return "not_verified"
    return None


def _note(item: Item, result: Check, to_status: str) -> str:
    said = SAID.get(result.code, "")
    if to_status == "verified":
        if item.status in OPEN:
            return f"Cloudoptics found the change was made without being marked done. {said}"
        return f"Checked in AWS. {said}"
    if to_status == "not_verified":
        return (
            "Marked done more than 72 hours ago, but the latest scan still shows no change. "
            "It is checked again after each scan."
        )
    if item.gone:
        return "The resource was removed outside Cloudoptics."
    return "This no longer applies: the resource changed outside Cloudoptics."


def _changed_at(item: Item, result: Check, now: datetime) -> datetime:
    seen = [t for t in (result.at, item.done_at) if t is not None]
    if not seen and item.resource is not None:
        seen.append(item.resource.last_seen_at)
    return min(seen) if seen else now


LOAD = text(
    "SELECT r.id, r.rule_id, r.status, r.account_id, r.resource_id, r.current_config, "
    "r.recommended_config, r.cost_now_usd_month, r.savings_usd_month, r.fix, r.baseline, "
    "r.done_at, r.generation_id, r.resource_ref, s.id AS sid, s.resource_type, s.state, "
    "s.config, s.tags, s.arn, s.state_since, s.last_seen_at, s.deleted_at FROM recommendation r "
    "LEFT JOIN resource s ON s.id = r.resource_ref WHERE r.status = ANY(:statuses) "
    "ORDER BY r.id"
)


def _item(row: Any, generation: int | None, settings: RuleSettings) -> Item:
    baseline = row.baseline or {}
    resource = None
    if row.sid is not None:
        resource = Observed(
            resource_type=row.resource_type,
            state=row.state,
            config=row.config or {},
            tags=row.tags or {},
            arn=row.arn,
            state_since=row.state_since,
            last_seen_at=row.last_seen_at,
            deleted_at=row.deleted_at,
        )
    suppressed = row.rule_id in settings.disabled_rules or (
        resource is not None and settings.ignores(resource.tags)
    )
    return Item(
        id=row.id,
        rule_id=row.rule_id,
        status=row.status,
        account_id=row.account_id.strip(),
        resource_id=row.resource_id,
        current_config=baseline.get("currentConfig", row.current_config or {}),
        recommended_config=baseline.get("recommendedConfig", row.recommended_config or {}),
        cost_now_usd_month=float(baseline.get("costNowUsdMonth", row.cost_now_usd_month)),
        estimate_usd_month=float(baseline.get("savingsUsdMonth", row.savings_usd_month)),
        fix=row.fix or {},
        done_at=row.done_at,
        stale=generation is not None and row.generation_id < generation,
        suppressed=suppressed,
        resource=resource,
        baseline=baseline,
        bill_only=row.resource_ref is None,
    )


def baseline_of(row: Any) -> dict[str, Any]:
    """What a change is checked and credited against, from the recommendation's columns."""
    return {
        "currentConfig": row.current_config or {},
        "recommendedConfig": row.recommended_config or {},
        "costNowUsdMonth": float(row.cost_now_usd_month),
        "savingsUsdMonth": float(row.savings_usd_month),
    }


async def _event(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    recommendation_id: uuid.UUID,
    kind: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    body: str | None = None,
) -> None:
    await session.execute(
        text(
            "INSERT INTO recommendation_event (id, tenant_id, recommendation_id, actor_id, kind, "
            "from_status, to_status, body) VALUES (:id, :t, :r, NULL, :kind, :f, :to, :body)"
        ),
        {
            "id": uuid7(),
            "t": tenant_id,
            "r": recommendation_id,
            "kind": kind,
            "f": from_status,
            "to": to_status,
            "body": body,
        },
    )


async def _move(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    item: Item,
    result: Check,
    to_status: str,
    now: datetime,
) -> None:
    estimate = (
        result.estimate_usd_month
        if result.estimate_usd_month is not None
        else item.estimate_usd_month
    )
    verification = {
        "check": result.code,
        "outcome": result.outcome,
        "checkedAt": now.isoformat(),
        "estimateUsdMonth": round(estimate, 6),
    }
    changed = None if to_status == "not_verified" else _changed_at(item, result, now)
    baseline = item.baseline or {
        "currentConfig": item.current_config,
        "recommendedConfig": item.recommended_config,
        "costNowUsdMonth": item.cost_now_usd_month,
        "savingsUsdMonth": item.estimate_usd_month,
    }
    await session.execute(
        text(
            "UPDATE recommendation SET status = :s, verification = CAST(:v AS jsonb), "
            "changed_at = :changed, verified_at = :verified, baseline = CAST(:b AS jsonb), "
            "realised_savings_usd_month = NULL, realised_basis = NULL, realised_at = NULL "
            "WHERE id = :id"
        ),
        {
            "s": to_status,
            "v": json.dumps(verification),
            "changed": changed,
            "verified": now if to_status == "verified" else None,
            "b": json.dumps(baseline),
            "id": item.id,
        },
    )
    note = _note(item, result, to_status)
    await _event(
        session,
        tenant_id,
        item.id,
        "status_changed",
        from_status=item.status,
        to_status=to_status,
        body=note,
    )
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor=SYSTEM,
        action="recommendation.status_changed",
        target_type="recommendation",
        target_id=str(item.id),
        before={"status": item.status},
        after={"status": to_status, "check": result.code},
    )


async def _record_check(session: AsyncSession, item: Item, result: Check, now: datetime) -> None:
    """Marked done, not confirmed yet: keep the last check for the detail page."""
    await session.execute(
        text(
            "UPDATE recommendation SET verification = CAST(:v AS jsonb), "
            "baseline = coalesce(baseline, CAST(:b AS jsonb)) WHERE id = :id"
        ),
        {
            "v": json.dumps(
                {"check": result.code, "outcome": result.outcome, "checkedAt": now.isoformat()}
            ),
            "b": json.dumps(
                {
                    "currentConfig": item.current_config,
                    "recommendedConfig": item.recommended_config,
                    "costNowUsdMonth": item.cost_now_usd_month,
                    "savingsUsdMonth": item.estimate_usd_month,
                }
            ),
            "id": item.id,
        },
    )


@dataclass(frozen=True)
class Measurement:
    before_days: int
    after_days: int
    before_usd_day: float | None
    after_usd_day: float | None

    @property
    def realised_usd_month(self) -> float | None:
        if self.before_usd_day is None or self.after_usd_day is None:
            return None
        return round((self.before_usd_day - self.after_usd_day) * WINDOW_DAYS, 6)


def compare(daily: dict[date, float], change_day: date, last_bill_day: date) -> Measurement:
    """The average daily cost before ``change_day`` (up to 30 days, from the first day the
    resource has a cost) against the days after it, up to 30 or the last day billed. The
    change day itself is left out of both; days without a cost after it count as zero."""
    after_end = min(last_bill_day, change_day + timedelta(days=WINDOW_DAYS))
    after_days = max(0, (after_end - change_day).days)
    start = change_day - timedelta(days=WINDOW_DAYS)
    before = {d: c for d, c in daily.items() if start <= d < change_day}
    if not before or after_days == 0:
        return Measurement(len(before), after_days, None, None)
    before_days = (change_day - min(before)).days
    after = sum(c for d, c in daily.items() if change_day < d <= after_end)
    return Measurement(
        before_days, after_days, sum(before.values()) / before_days, after / after_days
    )


def _like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def resource_keys(resource_id: str, arn: str | None, config: dict[str, Any]) -> list[str]:
    """How the CUR may name the resource: its ID, its ARN, or (addresses) the public IP."""
    keys = [resource_id]
    if arn:
        keys.append(arn)
    ip = config.get("publicIp")
    if isinstance(ip, str) and ip:
        keys.append(ip)
    return keys


MEASURE_DUE = text(
    "SELECT r.id, r.status, r.account_id, r.resource_id, r.changed_at, r.savings_usd_month, "
    "r.verification, r.baseline, r.realised_at, s.arn FROM recommendation r "
    "LEFT JOIN resource s ON s.id = r.resource_ref "
    "WHERE r.status IN ('verified', 'resolved_externally') AND r.changed_at IS NOT NULL "
    "AND r.changed_at > :oldest AND coalesce((r.verification->'measurement'->>'afterDays')::int, "
    "0) < :window ORDER BY r.id"
)


async def measure(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> int:
    """Realised savings for verified and externally resolved recommendations whose bills
    cover at least 7 days after the change. Returns how many were measured."""
    rows = (
        await session.execute(MEASURE_DUE, {"oldest": now - MEASURE_FOR, "window": WINDOW_DAYS})
    ).all()
    measured = 0
    last_bill: dict[str, date | None] = {}
    for r in rows:
        account = r.account_id.strip()
        if account not in last_bill:
            last_bill[account] = (
                await session.execute(
                    text("SELECT max(usage_date) FROM cost_daily WHERE account_id = :a"),
                    {"a": account},
                )
            ).scalar_one()
        last = last_bill[account]
        change_day = r.changed_at.astimezone(UTC).date()
        if last is None or (last - change_day).days < MIN_AFTER_DAYS:
            continue
        baseline = r.baseline or {}
        keys = resource_keys(r.resource_id, r.arn, baseline.get("currentConfig") or {})
        suffixes = [f"%/{_like(r.resource_id)}", f"%:{_like(r.resource_id)}"]
        daily = {
            d: float(c)
            for d, c in await session.execute(
                text(
                    "SELECT usage_date, sum(effective_cost_usd) FROM cost_resource_daily "
                    "WHERE account_id = :a AND usage_date >= :start AND usage_date <= :end "
                    "AND (resource_id = ANY(:keys) OR resource_id LIKE ANY(:suffixes)) "
                    "GROUP BY usage_date"
                ),
                {
                    "a": account,
                    "start": change_day - timedelta(days=WINDOW_DAYS),
                    "end": change_day + timedelta(days=WINDOW_DAYS),
                    "keys": keys,
                    "suffixes": suffixes,
                },
            )
        }
        m = compare(daily, change_day, last)
        verification = r.verification or {}
        realised = m.realised_usd_month
        basis: str | None = "cur"
        if realised is None:
            if r.status == "verified":
                estimate = verification.get("estimateUsdMonth", baseline.get("savingsUsdMonth"))
                realised = float(estimate if estimate is not None else r.savings_usd_month)
                basis = "estimate"
            else:
                basis = None  # resolved outside Cloudoptics: credited only from the bills
        verification["measurement"] = {
            "beforeDays": m.before_days,
            "afterDays": m.after_days,
            "beforeUsdDay": None if m.before_usd_day is None else round(m.before_usd_day, 6),
            "afterUsdDay": None if m.after_usd_day is None else round(m.after_usd_day, 6),
            "measuredAt": now.isoformat(),
        }
        await session.execute(
            text(
                "UPDATE recommendation SET realised_savings_usd_month = :v, realised_basis = :b, "
                "realised_at = CASE WHEN CAST(:v AS numeric) IS NULL THEN NULL ELSE :now END, "
                "verification = CAST(:ver AS jsonb) WHERE id = :id"
            ),
            {
                "v": realised,
                "b": basis,
                "now": now,
                "ver": json.dumps(verification),
                "id": r.id,
            },
        )
        if realised is not None and r.realised_at is None:
            measured += 1
            await _event(session, tenant_id, r.id, "savings_measured")
            await audit.record(
                session,
                tenant_id=tenant_id,
                actor=SYSTEM,
                action="recommendation.savings_measured",
                target_type="recommendation",
                target_id=str(r.id),
                after={"realisedUsdMonth": round(realised, 2), "basis": basis},
            )
    return measured


async def verify(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    now: datetime | None = None,
    *,
    generation: int | None = None,
    settings: RuleSettings | None = None,
) -> dict[str, int]:
    """Check every open, done and not verified recommendation, then measure realised
    savings. ``generation`` is the analysis run just stored, when called after one: rows
    from older runs are no longer found by their rule. Inside a tenant-scoped
    transaction."""
    now = now or datetime.now(UTC)
    settings = settings or await load_rule_settings(session)
    rows = (await session.execute(LOAD, {"statuses": [*OPEN, *CHECKED]})).all()
    counts = {"verified": 0, "not_verified": 0, "resolved": 0}
    for row in rows:
        item = _item(row, generation, settings)
        result = check_for(item.rule_id)(item)
        to_status = decide(item, result, now)
        if to_status is None:
            if item.status in CHECKED:
                await _record_check(session, item, result, now)
            continue
        await _move(session, tenant_id, item, result, to_status, now)
        counts["resolved" if to_status == "resolved_externally" else to_status] += 1
    counts["measured"] = await measure(session, tenant_id, now)
    return counts
