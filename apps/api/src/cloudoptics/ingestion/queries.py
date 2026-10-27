"""``CostQueryService`` (docs/11b §2.7): read-side cost queries for the API. Callers run
inside a tenant-scoped transaction; row-level security limits every query to that tenant."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SummaryStatus = Literal["no_connection", "waiting", "provisional", "ready"]
Source = Literal["cur", "cost_explorer"]
Dimension = Literal["service", "account", "region"]
TOP_ITEMS = 8


@dataclass(frozen=True)
class Forecast:
    amount: float
    method: Literal["run_rate"]
    days_covered: int
    days_in_month: int


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
    days_in_month = calendar.monthrange(start.year, start.month)[1]
    covered = (last_day - start).days + 1
    daily = run_rate_base / covered
    return Forecast(
        amount=round(effective + daily * (days_in_month - covered), 2),
        method="run_rate",
        days_covered=covered,
        days_in_month=days_in_month,
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
                "SELECT coalesce(sum(effective_cost_usd), 0) AS effective, "
                "coalesce(sum(effective_cost_usd) FILTER (WHERE charge_category <> 'Tax'), 0) "
                "AS run_rate_base FROM cost_daily WHERE usage_date >= :start "
                "AND usage_date < :end AND (CAST(:accounts AS text[]) IS NULL "
                "OR account_id = ANY(CAST(:accounts AS text[])))"
            ),
            {"start": start, "end": end, "accounts": accounts},
        )
    ).one()
    effective = float(totals.effective)
    forecast = _run_rate(effective, float(totals.run_rate_base), start, last_day)
    return MonthSpend(round(effective, 2), forecast.amount, last_day)


async def summary(session: AsyncSession, today: date) -> CostSummary:
    """Month-to-date spend (amortised, after credits) and a run-rate month-end forecast.

    The forecast is the month-to-date total plus the average daily non-tax spend over the
    days with data, times the days left. Proper forecasting arrives with AN-12 (Sprint 5).
    """
    start, end = _month_bounds(today)
    prev_start, _ = _month_bounds(start - timedelta(days=1))
    month = f"{start:%Y-%m}"

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
        await session.execute(text("SELECT max(usage_date) FROM cost_daily"))
    ).scalar_one()
    if not await _has_cur_month(session, start, end):
        snapshots = await _snapshots(session, start)
        if snapshots:
            return _provisional(snapshots, start, month, data_through, last)
    base = {
        "source": "cur" if data_through is not None else None,
        "month": month,
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
                "AS credits, "
                "coalesce(sum(effective_cost_usd) FILTER (WHERE charge_category <> 'Tax'), 0) "
                "AS run_rate_base, max(usage_date) AS last_day "
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
    if totals.last_day is not None:
        forecast = _run_rate(
            float(totals.effective), float(totals.run_rate_base), start, totals.last_day
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
    snapshots: list[Any], start: date, month: str, data_through: date | None, last: Any
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
        forecast=_run_rate(effective, effective, start, through),
        previous_month=None,
    )


async def daily(session: AsyncSession, today: date, days: int = 30) -> DailyCosts:
    """Daily effective cost for the ``days`` days up to the latest data (or ``today``).

    Uses CUR data; while the current month has none, the month so far comes from the Cost
    Explorer snapshot instead.
    """
    start, end = _month_bounds(today)
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
    last = (
        await session.execute(
            text("SELECT max(usage_date) FROM cost_daily WHERE usage_date <= :t"), {"t": today}
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
        "SELECT c.account_id AS key, coalesce(max(a.name), c.account_id) AS label, "
        "sum(c.effective_cost_usd) AS amount FROM cost_daily c "
        "LEFT JOIN cloud_account a ON a.tenant_id = c.tenant_id "
        "AND a.aws_account_id = c.account_id "
        "WHERE c.usage_date >= :start AND c.usage_date < :end AND c.charge_category <> 'Tax' "
        "GROUP BY c.account_id"
    ),
    "region": (
        "SELECT coalesce(region_id, 'global') AS key, coalesce(region_id, 'Global') AS label, "
        "sum(effective_cost_usd) AS amount FROM cost_daily "
        "WHERE usage_date >= :start AND usage_date < :end AND charge_category <> 'Tax' "
        "GROUP BY coalesce(region_id, 'global'), coalesce(region_id, 'Global')"
    ),
}


def _top(rows: list[tuple[str, str, float]], limit: int) -> tuple[float, list[BreakdownItem]]:
    rows = sorted((r for r in rows if round(r[2], 2) != 0), key=lambda r: -r[2])
    total = sum(r[2] for r in rows)
    head, tail = rows[:limit], rows[limit:]
    if tail:
        head.append(("other", f"{len(tail)} more", sum(r[2] for r in tail)))
    share = (lambda v: round(v / total, 4)) if total else (lambda v: 0.0)
    return round(total, 2), [BreakdownItem(k, label, round(v, 2), share(v)) for k, label, v in head]


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
            total, items = _top([(k, k, v) for k, v in services.items()], limit)
            return Breakdown("cost_explorer", month, dimension, "USD", total, items)
        return Breakdown(None, month, dimension, "USD", 0.0, [])
    rows = await session.execute(text(_BREAKDOWN_SQL[dimension]), {"start": start, "end": end})
    total, items = _top([(r.key, r.label, float(r.amount)) for r in rows], limit)
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
            "SELECT account_id, service_code, max(service_name) AS service_name, usage_date, "
            "sum(effective_cost_usd) AS amount FROM cost_daily "
            "WHERE usage_date BETWEEN :first AND :last "
            "AND charge_category IN ('Usage', 'Purchase') "
            "AND (CAST(:account AS text) IS NULL OR account_id = CAST(:account AS text)) "
            "AND (CAST(:service AS text) IS NULL OR service_code = CAST(:service AS text)) "
            "GROUP BY account_id, service_code, usage_date"
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
            "WHERE account_id = :account AND service_code = :service "
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
