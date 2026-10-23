"""``CostQueryService`` (docs/11b §2.7): read-side cost queries for the API. Callers run
inside a tenant-scoped transaction; row-level security limits every query to that tenant."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

SummaryStatus = Literal["no_connection", "waiting", "ready"]


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
class CostSummary:
    status: SummaryStatus
    month: str
    currency: str
    data_through: date | None
    last_load_at: datetime | None
    last_load_status: str | None
    month_to_date: MonthToDate | None
    forecast: Forecast | None
    previous_month: float | None


def _month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    return start, (start + timedelta(days=32)).replace(day=1)


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
    base = {
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

    days_in_month = calendar.monthrange(start.year, start.month)[1]
    forecast = None
    if totals.last_day is not None:
        covered = (totals.last_day - start).days + 1
        daily = float(totals.run_rate_base) / covered
        forecast = Forecast(
            amount=round(float(totals.effective) + daily * (days_in_month - covered), 2),
            method="run_rate",
            days_covered=covered,
            days_in_month=days_in_month,
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
        **base,
    )
