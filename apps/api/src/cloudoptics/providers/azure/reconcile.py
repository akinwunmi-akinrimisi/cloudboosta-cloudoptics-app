"""Azure reconciliation and month-to-date snapshot, from the Cost Management query API
(ADR-020). Both calls are free (rate-limited by Azure).

- ``reconcile_period``: after a run loads, the period's billed total per subscription in the
  bill's own currency is compared with Cost Management's ``ActualCost`` for the same days
  (the shared rule: over 1% and at least 1 unit is a mismatch). Comparing in the native
  currency keeps exchange rates out of it; ``cost_reconciliation`` stores both totals in USD
  at the rate the loaded rows used.
- ``refresh_snapshot``: until this month's export has loaded, one daily query by service
  gives provisional month-to-date costs (``cost_snapshot_ce``, native and USD amounts; USD
  from Microsoft's ``CostUSD``).
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text

from cloudoptics.analytics import funnel
from cloudoptics.ingestion.reconcile import compare
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.providers.azure import connection as azconn
from cloudoptics.providers.azure.client import API, AZURE_ERRORS, AzureApi

log = logging.getLogger(__name__)

QUERY_API = API["cost_management"]
LOADED = text(
    "SELECT provider_account_id AS account_id, sum(billed_cost) AS billed, "
    "sum(billed_cost_usd) AS billed_usd, max(usage_date) AS last, max(data_version) AS version, "
    "max(billing_currency) AS currency FROM cost_daily WHERE connection_id = :c "
    "AND billing_period = :p GROUP BY provider_account_id"
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
SYNC_RUN = text(
    "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, started_at, "
    "finished_at, records, error_code, est_customer_api_cost_usd) VALUES (:id, :t, :c, :job, "
    "CAST(:scope AS jsonb), :status, :started, now(), :records, :code, 0)"
)


def _columns(props: dict[str, Any]) -> dict[str, int]:
    return {str(c.get("name")).lower(): i for i, c in enumerate(props.get("columns") or [])}


async def query(api: AzureApi, scope_path: str, body: dict[str, Any]) -> dict[str, Any]:
    """One Cost Management query; ``properties`` with ``columns`` and every ``rows`` page."""
    page = await api.arm(
        "POST",
        f"{scope_path}/providers/Microsoft.CostManagement/query",
        api_version=QUERY_API,
        body=body,
    )
    props = dict(page.get("properties") or {})
    rows = list(props.get("rows") or [])
    link = props.get("nextLink")
    for _ in range(20):
        if not link:
            break
        # nextLink carries its own api-version and $skiptoken; the body is sent again.
        page = await api.arm("POST", api.same_host(str(link)), api_version=None, body=body)
        more = page.get("properties") or {}
        rows.extend(more.get("rows") or [])
        link = more.get("nextLink")
    props["rows"] = rows
    return props


def _period(start: date, through: date) -> dict[str, str]:
    return {"from": f"{start:%Y-%m-%d}T00:00:00Z", "to": f"{through:%Y-%m-%d}T23:59:59Z"}


async def reference_totals(
    api: AzureApi, s: azconn.AzureSettings, start: date, through: date
) -> dict[str, float]:
    """Cost Management's actual cost per subscription, in the billing currency."""
    props = await query(
        api,
        s.scope,
        {
            "type": "ActualCost",
            "timeframe": "Custom",
            "timePeriod": _period(start, through),
            "dataset": {
                "granularity": "None",
                "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                "grouping": [{"type": "Dimension", "name": "SubscriptionId"}],
            },
        },
    )
    cols = _columns(props)
    cost_i = cols.get("cost", cols.get("pretaxcost", 0))
    sub_i = cols.get("subscriptionid", 1)
    totals: dict[str, float] = defaultdict(float)
    for row in props["rows"]:
        totals[str(row[sub_i]).lower()] += float(row[cost_i] or 0)
    return dict(totals)


async def reconcile_period(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, billing_period: date
) -> str:
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        row = await azconn.load(session, connection_id)
        if row is None or row.status == "disconnected":
            return "skipped"
        s = azconn.from_row(row)
        loaded = (await session.execute(LOADED, {"c": connection_id, "p": billing_period})).all()
    if not loaded:
        return "skipped"
    month_end = (billing_period + timedelta(days=32)).replace(day=1)
    through = min(max(r.last for r in loaded), month_end - timedelta(days=1))
    ours = {r.account_id: float(r.billed) for r in loaded}
    native = sum(ours.values())
    usd = sum(float(r.billed_usd) for r in loaded)
    ratio = usd / native if native else 1.0
    api = AzureApi(s.directory_id)
    comparison = None
    error = None
    started = datetime.now(UTC)
    try:
        comparison = compare(ours, await reference_totals(api, s, billing_period, through))
    except AZURE_ERRORS as exc:
        error = getattr(exc, "code", type(exc).__name__)
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        error = type(exc).__name__
    status = comparison.status if comparison else "failed"
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await session.execute(
            UPSERT_RECONCILIATION,
            {
                "t": tenant_id,
                "c": connection_id,
                "p": billing_period,
                "v": max(r.version for r in loaded),
                "through": through,
                "status": status,
                "cur": round(comparison.cur_total * ratio, 6) if comparison else None,
                "ref": round(comparison.reference * ratio, 6) if comparison else None,
                "pct": comparison.difference_pct if comparison else None,
                "accounts": json.dumps(comparison.accounts if comparison else []),
                "code": error,
            },
        )
        await session.execute(
            SYNC_RUN,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "job": "cost.reconcile",
                "scope": json.dumps({"billing_period": billing_period.isoformat()}),
                "status": "failed" if error else "succeeded",
                "started": started,
                "records": None,
                "code": error,
            },
        )
    if status == "mismatch" and comparison:
        log.warning(
            "azure reconciliation mismatch tenant=%s connection=%s period=%s ours=%.2f "
            "reference=%.2f",
            tenant_id,
            connection_id,
            billing_period,
            comparison.cur_total,
            comparison.reference,
        )
    return status


UPSERT_SNAPSHOT = text(
    "INSERT INTO cost_snapshot_ce (tenant_id, connection_id, provider, period_start, "
    "through_date, billing_currency, effective_cost, billed_cost, credits, effective_cost_usd, "
    "billed_cost_usd, credits_usd, daily, by_service, fetched_at) VALUES (:t, :c, 'azure', :p, "
    ":through, :ccy, :native, :native, :credits, :usd, :usd, :credits_usd, "
    "CAST(:daily AS jsonb), CAST(:services AS jsonb), now()) "
    "ON CONFLICT (connection_id, period_start) DO UPDATE SET "
    "through_date = EXCLUDED.through_date, billing_currency = EXCLUDED.billing_currency, "
    "effective_cost = EXCLUDED.effective_cost, billed_cost = EXCLUDED.billed_cost, "
    "credits = EXCLUDED.credits, effective_cost_usd = EXCLUDED.effective_cost_usd, "
    "billed_cost_usd = EXCLUDED.billed_cost_usd, credits_usd = EXCLUDED.credits_usd, "
    "daily = EXCLUDED.daily, by_service = EXCLUDED.by_service, fetched_at = now()"
)


def _day(value: Any) -> date | None:
    raw = str(value)
    try:
        if raw.isdigit() and len(raw) == 8:
            return date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
        return datetime.fromisoformat(raw.replace("Z", "+00:00")[:19]).date()
    except ValueError:
        return None


async def refresh_snapshot(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    maker = get_sessionmaker()
    today = datetime.now(UTC).date()
    start = today.replace(day=1)
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        row = await azconn.load(session, connection_id)
        if row is None or row.status not in ("healthy", "degraded"):
            return "skipped"
        s = azconn.from_row(row)
        if not s.verified:
            return "skipped"
        has_bill = (
            await session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM cost_daily WHERE connection_id = :c "
                    "AND billing_period = :p)"
                ),
                {"c": connection_id, "p": start},
            )
        ).scalar_one()
    if has_bill:
        return "cur_loaded"
    api = AzureApi(s.directory_id)
    started = datetime.now(UTC)
    status, error = "succeeded", None
    snap: dict[str, Any] | None = None
    try:
        props = await query(
            api,
            s.scope,
            {
                "type": "ActualCost",
                "timeframe": "MonthToDate",
                "dataset": {
                    "granularity": "Daily",
                    "aggregation": {
                        "totalCost": {"name": "Cost", "function": "Sum"},
                        "totalCostUSD": {"name": "CostUSD", "function": "Sum"},
                    },
                    "grouping": [{"type": "Dimension", "name": "ServiceName"}],
                },
            },
        )
        snap = _snapshot(props)
    except AZURE_ERRORS as exc:
        status, error = "failed", getattr(exc, "code", type(exc).__name__)
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        status, error = "failed", type(exc).__name__
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        if snap is not None:
            await funnel.emit(session, "first_costs", tenant_id=tenant_id, detail="provisional")
            await session.execute(
                UPSERT_SNAPSHOT,
                {
                    "t": tenant_id,
                    "c": connection_id,
                    "p": start,
                    "through": snap["through"] or today,
                    "ccy": snap["currency"],
                    "native": round(snap["native"], 6),
                    "usd": round(snap["usd"], 6),
                    "credits": 0,
                    "credits_usd": 0,
                    "daily": json.dumps(
                        [
                            {"date": d.isoformat(), "amount": round(v, 6)}
                            for d, v in sorted(snap["daily"].items())
                        ]
                    ),
                    "services": json.dumps(
                        [
                            {"service": k, "amount": round(v, 6)}
                            for k, v in sorted(snap["services"].items(), key=lambda kv: -kv[1])
                        ]
                    ),
                },
            )
        await session.execute(
            SYNC_RUN,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "job": "ce.snapshot",
                "scope": json.dumps({"period_start": start.isoformat()}),
                "status": status,
                "started": started,
                "records": None if snap is None else len(snap["daily"]),
                "code": error,
            },
        )
    return status


def _snapshot(props: dict[str, Any]) -> dict[str, Any]:
    cols = _columns(props)
    cost_i, usd_i = cols["cost"], cols.get("costusd")
    day_i, svc_i = cols["usagedate"], cols.get("servicename")
    ccy_i = cols.get("currency")
    daily: dict[date, float] = defaultdict(float)
    services: dict[str, float] = defaultdict(float)
    native = usd = 0.0
    currency = "USD"
    for row in props["rows"]:
        day = _day(row[day_i])
        amount_usd = float(row[usd_i] if usd_i is not None else row[cost_i] or 0)
        native += float(row[cost_i] or 0)
        usd += amount_usd
        if day is not None:
            daily[day] += amount_usd
        if svc_i is not None:
            services[str(row[svc_i] or "Other")] += amount_usd
        if ccy_i is not None and row[ccy_i]:
            currency = str(row[ccy_i]).upper()
    if usd_i is None and currency != "USD":
        raise ValueError("no USD amounts in the answer")
    return {
        "native": native,
        "usd": usd,
        "currency": currency,
        "daily": dict(daily),
        "services": dict(services),
        "through": max(daily) if daily else None,
    }
