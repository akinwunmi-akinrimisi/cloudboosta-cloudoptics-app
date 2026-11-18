"""What changed (PRD FR-ANA-12).

Month to date (the 1st up to the latest day of bills) against the same days last month
(cut at that month's end), by service and region, from usage and purchases (amortised;
credits, refunds and tax are left out). The three biggest rises and the three biggest
falls of at least USD 1 each get a sentence:
"Amazon EC2 in eu-west-1 rose USD 120.00 (+40%) mainly due to EUW1-BoxUsage:m5.large",
naming the usage type whose change in the same direction was largest.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.ingestion import queries

TOP = 3
MIN_CHANGE_USD = 1.0


@dataclass(frozen=True)
class Contributor:
    service_code: str
    service_name: str
    region: str | None
    usd: float
    previous_usd: float
    usage_type: str | None  # the usage type behind most of the change
    usage_type_change_usd: float

    @property
    def change_usd(self) -> float:
        return round(self.usd - self.previous_usd, 2)

    @property
    def change_percent(self) -> float | None:
        if abs(self.previous_usd) < 0.005:
            return None
        return round(100 * (self.usd - self.previous_usd) / self.previous_usd, 1)


@dataclass(frozen=True)
class WhatChanged:
    month: date
    current: tuple[date, date]
    previous: tuple[date, date]
    usd: float
    previous_usd: float
    rises: list[Contributor]
    falls: list[Contributor]

    @property
    def change_usd(self) -> float:
        return round(self.usd - self.previous_usd, 2)

    def top(self) -> Contributor | None:
        """The single biggest change either way."""
        both = self.rises[:1] + self.falls[:1]
        return max(both, key=lambda c: abs(c.change_usd)) if both else None


def periods(last: date) -> tuple[tuple[date, date], tuple[date, date]]:
    """Month to date up to ``last``, and the same days of the month before."""
    start = last.replace(day=1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end.replace(day=1)
    return (start, last), (prev_start, min(prev_start + (last - start), prev_end))


def contributors(rows: list[queries.ChangeRow]) -> list[Contributor]:
    """Every service and region whose spend changed by at least USD 1, biggest first."""
    groups: dict[tuple[str, str | None], dict[str | None, list[float]]] = {}
    names: dict[str, str] = {}
    for r in rows:
        by_type = groups.setdefault((r.service_code, r.region), {})
        sums = by_type.setdefault(r.usage_type, [0.0, 0.0])
        sums[0 if r.current else 1] += r.amount
        names[r.service_code] = r.service_name or names.get(r.service_code) or r.service_code
    out: list[Contributor] = []
    for (code, region), by_type in groups.items():
        usd = sum(s[0] for s in by_type.values())
        previous = sum(s[1] for s in by_type.values())
        change = usd - previous
        if abs(change) < MIN_CHANGE_USD:
            continue
        sign = 1 if change > 0 else -1
        usage_type, type_change = max(
            ((t, s[0] - s[1]) for t, s in by_type.items()), key=lambda item: sign * item[1]
        )
        out.append(
            Contributor(
                service_code=code,
                service_name=names[code],
                region=region,
                usd=round(usd, 2),
                previous_usd=round(previous, 2),
                usage_type=usage_type if sign * type_change > 0 else None,
                usage_type_change_usd=round(type_change, 2),
            )
        )
    return sorted(out, key=lambda c: -abs(c.change_usd))


async def what_changed(
    session: AsyncSession, through: date, limit: int = TOP
) -> WhatChanged | None:
    """Up to the latest day of bills on or before ``through``; ``None`` without bills.
    Inside a tenant-scoped transaction."""
    span = await queries.data_range(session, through)
    if span is None:
        return None
    current, previous = periods(span[1])
    rows = await queries.change_rows(session, current, previous)
    found = contributors(rows)
    return WhatChanged(
        month=current[0],
        current=current,
        previous=previous,
        usd=round(sum(r.amount for r in rows if r.current), 2),
        previous_usd=round(sum(r.amount for r in rows if not r.current), 2),
        rises=[c for c in found if c.change_usd > 0][:limit],
        falls=[c for c in found if c.change_usd < 0][:limit],
    )


def sentence(c: Contributor, money: Callable[[float], str]) -> str:
    """FR-ANA-12's template: "{service} in {region} rose {amount} ({pct}) mainly due to
    {usage type}"."""
    where = f"{c.service_name} in {c.region}" if c.region else c.service_name
    verb = "rose" if c.change_usd > 0 else "fell"
    pct = c.change_percent
    detail = "new this month" if pct is None else f"{pct:+.0f}%"
    text_ = f"{where} {verb} {money(abs(c.change_usd))} ({detail})"
    if c.usage_type:
        text_ += f" mainly due to {c.usage_type}"
    return text_ + "."


def headline(w: WhatChanged | None, money: Callable[[float], str]) -> str | None:
    """The biggest change as one sentence, for the weekly digest and the monthly report."""
    top = w.top() if w else None
    return None if top is None else sentence(top, money)
