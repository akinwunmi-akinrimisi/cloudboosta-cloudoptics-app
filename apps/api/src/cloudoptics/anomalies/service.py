"""Anomaly detection and alerts (PRD US-6.3, FR-ANA-08, FR-NOT-04).

Per (account, service) and day, from the bills (CUR):

- expected cost = the median of the same weekday over the four weeks before, and MAD the
  median absolute deviation of those four days (a day without spend counts as zero);
- anomaly when cost > expected + max(3 x MAD, min_percent of expected) **and** cost minus
  expected is at least the workspace minimum (default USD 50 a day and 30%).

Each run re-checks the last seven days of bills, because a day's bill keeps filling in for a
while; an anomaly found again keeps its status and gets the new figures. A day needs four
full weeks of bills before it, so a new workspace sees anomalies from its 29th day of data.

An anomaly the same scope had marked "expected" in the seven days before suppresses a new
one: it is stored as expected (``suppressed_by``) and sends no alert. Open anomalies found
in one run go out as one email, at most once per anomaly (``notification`` key
``anomaly:<id>``; a failed send is tried again on the next run).
"""

from __future__ import annotations

import html
import json
import logging
import statistics
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.fx import service as fx
from cloudoptics.ingestion.queries import data_range, scope_daily, scope_usage_types
from cloudoptics.notifications import preferences
from cloudoptics.notifications.email import Email, EmailError, get_mailer
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

log = logging.getLogger(__name__)

ENGINE_VERSION = "anomaly-1"
Status = Literal["open", "investigating", "expected", "resolved"]
STATUSES: tuple[Status, ...] = ("open", "investigating", "expected", "resolved")
WEEKS = 4
RECHECK_DAYS = 7
SUPPRESS_DAYS = 7
MAX_CONTRIBUTORS = 5
DEFAULT_MIN_IMPACT = 50.0
DEFAULT_MIN_PERCENT = 30


@dataclass(frozen=True)
class AlertSettings:
    """A workspace's anomaly thresholds (FR-REC-08) and extra alert addresses."""

    min_impact: float = DEFAULT_MIN_IMPACT
    min_impact_currency: str = "USD"
    min_percent: int = DEFAULT_MIN_PERCENT
    emails: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> AlertSettings:
        data = data or {}
        return cls(
            min_impact=float(data.get("anomalyMinImpact", DEFAULT_MIN_IMPACT)),
            min_impact_currency=str(data.get("anomalyMinImpactCurrency") or "USD"),
            min_percent=int(data.get("anomalyMinPercent", DEFAULT_MIN_PERCENT)),
            emails=tuple(sorted({str(e).lower() for e in data.get("anomalyEmails", [])})),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "anomalyMinImpact": self.min_impact,
            "anomalyMinImpactCurrency": self.min_impact_currency,
            "anomalyMinPercent": self.min_percent,
            "anomalyEmails": list(self.emails),
        }


async def load_settings(session: AsyncSession) -> AlertSettings:
    """Inside a tenant-scoped transaction."""
    data = (await session.execute(text("SELECT settings FROM alert_setting"))).scalar_one_or_none()
    return AlertSettings.from_json(data)


@dataclass(frozen=True)
class MinImpact:
    """The minimum impact in USD, and the rate it was converted with (``None``: the
    workspace currency has no usable rate, so the USD default applies)."""

    usd: float
    rate: float | None


async def min_impact_usd(session: AsyncSession, settings: AlertSettings, today: date) -> MinImpact:
    if settings.min_impact_currency == "USD":
        return MinImpact(settings.min_impact, 1.0)
    quote = await fx.quote(session, settings.min_impact_currency, today)
    if quote is None:
        return MinImpact(DEFAULT_MIN_IMPACT, None)
    return MinImpact(round(settings.min_impact / quote.rate, 2), quote.rate)


def baseline(history: list[float]) -> tuple[float, float]:
    """Median and median absolute deviation."""
    median = statistics.median(history)
    return median, statistics.median(abs(v - median) for v in history)


def threshold(expected: float, mad: float, min_percent: int) -> float:
    """The cost above which a day is unusual (before the minimum impact)."""
    return expected + max(3 * mad, expected * min_percent / 100)


def is_anomaly(
    actual: float, expected: float, mad: float, min_percent: int, min_usd: float
) -> bool:
    return actual > threshold(expected, mad, min_percent) and actual - expected >= min_usd


def baseline_days(day: date) -> list[date]:
    return [day - timedelta(weeks=k) for k in range(1, WEEKS + 1)]


@dataclass(frozen=True)
class Finding:
    usage_date: date
    account_id: str
    service_code: str
    service_name: str
    actual: float
    expected: float
    mad: float

    @property
    def impact(self) -> float:
        return self.actual - self.expected


def find(
    costs: dict[tuple[str, str], dict[date, float]],
    names: dict[str, str],
    days: list[date],
    min_percent: int,
    min_usd: float,
) -> list[Finding]:
    """Anomalies among ``costs`` (per scope, per day) on ``days``."""
    found = []
    for (account, service), by_day in costs.items():
        for day in days:
            actual = by_day.get(day, 0.0)
            expected, mad = baseline([by_day.get(d, 0.0) for d in baseline_days(day)])
            if is_anomaly(actual, expected, mad, min_percent, min_usd):
                found.append(Finding(day, account, service, names[service], actual, expected, mad))
    return found


async def contributors(session: AsyncSession, f: Finding) -> list[dict[str, Any]]:
    """The usage types (by region) that rose most against their own same-weekday median."""
    history = baseline_days(f.usage_date)
    rows = await scope_usage_types(session, f.account_id, f.service_code, [f.usage_date, *history])
    by_key: dict[tuple[str, str | None], dict[date, float]] = defaultdict(dict)
    for r in rows:
        by_key[(r.usage_type, r.region)][r.usage_date] = r.amount
    items: list[dict[str, Any]] = []
    for (usage_type, region), by_day in by_key.items():
        actual = by_day.get(f.usage_date, 0.0)
        expected = statistics.median(by_day.get(d, 0.0) for d in history)
        if actual - expected > 0.005:
            items.append(
                {
                    "usageType": usage_type,
                    "region": region,
                    "actualUsd": round(actual, 2),
                    "expectedUsd": round(expected, 2),
                    "impactUsd": round(actual - expected, 2),
                }
            )
    items.sort(key=lambda i: -i["impactUsd"])
    return items[:MAX_CONTRIBUTORS]


@dataclass(frozen=True)
class DetectResult:
    found: int
    new: int
    days: list[date]


async def detect(
    session: AsyncSession, tenant_id: uuid.UUID, today: date, settings: AlertSettings
) -> DetectResult:
    """Find anomalies in the last week of bills and store them. Inside a tenant-scoped
    transaction."""
    span = await data_range(session, today)
    if span is None:
        return DetectResult(0, 0, [])
    earliest, last = span
    days = [
        last - timedelta(days=i)
        for i in range(RECHECK_DAYS - 1, -1, -1)
        if last - timedelta(days=i) - timedelta(weeks=WEEKS) >= earliest
    ]
    if not days:
        return DetectResult(0, 0, [])
    costs: dict[tuple[str, str], dict[date, float]] = defaultdict(dict)
    names: dict[str, str] = {}
    for c in await scope_daily(session, days[0] - timedelta(weeks=WEEKS), last):
        costs[(c.account_id, c.service_code)][c.usage_date] = c.amount
        names[c.service_code] = c.service_name
    minimum = await min_impact_usd(session, settings, today)
    new = 0
    findings = find(costs, names, days, settings.min_percent, minimum.usd)
    for f in sorted(findings, key=lambda f: f.usage_date):
        new += await _store(session, tenant_id, f, await contributors(session, f))
    return DetectResult(len(findings), new, days)


async def _store(
    session: AsyncSession, tenant_id: uuid.UUID, f: Finding, top: list[dict[str, Any]]
) -> int:
    """Insert or refresh one anomaly; returns 1 when it is new."""
    suppressor = (
        await session.execute(
            text(
                "SELECT id FROM anomaly WHERE account_id = :account AND service_code = :service "
                "AND status = 'expected' AND usage_date >= :from AND usage_date < :day "
                "ORDER BY usage_date DESC LIMIT 1"
            ),
            {
                "account": f.account_id,
                "service": f.service_code,
                "from": f.usage_date - timedelta(days=SUPPRESS_DAYS),
                "day": f.usage_date,
            },
        )
    ).scalar_one_or_none()
    inserted = (
        await session.execute(
            text(
                "INSERT INTO anomaly (id, tenant_id, usage_date, account_id, service_code, "
                "service_name, actual_usd, expected_usd, mad_usd, impact_usd, contributors, "
                "status, suppressed_by, engine_version) VALUES (:id, :t, :day, :account, "
                ":service, :name, :actual, :expected, :mad, :impact, CAST(:top AS jsonb), "
                ":status, :suppressor, :engine) "
                "ON CONFLICT (tenant_id, usage_date, account_id, service_code) DO UPDATE SET "
                "service_name = EXCLUDED.service_name, actual_usd = EXCLUDED.actual_usd, "
                "expected_usd = EXCLUDED.expected_usd, mad_usd = EXCLUDED.mad_usd, "
                "impact_usd = EXCLUDED.impact_usd, contributors = EXCLUDED.contributors, "
                "engine_version = EXCLUDED.engine_version, updated_at = now() "
                "RETURNING (xmax = 0) AS inserted"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "day": f.usage_date,
                "account": f.account_id,
                "service": f.service_code,
                "name": f.service_name,
                "actual": round(f.actual, 6),
                "expected": round(f.expected, 6),
                "mad": round(f.mad, 6),
                "impact": round(f.impact, 6),
                "top": json.dumps(top),
                "status": "expected" if suppressor else "open",
                "suppressor": suppressor,
                "engine": ENGINE_VERSION,
            },
        )
    ).scalar_one()
    return 1 if inserted else 0


def _usd(amount: float) -> str:
    return f"USD {amount:,.2f}"


def _pct(row: Any) -> str:
    expected = float(row.expected_usd)
    return "new spend" if expected < 0.005 else f"+{100 * float(row.impact_usd) / expected:.0f}%"


def anomaly_email(rows: Sequence[Any], app_url: str) -> tuple[str, str, str]:
    """Subject, text and HTML for anomalies just found."""
    link = f"{app_url.rstrip('/')}/anomalies"
    total = sum(float(r.impact_usd) for r in rows)
    if len(rows) == 1:
        r = rows[0]
        subject = (
            f"Unusual spend: {r.service_name} in {r.account_label} is {_usd(float(r.impact_usd))}"
            f" above normal on {r.usage_date:%d %b}"
        )
    else:
        subject = f"Unusual spend: {len(rows)} anomalies, {_usd(total)} above normal"
    lines: list[str] = []
    items = []
    for r in rows:
        top = ", ".join(
            f"{c['usageType']}{' (' + c['region'] + ')' if c.get('region') else ''} "
            f"+{_usd(c['impactUsd'])}"
            for c in (r.contributors or [])[:3]
        )
        head = (
            f"{r.usage_date:%a %d %b}: {r.service_name} in {r.account_label}: "
            f"{_usd(float(r.actual_usd))} against {_usd(float(r.expected_usd))} expected "
            f"({_usd(float(r.impact_usd))} above, {_pct(r)})"
        )
        lines += [f"- {head}", *([f"  Mostly: {top}"] if top else [])]
        items.append(
            f"<li>{html.escape(head)}"
            + (f"<br><small>Mostly: {html.escape(top)}</small>" if top else "")
            + "</li>"
        )
    lines += [
        "",
        "Mark each one Expected, Investigating or Resolved; Expected stops alerts for the same",
        "account and service for a week.",
        f"See the anomalies: {link}",
    ]
    body = (
        "<p>Cloudoptics found spend well above the usual level for that weekday:</p>"
        f"<ul>{''.join(items)}</ul><p>Mark each one Expected, Investigating or Resolved; "
        "Expected stops alerts for the same account and service for a week.</p>"
        f'<p><a href="{html.escape(link)}">See the anomalies</a></p>'
    )
    return subject, "\n".join(lines), body


async def recipients(
    session: AsyncSession, tenant_id: uuid.UUID, settings: AlertSettings
) -> list[str]:
    """Workspace admins and members (viewers read the page) who have not turned anomaly
    emails off, plus the extra addresses."""
    people = await preferences.recipients(session, tenant_id, "anomaly_alert", ("admin", "member"))
    return sorted({p.email for p in people} | set(settings.emails))


SELECT_UNSENT = text(
    "SELECT a.id, a.usage_date, a.account_id, a.service_name, a.actual_usd, a.expected_usd, "
    "a.impact_usd, a.contributors, coalesce(c.name, a.account_id) AS account_label "
    "FROM anomaly a LEFT JOIN cloud_account c ON c.tenant_id = a.tenant_id "
    "AND c.aws_account_id = a.account_id "
    "WHERE a.status = 'open' AND a.usage_date >= :since AND NOT EXISTS ("
    "SELECT 1 FROM notification n WHERE n.dedupe_key = 'anomaly:' || CAST(a.id AS text) "
    "AND n.status <> 'failed') ORDER BY a.usage_date, a.impact_usd DESC"
)


async def alert(session: AsyncSession, tenant_id: uuid.UUID, settings: AlertSettings) -> int:
    """Email open anomalies of the last week not yet sent, as one email. Returns emails sent."""
    span = await data_range(session, date.max)
    if span is None:
        return 0
    rows = (
        await session.execute(SELECT_UNSENT, {"since": span[1] - timedelta(days=RECHECK_DAYS)})
    ).all()
    if not rows:
        return 0
    to = await recipients(session, tenant_id, settings)
    subject, body_text, body_html = anomaly_email(rows, get_settings().app_url)
    status, error, sent = "skipped", None, 0
    if to:
        try:
            await get_mailer().send(Email(to, subject, body_text, body_html, tag="anomaly-alert"))
            status, sent = "sent", 1
        except EmailError as exc:
            status, error = "failed", str(exc)
            log.warning("anomaly alert for %s not sent: %s", tenant_id, exc)
    for r in rows:
        await session.execute(
            text(
                "INSERT INTO notification (id, tenant_id, kind, dedupe_key, recipients, "
                "subject, status, error) VALUES (:id, :t, 'anomaly_alert', :k, :to, :subject, "
                ":status, :error) ON CONFLICT (tenant_id, dedupe_key) DO UPDATE SET "
                "recipients = EXCLUDED.recipients, subject = EXCLUDED.subject, "
                "status = EXCLUDED.status, error = EXCLUDED.error, created_at = now()"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "k": f"anomaly:{r.id}",
                "to": to,
                "subject": subject,
                "status": status,
                "error": error,
            },
        )
    return sent


async def detect_and_alert(session: AsyncSession, tenant_id: uuid.UUID, today: date) -> int:
    """Inside a tenant-scoped transaction. Returns emails sent."""
    settings = await load_settings(session)
    await detect(session, tenant_id, today, settings)
    return await alert(session, tenant_id, settings)


@dataclass(frozen=True)
class Point:
    date: date
    actual: float
    expected: float | None
    upper: float | None


async def series(session: AsyncSession, row: Any, settings: AlertSettings) -> list[Point]:
    """Daily cost of the anomaly's scope for four weeks before it and up to a week after,
    with the expected cost and the threshold (where four weeks of history exist)."""
    span = await data_range(session, date.max)
    if span is None:
        return []
    earliest, last = span
    day: date = row.usage_date
    first, end = day - timedelta(days=27), min(day + timedelta(days=7), last)
    costs = {
        c.usage_date: c.amount
        for c in await scope_daily(
            session, first - timedelta(weeks=WEEKS), end, row.account_id.strip(), row.service_code
        )
    }
    points = []
    for i in range((end - first).days + 1):
        d = first + timedelta(days=i)
        expected = upper = None
        if d - timedelta(weeks=WEEKS) >= earliest:
            median, mad = baseline([costs.get(h, 0.0) for h in baseline_days(d)])
            expected, upper = (
                round(median, 2),
                round(threshold(median, mad, settings.min_percent), 2),
            )
        points.append(Point(d, round(costs.get(d, 0.0), 2), expected, upper))
    return points


@dataclass(frozen=True)
class AnomalyPeriod:
    count: int
    impact_usd: float
    open_count: int  # open now, of any day


async def period(session: AsyncSession, first: date, last: date) -> AnomalyPeriod:
    """Anomalies on days ``first`` to ``last`` (suppressed ones left out), for the digest
    and the monthly report."""
    row = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE usage_date BETWEEN :first AND :last "
                "AND suppressed_by IS NULL) AS n, coalesce(sum(impact_usd) FILTER "
                "(WHERE usage_date BETWEEN :first AND :last AND suppressed_by IS NULL), 0) "
                "AS impact, count(*) FILTER (WHERE status = 'open') AS open FROM anomaly"
            ),
            {"first": first, "last": last},
        )
    ).one()
    return AnomalyPeriod(row.n, round(float(row.impact), 2), row.open)
