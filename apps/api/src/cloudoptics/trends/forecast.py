"""Forecasts (PRD FR-ANA-09, US-3.2).

- This month: the Overview's month-end forecast (``ingestion.queries.month_end_forecast``):
  month to date + the last 7 days' average daily spend x the days left + the known
  monthly charges not billed yet.
- The next three months: a straight-line trend fitted to the last 90 days of daily spend
  (least squares; once-a-month charges and tax left out, then added back at last month's
  amounts and the window's tax share). Bounded: no projected day goes below half or above
  one and a half times the last 30 days' average, nor below zero. With under 28 days of
  bills there is no trend, and the run rate carries on flat.
- Range: about 90% of the daily noise around the fit over the days forecast
  (1.645 x residual standard deviation x the square root of the days), widened with each
  month ahead; at least 5% a month ahead and at most 50%.

Amounts are USD. The workspace currency uses today's rate (``fx.quote``: staff-set, carried
forward up to five days and flagged estimated); the page applies a scenario to that rate.
Last month against the month before is split into usage and exchange-rate effects with
each day converted at its own rate (FR-FX-05).
"""

from __future__ import annotations

import calendar
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.fx import service as fx
from cloudoptics.ingestion import queries

TREND_DAYS = 90
MIN_TREND_DAYS = 28
LEVEL_DAYS = 30
BOUND = 0.5
Z = 1.645
MIN_SPREAD = 0.05
MAX_SPREAD = 0.5
MONTHS_AHEAD = 3

Method = Literal["trend", "run_rate"]


def month_start(day: date) -> date:
    return day.replace(day=1)


def next_month(month: date) -> date:
    return (month + timedelta(days=32)).replace(day=1)


def month_days(month: date) -> int:
    return calendar.monthrange(month.year, month.month)[1]


@dataclass(frozen=True)
class Fit:
    """``daily(t)`` = intercept + slope x t, for day ``t`` counted from the first day of
    the fitted series; ``sigma`` is the residual standard deviation."""

    intercept: float
    slope: float
    sigma: float
    level: float  # the last 30 days' average, which bounds the projection
    method: Method

    def daily(self, t: int) -> float:
        value = self.intercept + self.slope * t
        if self.method == "trend":
            value = min(max(value, self.level * (1 - BOUND)), self.level * (1 + BOUND))
        return max(value, 0.0)


def fit(values: Sequence[float], run_rate: float) -> Fit:
    """A bounded straight line through ``values`` (one per day, oldest first), or the flat
    ``run_rate`` when there are fewer than 28 days."""
    n = len(values)
    level = sum(values[-LEVEL_DAYS:]) / min(n, LEVEL_DAYS) if n else run_rate
    if n < MIN_TREND_DAYS:
        sigma = _sd([v - run_rate for v in values]) if n >= 2 else 0.0
        return Fit(run_rate, 0.0, sigma, level, "run_rate")
    mean_t = (n - 1) / 2
    mean_v = sum(values) / n
    sxx = sum((t - mean_t) ** 2 for t in range(n))
    slope = sum((t - mean_t) * (v - mean_v) for t, v in enumerate(values)) / sxx
    intercept = mean_v - slope * mean_t
    residuals = [v - (intercept + slope * t) for t, v in enumerate(values)]
    return Fit(intercept, slope, _sd(residuals), level, "trend")


def _sd(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def spread(amount: float, sigma: float, days: int, ahead: int) -> float:
    """Half the range for a month ``ahead`` months out (0 for this month)."""
    noise = Z * sigma * math.sqrt(days) * math.sqrt(max(ahead, 1))
    if ahead == 0:
        return round(noise, 2)
    size = abs(amount)
    return round(min(max(noise, MIN_SPREAD * ahead * size), MAX_SPREAD * size), 2)


@dataclass(frozen=True)
class MonthForecast:
    month: date
    amount: float
    low: float
    high: float
    method: Method


@dataclass(frozen=True)
class Decomposition:
    """Last month against the month before, in the workspace currency (FR-FX-05)."""

    month: date
    previous_month: date
    usd: float
    previous_usd: float
    local: float | None
    previous_local: float | None
    rate: float | None  # month average, local per USD
    previous_rate: float | None
    usage_effect: float | None
    fx_effect: float | None


@dataclass(frozen=True)
class Outlook:
    status: Literal["no_data", "ready"]
    currency: str
    basis: fx.Basis
    data_through: date | None
    quote: fx.Quote | None
    month_end: queries.Forecast | None
    month_end_month: date | None
    month_end_range: tuple[float, float] | None
    months: list[MonthForecast]
    monthly_charges_usd: float  # once-a-month charges included in each month ahead
    tax_share: float  # tax as a share of the rest, added to each month ahead
    trend_days: int
    decomposition: Decomposition | None


async def outlook(session: AsyncSession, tenant_id: uuid.UUID, today: date) -> Outlook:
    """Inside a tenant-scoped transaction."""
    tenant = (
        await session.execute(
            text("SELECT display_currency, fx_rate_basis FROM tenant WHERE id = :t"),
            {"t": tenant_id},
        )
    ).one()
    currency, basis = tenant.display_currency.strip(), tenant.fx_rate_basis
    quote = await fx.quote(session, currency, today, basis)
    span = await queries.data_range(session, today)
    if span is None:
        return Outlook(
            "no_data", currency, basis, None, quote, None, None, None, [], 0.0, 0.0, 0, None
        )
    first, last = span
    start = month_start(last)
    effective = (await queries.period_totals(session, start, last)).total
    month_end = await queries.month_end_forecast(session, start, last, effective)

    days = await queries.trend_days(
        session, max(first, last - timedelta(days=TREND_DAYS - 1)), last
    )
    by_day = {d.usage_date: d for d in days}
    window_first = max(first, last - timedelta(days=TREND_DAYS - 1))
    span_days = (last - window_first).days + 1
    series = [
        by_day[d].spend if d in by_day else 0.0
        for d in (window_first + timedelta(days=i) for i in range(span_days))
    ]
    line = fit(series, month_end.daily_run_rate)
    charges = await queries.monthly_charges(session, start, last, last)
    monthly = sum(c.expected for c in charges)
    if not charges:
        # Last month had none; take the ones this month's bills already show.
        monthly = sum(d.monthly for d in days if d.usage_date >= start)
    before_tax = sum(d.spend + d.monthly for d in days)
    tax_share = sum(d.tax for d in days) / before_tax if before_tax > 0.005 else 0.0

    remaining = month_days(start) - month_end.days_covered
    half = spread(month_end.amount, line.sigma, remaining, 0)
    months: list[MonthForecast] = []
    month = start
    for ahead in range(1, MONTHS_AHEAD + 1):
        month = next_month(month)
        n = month_days(month)
        offset = (month - window_first).days
        usd = (sum(line.daily(offset + i) for i in range(n)) + monthly) * (1 + tax_share)
        width = spread(usd, line.sigma, n, ahead)
        months.append(
            MonthForecast(
                month,
                round(usd, 2),
                round(max(usd - width, 0.0), 2),
                round(usd + width, 2),
                line.method,
            )
        )
    return Outlook(
        status="ready",
        currency=currency,
        basis=basis,
        data_through=last,
        quote=quote,
        month_end=month_end,
        month_end_month=start,
        month_end_range=(
            round(max(month_end.amount - half, 0.0), 2),
            round(month_end.amount + half, 2),
        ),
        months=months,
        monthly_charges_usd=round(monthly, 2),
        tax_share=round(tax_share, 4),
        trend_days=len(series),
        decomposition=await decomposition(session, currency, start, first),
    )


async def decomposition(
    session: AsyncSession, currency: str, this_month: date, first: date
) -> Decomposition | None:
    """The month before ``this_month`` against the one before that; ``None`` until the
    bills cover both."""
    month = month_start(this_month - timedelta(days=1))
    previous = month_start(month - timedelta(days=1))
    if first > previous:
        return None
    end = this_month - timedelta(days=1)
    cur = await queries.period_totals(session, month, end)
    prev = await queries.period_totals(session, previous, month - timedelta(days=1))
    cur_local = await fx.convert_days(session, currency, cur.by_day)
    prev_local = await fx.convert_days(session, currency, prev.by_day)
    split = (
        fx.decompose(prev.total, prev_local.rate, cur.total, cur_local.rate)
        if cur_local and prev_local
        else None
    )
    return Decomposition(
        month=month,
        previous_month=previous,
        usd=cur.total,
        previous_usd=prev.total,
        local=cur_local.local if cur_local else None,
        previous_local=prev_local.local if prev_local else None,
        rate=cur_local.rate if cur_local else None,
        previous_rate=prev_local.rate if prev_local else None,
        usage_effect=split.usage_effect if split else None,
        fx_effect=split.fx_effect if split else None,
    )
