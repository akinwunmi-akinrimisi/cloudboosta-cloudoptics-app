"""DigitalOcean bills in the shared cost tables (ADR-021): invoices, month to date, checks.

* **Closed months** come from invoices: ``GET /v2/customers/my/invoices`` lists them, each
  one's CSV (``/csv``) has the line items and its summary (``/summary``) the taxes and
  credits. One invoice is one export version (its UUID), so a month is loaded once and the
  shared poll is idempotent; the shared ``_store`` replaces the month's rows in one
  transaction exactly as it does for AWS.
* **This month** is provisional, like AWS's Cost Explorer snapshot: the balance endpoint's
  ``month_to_date_usage`` is the total, and the daily "insights" endpoint gives the days and
  services when the team has it (DigitalOcean's daily data starts in December 2025).
* **Reconciliation** compares what was loaded for a month with the invoice's own amount.

Line items are spread over the days they cover (``start`` to ``end``) in proportion to
hours, to the micro-dollar, with the remainder on the last day, so a month's rows always sum
to the invoice. DigitalOcean bills in USD. Projects become a tag (``do:project``), so the
cost breakdown by tag splits by project. Resource names and descriptions are not stored.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from cloudoptics.analytics import funnel
from cloudoptics.connectors.service import get_connection
from cloudoptics.ingestion import reconcile as shared_reconcile
from cloudoptics.ingestion.aggregate import DailyCost, tag_hash
from cloudoptics.ingestion.pipeline import LoadedExport, LoadedFile
from cloudoptics.ingestion.quality import Check, blocked
from cloudoptics.ingestion.service import INGESTABLE, IngestResult, _record_run, _store
from cloudoptics.ingestion.source import Manifest
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.providers.digitalocean.client import Client, DigitalOceanError
from cloudoptics.providers.digitalocean.connect import usable_token

MAPPING_VERSION = "do-invoice-csv-1"
CSV_COLUMNS = (
    "product",
    "group_description",
    "description",
    "hours",
    "start",
    "end",
    "USD",
    "project_name",
    "category",
)
#: How many closed months a new connection loads.
BACKFILL_MONTHS = 12
MIN_REFRESH_INTERVAL = timedelta(hours=6)
PROJECT_TAG = "do:project"
MICRO = Decimal("0.000001")
CENT = Decimal("0.01")

# Product name (lower case, first match) -> FOCUS ServiceCategory.
CATEGORIES: tuple[tuple[str, str], ...] = (
    ("genai", "AI and Machine Learning"),
    ("gradient", "AI and Machine Learning"),
    ("inference", "AI and Machine Learning"),
    ("droplet", "Compute"),
    ("kubernetes", "Compute"),
    ("app platform", "Compute"),
    ("apps", "Compute"),
    ("function", "Compute"),
    ("gpu", "Compute"),
    ("database", "Databases"),
    ("mysql", "Databases"),
    ("postgres", "Databases"),
    ("redis", "Databases"),
    ("valkey", "Databases"),
    ("mongodb", "Databases"),
    ("kafka", "Databases"),
    ("opensearch", "Databases"),
    ("volume", "Storage"),
    ("block storage", "Storage"),
    ("spaces", "Storage"),
    ("snapshot", "Storage"),
    ("backup", "Storage"),
    ("container registry", "Storage"),
    ("nfs", "Storage"),
    ("load balancer", "Networking"),
    ("floating ip", "Networking"),
    ("reserved ip", "Networking"),
    ("bandwidth", "Networking"),
    ("nat", "Networking"),
    ("vpc", "Networking"),
    ("cdn", "Networking"),
    ("monitoring", "Management and Governance"),
    ("uptime", "Management and Governance"),
)
# Insights SKU parts (``1-DO-DROP-0109``) -> product name, for this month's services.
SKU_PRODUCTS: dict[str, str] = {
    "DROP": "Droplets",
    "KS": "Kubernetes",
    "K8S": "Kubernetes",
    "K8SWN": "Kubernetes",
    "DOKS": "Kubernetes",
    "VOL": "Volumes",
    "BS": "Volumes",
    "BLOCK": "Volumes",
    "SNAP": "Snapshots",
    "BKUP": "Backups",
    "LB": "Load Balancers",
    "LBAAS": "Load Balancers",
    "FIP": "Reserved IPs",
    "RIP": "Reserved IPs",
    "DB": "Managed Databases",
    "DBAAS": "Managed Databases",
    "SPACES": "Spaces",
    "SPC": "Spaces",
    "APP": "App Platform",
    "APPS": "App Platform",
    "BW": "Bandwidth",
    "DOCR": "Container Registry",
    "CR": "Container Registry",
    "FN": "Functions",
}


def service_code(product: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", product.lower()).strip("-") or "other"


def service_category(product: str) -> str:
    lowered = product.lower()
    return next((cat for word, cat in CATEGORIES if word in lowered), "Other")


def sku_product(sku: str) -> str:
    return next((SKU_PRODUCTS[p] for p in sku.upper().split("-") if p in SKU_PRODUCTS), "Other")


def parse_usd(value: str | None) -> Decimal | None:
    """``$3.11``, ``-$1.00``, ``($1.00)``, ``1,234.56`` -> Decimal; None if unreadable."""
    raw = (value or "").strip()
    if not raw:
        return None
    negative = raw.startswith("-") or (raw.startswith("(") and raw.endswith(")"))
    digits = re.sub(r"[^0-9.]", "", raw)
    if not digits:
        return None
    try:
        amount = Decimal(digits)
    except InvalidOperation:
        return None
    return -amount if negative else amount


def parse_time(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _usage_type(product: str, description: str) -> str:
    """The size slug in brackets (``s-1vcpu-1gb``), else the product: never a name."""
    found = re.findall(r"\(([a-z0-9][a-z0-9.\-]{0,62})\)", description or "")
    return found[-1] if found else product


def month_end(period: date) -> date:
    return (period + timedelta(days=32)).replace(day=1)


def split_by_day(
    amount: Decimal, start: datetime | None, end: datetime | None, period: date
) -> list[tuple[date, Decimal, Decimal]]:
    """(day, amount, hours) for each day the item covers inside the month, summing to
    ``amount`` exactly. An item without usable times goes on its start day (or the 1st)."""
    first = datetime(period.year, period.month, 1, tzinfo=UTC)
    last = datetime.combine(month_end(period), datetime.min.time(), tzinfo=UTC)
    lo = max(start or first, first)
    hi = min(end or last, last)
    if start is None or end is None or hi <= lo:
        day = start.date() if start is not None and first <= start < last else period
        return [(day, amount, Decimal(0))]
    pieces: list[tuple[date, Decimal]] = []
    cursor = lo
    while cursor < hi:
        nxt = min(
            datetime.combine(cursor.date() + timedelta(days=1), datetime.min.time(), tzinfo=UTC), hi
        )
        pieces.append((cursor.date(), Decimal((nxt - cursor).total_seconds()) / 3600))
        cursor = nxt
    total_hours = sum((h for _, h in pieces), Decimal(0))
    out: list[tuple[date, Decimal, Decimal]] = []
    left = amount
    for i, (day, hours) in enumerate(pieces):
        share = (
            left
            if i == len(pieces) - 1
            else (amount * hours / total_hours).quantize(MICRO, ROUND_HALF_EVEN)
        )
        left -= share
        out.append((day, share, hours.quantize(MICRO)))
    return out


@dataclass
class Invoice:
    uuid: str
    period: date
    amount: Decimal


@dataclass
class InvoiceData:
    invoice: Invoice
    csv_text: str
    summary: dict[str, Any] = field(default_factory=dict)


def _summary_amount(summary: dict[str, Any], key: str) -> Decimal:
    part = summary.get(key) or {}
    return parse_usd(str(part.get("amount") or "0")) or Decimal(0)


def map_invoice(data: InvoiceData, account_id: str, team_name: str | None) -> LoadedExport:
    """One invoice as FOCUS-shaped daily rows, with its quality checks."""
    inv = data.invoice
    key = f"digitalocean/invoices/{inv.uuid}.csv"
    raw = data.csv_text.encode()
    reader = csv.DictReader(io.StringIO(data.csv_text))
    header = tuple(reader.fieldnames or ())
    checks: list[Check] = []
    missing = [c for c in ("product", "start", "end", "USD") if c not in header]
    sums: dict[tuple[Any, ...], list[Decimal]] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    tag_sets: dict[bytes, dict[str, str]] = {}
    rows_read = unreadable = outside = 0
    line_total = Decimal(0)
    if missing:
        checks.append(Check("schema", "error", f"Missing columns: {', '.join(missing)}."))
    else:
        checks.append(Check("schema", "ok", "All required columns are present."))
        for row in reader:
            rows_read += 1
            amount = parse_usd(row.get("USD"))
            product = (row.get("product") or "").strip() or "Other"
            if amount is None:
                unreadable += 1
                continue
            line_total += amount
            start, end = parse_time(row.get("start")), parse_time(row.get("end"))
            if start and not (inv.period <= start.date() < month_end(inv.period)):
                outside += 1
            project = (row.get("project_name") or "").strip()
            digest, pairs = tag_hash([(PROJECT_TAG, project)] if project else None)
            if digest is not None:
                tag_sets.setdefault(digest, pairs)
            usage_type = _usage_type(product, row.get("description") or "")
            hours_given = parse_usd(row.get("hours")) is not None
            for day, share, hours in split_by_day(amount, start, end, inv.period):
                k = (day, product, usage_type, "Usage", digest)
                sums[k][0] += share
                sums[k][1] += hours if hours_given else Decimal(0)
    checks.append(
        Check("nulls", "error", f"{unreadable} line items have an amount that could not be read.")
        if unreadable
        else Check("nulls", "ok", "No missing key values.")
    )
    checks.append(Check("completeness", "ok", f"{rows_read} rows read."))
    if outside:
        checks.append(
            Check("period_bounds", "warning", f"{outside} rows start outside the billing period.")
        )

    period_last = month_end(inv.period) - timedelta(days=1)
    taxes = _summary_amount(data.summary, "taxes")
    credits = _summary_amount(data.summary, "credits_and_adjustments")
    if taxes:
        sums[(period_last, "Taxes", "Tax", "Tax", None)][0] += taxes
    if credits:
        sums[(period_last, "Credits", "Credit", "Credit", None)][0] += credits
    total = line_total + taxes + credits
    gap = (total - inv.amount).copy_abs()
    checks.append(
        Check(
            "invoice_total",
            "warning",
            f"Line items, taxes and credits add up to USD {total:.2f}; the invoice says "
            f"USD {inv.amount:.2f}.",
        )
        if gap >= CENT
        else Check("invoice_total", "ok", f"Matches the invoice (USD {inv.amount:.2f}).")
    )

    loaded = LoadedExport(
        manifest=Manifest(key, inv.period, inv.uuid, (key,), rows_read),
        files=[LoadedFile(key, hashlib.sha256(raw).hexdigest(), len(raw), rows_read)],
        checks=checks,
        mapping_version=MAPPING_VERSION,
    )
    if blocked(checks):
        return loaded
    loaded.rows = sorted(
        (
            DailyCost(
                usage_date=day,
                account_id=account_id,
                service_code=service_code(product),
                service_name=product,
                service_category=service_category(product) if charge == "Usage" else "Other",
                region_id=None,
                usage_type=usage_type,
                charge_category=charge,
                tag_set_hash=digest,
                billed_cost=cost.quantize(MICRO),
                effective_cost=cost.quantize(MICRO),
                net_cost=cost.quantize(MICRO),
                list_cost=None,
                usage_quantity=hours.quantize(MICRO) if hours else None,
            )
            for (day, product, usage_type, charge, digest), (cost, hours) in sums.items()
        ),
        key=lambda r: (r.usage_date, r.service_code, r.usage_type or "", r.charge_category),
    )
    loaded.tag_sets = tag_sets
    loaded.accounts = {account_id: team_name}
    return loaded


# --- reading from DigitalOcean ------------------------------------------------------------


def list_invoices(client: Client) -> list[Invoice]:
    """Closed invoices, newest first (``invoice_preview``, this month's, is left out)."""
    out: list[Invoice] = []
    for item in client.paged("/v2/customers/my/invoices", "invoices"):
        try:
            year, month = (int(p) for p in str(item["invoice_period"]).split("-")[:2])
            amount = parse_usd(str(item.get("amount")))
            out.append(
                Invoice(str(item["invoice_uuid"]), date(year, month, 1), amount or Decimal(0))
            )
        except (KeyError, ValueError):
            continue
    out.sort(key=lambda i: i.period, reverse=True)
    return out


def _safe_uuid(value: str) -> str:
    return str(uuid.UUID(value))  # refuses anything that is not a UUID (it goes in a path)


def fetch_invoices(
    token: str, done: set[tuple[date, str]], today: date
) -> tuple[list[InvoiceData], list[str], int]:
    """New closed invoices of the last ``BACKFILL_MONTHS`` months, their CSV and summary."""
    current = today.replace(day=1)
    oldest = current
    for _ in range(BACKFILL_MONTHS):
        oldest = (oldest - timedelta(days=1)).replace(day=1)
    out: list[InvoiceData] = []
    errors: list[str] = []
    with Client(token) as client:
        invoices = list_invoices(client)
        for inv in invoices:
            if inv.period >= current or inv.period < oldest or (inv.period, inv.uuid) in done:
                continue
            label = f"{inv.period:%Y-%m}@{inv.uuid}"
            try:
                path = f"/v2/customers/my/invoices/{_safe_uuid(inv.uuid)}"
                csv_text = client.get_text(f"{path}/csv")
                summary = client.get(f"{path}/summary") or {}
            except (DigitalOceanError, ValueError) as exc:
                errors.append(f"{label}: {getattr(exc, 'code', type(exc).__name__)}")
                continue
            out.append(InvoiceData(inv, csv_text, _keep_amounts(summary)))
        return out, errors, client.calls


def _keep_amounts(summary: dict[str, Any]) -> dict[str, Any]:
    """Only the money from a summary: it also names the customer and their address."""
    return {
        key: {"amount": (summary.get(key) or {}).get("amount")}
        for key in ("taxes", "credits_and_adjustments", "product_charges", "overages")
        if isinstance(summary.get(key), dict)
    }


# --- the BillingSource capability ---------------------------------------------------------

DONE = text("SELECT billing_period, export_version FROM ingest_batch WHERE connection_id = :c")


async def ingest_connection(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, today: date | None = None
) -> IngestResult:
    """Load every closed invoice not loaded yet."""
    today = today or datetime.now(UTC).date()
    maker = get_sessionmaker()
    started = datetime.now(UTC)
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        token = await usable_token(session, tenant_id, conn)
        done = {
            (r.billing_period, r.export_version)
            for r in await session.execute(DONE, {"c": connection_id})
        }
    if conn.status not in INGESTABLE or token is None:
        return IngestResult("skipped")

    result = IngestResult("up_to_date")
    try:
        invoices, result.errors, _ = await run_in_threadpool(fetch_invoices, token, done, today)
    except DigitalOceanError as exc:
        result.status = "failed"
        result.errors.append(exc.code)
        await _record_run(maker, tenant_id, connection_id, started, result)
        return result
    del token
    team = await _team_name(tenant_id, connection_id)
    for data in sorted(invoices, key=lambda d: d.invoice.period):
        loaded = map_invoice(data, conn.aws_account_id, team)
        async with maker() as session, session.begin():
            await scope(session, tenant_id=tenant_id)
            await _store(session, tenant_id, conn, loaded)
        label = f"{data.invoice.period:%Y-%m}@{data.invoice.uuid}"
        if loaded.ok:
            result.loaded.append(label)
            result.loaded_periods.append(data.invoice.period)
            result.records += loaded.row_count
        else:
            result.rejected.append(label)
    if result.errors or result.rejected:
        result.status = "partial" if result.loaded else "failed"
    elif result.loaded:
        result.status = "succeeded"
    await _record_run(maker, tenant_id, connection_id, started, result)
    return result


async def _team_name(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str | None:
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        return (
            await session.execute(
                text(
                    "SELECT name FROM cloud_account WHERE connection_id = :c "
                    "AND provider = 'digitalocean' LIMIT 1"
                ),
                {"c": connection_id},
            )
        ).scalar_one_or_none()


@dataclass
class ReloadResult:
    status: str
    reloaded: list[date] = field(default_factory=list)


async def reload_connection(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, period: date | None, today: date | None = None
) -> ReloadResult:
    """Read the invoices again (``period``, or every month loaded), even when loaded already."""
    today = today or datetime.now(UTC).date()
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        token = await usable_token(session, tenant_id, conn)
    if conn.status not in INGESTABLE or token is None:
        return ReloadResult("skipped")
    try:
        invoices, errors, _ = await run_in_threadpool(fetch_invoices, token, set(), today)
    except DigitalOceanError:
        return ReloadResult("failed")
    del token
    team = await _team_name(tenant_id, connection_id)
    result = ReloadResult("succeeded")
    for data in invoices:
        if period is not None and data.invoice.period != period:
            continue
        loaded = map_invoice(data, conn.aws_account_id, team)
        async with maker() as session, session.begin():
            await scope(session, tenant_id=tenant_id)
            await _store(session, tenant_id, conn, loaded, reload=True)
        if loaded.ok:
            result.reloaded.append(data.invoice.period)
        else:
            errors.append(f"{data.invoice.period:%Y-%m}: quality_check_failed")
    if errors:
        result.status = "partial" if result.reloaded else "failed"
    return result


# --- month to date ------------------------------------------------------------------------


@dataclass
class MonthToDate:
    period_start: date
    through_date: date
    total: Decimal
    daily: dict[date, Decimal] = field(default_factory=dict)
    by_service: dict[str, Decimal] = field(default_factory=dict)


def fetch_month_to_date(token: str, team_uuid: str, today: date) -> MonthToDate:
    start = today.replace(day=1)
    with Client(token) as client:
        balance = client.get("/v2/customers/my/balance") or {}
        total = parse_usd(str(balance.get("month_to_date_usage") or "0")) or Decimal(0)
        snap = MonthToDate(start, today, total)
        path = f"/v2/billing/do:team:{_safe_uuid(team_uuid)}/insights/{start}/{today}"
        page, pages = 1, 1
        try:
            while page <= min(pages, 50):
                body = client.get(path, {"page": page, "per_page": 200}) or {}
                for point in body.get("data_points") or []:
                    amount = parse_usd(str(point.get("total_amount") or "0")) or Decimal(0)
                    try:
                        day = date.fromisoformat(str(point.get("start_date"))[:10])
                    except ValueError:
                        continue
                    snap.daily[day] = snap.daily.get(day, Decimal(0)) + amount
                    product = sku_product(str(point.get("sku") or ""))
                    snap.by_service[product] = snap.by_service.get(product, Decimal(0)) + amount
                pages = int(body.get("total_pages") or 1)
                page += 1
        except DigitalOceanError as exc:
            if not (exc.forbidden or exc.status in (400, 404)):
                raise
            snap.daily, snap.by_service = {}, {}  # no daily data: the balance alone
        if snap.daily:
            snap.through_date = max(snap.daily)
        return snap


SNAPSHOT_STATE = text(
    "SELECT EXISTS (SELECT 1 FROM cost_daily WHERE connection_id = :c AND billing_period = :p) "
    "AS has_bill, (SELECT fetched_at FROM cost_snapshot_ce WHERE connection_id = :c "
    "AND period_start = :p) AS fetched_at"
)
UPSERT_SNAPSHOT = text(
    "INSERT INTO cost_snapshot_ce (tenant_id, connection_id, provider, period_start, "
    "through_date, effective_cost_usd, billed_cost_usd, credits_usd, daily, by_service, "
    "fetched_at) VALUES (:t, :c, 'digitalocean', :p, :through, :total, :total, 0, "
    "CAST(:daily AS jsonb), CAST(:services AS jsonb), now()) ON CONFLICT (connection_id, "
    "period_start) DO UPDATE SET through_date = EXCLUDED.through_date, "
    "effective_cost_usd = EXCLUDED.effective_cost_usd, billed_cost_usd = EXCLUDED.billed_cost_usd, "
    "credits_usd = EXCLUDED.credits_usd, daily = EXCLUDED.daily, "
    "by_service = EXCLUDED.by_service, fetched_at = now()"
)
RECORD_SNAPSHOT = text(
    "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, status, started_at, "
    "finished_at, records, error_code) VALUES (:id, :t, :c, 'ce.snapshot', :status, :started, "
    "now(), :records, :code)"
)


async def refresh_snapshot(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    today: date | None = None,
    force: bool = False,
) -> str:
    """This month's provisional costs (the ``reconcile.snapshot`` capability)."""
    today = today or datetime.now(UTC).date()
    start = today.replace(day=1)
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        if conn.status not in INGESTABLE:
            return "skipped"
        state = (await session.execute(SNAPSHOT_STATE, {"c": connection_id, "p": start})).one()
        if state.has_bill:
            return "cur_loaded"
        if (
            state.fetched_at
            and not force
            and datetime.now(UTC) - state.fetched_at < MIN_REFRESH_INTERVAL
        ):
            return "fresh"
        token = await usable_token(session, tenant_id, conn)
    if token is None:
        return "skipped"
    started = datetime.now(UTC)
    snap: MonthToDate | None = None
    error: str | None = None
    try:
        snap = await run_in_threadpool(fetch_month_to_date, token, conn.aws_account_id, today)
    except DigitalOceanError as exc:
        error = exc.code
    del token
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        if snap is not None:
            await funnel.emit(session, "first_costs", tenant_id=tenant_id, detail="provisional")
            await session.execute(
                UPSERT_SNAPSHOT,
                {
                    "t": tenant_id,
                    "c": connection_id,
                    "p": snap.period_start,
                    "through": snap.through_date,
                    "total": snap.total.quantize(MICRO),
                    "daily": json.dumps(
                        [
                            {"date": d.isoformat(), "amount": float(v)}
                            for d, v in sorted(snap.daily.items())
                        ]
                    ),
                    "services": json.dumps(
                        [
                            {"service": k, "amount": float(v)}
                            for k, v in sorted(snap.by_service.items(), key=lambda kv: -kv[1])
                        ]
                    ),
                },
            )
        await session.execute(
            RECORD_SNAPSHOT,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "status": "failed" if error else "succeeded",
                "started": started,
                "records": len(snap.daily) if snap else 0,
                "code": error,
            },
        )
    return "failed" if error else "succeeded"


# --- reconciliation -----------------------------------------------------------------------

LOADED_TOTAL = text(
    "SELECT sum(billed_cost_usd) AS billed, max(usage_date) AS last, "
    "max(data_version) AS version FROM cost_daily WHERE connection_id = :c "
    "AND billing_period = :p"
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
RECORD_RECONCILE = text(
    "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, started_at, "
    "finished_at, error_code) VALUES (:id, :t, :c, 'cost.reconcile', CAST(:scope AS jsonb), "
    ":status, now(), now(), :code)"
)


def invoice_amount(token: str, period: date) -> Decimal | None:
    with Client(token) as client:
        return next((i.amount for i in list_invoices(client) if i.period == period), None)


async def reconcile_period(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, billing_period: date
) -> str:
    """The month's loaded rows against the invoice's own amount (1% and USD 1 tolerance)."""
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        token = await usable_token(session, tenant_id, conn)
        loaded = (
            await session.execute(LOADED_TOTAL, {"c": connection_id, "p": billing_period})
        ).one()
    if token is None or loaded.billed is None:
        return "skipped"
    error: str | None = None
    comparison = None
    try:
        reference = await run_in_threadpool(invoice_amount, token, billing_period)
        if reference is None:
            error = "invoice_not_found"
        else:
            account = conn.aws_account_id
            comparison = shared_reconcile.compare(
                {account: float(loaded.billed)}, {account: float(reference)}
            )
    except DigitalOceanError as exc:
        error = exc.code
    del token
    status = comparison.status if comparison else "failed"
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await session.execute(
            UPSERT_RECONCILIATION,
            {
                "t": tenant_id,
                "c": connection_id,
                "p": billing_period,
                "v": loaded.version,
                "through": min(loaded.last, month_end(billing_period) - timedelta(days=1)),
                "status": status,
                "cur": round(comparison.cur_total, 6) if comparison else None,
                "ref": round(comparison.reference, 6) if comparison else None,
                "pct": comparison.difference_pct if comparison else None,
                "accounts": json.dumps(comparison.accounts if comparison else []),
                "code": error,
            },
        )
        await session.execute(
            RECORD_RECONCILE,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "scope": json.dumps({"billing_period": billing_period.isoformat()}),
                "status": "failed" if error else "succeeded",
                "code": error,
            },
        )
    return status
