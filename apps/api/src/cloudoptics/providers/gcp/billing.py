"""Google Cloud bills: FOCUS rows from BigQuery into the shared cost tables (ADR-019).

Unit of work: (connection, billing period). BigQuery does the aggregation: one query per
period groups the FOCUS rows to the ``cost_daily`` grain (day x project x service x region x
SKU x charge category x tags), so what Cloudoptics reads is small whatever the bill's size.
Every query is filtered on ``BillingPeriodStart`` (the invoice month) and capped by
``maximumBytesBilled``; it runs
in the connection's query project, which pays for it (BigQuery on-demand pricing; the first
TiB a month is free, and a month of grouped FOCUS rows is usually a few MB).

The result's SHA-256 is the period's version. Google rewrites the export several times a day
and adjusts a month until its invoice closes, so the current and the previous month are read
at every poll; a version already loaded is skipped, and a new one replaces the period's rows
in one transaction (the same idempotent period replace as AWS's CUR loads).

Google's managed FOCUS export (``gcp_billing_export_focus_<billing account>`` in the linked
dataset ``gcp_billing_immutable_<billing account>_<location>``) has no ``Credit`` or
``Purchase`` rows and no FOCUS ``Tags`` column: credits are inside ``BilledCost`` and
``EffectiveCost`` (cost + credits) and itemised in ``x_Credits``; resource labels are
``x_Labels``. The load reads ``Tags`` when a table has it (a FOCUS view) and ``x_Labels``
otherwise.

``BillingPeriodStart`` is a TIMESTAMP made from the invoice month, and Google does not say at
which hour (Google's invoice months follow US Pacific time). Periods are therefore matched
within a day of midnight UTC, which holds whichever way it is stored.

Amounts are stored in the bill's own currency beside USD (migration 0043). Non-USD bills
convert day by day with the FX service (a staff-set rate first, ADR-010), falling back to the
rate Google used when the export carries one (``x_CurrencyConversionRate``), flagged as a
warning; a day with neither blocks the period, and the previous load stays.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.analytics import funnel
from cloudoptics.connectors.accounts import OrgAccount
from cloudoptics.connectors.accounts import _store as store_accounts
from cloudoptics.connectors.service import get_connection
from cloudoptics.fx.service import quote
from cloudoptics.ingestion.aggregate import tag_hash
from cloudoptics.ingestion.quality import Check, blocked
from cloudoptics.ingestion.reconcile import compare
from cloudoptics.ingestion.reload import (
    PeriodResult,
    ReloadResult,
    _finish_run,
    _start_run,
    _status,
)
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.providers.gcp.client import GcpApi, GcpError, bq_cost_usd, param
from cloudoptics.providers.gcp.connect import GcpSettingsRow, load_settings
from cloudoptics.providers.gcp.settings import get_gcp_settings

MAPPING_VERSION = "gcp-focus-1"
INGESTABLE = ("healthy", "degraded")
CHARGE_CATEGORIES = frozenset({"Usage", "Purchase", "Tax", "Credit", "Adjustment"})
NEGATIVE_ALLOWED = frozenset({"Credit", "Adjustment"})
PROJECT_ID = r"[a-z][a-z0-9-]{4,28}[a-z0-9]"

# Optional FOCUS columns: NULL when the table has none (the FOCUS view's versions differ).
OPTIONAL = {
    "project_name": ("SubAccountName", "ANY_VALUE(SubAccountName)"),
    "service_category": ("ServiceCategory", "ANY_VALUE(ServiceCategory)"),
    "list_cost": ("ListCost", "SUM(ListCost)"),
    "list_n": ("ListCost", "COUNTIF(ListCost IS NOT NULL)"),
    "quantity": ("ConsumedQuantity", "SUM(ConsumedQuantity)"),
    "quantity_n": ("ConsumedQuantity", "COUNTIF(ConsumedQuantity IS NOT NULL)"),
    "conversion_rate": ("x_CurrencyConversionRate", "AVG(x_CurrencyConversionRate)"),
}
GROUPED = {
    "region_id": ("RegionId", "RegionId"),
    "sku_id": ("SkuId", "SkuId"),
    "tags": ("Tags", "TO_JSON_STRING(Tags)"),
}
#: Read instead of a missing grouped column: Google's FOCUS export has labels, not Tags.
FALLBACK = {"tags": ("x_Labels", "TO_JSON_STRING(x_Labels)")}

DAILY_SQL = (
    "/* co:daily */ SELECT DATE(ChargePeriodStart) AS usage_date, "
    "BillingAccountId AS billing_account_id, SubAccountId AS project_id, "
    "ServiceName AS service_name, ChargeCategory AS charge_category, "
    "BillingCurrency AS billing_currency, {grouped}, "
    "SUM(BilledCost) AS billed_cost, SUM(EffectiveCost) AS effective_cost, {optional}, "
    "COUNT(*) AS line_items FROM `{table}` "
    "WHERE BillingPeriodStart >= TIMESTAMP_SUB(TIMESTAMP(@period), INTERVAL 1 DAY) "
    "AND BillingPeriodStart < TIMESTAMP_ADD(TIMESTAMP(@period), INTERVAL 1 DAY) "
    "GROUP BY usage_date, billing_account_id, project_id, service_name, charge_category, "
    "billing_currency, {group_by}"
)  # The period: BillingPeriodStart within a day of midnight UTC (module doc).
TOTALS_SQL = (
    "/* co:totals */ SELECT SubAccountId AS project_id, SUM(BilledCost) AS billed "
    "FROM `{table}` WHERE BillingPeriodStart >= TIMESTAMP_SUB(TIMESTAMP(@period), INTERVAL 1 DAY) "
    "AND BillingPeriodStart < TIMESTAMP_ADD(TIMESTAMP(@period), INTERVAL 1 DAY) "
    "GROUP BY project_id"
)
PROJECTS_SQL = (
    "/* co:projects */ SELECT SubAccountId AS project_id, {name} AS name FROM `{table}` "
    "WHERE BillingPeriodStart >= TIMESTAMP_SUB(TIMESTAMP(@since), INTERVAL 1 DAY) "
    "AND SubAccountId IS NOT NULL GROUP BY project_id LIMIT 2000"
)

LOADED_SQL = text(
    "SELECT billing_period, export_version, status, reload FROM ingest_batch "
    "WHERE connection_id = :c"
)
LATEST_SQL = text(
    "SELECT DISTINCT ON (billing_period) billing_period, export_version FROM ingest_batch "
    "WHERE connection_id = :c AND status = 'succeeded' ORDER BY billing_period, created_at DESC"
)
INSERT_BATCH = text(
    "INSERT INTO ingest_batch (id, tenant_id, connection_id, billing_period, export_version, "
    "manifest_key, mapping_version, status, row_count, checks, error_code, reload) VALUES "
    "(:id, :t, :c, :p, :v, :key, :mv, :status, :rows, CAST(:checks AS jsonb), :code, :reload)"
)
SUPERSEDE = text(
    "UPDATE ingest_batch SET status = 'superseded' WHERE connection_id = :c "
    "AND billing_period = :p AND id <> :id AND status = 'succeeded'"
)
DELETE_PERIOD = text("DELETE FROM cost_daily WHERE connection_id = :c AND billing_period = :p")
INSERT_TAGS = text(
    "INSERT INTO tag_set (tenant_id, hash, tags) VALUES (:t, :h, CAST(:tags AS jsonb)) "
    "ON CONFLICT DO NOTHING"
)
INSERT_COSTS = text(
    "INSERT INTO cost_daily (tenant_id, connection_id, provider, usage_date, billing_period, "
    "provider_account_id, service_code, service_name, service_category, region_id, usage_type, "
    "charge_category, tag_set_hash, billing_currency, billed_cost, effective_cost, net_cost, "
    "list_cost, billed_cost_usd, effective_cost_usd, net_cost_usd, list_cost_usd, "
    "usage_quantity, data_version) VALUES (:t, :c, 'gcp', :d, :p, :acct, :svc, :svc, :cat, "
    ":region, :usage_type, :charge, :tags, :ccy, :billed, :effective, :effective, :list, "
    ":billed_usd, :effective_usd, :effective_usd, :list_usd, :qty, :v)"
)
UPSERT_ACCOUNTS = text(
    "INSERT INTO cloud_account (id, tenant_id, connection_id, provider, provider_account_id, "
    "name, account_type) VALUES (:id, :t, :c, 'gcp', :acct, :name, :type) "
    "ON CONFLICT (tenant_id, provider, provider_account_id) "
    "DO UPDATE SET name = COALESCE(EXCLUDED.name, cloud_account.name)"
)
RECORD_RUN = text(
    "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, started_at, "
    "finished_at, records, error_code, error_detail, est_customer_api_cost_usd) VALUES (:id, "
    ":t, :c, :job, CAST(:scope AS jsonb), :status, :started, now(), :records, :code, :detail, "
    ":cost)"
)
OUR_TOTALS = text(
    "SELECT provider_account_id AS account_id, sum(billed_cost) AS native, "
    "sum(billed_cost_usd) AS usd, max(usage_date) AS last, max(data_version) AS version "
    "FROM cost_daily WHERE connection_id = :c AND billing_period = :p "
    "GROUP BY provider_account_id"
)
UPSERT_RECONCILIATION = text(
    "INSERT INTO cost_reconciliation (tenant_id, connection_id, billing_period, export_version, "
    "through_date, status, cur_total_usd, reference_usd, difference_pct, accounts, error_code, "
    "checked_at) VALUES (:t, :c, :p, :v, :through, :status, :cur, :ref, :pct, "
    "CAST(:accounts AS jsonb), :code, now()) ON CONFLICT (connection_id, billing_period) DO "
    "UPDATE SET export_version = EXCLUDED.export_version, through_date = EXCLUDED.through_date, "
    "status = EXCLUDED.status, cur_total_usd = EXCLUDED.cur_total_usd, "
    "reference_usd = EXCLUDED.reference_usd, difference_pct = EXCLUDED.difference_pct, "
    "accounts = EXCLUDED.accounts, error_code = EXCLUDED.error_code, checked_at = now()"
)


def month_start(day: date) -> date:
    return day.replace(day=1)


def add_months(period: date, months: int) -> date:
    index = period.year * 12 + period.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def periods_to_read(today: date, loaded: set[date], backfill: int) -> list[date]:
    """This month and last month at every poll; on the first load also ``backfill`` months
    before the current one; and any month in that window never loaded."""
    current = month_start(today)
    window = [add_months(current, -i) for i in range(backfill, -1, -1)]
    if not loaded:
        return window
    recent = {current, add_months(current, -1)}
    return [p for p in window if p in recent or p not in loaded]


def _grouped(alias: str, col: str, expr: str, columns: set[str]) -> str:
    if col in columns:
        return expr
    fallback = FALLBACK.get(alias)
    if fallback and fallback[0] in columns:
        return fallback[1]
    return "CAST(NULL AS STRING)"


def daily_sql(gs: GcpSettingsRow, columns: set[str]) -> str:
    grouped = [
        f"{_grouped(alias, col, expr, columns)} AS {alias}"
        for alias, (col, expr) in GROUPED.items()
    ]
    optional = [
        f"{expr if col in columns else 'NULL'} AS {alias}"
        for alias, (col, expr) in OPTIONAL.items()
    ]
    return DAILY_SQL.format(
        table=gs.table_ref,
        grouped=", ".join(grouped),
        optional=", ".join(optional),
        group_by=", ".join(GROUPED),
    )


def version_of(rows: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        sorted(json.dumps(r, sort_keys=True, default=str) for r in rows), separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:20]


def _kv(item: dict[str, Any], name: str) -> Any:
    return item.get(name, item.get(name.capitalize()))


def parse_tags(raw: Any) -> list[tuple[str, str]]:
    """FOCUS ``Tags`` as BigQuery's JSON: an object, or a list of key/value records
    (``x_Labels`` is ``[{"Key": ..., "Value": ...}]``; the detailed export's ``labels`` are
    lower case)."""
    if raw in (None, "", "null"):
        return []
    value = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict):
        return [(str(k), "" if v is None else str(v)) for k, v in value.items()]
    if isinstance(value, list):
        return [
            (str(_kv(i, "key")), "" if _kv(i, "value") is None else str(_kv(i, "value")))
            for i in value
            if isinstance(i, dict) and _kv(i, "key") is not None
        ]
    return []


@dataclass
class Period:
    """One billing period read from BigQuery, mapped and checked."""

    period: date
    version: str
    rows: list[dict[str, Any]]
    checks: list[Check] = field(default_factory=list)
    costs: list[dict[str, Any]] = field(default_factory=list)
    tag_sets: dict[bytes, dict[str, str]] = field(default_factory=dict)
    accounts: dict[str, str | None] = field(default_factory=dict)
    line_items: int = 0

    @property
    def ok(self) -> bool:
        return not blocked(self.checks)


def _money(value: Decimal | None) -> Decimal | None:
    return None if value is None else Decimal(value).quantize(Decimal("0.000001"))


async def map_period(
    session: AsyncSession, gs: GcpSettingsRow, period: date, rows: list[dict[str, Any]]
) -> Period:
    """FOCUS groups to ``cost_daily`` rows, with the quality checks (docs/11b §2.4) and USD."""
    out = Period(period, version_of(rows), rows)
    out.line_items = sum(int(r.get("line_items") or 0) for r in rows)
    out.checks.append(
        Check("completeness", "ok", f"{out.line_items} line items in {len(rows)} groups.")
    )
    end = add_months(period, 1)
    nulls = other_accounts = out_of_period = 0
    unknown: set[str] = set()
    negative = Decimal(0)
    currencies = {str(r.get("billing_currency") or "") for r in rows}
    rates: dict[tuple[str, date], Decimal | None] = {}
    fallback_days: set[date] = set()
    missing_days: set[date] = set()

    for r in rows:
        day, charge, billed = r.get("usage_date"), r.get("charge_category"), r.get("billed_cost")
        if day is None or not charge or billed is None or not r.get("billing_currency"):
            nulls += 1
            continue
        if r.get("billing_account_id") != gs.billing_account_id:
            other_accounts += 1
        if not period <= day < end:
            out_of_period += 1
        if charge not in CHARGE_CATEGORIES:
            unknown.add(str(charge))
            charge = "Adjustment"
        effective = Decimal(r.get("effective_cost") or 0)
        if effective < 0 and charge not in NEGATIVE_ALLOWED:
            negative += effective
        currency = str(r["billing_currency"]).upper()
        usd_rate: Decimal | None  # billing currency units per USD
        if currency == "USD":
            usd_rate = Decimal(1)
        else:
            key = (currency, day)
            if key not in rates:
                q = await quote(session, currency, day)
                rates[key] = Decimal(str(q.rate)) if q is not None else None
            usd_rate = rates[key]
            if usd_rate is None and r.get("conversion_rate"):
                usd_rate = Decimal(r["conversion_rate"])
                fallback_days.add(day)
            if usd_rate is None or usd_rate <= 0:
                missing_days.add(day)
                continue
        project = r.get("project_id")
        account = str(project) if project else gs.billing_account_id
        if project:
            name = r.get("project_name")
            if name or account not in out.accounts:
                out.accounts[account] = name
        digest, pairs = tag_hash(parse_tags(r.get("tags")))
        if digest is not None:
            out.tag_sets.setdefault(digest, pairs)
        list_cost = Decimal(r["list_cost"]) if r.get("list_n") else None
        out.costs.append(
            {
                "d": day,
                "acct": account,
                "svc": str(r.get("service_name") or "Unknown"),
                "cat": str(r.get("service_category") or "Other"),
                "region": r.get("region_id") or None,
                "usage_type": r.get("sku_id") or None,
                "charge": charge,
                "tags": digest,
                "ccy": currency,
                "billed": _money(Decimal(billed)),
                "effective": _money(effective),
                "list": _money(list_cost),
                "billed_usd": _money(Decimal(billed) / usd_rate),
                "effective_usd": _money(effective / usd_rate),
                "list_usd": None if list_cost is None else _money(list_cost / usd_rate),
                "qty": _money(Decimal(r["quantity"])) if r.get("quantity_n") else None,
            }
        )

    if nulls:
        out.checks.append(
            Check("nulls", "error", f"{nulls} groups have no date, charge category or cost.")
        )
    else:
        out.checks.append(Check("nulls", "ok", "No missing key values."))
    if other_accounts:
        out.checks.append(
            Check(
                "billing_account",
                "error",
                f"{other_accounts} groups belong to another billing account than "
                f"{gs.billing_account_id}.",
            )
        )
    if len(currencies - {""}) > 1:
        out.checks.append(
            Check(
                "currency",
                "error",
                f"More than one billing currency in the period: {', '.join(sorted(currencies))}.",
            )
        )
    if missing_days:
        first = min(missing_days)
        out.checks.append(
            Check(
                "fx",
                "error",
                f"No USD rate for {', '.join(sorted(currencies - {'USD', ''}))} on "
                f"{len(missing_days)} days from {first.isoformat()}; Cloudoptics staff set "
                "rates on the Exchange rates page.",
            )
        )
    elif fallback_days:
        out.checks.append(
            Check(
                "fx",
                "warning",
                f"{len(fallback_days)} days used Google's own exchange rate (no Cloudoptics "
                "rate for those days).",
            )
        )
    if out_of_period:
        out.checks.append(
            Check(
                "period_bounds", "warning", f"{out_of_period} groups are dated outside the period."
            )
        )
    if negative < 0:
        out.checks.append(
            Check(
                "negative_costs",
                "warning",
                f"{negative:.2f} of negative cost outside credits and adjustments.",
            )
        )
    if unknown:
        out.checks.append(
            Check(
                "charge_categories",
                "warning",
                "Unmapped charge categories (counted as Adjustment): "
                f"{', '.join(sorted(unknown))}.",
            )
        )
    return out


async def store_period(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conn: Any,
    gs: GcpSettingsRow,
    loaded: Period,
    *,
    reload: bool = False,
) -> None:
    """Record the batch; when it passed the checks, replace the period's rows."""
    batch_id = uuid7()
    await session.execute(
        INSERT_BATCH,
        {
            "id": batch_id,
            "t": tenant_id,
            "c": conn.id,
            "p": loaded.period,
            "v": loaded.version,
            "key": f"bq://{gs.table_ref}",
            "mv": MAPPING_VERSION,
            "status": "succeeded" if loaded.ok else "failed",
            "rows": loaded.line_items,
            "checks": json.dumps([c.as_json() for c in loaded.checks]),
            "code": None if loaded.ok else "quality_check_failed",
            "reload": reload,
        },
    )
    if not loaded.ok:
        return
    await session.execute(SUPERSEDE, {"c": conn.id, "p": loaded.period, "id": batch_id})
    await session.execute(DELETE_PERIOD, {"c": conn.id, "p": loaded.period})
    if loaded.tag_sets:
        await session.execute(
            INSERT_TAGS,
            [
                {"t": tenant_id, "h": h, "tags": json.dumps(tags)}
                for h, tags in loaded.tag_sets.items()
            ],
        )
    if loaded.costs:
        await funnel.emit(session, "first_costs", tenant_id=tenant_id, detail="bills")
        await session.execute(
            INSERT_COSTS,
            [
                {**c, "t": tenant_id, "c": conn.id, "p": loaded.period, "v": loaded.version}
                for c in loaded.costs
            ],
        )
    accounts: dict[str, tuple[str | None, str]] = {
        gs.billing_account_id: (None, "management"),
        **{a: (n, "member") for a, n in loaded.accounts.items()},
    }
    await session.execute(
        UPSERT_ACCOUNTS,
        [
            {"id": uuid7(), "t": tenant_id, "c": conn.id, "acct": a, "name": n, "type": kind}
            for a, (n, kind) in sorted(accounts.items())
        ],
    )


@dataclass
class GcpIngestResult:
    """``IngestOutcome`` of the provider protocol."""

    status: str
    loaded: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    records: int = 0
    loaded_periods: list[date] = field(default_factory=list)


async def _context(
    tenant_id: uuid.UUID, connection_id: uuid.UUID
) -> tuple[Any, GcpSettingsRow | None, list[Any]]:
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        gs = await load_settings(session, connection_id)
        batches = list(await session.execute(LOADED_SQL, {"c": connection_id}))
    return conn, gs, batches


async def _columns(api: GcpApi, gs: GcpSettingsRow) -> set[str]:
    table = await api.table(gs.export_project_id, gs.export_dataset_id, gs.export_table_id)
    return {str(f.get("name")) for f in (table.get("schema") or {}).get("fields") or []}


async def _read(api: GcpApi, gs: GcpSettingsRow, sql: str, period: date) -> list[dict[str, Any]]:
    result = await api.query(
        gs.query_project_id,
        sql,
        [param("period", "STRING", period.isoformat())],
        location=gs.dataset_location,
        max_rows=get_gcp_settings().max_rows_per_period,
    )
    return result.rows


def _code(exc: GcpError) -> str:
    if exc.code in ("TooManyRows", "bytesBilledLimitExceeded"):
        return f"too_large: {exc.message}"
    return exc.code


async def ingest(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> GcpIngestResult:
    """Read every period that may have changed; store each new version."""
    started = datetime.now(UTC)
    conn, gs, batches = await _context(tenant_id, connection_id)
    if conn.status not in INGESTABLE or gs is None:
        return GcpIngestResult("skipped")
    done = {(b.billing_period, b.export_version) for b in batches if not b.reload}
    done |= {(b.billing_period, b.export_version) for b in batches}
    loaded_periods = {b.billing_period for b in batches if b.status != "failed"}
    api = GcpApi()
    result = GcpIngestResult("up_to_date")
    try:
        sql = daily_sql(gs, await _columns(api, gs))
    except GcpError as exc:
        result.status = "failed"
        result.errors.append(_code(exc))
        await _record(tenant_id, connection_id, "cur.ingest", started, result, api)
        return result
    maker = get_sessionmaker()
    settings = get_gcp_settings()
    for period in periods_to_read(started.date(), loaded_periods, settings.backfill_months):
        label = f"{period:%Y-%m}"
        try:
            rows = await _read(api, gs, sql, period)
        except GcpError as exc:
            result.errors.append(f"{label}: {_code(exc)}")
            continue
        if not rows or (period, version_of(rows)) in done:
            continue
        async with maker() as session, session.begin():
            await scope(session, tenant_id=tenant_id)
            loaded = await map_period(session, gs, period, rows)
            await store_period(session, tenant_id, conn, gs, loaded)
        if loaded.ok:
            result.loaded.append(f"{label}@{loaded.version}")
            result.loaded_periods.append(period)
            result.records += loaded.line_items
        else:
            result.rejected.append(f"{label}@{loaded.version}")
    if result.errors or result.rejected:
        result.status = "partial" if result.loaded else "failed"
    elif result.loaded:
        result.status = "succeeded"
    await _record(tenant_id, connection_id, "cur.ingest", started, result, api)
    return result


async def _record(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    job: str,
    started: datetime,
    result: GcpIngestResult,
    api: GcpApi,
) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await session.execute(
            RECORD_RUN,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "job": job,
                "scope": json.dumps(
                    {
                        "loaded": result.loaded,
                        "rejected": result.rejected,
                        "bytesBilled": api.bytes_billed,
                    }
                ),
                "status": "succeeded" if result.status == "up_to_date" else result.status,
                "started": started,
                "records": result.records,
                "code": "quality_check_failed"
                if result.rejected and not result.errors
                else ("load_failed" if result.errors else None),
                "detail": "; ".join(result.errors)[:2000] or None,
                "cost": bq_cost_usd(api.bytes_billed),
            },
        )


async def reload(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, period: date | None = None
) -> ReloadResult:
    """An admin's reload: read ``period`` (every loaded month and the current one when None)
    again and store it when its figures changed. Recorded like AWS reloads (``cur.reload``)."""
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        gs = await load_settings(session, connection_id)
        loaded_before = {
            (b.billing_period, b.export_version)
            for b in await session.execute(LOADED_SQL, {"c": connection_id})
        }
        latest = {
            r.billing_period: r.export_version
            for r in await session.execute(LATEST_SQL, {"c": connection_id})
        }
        run_id = await _start_run(session, tenant_id, connection_id, period)
    result = ReloadResult("succeeded", period=period)
    if conn.status not in INGESTABLE or gs is None:
        result.status = "skipped"
        await _finish_run(maker, tenant_id, run_id, result)
        return result
    api = GcpApi()
    try:
        sql = daily_sql(gs, await _columns(api, gs))
    except GcpError as exc:
        result.status = "failed"
        result.errors.append(_code(exc))
        await _finish_run(maker, tenant_id, run_id, result)
        return result
    today = datetime.now(UTC).date()
    wanted = (
        [period]
        if period is not None
        else sorted(
            set(latest)
            | set(periods_to_read(today, set(latest), get_gcp_settings().backfill_months))
        )
    )
    for p in wanted:
        try:
            rows = await _read(api, gs, sql, p)
        except GcpError as exc:
            result.periods.append(PeriodResult(p, "failed", None, _code(exc)))
            continue
        if not rows:
            # A month asked for, or loaded before, that the export no longer has.
            if period is not None or p in latest:
                result.periods.append(PeriodResult(p, "not_in_bucket"))
            continue
        version = version_of(rows)
        if latest.get(p) == version:
            result.periods.append(PeriodResult(p, "unchanged", version))
            continue
        async with maker() as session, session.begin():
            await scope(session, tenant_id=tenant_id)
            loaded = await map_period(session, gs, p, rows)
            await store_period(
                session, tenant_id, conn, gs, loaded, reload=(p, version) in loaded_before
            )
        if loaded.ok:
            result.periods.append(PeriodResult(p, "reloaded", version))
            result.records += loaded.line_items
        else:
            failed = sorted(c.id for c in loaded.checks if c.status == "error")
            result.periods.append(
                PeriodResult(p, "failed", version, "Failed the checks: " + ", ".join(failed))
            )
    result.periods.sort(key=lambda r: r.period)
    result.status = _status(result, period)
    await _finish_run(maker, tenant_id, run_id, result)
    return result


async def reconcile(tenant_id: uuid.UUID, connection_id: uuid.UUID, billing_period: date) -> str:
    """Compare a loaded period with BigQuery's own total per project, straight from the
    source table. Google has no invoice API, so this checks the load end to end (rows lost,
    doubled or restated since) rather than against the invoice; see ADR-019."""
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        if conn.status == "disconnected":
            return "skipped"
        gs = await load_settings(session, connection_id)
        ours = (await session.execute(OUR_TOTALS, {"c": connection_id, "p": billing_period})).all()
    if gs is None or not ours:
        return "skipped"
    native = sum(Decimal(r.native) for r in ours)
    usd = sum(Decimal(r.usd) for r in ours)
    ratio = float(usd / native) if native else 1.0  # USD per billing currency unit
    cur = {r.account_id: float(r.usd) for r in ours}
    api = GcpApi()
    error: str | None = None
    comparison = None
    try:
        result = await api.query(
            gs.query_project_id,
            TOTALS_SQL.format(table=gs.table_ref),
            [param("period", "STRING", billing_period.isoformat())],
            location=gs.dataset_location,
        )
        reference = {
            str(r["project_id"] or gs.billing_account_id): float(r["billed"] or 0) * ratio
            for r in result.rows
        }
        comparison = compare(cur, reference)
    except GcpError as exc:
        error = exc.code
    status = comparison.status if comparison else "failed"
    end = add_months(billing_period, 1) - timedelta(days=1)
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await session.execute(
            UPSERT_RECONCILIATION,
            {
                "t": tenant_id,
                "c": connection_id,
                "p": billing_period,
                "v": max(r.version for r in ours),
                "through": min(max(r.last for r in ours), end),
                "status": status,
                "cur": round(comparison.cur_total, 6) if comparison else None,
                "ref": round(comparison.reference, 6) if comparison else None,
                "pct": comparison.difference_pct if comparison else None,
                "accounts": json.dumps(comparison.accounts if comparison else []),
                "code": error,
            },
        )
        await session.execute(
            RECORD_RUN,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "job": "cost.reconcile",
                "scope": json.dumps({"billing_period": billing_period.isoformat()}),
                "status": "failed" if error else "succeeded",
                "started": datetime.now(UTC),
                "records": None,
                "code": error,
                "detail": None,
                "cost": bq_cost_usd(api.bytes_billed),
            },
        )
    return status


async def discover(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    """The projects billed to the connection's billing account in the last two months
    (from the export, so only projects of the proven billing account); a project no longer
    billed is kept and marked NOT_LISTED, as AWS does for accounts that left."""
    started = datetime.now(UTC)
    conn, gs, _ = await _context(tenant_id, connection_id)
    if conn.status not in INGESTABLE or gs is None:
        return "skipped"
    api = GcpApi()
    error: str | None = None
    found: list[OrgAccount] = []
    try:
        columns = await _columns(api, gs)
        since = add_months(month_start(started.date()), -1)
        name = "ANY_VALUE(SubAccountName)" if "SubAccountName" in columns else "NULL"
        result = await api.query(
            gs.query_project_id,
            PROJECTS_SQL.format(table=gs.table_ref, name=name),
            [param("since", "STRING", since.isoformat())],
            location=gs.dataset_location,
        )
        found = [
            OrgAccount(str(r["project_id"]), r.get("name"), "ACTIVE")
            for r in result.rows
            if r.get("project_id")
        ]
    except GcpError as exc:
        error = exc.code
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        if error is None:
            await store_accounts(session, tenant_id, conn, found)
        await session.execute(
            RECORD_RUN,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "job": "accounts.discover",
                "scope": json.dumps({"accounts": len(found)}),
                "status": "failed" if error else "succeeded",
                "started": started,
                "records": len(found),
                "code": error,
                "detail": None,
                "cost": bq_cost_usd(api.bytes_billed),
            },
        )
    return "failed" if error else "succeeded"
