"""Credits runway (PRD US-2.4, FR-ANA-11).

- Burn: the average daily credits applied over the last 30 days of bills (fewer when the
  bills start later), from ``Credit`` lines (AWS counts refunds there too).
- Remaining: the newest balance an admin entered, less the credits applied on the days of
  bills after the day it was read; never below zero.
- Depletion: the latest day of bills plus remaining / burn; when the credits expire first,
  the expiry date is the end of the runway and the rest is lost.
- Spend after credits: the average daily spend before credits over the same days, for a
  30-day month, which is what the bill becomes once credits run out.

Runway under 90 days is ``ending_soon`` (shown as a warning, in the weekly digest and the
monthly report).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.ingestion.queries import credit_days, data_range

BURN_DAYS = 30
WARN_DAYS = 90
Status = Literal["no_credits", "no_balance", "ok", "ending_soon", "used_up"]


@dataclass(frozen=True)
class Balance:
    amount_usd: float
    as_of: date
    expires_on: date | None
    note: str | None
    entered_by: str | None
    created_at: Any


@dataclass(frozen=True)
class Runway:
    status: Status
    data_through: date | None
    applied_month_usd: float
    burn_usd_day: float
    burn_days: int
    spend_before_credits_usd_month: float
    balance: Balance | None
    used_since_balance_usd: float | None
    remaining_usd: float | None
    days_left: int | None
    depletion_date: date | None
    expires_first: bool  # the credits expire before they would run out
    daily: list[tuple[date, float]]  # credits applied per day, last 30 days


async def latest_balance(session: AsyncSession) -> Balance | None:
    row = (
        await session.execute(
            text(
                "SELECT b.amount_usd, b.as_of, b.expires_on, b.note, b.created_at, "
                "coalesce(u.display_name, u.email) AS entered_by FROM credit_balance b "
                "LEFT JOIN app_user u ON u.id = b.entered_by "
                "ORDER BY b.created_at DESC LIMIT 1"
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return Balance(
        float(row.amount_usd), row.as_of, row.expires_on, row.note, row.entered_by, row.created_at
    )


async def runway(session: AsyncSession, today: date) -> Runway:
    """Inside a tenant-scoped transaction."""
    balance = await latest_balance(session)
    span = await data_range(session, today)
    if span is None:
        return Runway(
            "no_balance" if balance else "no_credits", None, 0.0, 0.0, 0, 0.0, balance,
            None, balance.amount_usd if balance else None, None, None, False, [],
        )  # fmt: skip
    first, last = span
    window_start = max(first, last - timedelta(days=BURN_DAYS - 1))
    since = min(window_start, balance.as_of + timedelta(days=1)) if balance else window_start
    days = await credit_days(session, min(since, last.replace(day=1)), last)
    window = [d for d in days if d.usage_date >= window_start]
    covered = (last - window_start).days + 1
    burn = sum(d.credits for d in window) / covered
    spend_month = sum(d.spend for d in window) / covered * 30
    applied_month = sum(d.credits for d in days if d.usage_date >= last.replace(day=1))
    by_day = {d.usage_date: d.credits for d in window}
    daily = [
        (day, round(by_day.get(day, 0.0), 2))
        for day in (window_start + timedelta(days=i) for i in range(covered))
    ]

    used = remaining = None
    days_left = depletion = None
    expires_first = False
    if balance is not None:
        used = sum(d.credits for d in days if d.usage_date > balance.as_of)
        remaining = max(balance.amount_usd - used, 0.0)
        if burn > 0.005:
            days_left = math.floor(remaining / burn)
            depletion = last + timedelta(days=days_left)
        expiry = balance.expires_on
        if expiry and remaining > 0.005 and (depletion is None or expiry < depletion):
            expires_first = True
            depletion = expiry
            days_left = max((expiry - last).days, 0)

    status: Status
    if balance is None:
        status = "no_balance" if burn > 0.005 or applied_month > 0.005 else "no_credits"
    elif remaining is not None and remaining <= 0.005:
        status = "used_up"
    elif days_left is not None and days_left < WARN_DAYS:
        status = "ending_soon"
    else:
        status = "ok"
    return Runway(
        status=status,
        data_through=last,
        applied_month_usd=round(applied_month, 2),
        burn_usd_day=round(burn, 2),
        burn_days=covered,
        spend_before_credits_usd_month=round(spend_month, 2),
        balance=balance,
        used_since_balance_usd=None if used is None else round(used, 2),
        remaining_usd=None if remaining is None else round(remaining, 2),
        days_left=days_left,
        depletion_date=depletion,
        expires_first=expires_first,
        daily=daily,
    )


def describe(r: Runway, money: Callable[[float], str]) -> str | None:
    """One sentence about the runway for the digest and the monthly report, or ``None``
    without credits."""
    if r.status == "no_credits":
        return None
    after = f"the bill is then about {money(r.spend_before_credits_usd_month)} a month"
    if r.status == "no_balance":
        return (
            f"{money(r.applied_month_usd)} of credits applied this month. Enter the remaining "
            "balance from the AWS Billing console to see when they run out."
        )
    if r.status == "used_up":
        return f"Credits are used up: {after}."
    if r.days_left is None or r.depletion_date is None:
        return f"{money(r.remaining_usd or 0)} of credits left; none applied in the last 30 days."
    end = "expire" if r.expires_first else "run out"
    text_ = (
        f"{money(r.remaining_usd or 0)} of credits left at {money(r.burn_usd_day)} a day: "
        f"they {end} around {r.depletion_date:%d %b %Y} (about {r.days_left} days); {after}."
    )
    return f"Ending soon. {text_}" if r.status == "ending_soon" else text_
