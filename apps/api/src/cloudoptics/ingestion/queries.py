"""``CostQueryService`` (docs/11b §2.7): read-side cost queries for the API. Callers run
inside a tenant-scoped transaction; row-level security limits every query to that tenant."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SummaryStatus = Literal["no_connection", "waiting", "provisional", "ready"]
Source = Literal["cur", "cost_explorer"]
Dimension = Literal["service", "account", "region"]
MonthState = Literal["current", "changing", "final"]
TOP_ITEMS = 8
#: AWS issues the invoice in the first days of the month after; from the 6th an ended
#: month's bill counts as final (a later restatement is still loaded).
FINAL_AFTER_DAYS = 5
RUN_RATE_DAYS = 7
MONTHLY_CHARGE_MIN_USD = 1.0


@dataclass(frozen=True)
class Forecast:
    """Month-end forecast (PRD FR-ANA-09): month to date, plus the average daily spend over
    the last ``run_rate_days`` days of bills for the days left, plus the known monthly
    charges not billed yet this month (``monthly_charges``, at last month's amounts)."""

    amount: float
    method: Literal["run_rate"]
    days_covered: int
    days_in_month: int
    run_rate_days: int = 0
    daily_run_rate: float = 0.0
    monthly_charges: float = 0.0


@dataclass(frozen=True)
class MonthToDate:
    effective: float
    billed: float
    credits: float


@dataclass(frozen=True)
class Reconciliation:
    """The latest check of a month's CUR totals against Cost Explorer (FR-ING-07)."""

    status: Literal["ok", "mismatch", "failed"]
    month: str
    difference_pct: float | None
    accounts_mismatched: int
    checked_at: datetime


@dataclass(frozen=True)
class CostSummary:
    status: SummaryStatus
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
    #: ``current``: this month, so far; ``changing``: an ended month AWS may still restate;
    #: ``final``: an ended month whose bill is final.
    month_state: MonthState = "current"


@dataclass(frozen=True)
class DailyPoint:
    date: date
    amount: float


@dataclass(frozen=True)
class DailyCosts:
    source: Source | None
    currency: str
    points: list[DailyPoint]


@dataclass(frozen=True)
class BreakdownItem:
    key: str
    label: str
    amount: float
    share: float
    # The account's cloud, for the account dimension (ADR-018); None for the others.
    provider: str | None = None


@dataclass(frozen=True)
class Breakdown:
    source: Source | None
    month: str
    dimension: Dimension
    currency: str
    total: float
    items: list[BreakdownItem] = field(default_factory=list)


def _month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    return start, (start + timedelta(days=32)).replace(day=1)


def month_state(start: date, today: date) -> MonthState:
    """Whether the month starting ``start`` is the current one, ended but possibly still
    restated by AWS, or final."""
    _, end = _month_bounds(start)
    if today < end:
        return "current"
    return "final" if today >= end + timedelta(days=FINAL_AFTER_DAYS) else "changing"


def past_month(month: date | None, today: date) -> date | None:
    """``month``'s first day when it ended before ``today``'s month, else ``None`` (the
    current month, the default)."""
    if month is None or month.replace(day=1) >= today.replace(day=1):
        return None
    return month.replace(day=1)


@dataclass(frozen=True)
class MonthItem:
    """A month with spend: from the bills (CUR), or provisional (Cost Explorer) while the
    month has no bills yet."""

    month: date
    source: Source
    total: float  # amortised, after credits, with tax (as the month to date)
    data_through: date
    state: MonthState
    report: Literal["stored", "draft"] | None  # the month's executive report


async def months(session: AsyncSession, today: date) -> list[MonthItem]:
    """Every month with cost data up to ``today``'s, newest first."""
    current, end = _month_bounds(today)
    rows = await session.execute(
        text(
            "SELECT CAST(date_trunc('month', usage_date) AS date) AS month, "
            "sum(effective_cost_usd) AS total, max(usage_date) AS through "
            "FROM cost_daily WHERE usage_date < :end GROUP BY 1"
        ),
        {"end": end},
    )
    found = {r.month: (r.total, r.through) for r in rows}
    stored = set((await session.execute(text("SELECT month FROM monthly_report"))).scalars())
    items = [
        MonthItem(
            month=m,
            source="cur",
            total=round(float(total), 2),
            data_through=through,
            state=month_state(m, today),
            report="stored" if m in stored else ("draft" if m < current else None),
        )
        for m, (total, through) in found.items()
    ]
    snapshots = await session.execute(
        text(
            "SELECT period_start, sum(effective_cost_usd) AS total, "
            "max(through_date) AS through FROM cost_snapshot_ce "
            "WHERE period_start <= :current GROUP BY period_start"
        ),
        {"current": current},
    )
    items.extend(
        MonthItem(
            month=r.period_start,
            source="cost_explorer",
            total=round(float(r.total), 2),
            data_through=r.through,
            state=month_state(r.period_start, today),
            report=None,
        )
        for r in snapshots
        if r.period_start not in found
    )
    return sorted(items, key=lambda i: i.month, reverse=True)


async def _reconciliation(
    session: AsyncSession, prev_start: date, start: date
) -> Reconciliation | None:
    """This or last month's check, a mismatch first (any connection), else the newest."""
    row = (
        await session.execute(
            text(
                "SELECT status, billing_period, difference_pct, "
                "jsonb_array_length(accounts) AS accounts, checked_at FROM cost_reconciliation "
                "WHERE billing_period IN (:prev, :start) "
                "ORDER BY status = 'mismatch' DESC, billing_period DESC, checked_at DESC LIMIT 1"
            ),
            {"prev": prev_start, "start": start},
        )
    ).one_or_none()
    if row is None:
        return None
    return Reconciliation(
        status=row.status,
        month=f"{row.billing_period:%Y-%m}",
        difference_pct=None if row.difference_pct is None else float(row.difference_pct),
        accounts_mismatched=row.accounts,
        checked_at=row.checked_at,
    )


def _run_rate(effective: float, run_rate_base: float, start: date, last_day: date) -> Forecast:
    """The Cost Explorer (provisional) forecast: the average over the month's days so far,
    as that series has no charge detail to tell monthly fees apart."""
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    covered = (last_day - start).days + 1
    daily = run_rate_base / covered
    return Forecast(
        amount=round(effective + daily * (days_in_month - covered), 2),
        method="run_rate",
        days_covered=covered,
        days_in_month=days_in_month,
        run_rate_days=covered,
        daily_run_rate=round(daily, 2),
    )


@dataclass(frozen=True)
class MonthlyCharge:
    """A charge AWS bills once a month, on the 1st (AWS Support, monthly fees and
    subscriptions, Route 53 hosted zones), seen that way last month."""

    service_name: str
    usage_type: str
    expected: float  # last month's amount
    billed: bool  # already on this month's bills
    in_window: float  # this month's amount inside the run-rate window


_MONTHLY_CHARGES = """
WITH monthly AS (
    SELECT provider_account_id, service_code, max(service_name) AS service_name,
           coalesce(usage_type, '') AS usage_type, charge_category,
           sum(effective_cost_usd) AS amount
    FROM cost_daily
    WHERE usage_date >= :prev AND usage_date < :start
      AND charge_category NOT IN ('Tax', 'Credit')
      AND (CAST(:accounts AS text[]) IS NULL
           OR provider_account_id = ANY(CAST(:accounts AS text[])))
    GROUP BY provider_account_id, service_code, coalesce(usage_type, ''), charge_category
    HAVING count(DISTINCT usage_date) = 1 AND min(usage_date) = :prev
       AND abs(sum(effective_cost_usd)) >= :min
), cur AS (
    SELECT provider_account_id, service_code, coalesce(usage_type, '') AS usage_type,
           charge_category,
           sum(effective_cost_usd) AS amount,
           coalesce(sum(effective_cost_usd) FILTER (WHERE usage_date >= :window), 0)
               AS in_window
    FROM cost_daily
    WHERE usage_date >= :start AND usage_date <= :last
      AND (CAST(:accounts AS text[]) IS NULL
           OR provider_account_id = ANY(CAST(:accounts AS text[])))
    GROUP BY provider_account_id, service_code, coalesce(usage_type, ''), charge_category
)
SELECT m.service_name, m.usage_type, m.amount AS expected, c.amount IS NOT NULL AS billed,
       coalesce(c.in_window, 0) AS in_window
FROM monthly m LEFT JOIN cur c
  ON c.provider_account_id = m.provider_account_id AND c.service_code = m.service_code
 AND c.usage_type = m.usage_type AND c.charge_category = m.charge_category
ORDER BY m.amount DESC
"""


async def monthly_charges(
    session: AsyncSession,
    start: date,
    last_day: date,
    window_start: date,
    accounts: list[str] | None = None,
) -> list[MonthlyCharge]:
    """Last month's once-a-month charges (billed on the 1st only, at least USD 1), and
    whether this month's bills up to ``last_day`` have them yet."""
    prev, _ = _month_bounds(start - timedelta(days=1))
    rows = await session.execute(
        text(_MONTHLY_CHARGES),
        {
            "prev": prev,
            "start": start,
            "last": last_day,
            "window": window_start,
            "accounts": accounts,
            "min": MONTHLY_CHARGE_MIN_USD,
        },
    )
    return [
        MonthlyCharge(
            r.service_name, r.usage_type, float(r.expected), bool(r.billed), float(r.in_window)
        )
        for r in rows
    ]


async def month_end_forecast(
    session: AsyncSession,
    start: date,
    last_day: date,
    effective: float,
    accounts: list[str] | None = None,
) -> Forecast:
    """FR-ANA-09: ``effective`` (month to date) + the average daily spend over the last 7
    days of bills (fewer when the bills start later; tax and the monthly charges left out)
    x the days left + the monthly charges not billed yet this month."""
    first_day = (
        await session.execute(text("SELECT min(usage_date) FROM cost_daily"))
    ).scalar_one() or start
    window_start = max(last_day - timedelta(days=RUN_RATE_DAYS - 1), first_day)
    window_days = (last_day - window_start).days + 1
    window_total = (
        await session.execute(
            text(
                "SELECT coalesce(sum(effective_cost_usd), 0) FROM cost_daily "
                "WHERE usage_date BETWEEN :first AND :last AND charge_category <> 'Tax' "
                "AND (CAST(:accounts AS text[]) IS NULL "
                "OR provider_account_id = ANY(CAST(:accounts AS text[])))"
            ),
            {"first": window_start, "last": last_day, "accounts": accounts},
        )
    ).scalar_one()
    charges = await monthly_charges(session, start, last_day, window_start, accounts)
    daily = (float(window_total) - sum(c.in_window for c in charges)) / window_days
    to_come = sum(c.expected for c in charges if not c.billed)
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    covered = (last_day - start).days + 1
    return Forecast(
        amount=round(effective + daily * (days_in_month - covered) + to_come, 2),
        method="run_rate",
        days_covered=covered,
        days_in_month=days_in_month,
        run_rate_days=window_days,
        daily_run_rate=round(daily, 2),
        monthly_charges=round(to_come, 2),
    )


async def _has_cur_month(session: AsyncSession, start: date, end: date) -> bool:
    return bool(
        (
            await session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM cost_daily "
                    "WHERE usage_date >= :start AND usage_date < :end)"
                ),
                {"start": start, "end": end},
            )
        ).scalar_one()
    )


async def _snapshots(session: AsyncSession, start: date) -> list[Any]:
    """Cost Explorer snapshots for the month, used only while it has no CUR data."""
    return list(
        (
            await session.execute(
                text(
                    "SELECT through_date, effective_cost_usd, billed_cost_usd, credits_usd, "
                    "daily, by_service, fetched_at FROM cost_snapshot_ce "
                    "WHERE period_start = :p ORDER BY connection_id"
                ),
                {"p": start},
            )
        ).all()
    )


def _sum_series(rows: list[Any], column: str, key: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for row in rows:
        for item in getattr(row, column):
            totals[item[key]] = totals.get(item[key], 0.0) + float(item["amount"])
    return totals


@dataclass(frozen=True)
class MonthSpend:
    """A month's spend from the bills (CUR), for budgets: amortised, after credits."""

    effective: float
    forecast: float | None
    data_through: date | None


async def month_spend(
    session: AsyncSession, today: date, accounts: list[str] | None = None
) -> MonthSpend:
    """This month's spend for the workspace or some of its accounts, with the same run-rate
    forecast as the Overview. The forecast counts days up to the workspace's latest data,
    so an account with no spend on a day is not treated as missing data."""
    start, end = _month_bounds(today)
    last_day = (
        await session.execute(
            text(
                "SELECT max(usage_date) FROM cost_daily "
                "WHERE usage_date >= :start AND usage_date < :end"
            ),
            {"start": start, "end": end},
        )
    ).scalar_one()
    if last_day is None:
        return MonthSpend(0.0, None, None)
    totals = (
        await session.execute(
            text(
                "SELECT coalesce(sum(effective_cost_usd), 0) FROM cost_daily "
                "WHERE usage_date >= :start AND usage_date < :end "
                "AND (CAST(:accounts AS text[]) IS NULL "
                "OR provider_account_id = ANY(CAST(:accounts AS text[])))"
            ),
            {"start": start, "end": end, "accounts": accounts},
        )
    ).scalar_one()
    effective = float(totals)
    forecast = await month_end_forecast(session, start, last_day, effective, accounts)
    return MonthSpend(round(effective, 2), forecast.amount, last_day)


async def summary(session: AsyncSession, today: date, month: date | None = None) -> CostSummary:
    """Month-to-date spend (amortised, after credits) and the month-end forecast
    (``month_end_forecast``; while the month has only Cost Explorer figures, the average
    over its days so far).

    ``month``: an ended month instead: its whole spend and whether its bill is final; no
    forecast (that is the current month's)."""
    past = past_month(month, today)
    start, end = _month_bounds(past or today)
    state = month_state(start, today)
    prev_start, _ = _month_bounds(start - timedelta(days=1))
    label = f"{start:%Y-%m}"

    connections = (
        await session.execute(
            text("SELECT count(*) FROM cloud_connection WHERE status <> 'disconnected'")
        )
    ).scalar_one()
    last = (
        await session.execute(
            text("SELECT status, created_at FROM ingest_batch ORDER BY created_at DESC LIMIT 1")
        )
    ).one_or_none()
    data_through = (
        await session.execute(
            text("SELECT max(usage_date) FROM cost_daily WHERE usage_date < :end"),
            {"end": end if past else date.max},
        )
    ).scalar_one()
    if not await _has_cur_month(session, start, end):
        snapshots = await _snapshots(session, start)
        if snapshots:
            provisional = _provisional(
                snapshots, start, label, data_through, last, forecast=past is None
            )
            return replace(provisional, month_state=state)
    base = {
        "source": "cur" if data_through is not None else None,
        "month": label,
        "month_state": state,
        "currency": "USD",
        "data_through": data_through,
        "last_load_at": last.created_at if last else None,
        "last_load_status": last.status if last else None,
    }
    if data_through is None:
        return CostSummary(
            status="waiting" if connections else "no_connection",
            month_to_date=None,
            forecast=None,
            previous_month=None,
            **base,
        )

    totals = (
        await session.execute(
            text(
                "SELECT coalesce(sum(effective_cost_usd), 0) AS effective, "
                "coalesce(sum(billed_cost_usd), 0) AS billed, "
                "coalesce(sum(effective_cost_usd) FILTER (WHERE charge_category = 'Credit'), 0) "
                "AS credits, max(usage_date) AS last_day "
                "FROM cost_daily WHERE usage_date >= :start AND usage_date < :end"
            ),
            {"start": start, "end": end},
        )
    ).one()
    previous = (
        await session.execute(
            text(
                "SELECT sum(effective_cost_usd) FROM cost_daily "
                "WHERE usage_date >= :start AND usage_date < :end"
            ),
            {"start": prev_start, "end": start},
        )
    ).scalar_one()

    forecast = None
    if totals.last_day is not None and past is None:
        forecast = await month_end_forecast(
            session, start, totals.last_day, float(totals.effective)
        )
    return CostSummary(
        status="ready",
        month_to_date=MonthToDate(
            effective=round(float(totals.effective), 2),
            billed=round(float(totals.billed), 2),
            credits=round(float(totals.credits), 2),
        ),
        forecast=forecast,
        previous_month=None if previous is None else round(float(previous), 2),
        reconciliation=await _reconciliation(session, prev_start, start),
        **base,
    )


def _provisional(
    snapshots: list[Any],
    start: date,
    month: str,
    data_through: date | None,
    last: Any,
    *,
    forecast: bool = True,
) -> CostSummary:
    effective = sum(float(r.effective_cost_usd) for r in snapshots)
    through = max(r.through_date for r in snapshots)
    return CostSummary(
        status="provisional",
        source="cost_explorer",
        month=month,
        currency="USD",
        data_through=through,
        last_load_at=max(r.fetched_at for r in snapshots),
        last_load_status=last.status if last else None,
        month_to_date=MonthToDate(
            effective=round(effective, 2),
            billed=round(sum(float(r.billed_cost_usd) for r in snapshots), 2),
            credits=round(sum(float(r.credits_usd) for r in snapshots), 2),
        ),
        # Cost Explorer's daily series has no tax split, so tax counts towards the run rate.
        forecast=_run_rate(effective, effective, start, through) if forecast else None,
        previous_month=None,
    )


async def daily(
    session: AsyncSession, today: date, days: int = 30, month: date | None = None
) -> DailyCosts:
    """Daily effective cost for the ``days`` days up to the latest data (or ``today``).

    Uses CUR data; while the current month has none, the month so far comes from the Cost
    Explorer snapshot instead. ``month``: every day of that ended month instead.
    """
    past = past_month(month, today)
    start, end = _month_bounds(past or today)
    if not await _has_cur_month(session, start, end):
        snapshots = await _snapshots(session, start)
        if snapshots:
            series = _sum_series(snapshots, "daily", "date")
            return DailyCosts(
                source="cost_explorer",
                currency="USD",
                points=[
                    DailyPoint(date.fromisoformat(d), round(v, 2))
                    for d, v in sorted(series.items())
                ],
            )
    if past is not None:
        if not await _has_cur_month(session, start, end):
            return DailyCosts(source=None, currency="USD", points=[])
        last: date | None = end - timedelta(days=1)
        days = (end - start).days
    else:
        last = (
            await session.execute(
                text("SELECT max(usage_date) FROM cost_daily WHERE usage_date <= :t"),
                {"t": today},
            )
        ).scalar_one()
    if last is None:
        return DailyCosts(source=None, currency="USD", points=[])
    first = last - timedelta(days=days - 1)
    rows = await session.execute(
        text(
            "SELECT usage_date, sum(effective_cost_usd) AS amount FROM cost_daily "
            "WHERE usage_date BETWEEN :first AND :last GROUP BY usage_date ORDER BY usage_date"
        ),
        {"first": first, "last": last},
    )
    by_day = {r.usage_date: float(r.amount) for r in rows}
    return DailyCosts(
        source="cur",
        currency="USD",
        points=[
            DailyPoint(
                first + timedelta(days=i), round(by_day.get(first + timedelta(days=i), 0), 2)
            )
            for i in range(days)
        ],
    )


_BREAKDOWN_SQL = {
    "service": (
        "SELECT service_code AS key, max(service_name) AS label, "
        "sum(effective_cost_usd) AS amount FROM cost_daily "
        "WHERE usage_date >= :start AND usage_date < :end AND charge_category <> 'Tax' "
        "GROUP BY service_code"
    ),
    "account": (
        "SELECT c.provider_account_id AS key, "
        "coalesce(max(a.name), c.provider_account_id) AS label, c.provider, "
        "sum(c.effective_cost_usd) AS amount FROM cost_daily c "
        "LEFT JOIN cloud_account a ON a.tenant_id = c.tenant_id "
        "AND a.provider = c.provider AND a.provider_account_id = c.provider_account_id "
        "WHERE c.usage_date >= :start AND c.usage_date < :end AND c.charge_category <> 'Tax' "
        "GROUP BY c.provider, c.provider_account_id"
    ),
    "region": (
        "SELECT coalesce(region_id, 'global') AS key, coalesce(region_id, 'Global') AS label, "
        "sum(effective_cost_usd) AS amount FROM cost_daily "
        "WHERE usage_date >= :start AND usage_date < :end AND charge_category <> 'Tax' "
        "GROUP BY coalesce(region_id, 'global'), coalesce(region_id, 'Global')"
    ),
}


def _top(
    rows: list[tuple[str, str, float, str | None]], limit: int
) -> tuple[float, list[BreakdownItem]]:
    rows = sorted((r for r in rows if round(r[2], 2) != 0), key=lambda r: -r[2])
    total = sum(r[2] for r in rows)
    head, tail = rows[:limit], rows[limit:]
    if tail:
        head.append(("other", f"{len(tail)} more", sum(r[2] for r in tail), None))
    share = (lambda v: round(v / total, 4)) if total else (lambda v: 0.0)
    return round(total, 2), [
        BreakdownItem(k, label, round(v, 2), share(v), provider) for k, label, v, provider in head
    ]


async def breakdown(
    session: AsyncSession, today: date, dimension: Dimension, limit: int = TOP_ITEMS
) -> Breakdown:
    """The month's spend (excluding tax) by one dimension: the top ``limit`` plus the rest."""
    start, end = _month_bounds(today)
    month = f"{start:%Y-%m}"
    if not await _has_cur_month(session, start, end):
        snapshots = await _snapshots(session, start)
        if snapshots:
            if dimension != "service":
                # Cost Explorer is only asked for services; the rest waits for the CUR.
                return Breakdown("cost_explorer", month, dimension, "USD", 0.0, [])
            services = _sum_series(snapshots, "by_service", "service")
            services.pop("Tax", None)
            total, items = _top([(k, k, v, None) for k, v in services.items()], limit)
            return Breakdown("cost_explorer", month, dimension, "USD", total, items)
        return Breakdown(None, month, dimension, "USD", 0.0, [])
    rows = await session.execute(text(_BREAKDOWN_SQL[dimension]), {"start": start, "end": end})
    total, items = _top(
        [(r.key, r.label, float(r.amount), getattr(r, "provider", None)) for r in rows], limit
    )
    return Breakdown("cur", month, dimension, "USD", total, items)


@dataclass(frozen=True)
class ScopeCost:
    account_id: str
    service_code: str
    service_name: str
    usage_date: date
    amount: float


async def data_range(session: AsyncSession, today: date) -> tuple[date, date] | None:
    """The first and latest days with bills (CUR), up to ``today``."""
    row = (
        await session.execute(
            text("SELECT min(usage_date), max(usage_date) FROM cost_daily WHERE usage_date <= :t"),
            {"t": today},
        )
    ).one()
    return None if row[0] is None else (row[0], row[1])


async def scope_daily(
    session: AsyncSession,
    first: date,
    last: date,
    account_id: str | None = None,
    service_code: str | None = None,
) -> list[ScopeCost]:
    """Daily spend per (account, service) from ``first`` to ``last``, for anomaly detection;
    optionally for one account and service. Usage and purchases, amortised: credits,
    refunds and tax would make a day look unusual for reasons that are not spend."""
    rows = await session.execute(
        text(
            "SELECT provider_account_id AS account_id, service_code, "
            "max(service_name) AS service_name, usage_date, "
            "sum(effective_cost_usd) AS amount FROM cost_daily "
            "WHERE usage_date BETWEEN :first AND :last "
            "AND charge_category IN ('Usage', 'Purchase') "
            "AND (CAST(:account AS text) IS NULL OR provider_account_id = CAST(:account AS text)) "
            "AND (CAST(:service AS text) IS NULL OR service_code = CAST(:service AS text)) "
            "GROUP BY provider_account_id, service_code, usage_date"
        ),
        {"first": first, "last": last, "account": account_id, "service": service_code},
    )
    return [
        ScopeCost(
            r.account_id.strip(), r.service_code, r.service_name, r.usage_date, float(r.amount)
        )
        for r in rows
    ]


@dataclass(frozen=True)
class UsageCost:
    usage_type: str
    region: str | None
    usage_date: date
    amount: float


async def scope_usage_types(
    session: AsyncSession, account_id: str, service_code: str, days: list[date]
) -> list[UsageCost]:
    """Spend by usage type and region for one account and service on the given days."""
    rows = await session.execute(
        text(
            "SELECT coalesce(usage_type, 'Other') AS usage_type, region_id, usage_date, "
            "sum(effective_cost_usd) AS amount FROM cost_daily "
            "WHERE provider_account_id = :account AND service_code = :service "
            "AND usage_date = ANY(:days) AND charge_category IN ('Usage', 'Purchase') "
            "GROUP BY coalesce(usage_type, 'Other'), region_id, usage_date"
        ),
        {"account": account_id, "service": service_code, "days": days},
    )
    return [UsageCost(r.usage_type, r.region_id, r.usage_date, float(r.amount)) for r in rows]


@dataclass(frozen=True)
class ServiceTotal:
    service_code: str
    service_name: str
    amount: float


async def service_totals(session: AsyncSession, first: date, last: date) -> list[ServiceTotal]:
    """Spend (excluding tax) per service from ``first`` to ``last``, from the bills."""
    rows = await session.execute(
        text(
            "SELECT service_code, max(service_name) AS service_name, "
            "sum(effective_cost_usd) AS amount FROM cost_daily "
            "WHERE usage_date BETWEEN :first AND :last AND charge_category <> 'Tax' "
            "GROUP BY service_code"
        ),
        {"first": first, "last": last},
    )
    return [ServiceTotal(r.service_code, r.service_name, float(r.amount)) for r in rows]


@dataclass(frozen=True)
class PeriodTotals:
    """Spend from the bills over some days: per day (amortised, after credits, with tax,
    as the Overview's month to date) and the credits applied (zero or negative)."""

    by_day: dict[date, float]
    credits: float

    @property
    def total(self) -> float:
        return round(sum(self.by_day.values()), 2)


async def period_totals(session: AsyncSession, first: date, last: date) -> PeriodTotals:
    rows = await session.execute(
        text(
            "SELECT usage_date, sum(effective_cost_usd) AS amount, "
            "coalesce(sum(effective_cost_usd) FILTER (WHERE charge_category = 'Credit'), 0) "
            "AS credits FROM cost_daily WHERE usage_date BETWEEN :first AND :last "
            "GROUP BY usage_date"
        ),
        {"first": first, "last": last},
    )
    by_day: dict[date, float] = {}
    credits = 0.0
    for r in rows:
        by_day[r.usage_date] = float(r.amount)
        credits += float(r.credits)
    return PeriodTotals(by_day, round(credits, 2))


async def month_reconciliation(session: AsyncSession, month: date) -> Reconciliation | None:
    """The latest check of one month's bills against Cost Explorer (a mismatch first)."""
    return await _reconciliation(session, month, month)


@dataclass(frozen=True)
class CreditDay:
    usage_date: date
    credits: float  # credits applied that day, as a positive amount
    spend: float  # amortised spend before credits (usage, purchases, tax, adjustments)


async def credit_days(session: AsyncSession, first: date, last: date) -> list[CreditDay]:
    """Per day with bills from ``first`` to ``last``: credits applied (``Credit`` lines,
    which include refunds) and spend before them."""
    rows = await session.execute(
        text(
            "SELECT usage_date, "
            "coalesce(-sum(effective_cost_usd) FILTER (WHERE charge_category = 'Credit'), 0) "
            "AS credits, coalesce(sum(effective_cost_usd) FILTER "
            "(WHERE charge_category <> 'Credit'), 0) AS spend FROM cost_daily "
            "WHERE usage_date BETWEEN :first AND :last GROUP BY usage_date ORDER BY usage_date"
        ),
        {"first": first, "last": last},
    )
    return [CreditDay(r.usage_date, float(r.credits), float(r.spend)) for r in rows]


@dataclass(frozen=True)
class TrendDay:
    """One day of bills split for forecasting: ``spend`` (amortised, after credits),
    ``monthly`` (once-a-month charges billed on the 1st) and ``tax``."""

    usage_date: date
    spend: float
    monthly: float
    tax: float


_TREND_DAYS = """
WITH s AS (
    SELECT usage_date, provider_account_id, service_code, coalesce(usage_type, '') AS usage_type,
           charge_category, sum(effective_cost_usd) AS amount
    FROM cost_daily WHERE usage_date BETWEEN :first AND :last
    GROUP BY usage_date, provider_account_id, service_code, coalesce(usage_type, ''),
             charge_category
), k AS (
    SELECT s.*, count(*) OVER (PARTITION BY date_trunc('month', usage_date), provider_account_id,
                               service_code, usage_type, charge_category) AS n
    FROM s
)
SELECT usage_date,
       coalesce(sum(amount) FILTER (WHERE charge_category = 'Tax'), 0) AS tax,
       coalesce(sum(amount) FILTER (
           WHERE charge_category NOT IN ('Tax', 'Credit') AND n = 1
             AND extract(day FROM usage_date) = 1 AND abs(amount) >= :min
             AND (date_trunc('month', usage_date) < date_trunc('month', CAST(:last AS date))
                  OR CAST(:last AS date) - usage_date >= 7)
       ), 0) AS monthly,
       sum(amount) AS total
FROM k GROUP BY usage_date ORDER BY usage_date
"""


async def trend_days(session: AsyncSession, first: date, last: date) -> list[TrendDay]:
    """Days with bills from ``first`` to ``last``, split for forecasting. A charge counts
    as monthly when it is the only one of its kind (account, service, usage type, charge
    category) in its month, on the 1st, and at least USD 1 (in the latest month, only once
    a week of bills has followed without it)."""
    rows = await session.execute(
        text(_TREND_DAYS), {"first": first, "last": last, "min": MONTHLY_CHARGE_MIN_USD}
    )
    return [
        TrendDay(
            r.usage_date,
            float(r.total) - float(r.tax) - float(r.monthly),
            float(r.monthly),
            float(r.tax),
        )
        for r in rows
    ]


@dataclass(frozen=True)
class ChangeRow:
    """Usage and purchases for one service, region and usage type over a period."""

    current: bool  # the current period, else the one compared with
    service_code: str
    service_name: str
    region: str | None
    usage_type: str | None
    amount: float


async def change_rows(
    session: AsyncSession, current: tuple[date, date], previous: tuple[date, date]
) -> list[ChangeRow]:
    """Spend by service, region and usage type in two periods, for what changed (usage and
    purchases, amortised: credits, refunds and tax are not what drives a change)."""
    rows = await session.execute(
        text(
            "SELECT usage_date >= :cur_first AS current, service_code, "
            "max(service_name) AS service_name, region_id, usage_type, "
            "sum(effective_cost_usd) AS amount FROM cost_daily "
            "WHERE (usage_date BETWEEN :cur_first AND :cur_last "
            "OR usage_date BETWEEN :prev_first AND :prev_last) "
            "AND charge_category IN ('Usage', 'Purchase') "
            "GROUP BY usage_date >= :cur_first, service_code, region_id, usage_type"
        ),
        {
            "cur_first": current[0],
            "cur_last": current[1],
            "prev_first": previous[0],
            "prev_last": previous[1],
        },
    )
    return [
        ChangeRow(
            bool(r.current),
            r.service_code,
            r.service_name,
            r.region_id,
            r.usage_type,
            float(r.amount),
        )
        for r in rows
    ]
