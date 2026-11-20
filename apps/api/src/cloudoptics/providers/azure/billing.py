"""Azure bills: the Cost Management FOCUS export, loaded into the shared cost tables
(ADR-020, ADR-018).

Layout the improved exports write under the export's folder::

    <folder>/<export name>/<YYYYMMDD-YYYYMMDD>/<run id>/manifest.json
    <folder>/<export name>/<YYYYMMDD-YYYYMMDD>/<run id>/part_0_0001.parquet  (or .csv / .csv.gz)

A daily month-to-date export writes a new run each day; each run holds the whole month so
far. Unit of work: (connection, billing period, run). The newest run of a period that is not
loaded yet replaces the period's rows in one transaction (the same ``ingest_batch`` /
``billing_file`` bookkeeping as AWS), so a re-run or a restated month never doubles costs.

Mapping (``azure-focus-1``): FOCUS 1.0+ columns as Microsoft writes them, onto the same
FOCUS-shaped table AWS's CUR is mapped to (``ingestion.focus.FOCUS_SCHEMA``), then the shared
daily aggregator. ``SubAccountId`` (``/subscriptions/<guid>``) becomes the subscription GUID,
``provider_account_id``. Amounts stay in the bill's currency (``billing_currency`` and the
native columns) and are converted to USD per day with Cloudoptics' own rate (``fx.service``,
ADR-010). A day without one falls back to Microsoft's own exchange rate in the export
(``x_BilledCostInUsd``; a warning) and, without that, blocks the load (the previous good run
stays live). Credits are ``ChargeCategory = Credit`` rows, as in FOCUS.
"""

from __future__ import annotations

import gzip
import json
import re
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.compute as _pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from cloudoptics.analytics import funnel
from cloudoptics.fx import service as fx
from cloudoptics.ingestion.aggregate import DailyAggregator, DailyCost, ResourceDailyCost
from cloudoptics.ingestion.focus import FOCUS_SCHEMA
from cloudoptics.ingestion.quality import Check
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.azure import connection as azconn
from cloudoptics.providers.azure.client import AZURE_ERRORS, AzureApi, AzureError

pc: Any = _pc

MAPPING_VERSION = "azure-focus-1"
MANIFEST_MAX_BYTES = 2 * 1024 * 1024
MAX_BLOBS = 2000
RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
DATA_SUFFIXES = (".parquet", ".csv", ".csv.gz", ".gz")
INGESTABLE = ("healthy", "degraded")

REQUIRED = (
    "BilledCost",
    "BillingCurrency",
    "ChargeCategory",
    "ChargePeriodStart",
    "EffectiveCost",
    "ServiceName",
    "SubAccountId",
)
NUMERIC = (
    "BilledCost",
    "EffectiveCost",
    "ListCost",
    "ContractedCost",
    "ConsumedQuantity",
    "PricingQuantity",
    "x_BilledCostInUsd",
    "x_EffectiveCostInUsd",
    "x_BillingExchangeRate",
)
CHARGE_CATEGORIES = frozenset({"Usage", "Purchase", "Tax", "Credit", "Adjustment"})
NEGATIVE_ALLOWED = frozenset({"Credit", "Adjustment"})


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ExportRun:
    key: str
    billing_period: date
    run_id: str
    submitted: str
    data_keys: tuple[str, ...]
    num_rows: int | None


def _first_of_month(value: str) -> date:
    day = datetime.fromisoformat(value.replace("Z", "+00:00")[:19]).date()
    return day.replace(day=1)


def parse_manifest(key: str, body: str, prefix: str) -> ExportRun:
    """One run's manifest. Its data files must sit under the export folder."""
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ManifestError(f"{key}: not JSON") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{key}: not a manifest")
    config = data.get("exportConfig") or {}
    kind = str(config.get("type") or "")
    if kind.lower() != "focuscost":
        raise ManifestError(f"{key}: export type {kind or 'unknown'} is not FOCUS")
    run = data.get("runInfo") or {}
    run_id = str(run.get("runId") or "")
    if not RUN_ID.fullmatch(run_id):
        raise ManifestError(f"{key}: no usable run ID")
    try:
        period = _first_of_month(str(run.get("startDate") or ""))
    except ValueError as exc:
        raise ManifestError(f"{key}: no start date") from exc
    blobs = data.get("blobs") or []
    if not isinstance(blobs, list) or len(blobs) > MAX_BLOBS:
        raise ManifestError(f"{key}: more than {MAX_BLOBS} data files")
    names = []
    for blob in blobs:
        name = str((blob or {}).get("blobName") or "").strip()
        if not name.startswith(prefix) or ".." in name.split("/"):
            raise ManifestError(f"{key}: a data file is outside the export folder")
        if not name.lower().endswith(DATA_SUFFIXES):
            continue
        names.append(name)
    rows = data.get("dataRowCount")
    return ExportRun(
        key=key,
        billing_period=period,
        run_id=run_id,
        submitted=str(run.get("submittedTime") or ""),
        data_keys=tuple(names),
        num_rows=int(rows) if isinstance(rows, int) else None,
    )


def latest_runs(runs: list[ExportRun]) -> list[ExportRun]:
    """The newest run of each billing period, oldest period first."""
    best: dict[date, ExportRun] = {}
    for r in runs:
        current = best.get(r.billing_period)
        if current is None or (r.submitted, r.run_id) > (current.submitted, current.run_id):
            best[r.billing_period] = r
    return [best[p] for p in sorted(best)]


# --- reading and mapping ------------------------------------------------------------------


def _col(batch: pa.RecordBatch, name: str, type_: Any) -> Any:
    idx = batch.schema.get_field_index(name)
    if idx < 0:
        return pa.nulls(batch.num_rows, type_)
    arr = batch.column(idx)
    if arr.type == type_:
        return arr
    if pa.types.is_string(arr.type) and pa.types.is_floating(type_):
        arr = pc.if_else(pc.equal(arr, ""), pa.scalar(None, pa.string()), arr)
    return pc.cast(arr, type_)


def _text(batch: pa.RecordBatch, name: str) -> Any:
    arr = _col(batch, name, pa.string())
    return pc.if_else(pc.equal(arr, ""), pa.scalar(None, pa.string()), arr)


def _ts(batch: pa.RecordBatch, name: str) -> Any:
    idx = batch.schema.get_field_index(name)
    target = pa.timestamp("ms", tz="UTC")
    if idx < 0:
        return pa.nulls(batch.num_rows, target)
    arr = batch.column(idx)
    if pa.types.is_timestamp(arr.type):
        if arr.type.tz is None:
            arr = pc.assume_timezone(arr, "UTC")
        return pc.cast(arr, target)
    values = [_parse_time(v) for v in pc.cast(arr, pa.string()).to_pylist()]
    return pa.array(values, target)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text_ = value.strip().replace("Z", "+00:00")
    # Seven fractional digits (.0000000) are more than fromisoformat takes before 3.11's rules.
    text_ = re.sub(r"(\.\d{6})\d+", r"\1", text_)
    try:
        parsed = datetime.fromisoformat(text_)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_tags(value: Any) -> list[tuple[str, str]] | None:
    """Microsoft writes tags as JSON, sometimes without the braces."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        data: Any = value
    else:
        raw = str(value).strip()
        if not raw.startswith("{"):
            raw = "{" + raw + "}"
        try:
            data = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(data, dict) or not data:
        return None
    return sorted((str(k), "" if v is None else str(v)) for k, v in data.items())


def subscription_of(value: str | None) -> str | None:
    if not value:
        return None
    match = azconn.GUID.search(value)
    return match.group(0).lower() if match else value.strip().lower()


def normalise(batch: pa.RecordBatch, default_account: str = "unassigned") -> pa.Table:
    """One batch of a Microsoft FOCUS export as ``FOCUS_SCHEMA``, amounts in its currency."""
    n = batch.num_rows
    f64 = pa.float64()
    zero = pa.scalar(0.0, f64)
    billed = pc.fill_null(_col(batch, "BilledCost", f64), zero)
    effective = pc.fill_null(_col(batch, "EffectiveCost", f64), billed)
    start = _ts(batch, "ChargePeriodStart")
    meter = _text(batch, "x_SkuMeterCategory")
    service = pc.fill_null(_text(batch, "ServiceName"), "Unknown")
    category = _text(batch, "ChargeCategory")
    resource = _text(batch, "ResourceId")
    tags_idx = batch.schema.get_field_index("Tags")
    tags = (
        [parse_tags(v) for v in batch.column(tags_idx).to_pylist()] if tags_idx >= 0 else [None] * n
    )
    # Unused reservations and savings plans and rounding adjustments have no subscription
    # (Microsoft sets it null): they count against the connection's own account.
    subs = [subscription_of(v) or default_account for v in _text(batch, "SubAccountId").to_pylist()]
    region = _text(batch, "RegionId")
    columns: dict[str, Any] = {
        "BillingAccountId": _text(batch, "BillingAccountId"),
        "SubAccountId": pa.array(subs, pa.string()),
        "SubAccountName": _text(batch, "SubAccountName"),
        "BillingPeriodStart": _ts(batch, "BillingPeriodStart"),
        "ChargePeriodStart": start,
        "ChargePeriodEnd": _ts(batch, "ChargePeriodEnd"),
        "ChargeDate": pc.cast(start, pa.date32()),
        "ChargeCategory": category,
        "ChargeClass": _text(batch, "ChargeClass"),
        "ChargeDescription": _text(batch, "ChargeDescription"),
        "ProviderName": pa.array(["Microsoft"] * n, pa.string()),
        "ServiceName": service,
        "ServiceCategory": pc.fill_null(_text(batch, "ServiceCategory"), "Other"),
        "RegionId": pc.utf8_lower(region),
        "AvailabilityZone": _text(batch, "AvailabilityZone"),
        "ResourceId": pc.utf8_lower(resource),
        "PricingUnit": _text(batch, "PricingUnit"),
        "ConsumedQuantity": _col(batch, "ConsumedQuantity", f64),
        "BilledCost": billed,
        "EffectiveCost": effective,
        "ListCost": _col(batch, "ListCost", f64),
        "BillingCurrency": pc.utf8_upper(_text(batch, "BillingCurrency")),
        "CommitmentDiscountId": _text(batch, "CommitmentDiscountId"),
        "CommitmentDiscountType": _text(batch, "CommitmentDiscountType"),
        "Tags": pa.array(tags, pa.map_(pa.string(), pa.string())),
        "x_NetCost": billed,
        "x_ServiceCode": pc.coalesce(meter, service),
        # SkuMeter (FOCUS 1.2) replaces x_SkuMeterName (1.0); either may be present.
        "x_UsageType": pc.fill_null(
            pc.coalesce(_text(batch, "SkuMeter"), _text(batch, "x_SkuMeterName")), "Usage"
        ),
        "x_Operation": pa.nulls(n, pa.string()),
        "x_LineItemType": category,
        "x_LineItemId": _text(batch, "x_ChargeId"),
        "x_TimeInterval": pa.nulls(n, pa.string()),
        "x_CommitmentKind": pa.nulls(n, pa.string()),
    }
    return pa.Table.from_pydict(columns, schema=FOCUS_SCHEMA)


def _csv_reader(path: Path) -> Any:
    opener: Any = gzip.open if path.name.endswith(".gz") or _is_gzip(path) else open
    with opener(path, "rt", encoding="utf-8-sig", newline="") as fh:
        header = fh.readline()
    columns = [c.strip().strip('"') for c in header.rstrip("\r\n").split(",")]
    types = {c: (pa.float64() if c in NUMERIC else pa.string()) for c in columns}
    return pacsv.open_csv(
        str(path),
        read_options=pacsv.ReadOptions(block_size=16 * 1024 * 1024),
        convert_options=pacsv.ConvertOptions(
            column_types=types, strings_can_be_null=True, null_values=[""]
        ),
    )


def _is_gzip(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def batches(path: Path, name: str) -> Any:
    if name.lower().endswith(".parquet"):
        with pq.ParquetFile(path) as parquet:
            yield from parquet.iter_batches(batch_size=200_000)
        return
    reader = _csv_reader(path)
    yield from reader


@dataclass
class LoadedRun:
    run: ExportRun
    files: list[tuple[str, str, int, int]] = field(default_factory=list)  # key, sha, size, rows
    checks: list[Check] = field(default_factory=list)
    rows: list[DailyCost] = field(default_factory=list)
    resource_rows: list[ResourceDailyCost] = field(default_factory=list)
    tag_sets: dict[bytes, dict[str, str]] = field(default_factory=dict)
    subscriptions: dict[str, str | None] = field(default_factory=dict)
    currency: str = "USD"
    # day -> [native billed, Microsoft's USD for it], for the fallback rate
    usd_by_day: dict[date, list[float]] = field(default_factory=dict)
    row_count: int = 0

    @property
    def ok(self) -> bool:
        return not any(c.status == "error" for c in self.checks)


def aggregate(
    run: ExportRun, files: list[tuple[str, Path, str, int]], default_account: str = "unassigned"
) -> LoadedRun:
    """Map and aggregate a run's downloaded files (CPU-bound: runs in a worker thread)."""
    loaded = LoadedRun(run)
    aggregator = DailyAggregator()
    missing: set[str] = set()
    currencies: set[str] = set()
    nulls = out_of_period = 0
    negative = 0.0
    unknown_categories: set[str] = set()
    lo = run.billing_period - timedelta(days=1)
    hi = (run.billing_period + timedelta(days=32)).replace(day=1) + timedelta(days=1)
    max_rows = get_settings().cur_max_period_rows
    for key, path, sha, size in files:
        count = 0
        for batch in batches(path, key):
            missing |= {c for c in REQUIRED if batch.schema.get_field_index(c) < 0}
            if missing:
                break
            focus = normalise(batch, default_account)
            count += focus.num_rows
            loaded.row_count += focus.num_rows
            if loaded.row_count > max_rows:
                raise AzureError(0, "too_large", f"more than {max_rows:,} rows in the period")
            nulls += (
                pc.sum(
                    pc.cast(
                        pc.is_null(focus["ChargePeriodStart"]),
                        pa.int64(),
                    )
                ).as_py()
                or 0
            )
            day = focus["ChargeDate"]
            outside = pc.or_(
                pc.less(day, pa.scalar(lo, pa.date32())),
                pc.greater_equal(day, pa.scalar(hi, pa.date32())),
            )
            out_of_period += pc.sum(pc.cast(pc.fill_null(outside, False), pa.int64())).as_py() or 0
            currencies |= {c for c in pc.unique(focus["BillingCurrency"]).to_pylist() if c}
            categories = set(pc.unique(focus["ChargeCategory"]).to_pylist())
            unknown_categories |= {c or "(empty)" for c in categories} - CHARGE_CATEGORIES
            neg = pc.and_(
                pc.less(focus["BilledCost"], 0),
                pc.invert(pc.is_in(focus["ChargeCategory"], pa.array(sorted(NEGATIVE_ALLOWED)))),
            )
            negative += pc.sum(pc.filter(focus["BilledCost"], neg)).as_py() or 0.0
            for sub, name in zip(
                focus["SubAccountId"].to_pylist(), focus["SubAccountName"].to_pylist(), strict=True
            ):
                if sub and azconn.GUID.fullmatch(sub) and (name or sub not in loaded.subscriptions):
                    loaded.subscriptions[sub] = name
            usd = _col(batch, "x_BilledCostInUsd", pa.float64())
            for d, native, in_usd in zip(
                day.to_pylist(), focus["BilledCost"].to_pylist(), usd.to_pylist(), strict=True
            ):
                if d is None:
                    continue
                slot = loaded.usd_by_day.setdefault(d, [0.0, 0.0, 0.0])
                slot[0] += native or 0.0
                if in_usd is not None:
                    slot[1] += in_usd
                    slot[2] += native or 0.0
            # Unknown categories cannot be stored (cost_daily's CHECK): counted as Adjustment.
            if unknown_categories:
                focus = focus.set_column(
                    focus.schema.get_field_index("ChargeCategory"),
                    "ChargeCategory",
                    pc.if_else(
                        pc.fill_null(
                            pc.is_in(focus["ChargeCategory"], pa.array(sorted(CHARGE_CATEGORIES))),
                            False,
                        ),
                        focus["ChargeCategory"],
                        pa.scalar("Adjustment"),
                    ),
                )
            aggregator.update(focus)
        loaded.files.append((key, sha, size, count))

    checks = loaded.checks
    if missing:
        checks.append(Check("schema", "error", f"Missing columns: {', '.join(sorted(missing))}."))
    else:
        checks.append(Check("schema", "ok", "All required FOCUS columns are present."))
    if run.num_rows is not None and run.num_rows != loaded.row_count and not missing:
        checks.append(
            Check(
                "completeness",
                "error",
                f"Read {loaded.row_count} rows; the manifest lists {run.num_rows}.",
            )
        )
    else:
        checks.append(Check("completeness", "ok", f"{loaded.row_count} rows read."))
    if nulls:
        checks.append(Check("nulls", "error", f"{nulls} rows have no charge period."))
    else:
        checks.append(Check("nulls", "ok", "No missing key values."))
    if len(currencies) > 1:
        checks.append(
            Check(
                "currency",
                "error",
                f"The export mixes billing currencies ({', '.join(sorted(currencies))}).",
            )
        )
    elif currencies:
        loaded.currency = next(iter(currencies))
        if not re.fullmatch(r"[A-Z]{3}", loaded.currency):
            checks.append(Check("currency", "error", f"Unknown currency {loaded.currency}."))
    if out_of_period:
        checks.append(
            Check("period_bounds", "warning", f"{out_of_period} rows are dated outside the month.")
        )
    if negative < 0:
        checks.append(
            Check(
                "negative_costs",
                "warning",
                f"{loaded.currency} {negative:.2f} of negative cost outside credits and "
                "adjustments.",
            )
        )
    if unknown_categories:
        checks.append(
            Check(
                "charge_categories",
                "warning",
                "Unknown charge categories (counted as Adjustment): "
                f"{', '.join(sorted(unknown_categories))}.",
            )
        )
    if loaded.ok:
        loaded.rows = aggregator.finish()
        loaded.resource_rows = aggregator.resource_rows()
        loaded.tag_sets = aggregator.tag_sets
    return loaded


# --- currency -----------------------------------------------------------------------------


async def usd_rates(session: AsyncSession, loaded: LoadedRun) -> dict[date, float]:
    """Local units per USD for each day with rows; adds the ``fx`` check."""
    days = sorted(
        {r.usage_date for r in loaded.rows} | {r.usage_date for r in loaded.resource_rows}
    )
    if loaded.currency == "USD":
        return dict.fromkeys(days, 1.0)
    rates: dict[date, float] = {}
    fallback: list[date] = []
    missing: list[date] = []
    for day in days:
        quote = await fx.quote(session, loaded.currency, day)
        if quote is not None and quote.rate > 0:
            rates[day] = quote.rate
            continue
        slot = loaded.usd_by_day.get(day)
        if slot and abs(slot[1]) >= 0.005 and abs(slot[2]) >= 0.005:
            rates[day] = slot[2] / slot[1]
            fallback.append(day)
        elif slot is None or abs(slot[0]) < 0.005:
            rates[day] = 0.0  # nothing to convert that day
        else:
            missing.append(day)
    if missing:
        loaded.checks.append(
            Check(
                "fx",
                "error",
                f"No {loaded.currency} exchange rate for {len(missing)} day(s) from "
                f"{missing[0]:%d %b %Y}. Ask Cloudoptics staff to set one.",
            )
        )
    elif fallback:
        loaded.checks.append(
            Check(
                "fx",
                "warning",
                f"Converted {len(fallback)} day(s) with Microsoft's exchange rate: Cloudoptics "
                f"has no {loaded.currency} rate for them yet.",
            )
        )
    else:
        loaded.checks.append(
            Check("fx", "ok", f"Converted from {loaded.currency} at Cloudoptics' daily rates.")
        )
    return rates


def _usd(value: Decimal | None, rate: float) -> Decimal | None:
    if value is None:
        return None
    if rate <= 0:
        return Decimal("0")
    return (value / Decimal(str(rate))).quantize(Decimal("0.000001"))


# --- the job ------------------------------------------------------------------------------


@dataclass
class IngestResult:
    status: Literal["skipped", "up_to_date", "succeeded", "partial", "failed"]
    loaded: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    records: int = 0
    loaded_periods: list[date] = field(default_factory=list)

    @property
    def reloaded(self) -> list[date]:
        return self.loaded_periods


def _prefix(s: azconn.AzureSettings) -> str:
    folder = s.export_path.strip("/")
    return f"{folder}/" if folder else ""


async def discover(api: AzureApi, s: azconn.AzureSettings) -> tuple[list[ExportRun], list[str]]:
    prefix = _prefix(s)
    blobs = await api.list_blobs(s.storage_account, s.container, prefix)
    runs, errors = [], []
    for blob in blobs:
        if not blob.name.endswith("/manifest.json") and blob.name != "manifest.json":
            continue
        try:
            body = await api.read_blob(
                s.storage_account, s.container, blob.name, MANIFEST_MAX_BYTES
            )
            run = parse_manifest(blob.name, body.decode("utf-8-sig"), prefix)
            if not run.data_keys:
                folder = blob.name.rsplit("/", 1)[0] + "/"
                keys = tuple(
                    b.name
                    for b in blobs
                    if b.name.startswith(folder) and b.name.lower().endswith(DATA_SUFFIXES)
                )
                run = replace(run, data_keys=keys)
            runs.append(run)
        except (*AZURE_ERRORS, ManifestError, UnicodeDecodeError) as exc:
            errors.append(f"{blob.name}: {exc}")
    return latest_runs(runs), errors


async def ingest_connection(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    periods: set[date] | None = None,
    reload: bool = False,
) -> IngestResult:
    """Load the newest run of each period not loaded yet (``reload``: of ``periods``, or
    every period, even when loaded)."""
    maker = get_sessionmaker()
    started = datetime.now(UTC)
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        row = await azconn.load(session, connection_id)
        if row is None:
            return IngestResult("skipped")
        s = azconn.from_row(row)
        done = {
            (r.billing_period, r.export_version)
            for r in await session.execute(
                text(
                    "SELECT billing_period, export_version FROM ingest_batch "
                    "WHERE connection_id = :c AND status <> 'failed'"
                ),
                {"c": connection_id},
            )
        }
    if s.status not in INGESTABLE or not s.verified:
        return IngestResult("skipped")
    api = AzureApi(s.directory_id)
    result = IngestResult("up_to_date")
    try:
        runs, result.errors = await discover(api, s)
    except AZURE_ERRORS as exc:
        result.status = "failed"
        result.errors.append(getattr(exc, "code", type(exc).__name__))
        await _record_run(maker, tenant_id, connection_id, started, result, api.calls)
        return result
    limit = get_settings().cur_max_period_bytes
    for run in runs:
        if periods is not None and run.billing_period not in periods:
            continue
        if not reload and (run.billing_period, run.run_id) in done:
            continue
        label = f"{run.billing_period:%Y-%m}@{run.run_id}"
        try:
            with tempfile.TemporaryDirectory(prefix="azure-") as tmp:
                files: list[tuple[str, Path, str, int]] = []
                left = limit
                for index, key in enumerate(run.data_keys):
                    local = Path(tmp) / f"{index:05d}{_suffix(key)}"
                    sha, size = await api.download_blob(
                        s.storage_account, s.container, key, local, left
                    )
                    left -= size
                    files.append((key, local, sha, size))
                loaded = await run_in_threadpool(aggregate, run, files, s.account_id)
        except (*AZURE_ERRORS, OSError, pa.ArrowException, ValueError) as exc:
            result.errors.append(f"{label}: {getattr(exc, 'code', '') or type(exc).__name__}")
            continue
        async with maker() as session, session.begin():
            await scope(session, tenant_id=tenant_id)
            rates = await usd_rates(session, loaded) if loaded.ok else {}
            await store(session, tenant_id, s, loaded, rates, reload=reload)
        if loaded.ok:
            result.loaded.append(label)
            result.loaded_periods.append(run.billing_period)
            result.records += loaded.row_count
        else:
            result.rejected.append(label)
    if result.errors or result.rejected:
        result.status = "partial" if result.loaded else "failed"
    elif result.loaded:
        result.status = "succeeded"
    await _record_run(maker, tenant_id, connection_id, started, result, api.calls)
    return result


def _suffix(key: str) -> str:
    lowered = key.lower()
    return next((x for x in (".csv.gz", ".parquet", ".csv") if lowered.endswith(x)), ".bin")


INSERT_BATCH = text(
    "INSERT INTO ingest_batch (id, tenant_id, connection_id, billing_period, export_version, "
    "manifest_key, mapping_version, status, row_count, checks, error_code, reload) VALUES (:id, "
    ":t, :c, :p, :v, :key, :mv, :status, :rows, CAST(:checks AS jsonb), :code, :reload) "
    "RETURNING id"
)
SUPERSEDE_BATCHES = text(
    "UPDATE ingest_batch SET status = 'superseded' WHERE connection_id = :c "
    "AND billing_period = :p AND id <> :id AND status = 'succeeded'"
)
SUPERSEDE_FILES = text(
    "UPDATE billing_file SET status = 'superseded' WHERE connection_id = :c "
    "AND billing_period = :p AND batch_id <> :id"
)
DELETE_DAILY = text("DELETE FROM cost_daily WHERE connection_id = :c AND billing_period = :p")
DELETE_RESOURCE_DAILY = text(
    "DELETE FROM cost_resource_daily WHERE connection_id = :c AND billing_period = :p"
)
INSERT_TAGS = text(
    "INSERT INTO tag_set (tenant_id, hash, tags) VALUES (:t, :h, CAST(:tags AS jsonb)) "
    "ON CONFLICT DO NOTHING"
)
INSERT_DAILY = text(
    "INSERT INTO cost_daily (tenant_id, connection_id, provider, usage_date, billing_period, "
    "provider_account_id, service_code, service_name, service_category, region_id, usage_type, "
    "charge_category, tag_set_hash, billing_currency, billed_cost, effective_cost, net_cost, "
    "list_cost, billed_cost_usd, effective_cost_usd, net_cost_usd, list_cost_usd, "
    "usage_quantity, data_version) VALUES (:t, :c, 'azure', :d, :p, :acct, :svc, :svc_name, "
    ":cat, :region, :usage_type, :charge, :tags, :ccy, :billed, :effective, :net, :list, "
    ":billed_usd, :effective_usd, :net_usd, :list_usd, :qty, :v)"
)
INSERT_RESOURCE_DAILY = text(
    "INSERT INTO cost_resource_daily (tenant_id, connection_id, provider, usage_date, "
    "billing_period, provider_account_id, service_code, region_id, usage_type, resource_id, "
    "tag_set_hash, billing_currency, effective_cost, effective_cost_usd, usage_quantity) "
    "VALUES (:t, :c, 'azure', :d, :p, :acct, :svc, :region, :usage_type, :resource, :tags, "
    ":ccy, :effective, :effective_usd, :qty)"
)
UPSERT_ACCOUNT = text(
    "INSERT INTO cloud_account (id, tenant_id, connection_id, provider, provider_account_id, "
    "name, account_type, resource_access) VALUES (:id, :t, :c, 'azure', :acct, :name, :type, "
    "false) ON CONFLICT (tenant_id, provider, provider_account_id) "
    "DO UPDATE SET name = COALESCE(EXCLUDED.name, cloud_account.name)"
)
INSERT_FILE = text(
    "INSERT INTO billing_file (id, tenant_id, connection_id, batch_id, billing_period, "
    "export_version, object_key, sha256, size_bytes, row_count, status) VALUES (:id, :t, :c, "
    ":b, :p, :v, :key, :sha, :size, :rows, :status) ON CONFLICT (tenant_id, connection_id, "
    "billing_period, export_version, object_key) DO UPDATE SET batch_id = EXCLUDED.batch_id, "
    "sha256 = EXCLUDED.sha256, size_bytes = EXCLUDED.size_bytes, "
    "row_count = EXCLUDED.row_count, status = EXCLUDED.status"
)


async def store(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    s: azconn.AzureSettings,
    loaded: LoadedRun,
    rates: dict[date, float],
    *,
    reload: bool = False,
) -> None:
    """Record the run and, when it passed the checks, replace the period's rows."""
    run = loaded.run
    period = run.billing_period
    ok = loaded.ok
    batch_id = (
        await session.execute(
            INSERT_BATCH,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": s.id,
                "p": period,
                "v": run.run_id,
                "key": run.key,
                "mv": MAPPING_VERSION,
                "status": "succeeded" if ok else "failed",
                "rows": loaded.row_count,
                "checks": json.dumps([c.as_json() for c in loaded.checks]),
                "code": None if ok else "quality_check_failed",
                "reload": reload,
            },
        )
    ).scalar_one()
    if ok:
        params = {"c": s.id, "p": period, "id": batch_id}
        await session.execute(SUPERSEDE_BATCHES, params)
        await session.execute(SUPERSEDE_FILES, params)
        await session.execute(DELETE_DAILY, {"c": s.id, "p": period})
        await session.execute(DELETE_RESOURCE_DAILY, {"c": s.id, "p": period})
        if loaded.tag_sets:
            await session.execute(
                INSERT_TAGS,
                [
                    {"t": tenant_id, "h": h, "tags": json.dumps(t)}
                    for h, t in loaded.tag_sets.items()
                ],
            )
        ccy = loaded.currency
        if loaded.rows:
            await funnel.emit(session, "first_costs", tenant_id=tenant_id, detail="bills")
            await session.execute(
                INSERT_DAILY,
                [
                    {
                        "t": tenant_id,
                        "c": s.id,
                        "d": r.usage_date,
                        "p": period,
                        "acct": r.account_id,
                        "svc": r.service_code,
                        "svc_name": r.service_name,
                        "cat": r.service_category,
                        "region": r.region_id,
                        "usage_type": r.usage_type,
                        "charge": r.charge_category,
                        "tags": r.tag_set_hash,
                        "ccy": ccy,
                        "billed": r.billed_cost,
                        "effective": r.effective_cost,
                        "net": r.net_cost,
                        "list": r.list_cost,
                        "billed_usd": _usd(r.billed_cost, rates.get(r.usage_date, 0.0)),
                        "effective_usd": _usd(r.effective_cost, rates.get(r.usage_date, 0.0)),
                        "net_usd": _usd(r.net_cost, rates.get(r.usage_date, 0.0)),
                        "list_usd": _usd(r.list_cost, rates.get(r.usage_date, 0.0)),
                        "qty": r.usage_quantity,
                        "v": run.run_id,
                    }
                    for r in loaded.rows
                ],
            )
        if loaded.resource_rows:
            await session.execute(
                INSERT_RESOURCE_DAILY,
                [
                    {
                        "t": tenant_id,
                        "c": s.id,
                        "d": r.usage_date,
                        "p": period,
                        "acct": r.account_id,
                        "svc": r.service_code,
                        "region": r.region_id,
                        "usage_type": r.usage_type,
                        "resource": r.resource_id,
                        "tags": r.tag_set_hash,
                        "ccy": ccy,
                        "effective": r.effective_cost,
                        "effective_usd": _usd(r.effective_cost, rates.get(r.usage_date, 0.0)),
                        "qty": r.usage_quantity,
                    }
                    for r in loaded.resource_rows
                ],
            )
        allowed = set(s.subscription_ids) | (
            {s.account_id} if s.scope_type == "subscription" else set()
        )
        for sub, name in sorted(loaded.subscriptions.items()):
            # A billing account's bill names its subscriptions; a subscription's bill only it.
            if s.scope_type == "subscription" and sub not in allowed:
                continue
            await session.execute(
                UPSERT_ACCOUNT,
                {
                    "id": uuid7(),
                    "t": tenant_id,
                    "c": s.id,
                    "acct": sub,
                    "name": name,
                    "type": "standalone" if s.scope_type == "subscription" else "member",
                },
            )
    if loaded.files:
        await session.execute(
            INSERT_FILE,
            [
                {
                    "id": uuid7(),
                    "t": tenant_id,
                    "c": s.id,
                    "b": batch_id,
                    "p": period,
                    "v": run.run_id,
                    "key": key,
                    "sha": sha,
                    "size": size,
                    "rows": rows,
                    "status": "aggregated" if ok else "failed",
                }
                for key, sha, size, rows in loaded.files
            ],
        )


async def _record_run(
    maker: Any,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    started: datetime,
    result: IngestResult,
    calls: int,
) -> None:
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await session.execute(
            text(
                "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, "
                "started_at, finished_at, records, error_code, error_detail) VALUES "
                "(:id, :t, :c, 'cur.ingest', CAST(:scope AS jsonb), :status, :started, now(), "
                ":records, :code, :detail)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "scope": json.dumps({"loaded": result.loaded, "rejected": result.rejected}),
                "status": "succeeded" if result.status == "up_to_date" else result.status,
                "started": started,
                "records": result.records,
                "code": "quality_check_failed"
                if result.rejected and not result.errors
                else ("load_failed" if result.errors else None),
                "detail": "; ".join(result.errors)[:2000] or None,
            },
        )


async def reload_connection(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, period: date | None
) -> Any:
    """Reload bills (admins): the newest run of ``period``, or of every month, read again even
    when loaded; answered as the shared ``ingestion.reload.ReloadResult``."""
    from cloudoptics.ingestion.reload import PeriodResult, ReloadResult

    result = await ingest_connection(
        tenant_id, connection_id, periods={period} if period else None, reload=True
    )
    out = ReloadResult(
        "skipped" if result.status == "skipped" else "succeeded",
        period=period,
        errors=list(result.errors),
        records=result.records,
    )
    seen: set[date] = set()
    for label, outcome in [(x, "reloaded") for x in result.loaded] + [
        (x, "failed") for x in result.rejected
    ]:
        month, version = label.split("@", 1)
        day = date.fromisoformat(f"{month}-01")
        seen.add(day)
        out.periods.append(PeriodResult(day, outcome, version))  # type: ignore[arg-type]
    for error in result.errors:
        head = error.split("@", 1)[0]
        if len(head) == 7 and head[4] == "-":
            day = date.fromisoformat(f"{head}-01")
            if day not in seen:
                seen.add(day)
                out.periods.append(PeriodResult(day, "failed", detail=error[:300]))
    if period is not None and period not in seen and result.status != "skipped":
        out.periods.append(PeriodResult(period, "not_in_bucket"))
    if any(p.outcome == "failed" for p in out.periods) or (result.errors and not out.periods):
        out.status = "partial" if out.reloaded else "failed"
    return out
