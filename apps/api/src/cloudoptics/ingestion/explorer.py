"""Provisional month-to-date costs from Cost Explorer (WBS CC-9, docs/11c §1.3).

AWS delivers the first Cost and Usage Report up to 24 hours after the export is created, so
right after a connection validates we ask Cost Explorer for the month so far and show it,
labelled provisional. Each refresh costs two ``GetCostAndUsage`` calls (USD 0.01 each, paid
by the customer): one daily series grouped by record type (totals and credits) and one
month total grouped by service. Refreshes stop once the month's CUR data is loaded.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from cloudoptics.connectors.aws import session_name
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.service import get_connection, get_session_factory
from cloudoptics.connectors.validation import CE_CALL_COST_USD
from cloudoptics.ingestion.service import INGESTABLE
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7

MIN_REFRESH_INTERVAL = timedelta(hours=6)
METRICS = ["AmortizedCost", "UnblendedCost"]

SnapshotStatus = Literal["skipped", "fresh", "cur_loaded", "succeeded", "failed"]


class ExplorerError(Exception):
    """Cost Explorer answered with something we cannot store (e.g. a non-USD currency)."""


@dataclass
class Snapshot:
    period_start: date
    through_date: date
    effective: float = 0.0
    billed: float = 0.0
    credits: float = 0.0
    daily: dict[date, float] = field(default_factory=dict)
    by_service: dict[str, float] = field(default_factory=dict)
    calls: int = 0


def _pages(ce: Any, **request: Any) -> tuple[list[dict[str, Any]], int]:
    results, calls, token = [], 0, None
    while True:
        page = ce.get_cost_and_usage(**request, **({"NextPageToken": token} if token else {}))
        calls += 1
        results.extend(page.get("ResultsByTime", []))
        token = page.get("NextPageToken")
        if not token:
            return results, calls


def _amount(metrics: dict[str, Any], name: str) -> float:
    metric = metrics.get(name) or {}
    unit = metric.get("Unit", "USD")
    if unit != "USD":
        raise ExplorerError(f"unsupported_currency:{unit}")
    return float(metric.get("Amount", 0) or 0)


def fetch_snapshot(ce: Any, today: date) -> Snapshot | None:
    """The month so far, up to yesterday (UTC). ``None`` on the 1st: no full day yet."""
    start = today.replace(day=1)
    if today <= start:
        return None
    period = {"Start": start.isoformat(), "End": today.isoformat()}
    snap = Snapshot(period_start=start, through_date=today - timedelta(days=1))

    daily, calls = _pages(
        ce,
        TimePeriod=period,
        Granularity="DAILY",
        Metrics=METRICS,
        GroupBy=[{"Type": "DIMENSION", "Key": "RECORD_TYPE"}],
    )
    snap.calls += calls
    for result in daily:
        day = date.fromisoformat(result["TimePeriod"]["Start"])
        for group in result.get("Groups", []):
            effective = _amount(group["Metrics"], "AmortizedCost")
            snap.daily[day] = snap.daily.get(day, 0.0) + effective
            snap.effective += effective
            snap.billed += _amount(group["Metrics"], "UnblendedCost")
            if group["Keys"][0] == "Credit":
                snap.credits += effective

    services, calls = _pages(
        ce,
        TimePeriod=period,
        Granularity="MONTHLY",
        Metrics=["AmortizedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )
    snap.calls += calls
    by_service: dict[str, float] = defaultdict(float)
    for result in services:
        for group in result.get("Groups", []):
            by_service[group["Keys"][0]] += _amount(group["Metrics"], "AmortizedCost")
    snap.by_service = {k: v for k, v in by_service.items() if round(v, 2) != 0}
    return snap


async def refresh_snapshot(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    today: date | None = None,
    *,
    force: bool = False,
) -> SnapshotStatus:
    """Fetch and store this month's provisional costs for one connection.

    Skipped when the connection is not usable, when the month's CUR data is already loaded,
    or (unless ``force``) when the last snapshot is less than six hours old.
    """
    today = today or datetime.now(UTC).date()
    start = today.replace(day=1)
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        if conn.status not in INGESTABLE:
            return "skipped"
        has_cur = (
            await session.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM cost_daily WHERE connection_id = :c "
                    "AND billing_period = :p)"
                ),
                {"c": connection_id, "p": start},
            )
        ).scalar_one()
        if has_cur:
            return "cur_loaded"
        last = (
            await session.execute(
                text(
                    "SELECT fetched_at FROM cost_snapshot_ce WHERE connection_id = :c "
                    "AND period_start = :p"
                ),
                {"c": connection_id, "p": start},
            )
        ).scalar_one_or_none()
        if last and not force and datetime.now(UTC) - last < MIN_REFRESH_INTERVAL:
            return "fresh"

    external_id = get_encryptor().decrypt(bytes(conn.external_id_enc), tenant_id)

    def _fetch() -> Snapshot | None:
        session = get_session_factory().assume(
            conn.role_arn, external_id, session_name(tenant_id, "ce")
        )
        return fetch_snapshot(session.client("ce"), today)

    started = datetime.now(UTC)
    error: str | None = None
    snap: Snapshot | None = None
    try:
        snap = await run_in_threadpool(_fetch)
    except ClientError as exc:
        error = str(exc.response.get("Error", {}).get("Code", "ClientError"))
    except (BotoCoreError, ExplorerError, KeyError, ValueError) as exc:
        error = str(exc) if isinstance(exc, ExplorerError) else type(exc).__name__

    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        if snap is not None:
            await session.execute(
                text(
                    "INSERT INTO cost_snapshot_ce (tenant_id, connection_id, period_start, "
                    "through_date, effective_cost_usd, billed_cost_usd, credits_usd, daily, "
                    "by_service, fetched_at) VALUES (:t, :c, :p, :through, :effective, :billed, "
                    ":credits, CAST(:daily AS jsonb), CAST(:services AS jsonb), now()) "
                    "ON CONFLICT (connection_id, period_start) DO UPDATE SET "
                    "through_date = EXCLUDED.through_date, "
                    "effective_cost_usd = EXCLUDED.effective_cost_usd, "
                    "billed_cost_usd = EXCLUDED.billed_cost_usd, "
                    "credits_usd = EXCLUDED.credits_usd, daily = EXCLUDED.daily, "
                    "by_service = EXCLUDED.by_service, fetched_at = now()"
                ),
                {
                    "t": tenant_id,
                    "c": connection_id,
                    "p": snap.period_start,
                    "through": snap.through_date,
                    "effective": round(snap.effective, 6),
                    "billed": round(snap.billed, 6),
                    "credits": round(snap.credits, 6),
                    "daily": json.dumps(
                        [
                            {"date": d.isoformat(), "amount": round(v, 6)}
                            for d, v in sorted(snap.daily.items())
                        ]
                    ),
                    "services": json.dumps(
                        [
                            {"service": k, "amount": round(v, 6)}
                            for k, v in sorted(snap.by_service.items(), key=lambda kv: -kv[1])
                        ]
                    ),
                },
            )
        await session.execute(
            text(
                "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, status, "
                "started_at, finished_at, records, error_code, est_customer_api_cost_usd) "
                "VALUES (:id, :t, :c, 'ce.snapshot', :status, :started, now(), :records, "
                ":code, :cost)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "status": "failed" if error else "succeeded",
                "started": started,
                "records": len(snap.daily) if snap else 0,
                "code": error,
                "cost": (snap.calls if snap else 1) * CE_CALL_COST_USD,
            },
        )
    if error:
        return "failed"
    return "succeeded" if snap else "skipped"
