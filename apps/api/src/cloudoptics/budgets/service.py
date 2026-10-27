"""Budget evaluation and alerts (PRD US-6.1/6.2, FR-NOT-04).

A budget is monthly, for the workspace or some of its accounts, in USD or a local currency.
Spend comes from the bills (CUR) in USD; a local-currency budget is converted with the
latest rate (FR-FX-03), and without a usable rate it is not evaluated (status ``no_rate``).

Each threshold (a percent of actual or of forecast spend) alerts at most once a month: the
``notification`` row ``budget:<id>:<kind>:<percent>:<YYYY-MM>`` records it. Thresholds
crossed in the same evaluation go out as one email.
"""

from __future__ import annotations

import html
import logging
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.fx import service as fx
from cloudoptics.ingestion.queries import month_spend
from cloudoptics.notifications.email import Email, EmailError, get_mailer
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

log = logging.getLogger(__name__)

Status = Literal["on_track", "at_risk", "over", "no_data", "no_rate"]
DEFAULT_THRESHOLDS = [
    {"kind": "actual", "percent": 50},
    {"kind": "actual", "percent": 80},
    {"kind": "actual", "percent": 100},
    {"kind": "forecast", "percent": 100},
]


@dataclass(frozen=True)
class Threshold:
    kind: Literal["actual", "forecast"]
    percent: int
    crossed: bool
    alerted: bool  # an alert went out (or was due) for it this month


@dataclass(frozen=True)
class Evaluation:
    period: date
    status: Status
    data_through: date | None
    rate: float | None  # budget currency per USD
    amount_usd: float | None
    actual_usd: float
    forecast_usd: float | None
    thresholds: list[Threshold]

    def in_budget_currency(self, usd: float | None) -> float | None:
        return None if usd is None or self.rate is None else round(usd * self.rate, 2)

    @property
    def actual_percent(self) -> float | None:
        if not self.amount_usd:
            return None
        return round(100 * self.actual_usd / self.amount_usd, 1)

    @property
    def forecast_percent(self) -> float | None:
        if not self.amount_usd or self.forecast_usd is None:
            return None
        return round(100 * self.forecast_usd / self.amount_usd, 1)


def dedupe_key(budget_id: uuid.UUID, kind: str, percent: int, period: date) -> str:
    return f"budget:{budget_id}:{kind}:{percent}:{period:%Y-%m}"


async def _alerted(session: AsyncSession, budget_id: uuid.UUID, period: date) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT dedupe_key FROM notification WHERE dedupe_key LIKE :prefix "
            "AND status <> 'failed'"
        ),
        {"prefix": f"budget:{budget_id}:%:{period:%Y-%m}"},
    )
    return {r.dedupe_key for r in rows}


async def evaluate(session: AsyncSession, budget: Any, today: date) -> Evaluation:
    """Inside a tenant-scoped transaction."""
    period = today.replace(day=1)
    scope = budget.scope or {}
    accounts = scope.get("accounts") if scope.get("type") == "accounts" else None
    spend = await month_spend(session, today, accounts)
    quote = await fx.quote(session, budget.currency.strip(), today)
    rate = quote.rate if quote else None
    amount_usd = round(float(budget.amount) / rate, 2) if rate else None
    alerted = await _alerted(session, budget.id, period)

    def crossed(kind: str, percent: int) -> bool:
        value = spend.effective if kind == "actual" else spend.forecast
        if amount_usd is None or value is None or spend.data_through is None:
            return False
        return value >= amount_usd * percent / 100

    thresholds = [
        Threshold(
            t["kind"],
            int(t["percent"]),
            crossed(t["kind"], int(t["percent"])),
            dedupe_key(budget.id, t["kind"], int(t["percent"]), period) in alerted,
        )
        for t in budget.thresholds
    ]
    status: Status
    if amount_usd is None:
        status = "no_rate"
    elif spend.data_through is None:
        status = "no_data"
    elif spend.effective >= amount_usd:
        status = "over"
    elif spend.forecast is not None and spend.forecast >= amount_usd:
        status = "at_risk"
    else:
        status = "on_track"
    return Evaluation(
        period=period,
        status=status,
        data_through=spend.data_through,
        rate=rate,
        amount_usd=amount_usd,
        actual_usd=spend.effective,
        forecast_usd=spend.forecast,
        thresholds=thresholds,
    )


def _money(amount: float | None, currency: str) -> str:
    return "unknown" if amount is None else f"{currency} {amount:,.2f}"


def budget_email(budget: Any, ev: Evaluation, due: list[Threshold]) -> tuple[str, str, str]:
    """Subject, text and HTML for thresholds just crossed."""
    ccy = budget.currency.strip()
    top = max(due, key=lambda t: (t.kind == "actual", t.percent))
    amount = _money(float(budget.amount), ccy)
    if top.kind == "actual":
        subject = f"Budget {budget.name}: {top.percent}% of {amount} spent"
    else:
        subject = f"Budget {budget.name}: forecast to reach {top.percent}% this month"
    link = f"{get_settings().app_url.rstrip('/')}/budgets"
    actual = _money(ev.in_budget_currency(ev.actual_usd), ccy)
    forecast = _money(ev.in_budget_currency(ev.forecast_usd), ccy)
    crossed = ", ".join(f"{t.percent}% of {t.kind}" for t in due)
    lines = [
        f"Budget: {budget.name} ({_money(float(budget.amount), ccy)} for {ev.period:%B %Y})",
        f"Spent so far: {actual} (USD {ev.actual_usd:,.2f}), data to {ev.data_through}",
        f"Forecast for the month: {forecast}",
        f"Crossed: {crossed}",
        "",
        f"See the budget: {link}",
    ]
    body = (
        f"<p>Budget <strong>{html.escape(budget.name)}</strong> "
        f"({html.escape(_money(float(budget.amount), ccy))} for {ev.period:%B %Y})</p>"
        f"<ul><li>Spent so far: {html.escape(actual)} (USD {ev.actual_usd:,.2f}), data to "
        f"{ev.data_through}</li><li>Forecast for the month: {html.escape(forecast)}</li>"
        f"<li>Crossed: {html.escape(crossed)}</li></ul>"
        f'<p><a href="{html.escape(link)}">See the budget</a></p>'
    )
    return subject, "\n".join(lines), body


async def recipients(session: AsyncSession, tenant_id: uuid.UUID, budget: Any) -> list[str]:
    """The budget's email addresses plus its users who are still members."""
    wanted = budget.recipients or {}
    users = [str(u) for u in wanted.get("users", [])]
    emails = {e.lower() for e in wanted.get("emails", [])}
    if users:
        rows = await session.execute(
            text(
                "SELECT u.email FROM membership m JOIN app_user u ON u.id = m.user_id "
                "WHERE m.tenant_id = :t AND CAST(m.user_id AS text) = ANY(:users)"
            ),
            {"t": tenant_id, "users": users},
        )
        emails |= {r.email.lower() for r in rows}
    return sorted(emails)


async def alert_budgets(session: AsyncSession, tenant_id: uuid.UUID, today: date) -> int:
    """Evaluate every active budget and email thresholds newly crossed. Returns emails sent."""
    sent = 0
    budgets = await session.execute(
        text(
            "SELECT id, name, scope, amount, currency, thresholds, recipients FROM budget "
            "WHERE is_active ORDER BY created_at"
        )
    )
    for budget in budgets.all():
        ev = await evaluate(session, budget, today)
        due = [t for t in ev.thresholds if t.crossed and not t.alerted]
        if not due:
            continue
        to = await recipients(session, tenant_id, budget)
        subject, body_text, body_html = budget_email(budget, ev, due)
        status, error = "skipped", None
        if to:
            try:
                await get_mailer().send(
                    Email(to, subject, body_text, body_html, tag="budget-alert")
                )
                status, sent = "sent", sent + 1
            except EmailError as exc:
                # Recorded as failed, so the next evaluation tries again.
                status, error = "failed", str(exc)
                log.warning("budget alert %s not sent: %s", budget.id, exc)
        for t in due:
            await session.execute(
                text(
                    "INSERT INTO notification (id, tenant_id, kind, dedupe_key, recipients, "
                    "subject, status, error) VALUES (:id, :t, 'budget_alert', :k, :to, "
                    ":subject, :status, :error) ON CONFLICT (tenant_id, dedupe_key) DO UPDATE "
                    "SET recipients = EXCLUDED.recipients, subject = EXCLUDED.subject, "
                    "status = EXCLUDED.status, error = EXCLUDED.error, created_at = now()"
                ),
                {
                    "id": uuid7(),
                    "t": tenant_id,
                    "k": dedupe_key(budget.id, t.kind, t.percent, ev.period),
                    "to": to,
                    "subject": subject,
                    "status": status,
                    "error": error,
                },
            )
    return sent
