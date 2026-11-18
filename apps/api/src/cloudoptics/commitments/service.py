"""Commitments: the daily refresh, the Savings Plan advisory, R8 and expiry alerts.

- ``refresh``: list the connection's Savings Plans and EC2/RDS reservations and ask Cost
  Explorer for its recommendations, coverage and utilisation (``aws``); stored in
  ``commitment`` and ``commitment_snapshot``. Cost Explorer is asked at most once every
  20 hours per connection (eight calls, USD 0.08, recorded in ``sync_run``).
- ``advice``: per connection, our simulation next to AWS's options (``advisor``).
- ``r8_candidates``: the advisory as an R8 recommendation, for the engine.
- ``alert_expiries``: an email when a commitment has 60, 30 or 15 days left (one per
  commitment and threshold), to admins and members who have not turned it off.
"""

from __future__ import annotations

import html
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from cloudoptics.analysis.rules import Candidate, Resource
from cloudoptics.commitments import advisor, aws
from cloudoptics.connectors.aws import resource_role_arn, session_name
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.service import get_connection, get_session_factory
from cloudoptics.connectors.validation import CE_CALL_COST_USD
from cloudoptics.fx import service as fx
from cloudoptics.notifications import preferences
from cloudoptics.notifications.email import Email, EmailError, get_mailer
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

log = logging.getLogger(__name__)

USABLE = ("healthy", "degraded")
MIN_EXPLORER_INTERVAL = timedelta(hours=20)
# A commitment the last refreshes did not list any more (sold, returned, or the account
# left) is no longer alerted on.
STALE_AFTER = timedelta(days=3)
EXPIRY_THRESHOLDS = (60, 30, 15)
OPEN = ["new", "assigned", "in_progress"]
# Open recommendations whose savings come out of the Savings-Plan-eligible baseline:
# idle (R4), rightsizing (R5), newer generation / Graviton (R6) and schedules (R7).
NETTED_RULES = r"^R[4-7](_|$)"
R8_RULE = "R8_savings_plan"
PURCHASE_URL = "https://console.aws.amazon.com/costmanagement/home#/savings-plans/purchase"

RefreshStatus = Literal["skipped", "succeeded", "partial", "failed"]

# --- Refresh ------------------------------------------------------------------------------


@dataclass
class Fetched:
    commitments: list[aws.Commitment] = field(default_factory=list)
    explorer: aws.ExplorerData | None = None
    errors: list[str] = field(default_factory=list)


def fetch(
    factory: Any,
    conn: Any,
    external_id: str,
    tenant_id: uuid.UUID,
    accounts: list[str],
    today: date,
    *,
    with_explorer: bool,
) -> Fetched:
    """The AWS side of a refresh (runs in a worker thread)."""
    out = Fetched()
    billing = factory.assume(conn.role_arn, external_id, session_name(tenant_id, "commitments"))
    try:
        out.commitments.extend(
            aws.savings_plans(billing.client("savingsplans"), conn.aws_account_id.strip())
        )
    except (ClientError, BotoCoreError) as exc:
        out.errors.append(f"savings plans: {aws.code(exc)}")
    if with_explorer:
        known = {c.commitment_id for c in out.commitments}
        out.explorer = aws.explorer(billing.client("ce"), today, known)
        out.commitments.extend(out.explorer.other_plans)
        out.errors.extend(out.explorer.errors)

    for account in accounts:
        try:
            session = factory.assume(
                resource_role_arn(account, conn.stack_name),
                external_id,
                session_name(tenant_id, "commitments"),
            )
            regions = aws.regions(session)
        except (ClientError, BotoCoreError) as exc:
            if aws.code(exc) not in ("AccessDenied", "AccessDeniedException"):
                out.errors.append(f"{account}: {aws.code(exc)}")
            continue
        for region in regions:
            try:
                out.commitments.extend(
                    aws.ec2_reservations(session.client("ec2", region), account, region)
                )
                out.commitments.extend(
                    aws.rds_reservations(session.client("rds", region), account, region)
                )
            except (ClientError, BotoCoreError, KeyError) as exc:
                out.errors.append(f"{account} {region}: {aws.code(exc)}")
    return out


UPSERT_COMMITMENT = text(
    "INSERT INTO commitment (id, tenant_id, connection_id, kind, commitment_id, account_id, "
    "region_id, description, plan_type, term_months, payment_option, state, quantity, "
    "hourly_commitment_usd, upfront_usd, starts_at, ends_at, utilisation_pct, details) "
    "VALUES (:id, :t, :c, :kind, :cid, :account, :region, :description, :plan_type, :term, "
    ":payment, :state, :quantity, :hourly, :upfront, :starts, :ends, :util, "
    "CAST(:details AS jsonb)) ON CONFLICT (tenant_id, connection_id, kind, commitment_id) "
    "DO UPDATE SET account_id = EXCLUDED.account_id, region_id = EXCLUDED.region_id, "
    "description = EXCLUDED.description, plan_type = EXCLUDED.plan_type, "
    "term_months = EXCLUDED.term_months, payment_option = EXCLUDED.payment_option, "
    "state = EXCLUDED.state, quantity = EXCLUDED.quantity, "
    "hourly_commitment_usd = EXCLUDED.hourly_commitment_usd, "
    "upfront_usd = EXCLUDED.upfront_usd, starts_at = EXCLUDED.starts_at, "
    "ends_at = EXCLUDED.ends_at, "
    "utilisation_pct = coalesce(EXCLUDED.utilisation_pct, commitment.utilisation_pct), "
    "details = EXCLUDED.details, last_seen_at = now()"
)


def _utilisation(c: aws.Commitment, by_id: dict[str, float]) -> float | None:
    if c.utilisation_pct is not None:
        return c.utilisation_pct
    for key in (c.commitment_id, c.details.get("leaseId"), c.details.get("savingsPlanId")):
        if key and str(key) in by_id:
            return by_id[str(key)]
    return None


async def _accounts(session: AsyncSession, connection_id: uuid.UUID) -> list[str]:
    return [
        r.aws_account_id.strip()
        for r in await session.execute(
            text(
                "SELECT aws_account_id FROM cloud_account WHERE connection_id = :c AND "
                "coalesce(org_status, 'ACTIVE') = 'ACTIVE' ORDER BY aws_account_id"
            ),
            {"c": connection_id},
        )
    ]


async def refresh(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    today: date | None = None,
    *,
    force: bool = False,
) -> RefreshStatus:
    """List one connection's commitments and refresh Cost Explorer's view of them."""
    today = today or datetime.now(UTC).date()
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        if conn.status not in USABLE:
            return "skipped"
        accounts = await _accounts(session, connection_id)
        last = (
            await session.execute(
                text("SELECT fetched_at FROM commitment_snapshot WHERE connection_id = :c"),
                {"c": connection_id},
            )
        ).scalar_one_or_none()
    with_explorer = force or last is None or datetime.now(UTC) - last >= MIN_EXPLORER_INTERVAL
    external_id = get_encryptor().decrypt(bytes(conn.external_id_enc), tenant_id)
    started = datetime.now(UTC)
    factory = get_session_factory()
    got: Fetched | None = None
    error: str | None = None
    try:
        got = await run_in_threadpool(
            fetch,
            factory,
            conn,
            external_id,
            tenant_id,
            accounts,
            today,
            with_explorer=with_explorer,
        )
    except (ClientError, BotoCoreError) as exc:
        error = aws.code(exc)
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        if got is None:
            await _record_run(session, tenant_id, connection_id, started, "failed", 0, error, 0)
            return "failed"
        by_id = got.explorer.by_commitment if got.explorer else {}
        seen: set[tuple[str, str]] = set()
        for c in got.commitments:
            if (c.kind, c.commitment_id) in seen:
                continue
            seen.add((c.kind, c.commitment_id))
            await session.execute(
                UPSERT_COMMITMENT,
                {
                    "id": uuid7(),
                    "t": tenant_id,
                    "c": connection_id,
                    "kind": c.kind,
                    "cid": c.commitment_id,
                    "account": c.account_id,
                    "region": c.region_id,
                    "description": c.description[:200],
                    "plan_type": c.plan_type,
                    "term": c.term_months,
                    "payment": c.payment_option,
                    "state": c.state,
                    "quantity": c.quantity,
                    "hourly": c.hourly_commitment_usd,
                    "upfront": c.upfront_usd,
                    "starts": c.starts_at,
                    "ends": c.ends_at,
                    "util": _utilisation(c, by_id),
                    "details": json.dumps(c.details, default=str),
                },
            )
        if got.explorer is not None:
            await session.execute(
                text(
                    "INSERT INTO commitment_snapshot (tenant_id, connection_id, recommendations, "
                    "coverage, utilisation, errors, fetched_at) VALUES (:t, :c, "
                    "CAST(:recs AS jsonb), CAST(:coverage AS jsonb), CAST(:util AS jsonb), "
                    "CAST(:errors AS jsonb), now()) ON CONFLICT (connection_id) DO UPDATE SET "
                    "recommendations = EXCLUDED.recommendations, coverage = EXCLUDED.coverage, "
                    "utilisation = EXCLUDED.utilisation, errors = EXCLUDED.errors, "
                    "fetched_at = now()"
                ),
                {
                    "t": tenant_id,
                    "c": connection_id,
                    "recs": json.dumps(got.explorer.recommendations),
                    "coverage": json.dumps(got.explorer.coverage),
                    "util": json.dumps(got.explorer.utilisation),
                    "errors": json.dumps(got.explorer.errors),
                },
            )
        status: RefreshStatus = "partial" if got.errors else "succeeded"
        calls = got.explorer.calls if got.explorer else 0
        await _record_run(
            session,
            tenant_id,
            connection_id,
            started,
            status,
            len(seen),
            "; ".join(got.errors)[:2000] or None,
            calls,
        )
    return status


async def _record_run(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    started: datetime,
    status: str,
    records: int,
    error: str | None,
    calls: int,
) -> None:
    await session.execute(
        text(
            "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, status, started_at, "
            "finished_at, records, error_code, error_detail, est_customer_api_cost_usd) VALUES "
            "(:id, :t, :c, 'commitments.refresh', :status, :started, now(), :records, :code, "
            ":detail, :cost)"
        ),
        {
            "id": uuid7(),
            "t": tenant_id,
            "c": connection_id,
            "status": "failed" if status == "failed" else "succeeded",
            "started": started,
            "records": records,
            "code": None if error is None else ("partial" if status == "partial" else "failed"),
            "detail": error,
            "cost": round(calls * CE_CALL_COST_USD, 2),
        },
    )


# --- Existing commitments -----------------------------------------------------------------


@dataclass(frozen=True)
class Held:
    id: uuid.UUID
    connection_id: uuid.UUID
    kind: str
    commitment_id: str
    account_id: str | None
    region_id: str | None
    description: str
    plan_type: str | None
    term_months: int | None
    payment_option: str | None
    state: str | None
    quantity: int | None
    hourly_commitment_usd: float | None
    upfront_usd: float | None
    starts_at: datetime | None
    ends_at: datetime | None
    utilisation_pct: float | None
    days_left: int | None
    current: bool
    last_seen_at: datetime


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


async def held(session: AsyncSession, now: datetime) -> list[Held]:
    """Every commitment, soonest expiry first. ``current``: listed by a recent refresh and
    not yet ended."""
    rows = await session.execute(
        text(
            "SELECT id, connection_id, kind, commitment_id, account_id, region_id, description, "
            "plan_type, term_months, payment_option, state, quantity, hourly_commitment_usd, "
            "upfront_usd, starts_at, ends_at, utilisation_pct, last_seen_at FROM commitment "
            "ORDER BY ends_at NULLS LAST, description"
        )
    )
    out = []
    for r in rows:
        ended = r.ends_at is not None and r.ends_at <= now
        left = None if r.ends_at is None else max(0, (r.ends_at.date() - now.date()).days)
        out.append(
            Held(
                id=r.id,
                connection_id=r.connection_id,
                kind=r.kind,
                commitment_id=r.commitment_id,
                account_id=r.account_id.strip() if r.account_id else None,
                region_id=r.region_id,
                description=r.description,
                plan_type=r.plan_type,
                term_months=r.term_months,
                payment_option=r.payment_option,
                state=r.state,
                quantity=r.quantity,
                hourly_commitment_usd=_f(r.hourly_commitment_usd),
                upfront_usd=_f(r.upfront_usd),
                starts_at=r.starts_at,
                ends_at=r.ends_at,
                utilisation_pct=_f(r.utilisation_pct),
                days_left=left,
                current=not ended and now - r.last_seen_at <= STALE_AFTER,
                last_seen_at=r.last_seen_at,
            )
        )  # fmt: skip
    return out


@dataclass(frozen=True)
class Snapshot:
    connection_id: uuid.UUID
    recommendations: list[dict[str, Any]]
    coverage: dict[str, Any]
    utilisation: dict[str, Any]
    errors: list[str]
    fetched_at: datetime


async def snapshots(session: AsyncSession) -> dict[uuid.UUID, Snapshot]:
    rows = await session.execute(
        text(
            "SELECT connection_id, recommendations, coverage, utilisation, errors, fetched_at "
            "FROM commitment_snapshot"
        )
    )
    return {
        r.connection_id: Snapshot(
            r.connection_id,
            list(r.recommendations or []),
            dict(r.coverage or {}),
            dict(r.utilisation or {}),
            list(r.errors or []),
            r.fetched_at,
        )
        for r in rows
    }


# --- Advisory -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Advice:
    connection_id: uuid.UUID
    payer_account_id: str
    status: advisor.Status
    data_through: date | None
    baseline: advisor.Baseline
    daily_eligible: list[tuple[date, float]]
    options: list[advisor.Option]
    default: advisor.Option | None
    ladder: list[advisor.LadderStep]
    fx: advisor.FxView | None
    fx_exposed: bool
    current_coverage_percent: float | None
    target_coverage_percent: float | None
    utilisation_percent: float | None
    decline_tolerance_percent: float | None
    aws_fetched_at: datetime | None
    aws_errors: list[str]


async def eligible_daily(
    session: AsyncSession, connection_id: uuid.UUID
) -> tuple[list[tuple[date, float]], date | None]:
    """Eligible on-demand spend per day over the last 60 days of bills (zero-filled from
    the connection's first bill day), and the last bill day."""
    span = (
        await session.execute(
            text(
                "SELECT min(usage_date) AS first, max(usage_date) AS last FROM cost_daily "
                "WHERE connection_id = :c AND charge_category = 'Usage'"
            ),
            {"c": connection_id},
        )
    ).one()
    if span.last is None:
        return [], None
    first = max(span.first, span.last - timedelta(days=advisor.BASELINE_DAYS - 1))
    rows = await session.execute(
        # Savings-Plan-eligible usage: EC2 instance hours, Fargate, Lambda duration. The
        # billed (unblended) cost of covered usage is cancelled by its SavingsPlanNegation
        # and reserved usage is billed at zero, so the sum is the on-demand spend not yet
        # covered.
        text(
            "SELECT usage_date, sum(billed_cost_usd) AS usd FROM cost_daily "
            "WHERE connection_id = :c AND usage_date BETWEEN :first AND :last "
            "AND charge_category = 'Usage' AND coalesce(usage_type, '') !~* 'spot' AND ("
            "(service_code = 'AmazonEC2' AND usage_type ~* '(BoxUsage|DedicatedUsage|HostUsage)') "
            "OR (service_code IN ('AmazonECS', 'AmazonEKS') AND usage_type ~* 'Fargate') "
            "OR (service_code = 'AWSLambda' "
            "AND usage_type ~* '(Lambda-GB-Second|Lambda-Provisioned)')) "
            "GROUP BY usage_date"
        ),
        {"c": connection_id, "first": first, "last": span.last},
    )
    by_day = {r.usage_date: float(r.usd) for r in rows}
    days = (span.last - first).days + 1
    series = [
        (d, round(by_day.get(d, 0.0), 6)) for d in (first + timedelta(i) for i in range(days))
    ]
    return series, span.last


async def netted_savings(session: AsyncSession, accounts: list[str]) -> float:
    """Monthly savings of open R4-R7 recommendations on EC2 servers in ``accounts``
    (primaries only, so alternatives for one server are not counted twice)."""
    value = (
        await session.execute(
            text(
                "SELECT coalesce(sum(r.savings_usd_month), 0) FROM recommendation r "
                "LEFT JOIN resource res ON res.id = r.resource_ref "
                "WHERE r.status = ANY(:open) AND r.primary_of IS NULL AND r.rule_id ~ :rules "
                "AND (res.resource_type = 'ec2_instance' OR "
                "(res.id IS NULL AND r.resource_id LIKE 'i-%')) "
                "AND trim(r.account_id) = ANY(CAST(:accounts AS text[]))"
            ),
            {"open": OPEN, "rules": NETTED_RULES, "accounts": accounts},
        )
    ).scalar_one()
    return float(value)


async def _connections(session: AsyncSession) -> list[Any]:
    return list(
        await session.execute(
            text(
                "SELECT c.id, c.aws_account_id, array_remove(array_agg(DISTINCT "
                "trim(a.aws_account_id)), NULL) AS accounts FROM cloud_connection c "
                "LEFT JOIN cloud_account a ON a.connection_id = c.id "
                "WHERE c.status <> 'disconnected' GROUP BY c.id ORDER BY c.created_at"
            )
        )
    )


async def advice(session: AsyncSession, today: date) -> list[Advice]:
    """The Savings Plan advisory for each connection (payer) with bills. Inside a
    tenant-scoped transaction."""
    tenant = (
        await session.execute(
            text(
                "SELECT display_currency, fx_rate_basis FROM tenant WHERE id = app_current_tenant()"
            )
        )
    ).one()
    quote = await fx.quote(session, tenant.display_currency, today, tenant.fx_rate_basis)
    snaps = await snapshots(session)
    out = []
    for conn in await _connections(session):
        daily, through = await eligible_daily(session, conn.id)
        if through is None:
            continue
        accounts = sorted({conn.aws_account_id.strip(), *(conn.accounts or [])})
        netted = await netted_savings(session, accounts)
        base = advisor.baseline([v for _, v in daily], netted)
        snap = snaps.get(conn.id)
        recs = snap.recommendations if snap else []
        options = advisor.matrix(base.target_hourly_usd, recs)
        default = advisor.default_option(options)
        state = advisor.status(base, default)
        sp_cov = (snap.coverage.get("savingsPlans") if snap else None) or {}
        sp_util = (snap.utilisation.get("savingsPlans") if snap else None) or {}
        fx_view = (
            advisor.fx_view(default, quote.currency, quote.rate, quote.rate_date, quote.estimated)
            if default is not None and quote is not None and quote.currency != "USD"
            else None
        )
        out.append(
            Advice(
                connection_id=conn.id,
                payer_account_id=conn.aws_account_id.strip(),
                status=state,
                data_through=through,
                baseline=base,
                daily_eligible=daily,
                options=options,
                default=default,
                ladder=advisor.ladder(default.hourly_commitment_usd, today)
                if default is not None and state == "ok"
                else [],
                fx=fx_view,
                fx_exposed=tenant.display_currency != "USD",
                current_coverage_percent=_f(sp_cov.get("percent")),
                target_coverage_percent=advisor.target_coverage_percent(
                    base,
                    default,
                    _f(sp_cov.get("coveredUsd")),
                    _f(sp_cov.get("onDemandUsd")),
                    int(sp_cov.get("days") or 0),
                )
                if default is not None
                else None,
                utilisation_percent=_f(sp_util.get("percent")),
                decline_tolerance_percent=advisor.decline_tolerance_percent(base, default)
                if default is not None
                else None,
                aws_fetched_at=snap.fetched_at if snap else None,
                aws_errors=snap.errors if snap else [],
            )
        )
    return out


# --- R8 -----------------------------------------------------------------------------------


def _aws_hourly(recs: list[dict[str, Any]], payment: str = "NO_UPFRONT") -> float | None:
    found = [r for r in recs if r.get("paymentOption") == payment]
    found.sort(key=lambda r: int(r.get("lookbackDays") or 0))
    return float(found[-1].get("hourlyCommitmentUsd") or 0) if found else None


def candidate(a: Advice, now: datetime, recs: list[dict[str, Any]]) -> Candidate | None:
    """The advisory as one recommendation per payer (PRD FR-REC-01): buy the default
    option, in the ladder's steps."""
    o = a.default
    if a.status != "ok" or o is None:
        return None
    resource = Resource(
        id=uuid.uuid5(uuid.NAMESPACE_URL, f"cloudoptics:r8:{a.connection_id}"),
        account_id=a.payer_account_id,
        region_id="global",
        resource_type="savings_plan_scope",
        resource_id="compute-savings-plan",
        name=None,
        state=None,
        config={},
        tags={},
        state_since=now,
        first_seen_at=now,
        virtual=True,
    )
    aws_hourly = _aws_hourly(recs)
    risk: list[dict[str, Any]] = [{"factor": "one_year_commitment", "points": 20}]
    if a.fx_exposed:
        risk.append({"factor": "paid_in_dollars", "points": 15})
    if a.decline_tolerance_percent is not None and a.decline_tolerance_percent < 30:
        risk.append({"factor": "usage_may_fall", "points": 15})
    notes = ["hourly_from_daily_cur", "advisory_only"]
    if a.baseline.netted_usd_month > 0:
        notes.append("other_savings_netted")
    first = a.ladder[0] if a.ladder else None
    on_demand = o.covered_on_demand_hourly_usd * advisor.HOURS_PER_MONTH
    return Candidate(
        rule_id=R8_RULE,
        rule_version=1,
        type="buy_savings_plan",
        resource=resource,
        title=f"Commit to a 1-year Compute Savings Plan of USD {o.hourly_commitment_usd:.3f} "
        f"an hour (No Upfront), bought in steps",
        current_config={
            "coveragePercent": a.current_coverage_percent,
            "eligibleOnDemandUsdHour": a.baseline.average_hourly_usd,
        },
        recommended_config={
            "instrument": "compute_savings_plan",
            "termYears": 1,
            "payment": "no_upfront",
            "hourlyCommitmentUsd": o.hourly_commitment_usd,
            "firstStepHourlyUsd": first.buy_hourly_usd if first else None,
            "targetCoveragePercent": a.target_coverage_percent,
        },
        cost_now=on_demand,
        cost_after=on_demand - o.monthly_savings_usd,
        basis="effective_rate",
        effort="S",
        reversibility="irreversible",
        confidence="high" if aws_hourly is not None and aws_hourly >= o.hourly_commitment_usd
        else "medium",
        confidence_notes=notes,
        risk_factors=risk,
        evidence=[
            {"kind": "eligibleOnDemandUsdHour", "value": a.baseline.average_hourly_usd,
             "days": a.baseline.days},
            {"kind": "baselineP10UsdHour", "value": a.baseline.p10_hourly_usd,
             "days": a.baseline.days},
            {"kind": "nettedSavingsUsdMonth", "value": a.baseline.netted_usd_month},
            {"kind": "awsHourlyCommitmentUsd", "value": aws_hourly},
            {"kind": "coveragePercent", "value": a.current_coverage_percent},
            {"kind": "usageDeclineTolerancePercent", "value": a.decline_tolerance_percent},
        ],
        rule_config={
            "lookbackDays": advisor.BASELINE_DAYS,
            "percentiles": list(advisor.PERCENTILES),
            "targetShare": list(advisor.TARGET_SHARE),
            "ladderFirst": advisor.LADDER_FIRST,
            "ladderStep": advisor.LADDER_STEP,
            "nettedRules": NETTED_RULES,
        },
        fix={
            "steps": [],
            "rollback": [],
            "checklist": [
                f"Buy the first step now: USD {first.buy_hourly_usd:.3f} an hour"
                if first
                else "Buy the commitment",
                "Then add about 10% of the target each month while usage holds (see the "
                "Commitments page for the ladder).",
                f"AWS lets you return a Savings Plan within {advisor.RETURN_WINDOW_DAYS} days "
                "of buying it if the hourly commitment is USD 100 or less: check the "
                "purchase in that week.",
            ],
            "console": {
                "url": PURCHASE_URL,
                "steps": [
                    "Open Savings Plans, Purchase Savings Plans in the AWS console "
                    "(payer account).",
                    "Choose Compute Savings Plans, 1-year term, No Upfront.",
                    "Enter the hourly commitment of the first step, add to cart and submit.",
                ],
            },
            "notes": "Advisory only: Cloudoptics never buys anything. A Savings Plan cannot "
            "be cancelled after the return window.",
        },
    )  # fmt: skip


async def r8_candidates(session: AsyncSession, now: datetime) -> list[Candidate]:
    """For the engine, inside its tenant-scoped transaction."""
    snaps = await snapshots(session)
    found = []
    for a in await advice(session, now.date()):
        snap = snaps.get(a.connection_id)
        c = candidate(a, now, snap.recommendations if snap else [])
        if c is not None:
            found.append(c)
    return found


# --- Expiry alerts --------------------------------------------------------------------------

KIND_LABEL = {
    "savings_plan": "Savings Plan",
    "ec2_reserved": "EC2 Reserved Instance",
    "rds_reserved": "RDS Reserved Instance",
}


def expiry_email(items: list[tuple[Held, int]], app_url: str) -> tuple[str, str, str]:
    """Subject, text and HTML for commitments reaching an expiry threshold."""
    soonest = min(h.days_left or 0 for h, _ in items)
    subject = (
        f"A commitment ends in {soonest} days"
        if len(items) == 1
        else f"{len(items)} commitments end soon (first in {soonest} days)"
    )
    link = f"{app_url.rstrip('/')}/commitments"

    def line(h: Held) -> str:
        end = h.ends_at.strftime("%d %b %Y") if h.ends_at else "?"
        hourly = (
            f", USD {h.hourly_commitment_usd:,.3f} an hour"
            if h.hourly_commitment_usd is not None
            else ""
        )
        return (
            f"{KIND_LABEL.get(h.kind, h.kind)}: {h.description} (account {h.account_id or '?'}"
            f"{hourly}) ends on {end}, in {h.days_left} days"
        )

    lines = [
        "These Savings Plans or Reserved Instances end soon. When they end, the usage they "
        "cover is billed at on-demand prices.",
        "",
        *[f"- {line(h)}" for h, _ in items],
        "",
        "Decide whether to renew or replace them; the Commitments page has the advisory, "
        f"with the cost in your currency: {link}",
    ]
    rows = "".join(f"<li>{html.escape(line(h))}</li>" for h, _ in items)
    body = (
        "<p>These Savings Plans or Reserved Instances end soon. When they end, the usage they "
        f"cover is billed at on-demand prices.</p><ul>{rows}</ul><p>Decide whether to renew "
        f'or replace them: <a href="{html.escape(link)}">open the Commitments page</a>.</p>'
    )
    return subject, "\n".join(lines), body


def due_threshold(days_left: int) -> int | None:
    """The tightest threshold reached (60, 30 or 15 days), or None."""
    reached = [t for t in EXPIRY_THRESHOLDS if days_left <= t]
    return min(reached) if reached else None


async def alert_expiries(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> int:
    """Email commitments that reached a threshold and were not alerted for it yet. Inside a
    tenant-scoped transaction. Returns emails sent (0 or 1)."""
    due: list[tuple[Held, int]] = []
    for h in await held(session, now):
        if not h.current or h.days_left is None:
            continue
        threshold = due_threshold(h.days_left)
        if threshold is None:
            continue
        done = (
            await session.execute(
                text("SELECT 1 FROM notification WHERE dedupe_key = :k AND status <> 'failed'"),
                {"k": f"commitment:{h.id}:{threshold}"},
            )
        ).first()
        if not done:
            due.append((h, threshold))
    if not due:
        return 0
    people = await preferences.recipients(
        session, tenant_id, "commitment_expiry", ("admin", "member")
    )
    to = [p.email for p in people]
    subject, body_text, body_html = expiry_email(due, get_settings().app_url)
    status, error, sent = "skipped", None, 0
    if to:
        try:
            await get_mailer().send(
                Email(to, subject, body_text, body_html, tag="commitment-expiry")
            )
            status, sent = "sent", 1
        except EmailError as exc:
            status, error = "failed", str(exc)
            log.warning("commitment expiry alert for %s not sent: %s", tenant_id, exc)
    for h, threshold in due:
        await session.execute(
            text(
                "INSERT INTO notification (id, tenant_id, kind, dedupe_key, recipients, subject, "
                "status, error) VALUES (:id, :t, 'commitment_expiry', :k, :to, :subject, "
                ":status, :error) ON CONFLICT (tenant_id, dedupe_key) DO UPDATE SET "
                "recipients = EXCLUDED.recipients, subject = EXCLUDED.subject, "
                "status = EXCLUDED.status, error = EXCLUDED.error, created_at = now()"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "k": f"commitment:{h.id}:{threshold}",
                "to": to,
                "subject": subject,
                "status": status,
                "error": error,
            },
        )
    return sent
