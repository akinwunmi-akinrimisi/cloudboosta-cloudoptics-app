"""Exchange-rate lookup, staff rate entry and FX decomposition (PRD FR-FX-01..05, -07).

Rates are local currency units per 1 USD. Lookup rules:

- A manual rate always wins (FR-FX-02). The rate for the charge date is used; when that
  date has none, the latest earlier manual rate is carried forward for at most five days
  and flagged estimated (FR-FX-04).
- The reference feed is used only for a currency that has never had a manual rate, and is
  always flagged estimated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.platform.ids import uuid7

Basis = Literal["official", "market"]
Source = Literal["manual", "feed"]
MAX_CARRY_FORWARD = timedelta(days=5)
WARN_CHANGE = 0.05
SECOND_ADMIN_CHANGE = 0.10


@dataclass(frozen=True)
class Quote:
    currency: str
    rate: float
    rate_date: date
    basis: Basis
    source: Source
    estimated: bool


@dataclass(frozen=True)
class LargeRateChangeError(Exception):
    """The new rate moves more than 5% from the previous rate or the feed (FR-FX-07)."""

    compared_with: Literal["previous", "feed"]
    reference: float
    change: float

    def __str__(self) -> str:
        return (
            f"The new rate differs from the {self.compared_with} rate ({self.reference:g}) by "
            f"{self.change:+.1%}. Check it and resend with confirm set to true."
        )


@dataclass(frozen=True)
class FxDecomposition:
    """Month-over-month local change = usage effect + FX effect (FR-FX-05)."""

    usage_effect: float
    fx_effect: float
    total: float


def decompose(
    prev_usd: float, prev_rate: float, cur_usd: float, cur_rate: float
) -> FxDecomposition:
    usage = (cur_usd - prev_usd) * prev_rate
    fx = cur_usd * (cur_rate - prev_rate)
    return FxDecomposition(round(usage, 2), round(fx, 2), round(usage + fx, 2))


_LATEST = (
    "SELECT rate, rate_date, basis, source FROM fx_rate WHERE currency = :ccy "
    "AND source = :source AND (CAST(:basis AS text) IS NULL OR basis = :basis) "
    "AND rate_date <= :day "
    "ORDER BY rate_date DESC, created_at DESC LIMIT 1"
)


async def _latest(
    session: AsyncSession, currency: str, day: date, source: Source, basis: Basis | None
) -> Any:
    return (
        await session.execute(
            text(_LATEST), {"ccy": currency, "source": source, "basis": basis, "day": day}
        )
    ).one_or_none()


def _quote(currency: str, row: Any, day: date, *, estimated: bool) -> Quote | None:
    if row is None or day - row.rate_date > MAX_CARRY_FORWARD:
        return None
    return Quote(
        currency=currency,
        rate=float(row.rate),
        rate_date=row.rate_date,
        basis=row.basis,
        source=row.source,
        estimated=estimated or row.rate_date != day,
    )


async def quote(
    session: AsyncSession, currency: str, day: date, basis: Basis = "official"
) -> Quote | None:
    """The rate to convert USD on ``day`` (or ``None`` when no usable rate exists)."""
    if currency == "USD":
        return Quote("USD", 1.0, day, basis, "manual", estimated=False)
    manual = await _latest(session, currency, day, "manual", basis)
    if manual is not None:
        return _quote(currency, manual, day, estimated=False)
    ever_manual = (
        await session.execute(
            text("SELECT EXISTS (SELECT 1 FROM fx_rate WHERE currency = :c AND source = 'manual')"),
            {"c": currency},
        )
    ).scalar_one()
    if ever_manual:
        return None
    feed = await _latest(session, currency, day, "feed", None)
    return _quote(currency, feed, day, estimated=True)


async def history(session: AsyncSession, currency: str, start: date, end: date) -> list[Any]:
    return list(
        await session.execute(
            text(
                "SELECT r.currency, r.rate_date, r.basis, r.rate, r.source, r.source_note, "
                "r.reason, u.email AS entered_by, r.created_at FROM fx_rate r "
                "LEFT JOIN app_user u ON u.id = r.entered_by WHERE r.currency = :c "
                "AND r.rate_date BETWEEN :start AND :end ORDER BY r.rate_date, r.created_at"
            ),
            {"c": currency, "start": start, "end": end},
        )
    )


def _change(new: float, old: float) -> float:
    return new / old - 1


async def set_rate(
    session: AsyncSession,
    *,
    actor: Actor,
    currency: str,
    rate: Decimal,
    rate_date: date,
    basis: Basis,
    source_note: str | None,
    reason: str | None,
    confirm: bool,
) -> Quote:
    """Record a manual rate. Raises ``LargeRateChangeError`` for a move over 5% unless confirmed.

    Changes over 10% are recorded with ``second_approval_required`` in the audit log; the
    two-person approval flow itself arrives with the admin console (WBS ADM-10).
    """
    previous = await _latest(session, currency, rate_date, "manual", basis)
    feed = await _latest(session, currency, rate_date, "feed", None)
    change = 0.0
    for kind, row in (("previous", previous), ("feed", feed)):
        if row is None:
            continue
        delta = _change(float(rate), float(row.rate))
        if abs(delta) > abs(change):
            change = delta
        if abs(delta) > WARN_CHANGE and not confirm:
            raise LargeRateChangeError(kind, float(row.rate), delta)  # type: ignore[arg-type]

    await session.execute(
        text(
            "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, source_note, "
            "reason, entered_by) VALUES (:id, :c, :d, :basis, :rate, 'manual', :note, "
            ":reason, :by)"
        ),
        {
            "id": uuid7(),
            "c": currency,
            "d": rate_date,
            "basis": basis,
            "rate": rate,
            "note": source_note,
            "reason": reason,
            "by": actor.id,
        },
    )
    await audit.record(
        session,
        tenant_id=None,
        actor=actor,
        action="fx_rate.set",
        target_type="fx_rate",
        target_id=f"{currency}:{basis}:{rate_date.isoformat()}",
        before={"rate": str(previous.rate), "rate_date": previous.rate_date} if previous else None,
        after={
            "rate": str(rate),
            "rate_date": rate_date,
            "source_note": source_note,
            "reason": reason,
            "change": round(change, 4),
            "second_approval_required": abs(change) > SECOND_ADMIN_CHANGE,
        },
    )
    return Quote(currency, float(rate), rate_date, basis, "manual", estimated=False)


async def store_feed(session: AsyncSession, rates: dict[str, float], day: date, note: str) -> int:
    """Append one day's reference rates (idempotent per currency and day)."""
    stored = set(
        (
            await session.execute(
                text("SELECT currency FROM fx_rate WHERE rate_date = :d AND source = 'feed'"),
                {"d": day},
            )
        ).scalars()
    )
    rows = [
        {"id": uuid7(), "c": ccy, "d": day, "rate": rate, "note": note}
        for ccy, rate in sorted(rates.items())
        if rate > 0 and ccy not in stored
    ]
    if rows:
        await session.execute(
            text(
                "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, source_note) "
                "VALUES (:id, :c, :d, 'market', :rate, 'feed', :note)"
            ),
            rows,
        )
    return len(rows)
