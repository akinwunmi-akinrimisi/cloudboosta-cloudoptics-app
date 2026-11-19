"""What each export contains and how its CSV file is written (PRD US-7.2).

Every query runs inside a tenant-scoped transaction, so row-level security limits an export
to the workspace it was asked for. Cells that hold text from the bills, tags or people
(account names, tag values, notes) are neutralised so a spreadsheet does not run them as
formulas (OWASP "CSV injection"). The file is written gzip-compressed, as it is stored.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.fx import service as fx
from cloudoptics.platform.api import ApiModel
from cloudoptics.recommendations.queries import OPEN

#: Up to this many rows the file is made during the request; more goes to the worker
#: (PRD US-7.2: "async for > 50k rows").
SYNC_ROWS = 50_000
#: The most rows one export may have (PRD US-7.2: "files up to 1M rows").
MAX_ROWS = 1_000_000
#: The most a stored file may take, compressed (the file lives in PostgreSQL).
MAX_BYTES = 50 * 1024 * 1024
#: The longest period a cost export covers.
MAX_COST_DAYS = 400
#: How long a file can be downloaded (docs/09 §4: "exports 7 days").
KEEP_DAYS = 7

#: First characters a spreadsheet reads as the start of a formula or a control code.
FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")
ACCOUNT_ID = r"^\d{12}$"
ProgressFn = Callable[[float], Awaitable[None]]


class ExportError(Exception):
    """The export cannot be made as asked (the message says why, for the user)."""


def neutralise(value: str) -> str:
    """Prefix a text cell that a spreadsheet would treat as a formula with ``'``, so it
    shows as text. Leading spaces do not hide a formula, so they are looked through."""
    stripped = value.lstrip(" ")
    if value.startswith(FORMULA_START) or stripped.startswith(FORMULA_START):
        return "'" + value
    return value


def cell(value: Any, places: int = 2) -> str:
    """One CSV cell. Numbers stay numbers (a credit's ``-20.00`` is not text); every other
    text is neutralised."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float | Decimal):
        return f"{float(value):.{places}f}"
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return neutralise(str(value))


Row = list[str]


# --- what can be exported ---------------------------------------------------------------

CostGroup = Literal["service", "account", "region", "usage_type", "tag"]


class CostsExport(ApiModel):
    """Spend between ``start`` and ``end`` (inclusive) by day or month, split by the chosen
    dimensions (``tag`` needs ``tagKey``). Like the Costs page: amortised cost after
    credits, without tax unless ``includeTax``."""

    kind: Literal["costs"]
    start: date
    end: date
    granularity: Literal["day", "month"] = "month"
    group_by: list[CostGroup] = Field(
        default_factory=lambda: list[CostGroup](["service"]), max_length=5
    )
    tag_key: str | None = Field(default=None, min_length=1, max_length=128)
    account_id: str | None = Field(default=None, pattern=ACCOUNT_ID)
    include_tax: bool = False

    @model_validator(mode="after")
    def _check(self) -> CostsExport:
        if self.end < self.start:
            raise ValueError("end is before start")
        if (self.end - self.start).days >= MAX_COST_DAYS:
            raise ValueError(f"choose at most {MAX_COST_DAYS} days")
        if len(set(self.group_by)) != len(self.group_by):
            raise ValueError("each dimension once")
        if ("tag" in self.group_by) != (self.tag_key is not None):
            raise ValueError("grouping by tag needs tagKey (and tagKey needs the tag group)")
        return self


class RecommendationsExport(ApiModel):
    """The recommendation list with its filters (``status``: open ones, or all)."""

    kind: Literal["recommendations"]
    status: Literal["open", "all"] = "open"
    rule_id: str | None = Field(default=None, max_length=64)
    account_id: str | None = Field(default=None, pattern=ACCOUNT_ID)
    risk: Literal["low", "medium", "high"] | None = None
    alternatives: bool = False


class AnomaliesExport(ApiModel):
    """The anomaly list: the last ``days`` days, optionally one status."""

    kind: Literal["anomalies"]
    days: int = Field(default=90, ge=1, le=400)
    status: Literal["open", "investigating", "expected", "resolved"] | None = None


class BudgetsExport(ApiModel):
    """Every budget and how this month is going."""

    kind: Literal["budgets"]


ExportSpec = Annotated[
    CostsExport | RecommendationsExport | AnomaliesExport | BudgetsExport,
    Field(discriminator="kind"),
]


# --- costs ------------------------------------------------------------------------------

# Literal statements only; request values are always bound parameters. A dimension that is
# not chosen is NULL in every row, so it does not split the groups. The count and the rows
# group by the same six keys.
COST_COUNT_SQL = """
SELECT count(*) FROM (
  SELECT CASE WHEN CAST(:monthly AS boolean)
              THEN CAST(date_trunc('month', c.usage_date) AS date) ELSE c.usage_date END,
         CASE WHEN CAST(:by_service AS boolean) THEN c.service_code END,
         CASE WHEN CAST(:by_account AS boolean) THEN c.account_id END,
         CASE WHEN CAST(:by_region AS boolean) THEN coalesce(c.region_id, 'global') END,
         CASE WHEN CAST(:by_usage_type AS boolean) THEN coalesce(c.usage_type, '') END,
         CASE WHEN CAST(:tag_key AS text) IS NOT NULL
              THEN coalesce(t.tags ->> CAST(:tag_key AS text), '') END
  FROM cost_daily c
  LEFT JOIN tag_set t ON t.tenant_id = c.tenant_id AND t.hash = c.tag_set_hash
  WHERE c.usage_date BETWEEN :first AND :last
    AND (CAST(:include_tax AS boolean) OR c.charge_category <> 'Tax')
    AND (CAST(:account AS text) IS NULL OR c.account_id = :account)
  GROUP BY 1, 2, 3, 4, 5, 6
) q
"""
# Local amounts convert each day at that day's rate (FR-FX-01): the rates come in as two
# arrays (day, rate); a group with spend on a day without a rate has no local amount.
COST_ROWS_SQL = """
SELECT CASE WHEN CAST(:monthly AS boolean)
            THEN CAST(date_trunc('month', c.usage_date) AS date) ELSE c.usage_date END AS period,
       CASE WHEN CAST(:by_service AS boolean) THEN c.service_code END AS service_code,
       CASE WHEN CAST(:by_account AS boolean) THEN c.account_id END AS account_id,
       CASE WHEN CAST(:by_region AS boolean) THEN coalesce(c.region_id, 'global') END AS region,
       CASE WHEN CAST(:by_usage_type AS boolean) THEN coalesce(c.usage_type, '') END
         AS usage_type,
       CASE WHEN CAST(:tag_key AS text) IS NOT NULL
            THEN coalesce(t.tags ->> CAST(:tag_key AS text), '') END AS tag_value,
       max(c.service_name) AS service_name,
       max(a.name) AS account_name,
       sum(c.effective_cost_usd) AS amortised_usd,
       sum(c.billed_cost_usd) AS billed_usd,
       sum(c.effective_cost_usd * fx.rate) AS amortised_local,
       bool_and(fx.rate IS NOT NULL OR c.effective_cost_usd = 0) AS converted
FROM cost_daily c
LEFT JOIN tag_set t ON t.tenant_id = c.tenant_id AND t.hash = c.tag_set_hash
LEFT JOIN cloud_account a ON a.tenant_id = c.tenant_id AND a.aws_account_id = c.account_id
LEFT JOIN unnest(CAST(:fx_days AS date[]), CAST(:fx_rates AS numeric[])) AS fx(day, rate)
  ON fx.day = c.usage_date
WHERE c.usage_date BETWEEN :first AND :last
  AND (CAST(:include_tax AS boolean) OR c.charge_category <> 'Tax')
  AND (CAST(:account AS text) IS NULL OR c.account_id = :account)
GROUP BY 1, 2, 3, 4, 5, 6
ORDER BY 1, sum(c.effective_cost_usd) DESC, 2, 3, 4, 5, 6
"""
COST_DAYS_SQL = (
    "SELECT DISTINCT usage_date FROM cost_daily WHERE usage_date BETWEEN :first AND :last"
)
TENANT_CURRENCY_SQL = "SELECT display_currency FROM tenant WHERE id = :t"


def _cost_params(spec: CostsExport) -> dict[str, Any]:
    return {
        "monthly": spec.granularity == "month",
        "by_service": "service" in spec.group_by,
        "by_account": "account" in spec.group_by,
        "by_region": "region" in spec.group_by,
        "by_usage_type": "usage_type" in spec.group_by,
        "tag_key": spec.tag_key,
        "first": spec.start,
        "last": spec.end,
        "include_tax": spec.include_tax,
        "account": spec.account_id,
    }


def _month_last(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def _cost_header(spec: CostsExport, currency: str) -> Row:
    header = ["period_start", "period_end"]
    for group in spec.group_by:
        header += {
            "service": ["service_code", "service_name"],
            "account": ["account_id", "account_name"],
            "region": ["region"],
            "usage_type": ["usage_type"],
            "tag": [f"tag:{spec.tag_key}"],
        }[group]
    return [
        *header,
        "amortised_cost_usd",
        "billed_cost_usd",
        f"amortised_cost_{currency.lower()}",
        "fx_rate_avg",
    ]


async def _currency(session: AsyncSession, tenant_id: uuid.UUID) -> str:
    value = (await session.execute(text(TENANT_CURRENCY_SQL), {"t": tenant_id})).scalar_one()
    return str(value).strip()


async def _cost_rows(
    session: AsyncSession, tenant_id: uuid.UUID, spec: CostsExport
) -> AsyncIterator[Row]:
    currency = await _currency(session, tenant_id)
    days = list(
        (
            await session.execute(text(COST_DAYS_SQL), {"first": spec.start, "last": spec.end})
        ).scalars()
    )
    fx_days: list[date] = []
    fx_rates: list[float] = []
    for day in days:
        quote = await fx.quote(session, currency, day)
        if quote is not None:
            fx_days.append(day)
            fx_rates.append(quote.rate)
    params = {**_cost_params(spec), "fx_days": fx_days, "fx_rates": fx_rates}
    result = await session.stream(text(COST_ROWS_SQL), params)
    async for r in result:
        # A month row covers only the part of the month inside the period asked for.
        first, last = r.period, r.period
        if spec.granularity == "month":
            first, last = max(r.period, spec.start), min(_month_last(r.period), spec.end)
        line = [cell(first), cell(last)]
        for group in spec.group_by:
            if group == "service":
                line += [cell(r.service_code), cell(r.service_name)]
            elif group == "account":
                account = str(r.account_id).strip()
                line += [cell(account), cell((r.account_name or account).strip())]
            elif group == "region":
                line.append(cell(r.region))
            elif group == "usage_type":
                line.append(cell(r.usage_type))
            else:
                line.append(cell(r.tag_value))
        usd = float(r.amortised_usd)
        local = float(r.amortised_local) if r.converted and r.amortised_local is not None else None
        rate = local / usd if local is not None and abs(usd) >= 0.005 else None
        line += [cell(usd), cell(r.billed_usd), cell(local), cell(rate, 4)]
        yield line


# --- recommendations --------------------------------------------------------------------

REC_COUNT_SQL = """
SELECT count(*) FROM recommendation r
WHERE (NOT :open_only OR r.status = ANY(:open))
  AND (CAST(:rule AS text) IS NULL OR r.rule_id = :rule)
  AND (CAST(:account AS text) IS NULL OR r.account_id = :account)
  AND (CAST(:risk AS text) IS NULL OR r.risk_level = :risk)
  AND (:alternatives OR r.primary_of IS NULL)
"""
REC_ROWS_SQL = """
SELECT r.id, r.title, r.rule_id, r.type, r.status, r.account_id, a.name AS account_name,
       r.region_id, r.resource_id, r.savings_usd_month, r.cost_now_usd_month,
       r.cost_after_usd_month, r.savings_basis, r.confidence, r.risk_level, r.effort,
       r.reversibility, r.priority_score, r.evidence, u.display_name AS assignee,
       r.first_detected_at, r.last_evaluated_at, r.done_at, r.verified_at,
       r.realised_savings_usd_month
FROM recommendation r
LEFT JOIN cloud_account a ON a.tenant_id = r.tenant_id AND a.aws_account_id = r.account_id
LEFT JOIN app_user u ON u.id = r.assignee_id
WHERE (NOT :open_only OR r.status = ANY(:open))
  AND (CAST(:rule AS text) IS NULL OR r.rule_id = :rule)
  AND (CAST(:account AS text) IS NULL OR r.account_id = :account)
  AND (CAST(:risk AS text) IS NULL OR r.risk_level = :risk)
  AND (:alternatives OR r.primary_of IS NULL)
ORDER BY r.priority_score DESC, r.savings_usd_month DESC, r.id
"""


def _rec_params(spec: RecommendationsExport) -> dict[str, Any]:
    return {
        "open_only": spec.status == "open",
        "open": OPEN,
        "rule": spec.rule_id,
        "account": spec.account_id,
        "risk": spec.risk,
        "alternatives": spec.alternatives,
    }


def evidence_summary(evidence: Sequence[dict[str, Any]] | None) -> str:
    """``kind: value (detail=…)`` for each piece of evidence, joined by ``; ``."""
    parts = []
    for item in evidence or []:
        extra = {k: v for k, v in item.items() if k not in ("kind", "value")}
        value = item.get("value")
        shown = json.dumps(value, default=str) if isinstance(value, dict | list) else value
        part = f"{item.get('kind', 'evidence')}: {'' if shown is None else shown}".rstrip()
        if extra:
            part += " (" + ", ".join(f"{k}={v}" for k, v in extra.items()) + ")"
        parts.append(part)
    return "; ".join(parts)


def _rec_header(currency: str) -> Row:
    return [
        "id",
        "title",
        "rule_id",
        "type",
        "status",
        "account_id",
        "account_name",
        "region",
        "resource_id",
        "savings_usd_month",
        f"savings_{currency.lower()}_month",
        "cost_now_usd_month",
        "cost_after_usd_month",
        "savings_basis",
        "confidence",
        "risk_level",
        "effort",
        "reversibility",
        "priority_score",
        "assignee",
        "first_detected_at",
        "last_evaluated_at",
        "done_at",
        "verified_at",
        "realised_savings_usd_month",
        "evidence_summary",
    ]


async def _rec_rows(
    session: AsyncSession, tenant_id: uuid.UUID, spec: RecommendationsExport
) -> AsyncIterator[Row]:
    currency = await _currency(session, tenant_id)
    # Savings are a monthly rate, so they convert at today's rate.
    quote = await fx.quote(session, currency, datetime.now(UTC).date())
    result = await session.stream(text(REC_ROWS_SQL), _rec_params(spec))
    async for r in result:
        account = str(r.account_id).strip()
        savings = float(r.savings_usd_month)
        yield [
            cell(r.id),
            cell(r.title),
            cell(r.rule_id),
            cell(r.type),
            cell(r.status),
            cell(account),
            cell((r.account_name or "").strip()),
            cell(r.region_id),
            cell(r.resource_id),
            cell(savings),
            cell(savings * quote.rate if quote else None),
            cell(r.cost_now_usd_month),
            cell(r.cost_after_usd_month),
            cell(r.savings_basis),
            cell(r.confidence),
            cell(r.risk_level),
            cell(r.effort),
            cell(r.reversibility),
            cell(r.priority_score),
            cell(r.assignee),
            cell(r.first_detected_at),
            cell(r.last_evaluated_at),
            cell(r.done_at),
            cell(r.verified_at),
            cell(r.realised_savings_usd_month),
            cell(evidence_summary(r.evidence)),
        ]


# --- anomalies and budgets (short lists: at most 500 anomalies, a few budgets) --------------

ANOMALY_HEADER = [
    "date",
    "account_id",
    "account_name",
    "service_code",
    "service_name",
    "actual_usd",
    "expected_usd",
    "impact_usd",
    "deviation_percent",
    "status",
    "suppressed",
    "note",
    "top_usage_types",
    "detected_at",
    "alerted",
]


async def _anomaly_rows(
    session: AsyncSession, tenant_id: uuid.UUID, spec: AnomaliesExport
) -> AsyncIterator[Row]:
    from cloudoptics.anomalies.routes import SELECT_ANOMALIES, _model

    since = datetime.now(UTC).date() - timedelta(days=spec.days)
    rows = await session.execute(
        SELECT_ANOMALIES, {"id": None, "since": since, "status": spec.status}
    )
    for r in rows:
        a = _model(r)
        top = "; ".join(f"{c.usage_type} {c.impact_usd:+.2f}" for c in a["contributors"][:5])
        yield [
            cell(a["date"]),
            cell(a["account_id"]),
            cell(a["account_name"]),
            cell(a["service_code"]),
            cell(a["service_name"]),
            cell(a["actual_usd"]),
            cell(a["expected_usd"]),
            cell(a["impact_usd"]),
            cell(a["deviation_percent"], 1),
            cell(a["status"]),
            cell(a["suppressed"]),
            cell(a["note"]),
            cell(top),
            cell(a["detected_at"]),
            cell(a["alerted"]),
        ]


BUDGET_HEADER = [
    "name",
    "scope",
    "currency",
    "amount",
    "period",
    "status",
    "actual",
    "actual_usd",
    "actual_percent",
    "forecast",
    "forecast_usd",
    "forecast_percent",
    "rate_per_usd",
    "data_through",
    "active",
    "thresholds",
]


async def _budget_rows(
    session: AsyncSession, tenant_id: uuid.UUID, spec: BudgetsExport
) -> AsyncIterator[Row]:
    from cloudoptics.budgets.routes import SELECT_BUDGETS, _out

    for row in (await session.execute(SELECT_BUDGETS)).all():
        b = await _out(session, row)
        scope = "workspace" if b.scope.type == "tenant" else " ".join(b.scope.accounts)
        thresholds = "; ".join(
            f"{t.kind} {t.percent}%{' crossed' if t.crossed else ''}" for t in b.thresholds
        )
        yield [
            cell(b.name),
            cell(scope),
            cell(b.currency),
            cell(b.amount),
            cell(b.period),
            cell(b.status),
            cell(b.actual),
            cell(b.actual_usd),
            cell(b.actual_percent, 1),
            cell(b.forecast),
            cell(b.forecast_usd),
            cell(b.forecast_percent, 1),
            cell(b.rate, 4),
            cell(b.data_through),
            cell(b.is_active),
            cell(thresholds),
        ]


# --- sizing, naming and writing ----------------------------------------------------------


async def count(session: AsyncSession, spec: ExportSpec) -> int | None:
    """How many rows the export will have; ``None`` for the short lists (always small)."""
    if isinstance(spec, CostsExport):
        return int((await session.execute(text(COST_COUNT_SQL), _cost_params(spec))).scalar_one())
    if isinstance(spec, RecommendationsExport):
        return int((await session.execute(text(REC_COUNT_SQL), _rec_params(spec))).scalar_one())
    return None


def file_name(spec: ExportSpec, today: date) -> str:
    """An ASCII file name (nothing the user typed goes into it)."""
    if isinstance(spec, CostsExport):
        return f"cloudoptics-costs-{spec.start:%Y%m%d}-{spec.end:%Y%m%d}.csv"
    return f"cloudoptics-{spec.kind}-{today:%Y%m%d}.csv"


async def _header(session: AsyncSession, tenant_id: uuid.UUID, spec: ExportSpec) -> Row:
    if isinstance(spec, CostsExport):
        return [cell(h) for h in _cost_header(spec, await _currency(session, tenant_id))]
    if isinstance(spec, RecommendationsExport):
        return _rec_header(await _currency(session, tenant_id))
    return ANOMALY_HEADER if isinstance(spec, AnomaliesExport) else BUDGET_HEADER


def _rows(session: AsyncSession, tenant_id: uuid.UUID, spec: ExportSpec) -> AsyncIterator[Row]:
    if isinstance(spec, CostsExport):
        return _cost_rows(session, tenant_id, spec)
    if isinstance(spec, RecommendationsExport):
        return _rec_rows(session, tenant_id, spec)
    if isinstance(spec, AnomaliesExport):
        return _anomaly_rows(session, tenant_id, spec)
    return _budget_rows(session, tenant_id, spec)


@dataclass(frozen=True)
class Built:
    content: bytes  # gzip
    rows: int
    size_bytes: int  # the CSV, uncompressed


PROGRESS_EVERY = 5_000


def _check_size(buffer: io.BytesIO) -> None:
    if buffer.tell() > MAX_BYTES:
        raise ExportError(
            "This export is too large to keep. Choose a shorter period or fewer dimensions."
        )


_FLUSH_EVERY = 1_000


async def build(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    spec: ExportSpec,
    *,
    total: int | None = None,
    progress: ProgressFn | None = None,
) -> Built:
    """Write the export's CSV (UTF-8 with a byte order mark, so spreadsheets read accents),
    gzip-compressed. Calls ``progress`` with the share of ``total`` rows written so far."""
    buffer = io.BytesIO()
    lines = io.StringIO()
    writer = csv.writer(lines, lineterminator="\r\n")
    size = 0
    rows = 0
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:

        def flush() -> None:
            nonlocal size
            data = lines.getvalue().encode("utf-8")
            size += len(data)
            gz.write(data)
            lines.seek(0)
            lines.truncate()
            _check_size(buffer)

        lines.write("\ufeff")
        writer.writerow(await _header(session, tenant_id, spec))
        async for line in _rows(session, tenant_id, spec):
            writer.writerow(line)
            rows += 1
            if rows > MAX_ROWS:
                raise ExportError(f"An export has at most {MAX_ROWS:,} rows. Narrow the filters.")
            if rows % _FLUSH_EVERY == 0:
                flush()
            if progress is not None and total and rows % PROGRESS_EVERY == 0:
                await progress(min(rows / total, 1.0))
        flush()
    _check_size(buffer)
    return Built(content=buffer.getvalue(), rows=rows, size_bytes=size)


_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def content_disposition(name: str) -> str:
    safe = name if _SAFE_NAME.match(name) else "cloudoptics-export.csv"
    return f'attachment; filename="{safe}"'


FINISH_SQL = (
    "UPDATE export_job SET status = 'succeeded', progress = 100, row_count = :rows, "
    "content = :content, size_bytes = :size, error = NULL, "
    "started_at = coalesce(started_at, now()), finished_at = now() WHERE id = :id"
)
FAIL_SQL = (
    "UPDATE export_job SET status = 'failed', error = :error, content = NULL, "
    "finished_at = now() WHERE id = :id"
)


async def finish(session: AsyncSession, export_id: uuid.UUID, built: Built) -> None:
    await session.execute(
        text(FINISH_SQL),
        {"id": export_id, "rows": built.rows, "content": built.content, "size": built.size_bytes},
    )


async def fail(session: AsyncSession, export_id: uuid.UUID, error: str) -> None:
    await session.execute(text(FAIL_SQL), {"id": export_id, "error": error[:500]})
