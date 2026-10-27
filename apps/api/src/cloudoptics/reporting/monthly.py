"""Monthly executive report (PRD US-7.1, FR-FX-05, docs/09 §2.4 ``report.monthly``).

On the 3rd working day of each month from 06:00 in the workspace's time zone, the report
for the month before is made and stored (``monthly_report``), then emailed as HTML to
the members who have not turned it off and to the extra addresses an admin set. It holds:
total spend in USD and the workspace currency (each day converted at that day's rate,
with the month's average rate stated), the change against the month before split into
usage and exchange-rate effects, the five services that changed most, credits applied,
budget status, savings found and marked done, the five best open recommendations, the
month's anomalies and a data-accuracy note (how far the bills go, and the latest check
against Cost Explorer).

The stored figures do not change afterwards; an admin can make it again and send it now.
The Reports page shows it with a print layout, so the browser saves it as PDF.
"""

from __future__ import annotations

import calendar
import html
import json
import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.anomalies import service as anomalies
from cloudoptics.budgets import service as budgets
from cloudoptics.credits import service as credits
from cloudoptics.fx import service as fx
from cloudoptics.ingestion import queries as costs
from cloudoptics.notifications import preferences
from cloudoptics.notifications.email import Email, EmailError, get_mailer
from cloudoptics.platform.api import ApiModel
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.recommendations import queries as recommendations
from cloudoptics.reporting.digest import BUDGET_STATUS, zone

log = logging.getLogger(__name__)

SEND_HOUR = 6
WORKING_DAY = 3
TOP = 5


def month_start(day: date) -> date:
    return day.replace(day=1)


def previous_month(day: date) -> date:
    return month_start(month_start(day) - timedelta(days=1))


def month_end(month: date) -> date:
    return month.replace(day=calendar.monthrange(month.year, month.month)[1])


def working_day(month: date, n: int = WORKING_DAY) -> date:
    """The ``n``th Monday-to-Friday day of the month (public holidays are not counted)."""
    day, seen = month, 0
    while True:
        if day.weekday() < 5:
            seen += 1
            if seen == n:
                return day
        day += timedelta(days=1)


def due(now: datetime, tz_name: str) -> date | None:
    """The month whose report is due now (its first day): on the 3rd working day of the
    next month from 06:00 in the workspace's time zone (failed sends retry that day);
    ``None`` at any other time."""
    local = now.astimezone(zone(tz_name))
    if local.date() != working_day(month_start(local.date())) or local.hour < SEND_HOUR:
        return None
    return previous_month(local.date())


class Spend(ApiModel):
    usd: float
    local: float | None
    rate: float | None = Field(description="Month average, local per USD")
    previous_usd: float
    previous_local: float | None
    previous_rate: float | None
    change_usd: float
    change_percent: float | None
    #: Local change split into usage and exchange-rate effects (FR-FX-05).
    usage_effect: float | None
    fx_effect: float | None
    credits_usd: float = Field(description="Credits applied this month (zero or negative)")


class Driver(ApiModel):
    service_name: str
    usd: float
    previous_usd: float
    change_usd: float


class BudgetRow(ApiModel):
    name: str
    currency: str
    amount: float
    status: str
    actual: float | None
    actual_percent: float | None


class Savings(ApiModel):
    open_count: int
    open_usd_month: float
    new_count: int
    done_count: int
    done_usd_month: float


class TopRecommendation(ApiModel):
    title: str
    savings_usd_month: float
    risk_level: str
    effort: str


class AnomalySummary(ApiModel):
    count: int
    impact_usd: float


class Accuracy(ApiModel):
    data_through: date
    reconciliation: Literal["ok", "mismatch", "failed"] | None
    difference_percent: float | None
    checked_at: datetime | None
    note: str


class MonthlyReport(ApiModel):
    month: str  # YYYY-MM
    workspace: str
    currency: str
    generated_at: datetime
    spend: Spend
    drivers: list[Driver]
    budgets: list[BudgetRow]
    savings: Savings
    top_recommendations: list[TopRecommendation]
    anomalies: AnomalySummary
    #: The credits runway at the end of the month, when there are credits.
    credits: str | None = None
    accuracy: Accuracy


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)


async def build(session: AsyncSession, tenant_id: uuid.UUID, month: date) -> MonthlyReport | None:
    """The report for ``month`` (its first day) from the data now; ``None`` without bills
    for it. Inside a tenant-scoped transaction."""
    end = month_end(month)
    span = await costs.data_range(session, end)
    if span is None or span[1] < month:
        return None
    tenant = (
        await session.execute(
            text("SELECT name, display_currency FROM tenant WHERE id = :t"), {"t": tenant_id}
        )
    ).one()
    currency = tenant.display_currency.strip()
    prev = previous_month(month)
    this, last = (
        await costs.period_totals(session, month, end),
        await costs.period_totals(session, prev, month_end(prev)),
    )
    now_local = await fx.convert_days(session, currency, this.by_day)
    prev_local = await fx.convert_days(session, currency, last.by_day)
    split = (
        fx.decompose(last.total, prev_local.rate, this.total, now_local.rate)
        if now_local and prev_local
        else None
    )
    change = this.total - last.total
    services = {s.service_code: s for s in await costs.service_totals(session, month, end)}
    before = {s.service_code: s for s in await costs.service_totals(session, prev, month_end(prev))}
    drivers = [
        Driver(
            service_name=(services.get(code) or before[code]).service_name,
            usd=round(services[code].amount if code in services else 0.0, 2),
            previous_usd=round(before[code].amount if code in before else 0.0, 2),
            change_usd=round(
                (services[code].amount if code in services else 0.0)
                - (before[code].amount if code in before else 0.0),
                2,
            ),
        )
        for code in set(services) | set(before)
    ]
    drivers = sorted(
        (d for d in drivers if abs(d.change_usd) >= 0.01), key=lambda d: -abs(d.change_usd)
    )
    start = datetime.combine(month, time(), UTC)
    recs = await recommendations.period(
        session, start, datetime.combine(end + timedelta(days=1), time(), UTC), limit=TOP
    )
    found = await anomalies.period(session, month, end)
    check = await costs.month_reconciliation(session, month)
    through = min(span[1], end)
    notes = [f"From the AWS bills (CUR 2.0), amortised, after credits, up to {through:%d %b %Y}."]
    if through < end:
        notes.append("The last days of the month may still change as AWS finalises the bill.")
    if check is not None and check.status == "mismatch":
        notes.append(
            f"The bills differ from Cost Explorer by {check.difference_pct or 0:.1f}% for this "
            "month; Cloudoptics will report again once they agree."
        )
    elif check is not None and check.status == "ok":
        notes.append("The bills agree with Cost Explorer (within 1%).")
    if now_local is None and currency != "USD":
        notes.append(f"No {currency} exchange rate for every day of the month: USD only.")
    elif now_local is not None and currency != "USD":
        notes.append(
            f"Converted at each day's official rate; month average {currency} "
            f"{now_local.rate:,.2f} per USD."
        )
    return MonthlyReport(
        month=f"{month:%Y-%m}",
        workspace=tenant.name,
        currency=currency,
        generated_at=datetime.now(UTC),
        spend=Spend(
            usd=this.total,
            local=now_local.local if now_local else None,
            rate=now_local.rate if now_local else None,
            previous_usd=last.total,
            previous_local=prev_local.local if prev_local else None,
            previous_rate=prev_local.rate if prev_local else None,
            change_usd=round(change, 2),
            change_percent=round(100 * change / last.total, 1)
            if abs(last.total) >= 0.005
            else None,
            usage_effect=split.usage_effect if split else None,
            fx_effect=split.fx_effect if split else None,
            credits_usd=this.credits,
        ),
        drivers=drivers[:TOP],
        budgets=[
            BudgetRow(
                name=b.name,
                currency=b.currency,
                amount=b.amount,
                status=b.evaluation.status,
                actual=b.evaluation.in_budget_currency(b.evaluation.actual_usd),
                actual_percent=b.evaluation.actual_percent,
            )
            for b in await budgets.active_budgets(session, end)
        ],
        savings=Savings(
            open_count=recs.open_count,
            open_usd_month=recs.open_savings_usd_month,
            new_count=recs.new_count,
            done_count=recs.done_count,
            done_usd_month=recs.done_savings_usd_month,
        ),
        top_recommendations=[
            TopRecommendation(
                title=t.title,
                savings_usd_month=t.savings_usd_month,
                risk_level=t.risk_level,
                effort=t.effort,
            )
            for t in recs.top
        ],
        anomalies=AnomalySummary(count=found.count, impact_usd=found.impact_usd),
        credits=credits.describe(
            await credits.runway(session, end), lambda usd: _money(usd, None, currency)
        ),
        accuracy=Accuracy(
            data_through=through,
            reconciliation=check.status if check else None,
            difference_percent=_round(check.difference_pct) if check else None,
            checked_at=check.checked_at if check else None,
            note=" ".join(notes),
        ),
    )


async def store(
    session: AsyncSession, tenant_id: uuid.UUID, report: MonthlyReport, by: uuid.UUID | None
) -> None:
    await session.execute(
        text(
            "INSERT INTO monthly_report (tenant_id, month, content, generated_at, generated_by) "
            "VALUES (:t, :month, CAST(:content AS jsonb), :at, :by) "
            "ON CONFLICT (tenant_id, month) DO UPDATE SET content = EXCLUDED.content, "
            "generated_at = EXCLUDED.generated_at, generated_by = EXCLUDED.generated_by"
        ),
        {
            "t": tenant_id,
            "month": date.fromisoformat(f"{report.month}-01"),
            "content": json.dumps(report.model_dump(mode="json", by_alias=True)),
            "at": report.generated_at,
            "by": by,
        },
    )


async def stored(session: AsyncSession, month: date) -> MonthlyReport | None:
    content = (
        await session.execute(
            text("SELECT content FROM monthly_report WHERE month = :m"), {"m": month}
        )
    ).scalar_one_or_none()
    return None if content is None else MonthlyReport.model_validate(content)


def _money(usd: float, local: float | None, currency: str) -> str:
    out = f"USD {usd:,.2f}"
    if local is not None and currency != "USD":
        out += f" ({currency} {local:,.0f})"
    return out


def render(r: MonthlyReport, app_url: str) -> tuple[str, str, str]:
    """Subject, text and HTML of the email."""
    month = date.fromisoformat(f"{r.month}-01")
    label = f"{month:%B %Y}"
    s = r.spend
    total = _money(s.usd, s.local, r.currency)
    trend = "" if s.change_percent is None else f" ({s.change_percent:+.0f}% on the month before)"
    subject = f"{r.workspace}: AWS spend for {label} was {total}{trend}"
    link = f"{app_url.rstrip('/')}/reports?month={r.month}"
    sections: list[tuple[str, list[str]]] = []
    lines = [
        f"Total: {total}; the month before {_money(s.previous_usd, s.previous_local, r.currency)}.",
    ]
    if s.usage_effect is not None and s.fx_effect is not None:
        lines.append(
            f"Change in {r.currency}: {s.usage_effect:+,.0f} from usage, {s.fx_effect:+,.0f} "
            "from the exchange rate."
        )
    if s.credits_usd:
        lines.append(f"AWS credits applied: USD {-s.credits_usd:,.2f}.")
    sections.append(("Spend", lines))
    if r.drivers:
        sections.append(
            (
                "What changed most",
                [
                    f"{d.service_name}: {'+' if d.change_usd >= 0 else '-'}USD "
                    f"{abs(d.change_usd):,.2f} (USD {d.usd:,.2f} this month)"
                    for d in r.drivers
                ],
            )
        )
    if r.budgets:
        sections.append(
            (
                "Budgets",
                [
                    f"{b.name} ({b.currency} {b.amount:,.0f}): "
                    f"{BUDGET_STATUS.get(b.status, b.status)}"
                    + ("" if b.actual_percent is None else f", {b.actual_percent:.0f}% spent")
                    for b in r.budgets
                ],
            )
        )
    sv = r.savings
    lines = [
        f"{sv.open_count} open recommendations worth USD {sv.open_usd_month:,.2f} a month"
        + (f" ({sv.new_count} found this month)." if sv.new_count else "."),
        f"Marked done this month: {sv.done_count}, saving USD {sv.done_usd_month:,.2f} a month.",
        *[
            f"{t.title}: USD {t.savings_usd_month:,.2f} a month, {t.risk_level} risk, effort "
            f"{t.effort}"
            for t in r.top_recommendations
        ],
    ]
    sections.append(("Savings", lines))
    if r.anomalies.count:
        days = "day" if r.anomalies.count == 1 else "days"
        sections.append(
            (
                "Anomalies",
                [
                    f"{r.anomalies.count} unusual {days}, "
                    f"USD {r.anomalies.impact_usd:,.2f} above normal."
                ],
            )
        )
    if r.credits:
        sections.append(("AWS credits", [r.credits]))
    sections.append(("About these figures", [r.accuracy.note]))
    body_text = "\n".join(
        [f"{r.workspace}: {label}", ""]
        + [line for title, items in sections for line in (title, *[f"- {i}" for i in items], "")]
        + [f"See the full report (print or save as PDF): {link}"]
    )
    body_html = (
        f'<h2 style="color:#0B1F4B">{html.escape(r.workspace)}: {html.escape(label)}</h2>'
        + "".join(
            f"<h3>{html.escape(title)}</h3><ul>"
            + "".join(f"<li>{html.escape(i)}</li>" for i in items)
            + "</ul>"
            for title, items in sections
        )
        + f'<p><a href="{html.escape(link)}">See the full report</a> (print or save as PDF).</p>'
        + "<p><small>Turn monthly reports off under Settings, Email notifications.</small></p>"
    )
    return subject, body_text, body_html


async def extra_emails(session: AsyncSession) -> list[str]:
    emails = (await session.execute(text("SELECT emails FROM report_setting"))).scalar_one_or_none()
    return sorted({e.lower() for e in emails or []})


async def recipients(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    people = await preferences.recipients(
        session, tenant_id, "monthly_report", ("admin", "member", "viewer")
    )
    return sorted({p.email for p in people} | set(await extra_emails(session)))


def dedupe_key(month: str, email: str) -> str:
    return f"report:{month}:{email}"


async def email(
    session: AsyncSession, tenant_id: uuid.UUID, report: MonthlyReport, *, again: bool = False
) -> int:
    """Email the report to its recipients who have not had it (everyone with ``again``).
    One email per recipient. Returns emails sent."""
    to = await recipients(session, tenant_id)
    if not again:
        done = {
            r.dedupe_key
            for r in await session.execute(
                text(
                    "SELECT dedupe_key FROM notification WHERE dedupe_key LIKE :prefix "
                    "AND status <> 'failed'"
                ),
                {"prefix": f"report:{report.month}:%"},
            )
        }
        to = [e for e in to if dedupe_key(report.month, e) not in done]
    if not to:
        return 0
    subject, body_text, body_html = render(report, get_settings().app_url)
    sent = 0
    for address in to:
        status, error = "sent", None
        try:
            await get_mailer().send(
                Email([address], subject, body_text, body_html, tag="monthly-report")
            )
            sent += 1
        except EmailError as exc:
            status, error = "failed", str(exc)
            log.warning("monthly report for %s not sent: %s", tenant_id, exc)
        await session.execute(
            text(
                "INSERT INTO notification (id, tenant_id, kind, dedupe_key, recipients, "
                "subject, status, error) VALUES (:id, :t, 'monthly_report', :k, :to, :subject, "
                ":status, :error) ON CONFLICT (tenant_id, dedupe_key) DO UPDATE SET "
                "subject = EXCLUDED.subject, status = EXCLUDED.status, error = EXCLUDED.error, "
                "created_at = now()"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "k": dedupe_key(report.month, address),
                "to": [address],
                "subject": subject,
                "status": status,
                "error": error,
            },
        )
    return sent


async def send_due(session: AsyncSession, tenant_id: uuid.UUID, now: datetime) -> int:
    """Make and email last month's report when it is due. Inside a tenant-scoped
    transaction. Returns emails sent."""
    tz_name = (
        await session.execute(text("SELECT time_zone FROM tenant WHERE id = :t"), {"t": tenant_id})
    ).scalar_one()
    month = due(now, tz_name)
    if month is None:
        return 0
    report = await stored(session, month)
    if report is None:
        report = await build(session, tenant_id, month)
        if report is None:
            return 0
        await store(session, tenant_id, report, None)
    return await email(session, tenant_id, report)
