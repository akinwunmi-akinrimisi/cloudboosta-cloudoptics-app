"""Weekly digest (PRD US-6.4, FR-NOT-02/03/06, docs/09 §2.4 ``digest.weekly``).

Every Monday from 07:00 in the workspace's time zone, each member who has not turned it
off gets one email about the week before (Monday to Sunday, AWS charge days in UTC):
spend against the week before and the services that changed most, open recommendations
(the best five, and what is new), savings marked done, budgets this month, and the week's
anomalies. Amounts are in USD, with the workspace currency beside them when it has a rate.

Each member gets their own email, at most once a week (``notification`` key
``digest:<week start>:<email>``; a failed send is tried again at the next hourly run that
Monday). A workspace without bills for the week gets no digest.
"""

from __future__ import annotations

import html
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.anomalies import service as anomalies
from cloudoptics.budgets import service as budgets
from cloudoptics.credits import service as credits
from cloudoptics.fx import service as fx
from cloudoptics.ingestion.queries import data_range, service_totals
from cloudoptics.notifications import preferences
from cloudoptics.notifications.email import Email, EmailError, get_mailer
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.recommendations import queries as recommendations

log = logging.getLogger(__name__)

SEND_HOUR = 7
TOP_CHANGES = 3
BUDGET_STATUS = {
    "on_track": "on track",
    "at_risk": "forecast over budget",
    "over": "over budget",
    "no_data": "no bills yet this month",
    "no_rate": "no exchange rate yet",
}


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def week_before(local_day: date) -> date:
    """The Monday of the full week before the one ``local_day`` is in."""
    return local_day - timedelta(days=local_day.weekday() + 7)


def due(now: datetime, tz: ZoneInfo) -> date | None:
    """The week to send now (its Monday), or ``None`` outside Monday 07:00-24:00 local."""
    local = now.astimezone(tz)
    if local.weekday() != 0 or local.hour < SEND_HOUR:
        return None
    return week_before(local.date())


@dataclass(frozen=True)
class Change:
    service_name: str
    this_week: float
    last_week: float

    @property
    def delta(self) -> float:
        return self.this_week - self.last_week


@dataclass(frozen=True)
class Digest:
    workspace: str
    week_start: date
    week_end: date
    data_through: date
    currency: str
    rate: float | None
    spend: float
    previous: float
    changes: list[Change]
    recommendations: recommendations.RecommendationPeriod
    budgets: list[budgets.BudgetLine]
    anomalies: anomalies.AnomalyPeriod
    credits: credits.Runway

    @property
    def change_percent(self) -> float | None:
        if self.previous < 0.005:
            return None
        return round(100 * (self.spend - self.previous) / self.previous, 1)

    def money(self, usd: float) -> str:
        text_ = f"USD {usd:,.2f}"
        if self.rate is not None and self.currency != "USD":
            text_ += f" ({self.currency} {usd * self.rate:,.0f})"
        return text_


async def build(session: AsyncSession, tenant_id: uuid.UUID, week_start: date) -> Digest | None:
    """The digest for the week starting ``week_start``; ``None`` without bills for it.
    Inside a tenant-scoped transaction."""
    week_end = week_start + timedelta(days=6)
    span = await data_range(session, week_end)
    if span is None or span[1] < week_start:
        return None
    tenant = (
        await session.execute(
            text("SELECT name, display_currency FROM tenant WHERE id = :t"), {"t": tenant_id}
        )
    ).one()
    this_week = {s.service_code: s for s in await service_totals(session, week_start, week_end)}
    last_week = {
        s.service_code: s
        for s in await service_totals(
            session, week_start - timedelta(days=7), week_start - timedelta(days=1)
        )
    }
    changes = [
        Change(
            (this_week.get(code) or last_week[code]).service_name,
            this_week[code].amount if code in this_week else 0.0,
            last_week[code].amount if code in last_week else 0.0,
        )
        for code in set(this_week) | set(last_week)
    ]
    changes = [c for c in changes if abs(c.delta) >= 0.01]
    changes.sort(key=lambda c: -abs(c.delta))
    currency = tenant.display_currency.strip()
    quote = await fx.quote(session, currency, week_end)
    start = datetime.combine(week_start, time(), UTC)
    return Digest(
        workspace=tenant.name,
        week_start=week_start,
        week_end=week_end,
        data_through=min(span[1], week_end),
        currency=currency,
        rate=quote.rate if quote else None,
        spend=round(sum(s.amount for s in this_week.values()), 2),
        previous=round(sum(s.amount for s in last_week.values()), 2),
        changes=changes[:TOP_CHANGES],
        recommendations=await recommendations.period(session, start, start + timedelta(days=7)),
        budgets=await budgets.active_budgets(session, week_end + timedelta(days=1)),
        anomalies=await anomalies.period(session, week_start, week_end),
        credits=await credits.runway(session, week_end),
    )


def _days(d: Digest) -> str:
    return f"{d.week_start:%d %b} to {d.week_end:%d %b %Y}"


def render(d: Digest, app_url: str) -> tuple[str, str, str]:
    """Subject, text and HTML."""
    app = app_url.rstrip("/")
    pct = d.change_percent
    trend = "" if pct is None else f" ({pct:+.0f}% on the week before)"
    subject = f"Your AWS week: {d.money(d.spend)} spent{trend}"
    sections: list[tuple[str, list[str], str]] = []

    spend = [f"Spent {d.money(d.spend)}, against {d.money(d.previous)} the week before."]
    if d.data_through < d.week_end:
        spend.append(f"Bills so far cover up to {d.data_through:%a %d %b}.")
    spend += [
        f"{c.service_name}: {'+' if c.delta >= 0 else '-'}{d.money(abs(c.delta))} "
        f"(now {d.money(c.this_week)})"
        for c in d.changes
    ]
    sections.append(("Spend", spend, f"{app}/costs"))

    r = d.recommendations
    recs = [
        f"{r.open_count} open, worth {d.money(r.open_savings_usd_month)} a month"
        + (f"; {r.new_count} new this week." if r.new_count else ".")
    ]
    recs += [
        f"{t.title}: {d.money(t.savings_usd_month)} a month, {t.risk_level} risk, effort "
        f"{t.effort}{' (new)' if t.new else ''}"
        for t in r.top
    ]
    if r.done_count:
        recs.append(
            f"Marked done this week: {r.done_count}, saving {d.money(r.done_savings_usd_month)}"
            " a month."
        )
    sections.append(("Recommendations", recs, f"{app}/recommendations"))

    if d.budgets:
        lines = []
        for b in d.budgets:
            ev = b.evaluation
            line = f"{b.name} ({b.currency} {b.amount:,.0f}): {BUDGET_STATUS[ev.status]}"
            if ev.actual_percent is not None:
                line += f", {ev.actual_percent:.0f}% spent"
            if ev.forecast_percent is not None:
                line += f", forecast {ev.forecast_percent:.0f}%"
            lines.append(line)
        sections.append(("Budgets this month", lines, f"{app}/budgets"))

    a = d.anomalies
    if a.count or a.open_count:
        lines = []
        if a.count:
            days = "day" if a.count == 1 else "days"
            lines.append(f"{a.count} unusual {days}, {d.money(a.impact_usd)} above normal.")
        if a.open_count:
            lines.append(f"{a.open_count} still open.")
        sections.append(("Anomalies", lines, f"{app}/anomalies"))

    runway = credits.describe(d.credits, d.money)
    if runway:
        sections.append(("AWS credits", [runway], f"{app}/costs"))

    settings_link = f"{app}/settings"
    footer = (
        f"You get this weekly digest as a member of {d.workspace} on Cloudoptics. "
        "Turn it off under Settings, Email notifications"
    )
    text_lines = [f"{d.workspace}: {_days(d)}", ""]
    for title, lines, link in sections:
        text_lines += [title, *[f"- {line}" for line in lines], f"  {link}", ""]
    text_lines += [f"{footer}: {settings_link}"]
    body = f"<p><strong>{html.escape(d.workspace)}</strong>: {html.escape(_days(d))}</p>"
    for title, lines, link in sections:
        items = "".join(f"<li>{html.escape(line)}</li>" for line in lines)
        body += (
            f"<h3>{html.escape(title)}</h3><ul>{items}</ul>"
            f'<p><a href="{html.escape(link)}">Open {html.escape(title.lower())}</a></p>'
        )
    body += (
        f'<p><small>{html.escape(footer)}: <a href="{html.escape(settings_link)}">'
        "notification settings</a>.</small></p>"
    )
    return subject, "\n".join(text_lines), body


def dedupe_key(week_start: date, email: str) -> str:
    return f"digest:{week_start.isoformat()}:{email}"


async def send(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> int:
    """Send this week's digest if it is due and not sent yet. Returns emails sent. Inside
    a tenant-scoped transaction."""
    tz_name = (
        await session.execute(text("SELECT time_zone FROM tenant WHERE id = :t"), {"t": tenant_id})
    ).scalar_one()
    week_start = due(now, zone(tz_name))
    if week_start is None:
        return 0
    people = await preferences.recipients(
        session, tenant_id, "weekly_digest", ("admin", "member", "viewer")
    )
    if not people:
        return 0
    done = {
        r.dedupe_key
        for r in await session.execute(
            text(
                "SELECT dedupe_key FROM notification WHERE dedupe_key LIKE :prefix "
                "AND status <> 'failed'"
            ),
            {"prefix": f"digest:{week_start.isoformat()}:%"},
        )
    }
    waiting = [p for p in people if dedupe_key(week_start, p.email) not in done]
    if not waiting:
        return 0
    digest = await build(session, tenant_id, week_start)
    if digest is None:
        return 0
    subject, body_text, body_html = render(digest, get_settings().app_url)
    sent = 0
    for person in waiting:
        status, error = "sent", None
        try:
            await get_mailer().send(
                Email([person.email], subject, body_text, body_html, tag="weekly-digest")
            )
            sent += 1
        except EmailError as exc:
            status, error = "failed", str(exc)
            log.warning("weekly digest for %s not sent: %s", tenant_id, exc)
        await session.execute(
            text(
                "INSERT INTO notification (id, tenant_id, kind, dedupe_key, recipients, "
                "subject, status, error) VALUES (:id, :t, 'weekly_digest', :k, :to, :subject, "
                ":status, :error) ON CONFLICT (tenant_id, dedupe_key) DO UPDATE SET "
                "subject = EXCLUDED.subject, status = EXCLUDED.status, error = EXCLUDED.error, "
                "created_at = now()"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "k": dedupe_key(week_start, person.email),
                "to": [person.email],
                "subject": subject,
                "status": status,
                "error": error,
            },
        )
    return sent
