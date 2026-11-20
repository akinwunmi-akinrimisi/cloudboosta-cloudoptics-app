"""/v1/tenants/{t}/costs, the manual ingest trigger and reload bills (docs/09 §3.2 "Cost
data")."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from cloudoptics import providers
from cloudoptics.audit import log as audit
from cloudoptics.connectors.service import ConnectionNotFoundError, get_connection
from cloudoptics.identity.deps import AdminAccess, Session, ViewerAccess
from cloudoptics.ingestion import queries, reload
from cloudoptics.ingestion.service import INGESTABLE, ingest_connection
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.conditional import ConditionalGet
from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.base import ProviderName

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["costs"])

MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"
#: An ended month (YYYY-MM) to show instead of the current one.
MonthParam = Annotated[str | None, Query(pattern=MONTH, description="YYYY-MM")]


def chosen_month(month: str | None, today: date) -> date | None:
    """The month asked for (its first day), or ``None`` for the current month. A month
    after the current one is refused."""
    if month is None:
        return None
    first = reload.parse_month(month)
    if first > today.replace(day=1):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Choose this month or an earlier one."
        )
    return first


class MonthToDate(ApiModel):
    effective: float
    billed: float
    credits: float


class Forecast(ApiModel):
    """Month to date + ``dailyRunRate`` (the average over the last ``runRateDays`` days of
    bills, before tax and monthly charges) x the days left + ``monthlyCharges`` (known
    once-a-month charges, such as AWS Support, not billed yet this month)."""

    amount: float
    method: Literal["run_rate"]
    days_covered: int
    days_in_month: int
    run_rate_days: int
    daily_run_rate: float
    monthly_charges: float


Source = Literal["cur", "cost_explorer"]
MonthState = Literal["current", "changing", "final"]


class Reconciliation(ApiModel):
    """``mismatch``: CUR totals differ from Cost Explorer by over 1% (show a banner)."""

    status: Literal["ok", "mismatch", "failed"]
    month: str
    difference_pct: float | None
    accounts_mismatched: int
    checked_at: datetime


class CostSummary(ApiModel):
    """``provisional``: no CUR data for the month yet, figures come from Cost Explorer.
    ``monthState``: ``current`` (this month so far), ``changing`` (an ended month AWS may
    still restate) or ``final``."""

    status: Literal["no_connection", "waiting", "provisional", "ready"]
    source: Source | None
    month: str
    currency: str
    data_through: date | None
    last_load_at: datetime | None
    last_load_status: str | None
    month_to_date: MonthToDate | None
    forecast: Forecast | None
    previous_month: float | None
    reconciliation: Reconciliation | None = None
    month_state: MonthState = "current"


class DailyPoint(ApiModel):
    date: date
    amount: float


class DailyCosts(ApiModel):
    source: Source | None
    currency: str
    points: list[DailyPoint]


class BreakdownItem(ApiModel):
    key: str
    label: str
    amount: float
    share: float
    # The account's cloud, for the account dimension (ADR-018); null for the others.
    provider: ProviderName | None = None


class Breakdown(ApiModel):
    source: Source | None
    month: str
    dimension: Literal["service", "account", "region"]
    currency: str
    total: float
    items: list[BreakdownItem]


class IngestOutcome(ApiModel):
    connection_id: uuid.UUID
    status: Literal["queued", "skipped", "up_to_date", "succeeded", "partial", "failed"]
    loaded: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)


class CostMonth(ApiModel):
    """A month with spend. ``report``: its executive report is ``stored`` (as sent) or can
    be made as a ``draft`` (an ended month with bills)."""

    month: str
    source: Source
    total: float
    data_through: date
    state: MonthState
    report: Literal["stored", "draft"] | None


class CostMonths(ApiModel):
    currency: str
    months: list[CostMonth]


@router.get("/costs/months", response_model=CostMonths)
async def cost_months(
    access: ViewerAccess, session: Session, cond: ConditionalGet, today: date | None = None
) -> CostMonths:
    """The months with cost data, newest first: total spend (amortised, after credits, with
    tax) and whether the figures are final."""

    async def load() -> CostMonths:
        items = await queries.months(session, today or datetime.now(UTC).date())
        return CostMonths(
            currency="USD",
            months=[
                CostMonth(
                    month=f"{i.month:%Y-%m}",
                    source=i.source,
                    total=i.total,
                    data_through=i.data_through,
                    state=i.state,
                    report=i.report,
                )
                for i in items
            ],
        )

    return await cond.cached(load)


@router.get("/costs/summary", response_model=CostSummary)
async def cost_summary(
    access: ViewerAccess,
    session: Session,
    cond: ConditionalGet,
    today: date | None = None,
    month: MonthParam = None,
) -> CostSummary:
    """``today`` (UTC) defaults to now; it exists so tests and demos can pin the month.
    ``month``: an ended month's whole spend instead (no forecast)."""
    day = today or datetime.now(UTC).date()
    chosen = chosen_month(month, day)

    async def load() -> CostSummary:
        return CostSummary.model_validate(await queries.summary(session, day, chosen))

    return await cond.cached(load)


@router.get("/costs/daily", response_model=DailyCosts)
async def cost_daily(
    access: ViewerAccess,
    session: Session,
    cond: ConditionalGet,
    today: date | None = None,
    days: int = Query(30, ge=7, le=92),
    month: MonthParam = None,
) -> DailyCosts:
    """Daily amortised cost after credits, for the trend chart. ``month``: every day of an
    ended month instead of the last ``days`` days."""
    day = today or datetime.now(UTC).date()
    chosen = chosen_month(month, day)

    async def load() -> DailyCosts:
        return DailyCosts.model_validate(await queries.daily(session, day, days, chosen))

    return await cond.cached(load)


@router.get("/costs/breakdown", response_model=Breakdown)
async def cost_breakdown(
    access: ViewerAccess,
    session: Session,
    cond: ConditionalGet,
    dimension: Literal["service", "account", "region"] = "service",
    today: date | None = None,
    limit: int = Query(queries.TOP_ITEMS, ge=1, le=50),
    month: MonthParam = None,
) -> Breakdown:
    """This month's (or ``month``'s) spend excluding tax, by service, account or region
    (top ``limit`` plus the rest)."""
    day = today or datetime.now(UTC).date()
    first = chosen_month(month, day) or day

    async def load() -> Breakdown:
        return Breakdown.model_validate(await queries.breakdown(session, first, dimension, limit))

    return await cond.cached(load)


@router.post(
    "/connections/{connection_id}/ingest",
    response_model=IngestOutcome,
    responses={202: {"model": IngestOutcome}},
)
async def ingest_now(
    connection_id: uuid.UUID, response: Response, access: AdminAccess, session: Session
) -> IngestOutcome:
    try:
        conn = await get_connection(session, connection_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found") from exc

    if get_settings().jobs_mode == "inline":
        plugin = providers.find(conn.provider)
        if conn.provider == "aws" or plugin is None or plugin.billing_source is None:
            result: Any = await ingest_connection(access.tenant_id, connection_id)
        else:
            result = await plugin.billing_source.ingest(access.tenant_id, connection_id)
        return IngestOutcome(
            connection_id=connection_id,
            status=result.status,
            loaded=result.loaded,
            rejected=result.rejected,
        )

    from cloudoptics.ingestion.jobs import defer_ingest
    from cloudoptics.platform.jobs import get_job_app

    await defer_ingest(get_job_app(), str(access.tenant_id), str(connection_id))
    response.status_code = status.HTTP_202_ACCEPTED
    return IngestOutcome(connection_id=connection_id, status="queued")


# --- reload bills ---------------------------------------------------------------------


class ReloadRequest(BaseModel):
    """``period``: one billing month (YYYY-MM); leave it out for every month in the export."""

    period: str | None = Field(default=None, pattern=MONTH)


class PeriodReload(ApiModel):
    """``reloaded``: read again and stored; ``unchanged``: the same figures as loaded;
    ``failed``: could not be read or failed the checks (the previous data stays);
    ``not_in_bucket``: no export for the month in the bucket."""

    period: str
    outcome: Literal["reloaded", "unchanged", "failed", "not_in_bucket"]
    version: str | None = None
    detail: str | None = None


class ReloadStatus(ApiModel):
    """A connection's latest reload. ``status`` ``queued``: the worker starts it shortly;
    ``none``: never reloaded. ``nextAllowedAt``: when another reload may start."""

    connection_id: uuid.UUID
    status: Literal["none", "queued", "running", "skipped", "succeeded", "partial", "failed"]
    period: str | None = None
    requested_at: datetime | None = None
    finished_at: datetime | None = None
    periods: list[PeriodReload] = Field(default_factory=list)
    error: str | None = None
    next_allowed_at: datetime | None = None


async def _connection(session: Any, connection_id: uuid.UUID) -> Any:
    try:
        return await get_connection(session, connection_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found") from exc


def _upcoming(at: datetime | None) -> datetime | None:
    return at if at is not None and at > datetime.now(UTC) else None


@router.get("/connections/{connection_id}/reload", response_model=ReloadStatus)
async def reload_status(
    connection_id: uuid.UUID, access: AdminAccess, session: Session
) -> ReloadStatus:
    """The latest reload of the connection's bills, to follow one in progress."""
    await _connection(session, connection_id)
    run = await reload.latest_run(session, connection_id)
    next_at = _upcoming(reload.next_allowed(await reload.last_request(session, connection_id)))
    if run is None:
        return ReloadStatus(connection_id=connection_id, status="none")
    return ReloadStatus(
        connection_id=connection_id,
        status=run.status,
        period=None if run.period is None else f"{run.period:%Y-%m}",
        requested_at=run.requested_at,
        finished_at=run.finished_at,
        periods=[PeriodReload.model_validate(p) for p in run.periods],
        error=run.error,
        next_allowed_at=next_at,
    )


@router.post(
    "/connections/{connection_id}/reload",
    response_model=ReloadStatus,
    responses={
        202: {"model": ReloadStatus},
        409: {"description": "The connection is not working or has no export"},
        429: {"description": "Reloaded within the hour, or a reload is running"},
    },
)
async def reload_bills(
    connection_id: uuid.UUID,
    request: Request,
    response: Response,
    access: AdminAccess,
    session: Session,
    body: ReloadRequest | None = None,
) -> ReloadStatus:
    """Read the bills again from the connection's export bucket: the latest export version
    of ``period``, or of every month in the bucket, even when it is loaded already. Then
    reconciliation, budgets, anomalies and the analysis run again. Once an hour per
    connection."""
    conn = await _connection(session, connection_id)
    # AWS reads a CUR bucket; other providers keep their export settings in their plug-in.
    if conn.status not in INGESTABLE or (conn.provider == "aws" and not conn.cur_bucket):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Check the connection first: bills are reloaded for a working connection with an "
            "export.",
        )
    month = body.period if body else None
    period = reload.parse_month(month) if month else None
    # One reload per connection at a time: held until this request's transaction ends.
    free = (
        await session.execute(
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
            {"k": f"reload:{connection_id}"},
        )
    ).scalar_one()
    next_at = _upcoming(reload.next_allowed(await reload.last_request(session, connection_id)))
    if not free or next_at is not None:
        wait = 60 if next_at is None else int((next_at - datetime.now(UTC)).total_seconds()) + 1
        message = (
            "A reload of this connection is already running."
            if next_at is None
            else "Bills for this connection were reloaded less than an hour ago. Try again "
            f"after {next_at:%H:%M} UTC."
        )
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, message, headers={"Retry-After": str(wait)}
        )

    async def record(outcome: str, periods: list[dict[str, Any]] | None = None) -> None:
        # Written last: the inline reload and analysis run in their own transactions and
        # append to the audit chain too (verification), which this one would otherwise
        # hold locked until the request ends.
        await audit.record(
            session,
            tenant_id=access.tenant_id,
            actor=user_actor(request, access.user.id),
            action="ingestion.reload_requested",
            target_type="cloud_connection",
            target_id=str(connection_id),
            after={"period": month, "status": outcome, "periods": periods},
        )

    requested_at = datetime.now(UTC)
    if get_settings().jobs_mode == "inline":
        source = providers.find(conn.provider)
        if source is None or source.billing_source is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "Bills cannot be reloaded for this cloud."
            )
        # Every plug-in answers with reload.ReloadResult (its periods and errors are shown).
        result = cast(
            reload.ReloadResult,
            await source.billing_source.reload(access.tenant_id, connection_id, period),
        )
        if result.reloaded:
            await reload.after_reload_inline(access.tenant_id, connection_id, result.reloaded)
        periods = [p.as_json() for p in result.periods]
        await record(result.status, periods)
        return ReloadStatus(
            connection_id=connection_id,
            status=result.status,
            period=month,
            requested_at=requested_at,
            finished_at=datetime.now(UTC),
            periods=[PeriodReload.model_validate(p) for p in periods],
            error="; ".join(result.errors) or None,
            next_allowed_at=_upcoming(
                reload.next_allowed(await reload.last_request(session, connection_id))
            ),
        )

    from cloudoptics.ingestion.jobs import defer_reload
    from cloudoptics.platform.jobs import get_job_app

    await reload.queue_run(session, access.tenant_id, connection_id, period)
    await defer_reload(get_job_app(), str(access.tenant_id), str(connection_id), month)
    await record("queued")
    response.status_code = status.HTTP_202_ACCEPTED
    return ReloadStatus(
        connection_id=connection_id,
        status="queued",
        period=month,
        requested_at=requested_at,
        next_allowed_at=requested_at + reload.RELOAD_EVERY,
    )
