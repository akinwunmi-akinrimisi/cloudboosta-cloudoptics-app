"""Reload bills: read a connection's export again (admins, docs/11c §1.1 "Reload bills").

The poll loads each export version once. A reload loads the **latest** version of one
billing period, or of every period in the bucket, again, even when it is loaded already,
through the same ``_store`` as the poll: the period's ``cost_daily`` and
``cost_resource_daily`` are replaced in one transaction and the batch is recorded (with
``reload`` set when the version was loaded before, so the first load stays in the history
as ``superseded``). It is how months loaded before a mapping change (for example
``cost_resource_daily`` in Sprint 5) get the new data.

Per period the outcome is ``reloaded`` (stored again), ``unchanged`` (the same version gave
the same figures, nothing written), ``failed`` (could not be read, or failed the checks;
the previous data stays) or ``not_in_bucket`` (no export for it: months before the export
was created need AWS to backfill it through a support case).

A reload is recorded in ``sync_run`` (``cur.reload``): ``running`` from the request (a
queued one is taken over by the worker), then the outcome per period in ``scope``. At most
one an hour per connection (``RELOAD_EVERY``). A failed reload counts too once it read a
bill (a period with an outcome other than ``not_in_bucket``): the customer writes the bucket
and could otherwise make every reload fail after the full read and ask again at once. Only
a reload that failed before reading any bill (the connection was not working, the bucket
could not be listed) does not count.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from cloudoptics.connectors.service import get_connection
from cloudoptics.ingestion import service
from cloudoptics.ingestion.pipeline import LoadedExport, load_export
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

JOB_TYPE = "cur.reload"
RELOAD_EVERY = timedelta(hours=1)
#: A reload still marked running after this did not finish (the worker restarted).
STALE_AFTER = timedelta(hours=2)
#: Two figures this close count as the same.
SAME = Decimal("0.005")

Outcome = Literal["reloaded", "unchanged", "failed", "not_in_bucket"]
RunStatus = Literal["skipped", "succeeded", "partial", "failed"]


@dataclass
class PeriodResult:
    period: date
    outcome: Outcome
    version: str | None = None
    detail: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "period": f"{self.period:%Y-%m}",
            "outcome": self.outcome,
            "version": self.version,
            "detail": self.detail,
        }


@dataclass
class ReloadResult:
    status: RunStatus
    period: date | None = None
    periods: list[PeriodResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    records: int = 0

    @property
    def reloaded(self) -> list[date]:
        return [p.period for p in self.periods if p.outcome == "reloaded"]


def parse_month(month: str) -> date:
    """``YYYY-MM`` to the month's first day."""
    return date.fromisoformat(f"{month}-01")


# --- the rate limit and the run record ---------------------------------------------------


# A reload counts unless it failed before reading any bill (module docstring).
LAST_REQUEST_SQL = (
    "SELECT max(started_at) FROM sync_run WHERE connection_id = :c AND job_type = :job "
    "AND (status <> 'failed' OR EXISTS (SELECT 1 FROM "
    "jsonb_array_elements(CASE WHEN jsonb_typeof(scope->'periods') = 'array' "
    "THEN scope->'periods' ELSE CAST('[]' AS jsonb) END) AS p "
    "WHERE p->>'outcome' <> 'not_in_bucket'))"
)


async def last_request(session: AsyncSession, connection_id: uuid.UUID) -> datetime | None:
    """When the latest reload that counts towards the limit was asked for."""
    value: datetime | None = (
        await session.execute(text(LAST_REQUEST_SQL), {"c": connection_id, "job": JOB_TYPE})
    ).scalar_one()
    return value


def next_allowed(last: datetime | None) -> datetime | None:
    return None if last is None else last + RELOAD_EVERY


async def queue_run(
    session: AsyncSession, tenant_id: uuid.UUID, connection_id: uuid.UUID, period: date | None
) -> None:
    """Show a queued reload as running at once (in the request's transaction)."""
    await session.execute(
        text(
            "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, "
            "started_at) VALUES (:id, :t, :c, :job, CAST(:scope AS jsonb), 'running', now())"
        ),
        {
            "id": uuid7(),
            "t": tenant_id,
            "c": connection_id,
            "job": JOB_TYPE,
            "scope": json.dumps({"queued": True, "period": _month(period)}),
        },
    )


def _month(period: date | None) -> str | None:
    return None if period is None else f"{period:%Y-%m}"


async def _start_run(
    session: AsyncSession, tenant_id: uuid.UUID, conn_id: uuid.UUID, period: date | None
) -> uuid.UUID:
    """Take over the row a queued request added, else add one. Any other reload of the
    connection still marked running did not finish."""
    scope_json = json.dumps({"period": _month(period)})
    run_id: uuid.UUID | None = (
        await session.execute(
            text(
                "UPDATE sync_run SET scope = CAST(:scope AS jsonb) WHERE id = (SELECT id "
                "FROM sync_run WHERE connection_id = :c AND job_type = :job AND "
                "status = 'running' AND scope->>'queued' = 'true' "
                "ORDER BY started_at DESC LIMIT 1) RETURNING id"
            ),
            {"c": conn_id, "job": JOB_TYPE, "scope": scope_json},
        )
    ).scalar_one_or_none()
    if run_id is None:
        run_id = uuid7()
        await session.execute(
            text(
                "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, "
                "started_at) VALUES (:id, :t, :c, :job, CAST(:scope AS jsonb), 'running', "
                "now())"
            ),
            {"id": run_id, "t": tenant_id, "c": conn_id, "job": JOB_TYPE, "scope": scope_json},
        )
    await session.execute(
        text(
            "UPDATE sync_run SET status = 'failed', finished_at = now(), records = 0, "
            "error_code = 'interrupted' WHERE connection_id = :c AND job_type = :job "
            "AND status = 'running' AND id <> :id"
        ),
        {"c": conn_id, "job": JOB_TYPE, "id": run_id},
    )
    return run_id


async def _finish_run(
    maker: Any, tenant_id: uuid.UUID, run_id: uuid.UUID, result: ReloadResult
) -> None:
    failed = [p for p in result.periods if p.outcome == "failed"]
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await session.execute(
            text(
                "UPDATE sync_run SET status = :status, finished_at = now(), records = :records, "
                "scope = CAST(:scope AS jsonb), error_code = :code, error_detail = :detail "
                "WHERE id = :id"
            ),
            {
                "id": run_id,
                # sync_run has no "skipped": a reload that could not start failed.
                "status": "failed" if result.status == "skipped" else result.status,
                "records": result.records,
                "scope": json.dumps(
                    {
                        "period": _month(result.period),
                        "periods": [p.as_json() for p in result.periods],
                    }
                ),
                "code": "not_ingestable"
                if result.status == "skipped"
                else ("reload_failed" if result.errors or failed else None),
                "detail": "; ".join(result.errors)[:2000] or None,
            },
        )


@dataclass(frozen=True)
class RunView:
    status: Literal["running", "succeeded", "partial", "failed"]
    period: date | None
    requested_at: datetime
    finished_at: datetime | None
    periods: list[dict[str, Any]]
    error: str | None


async def latest_run(session: AsyncSession, connection_id: uuid.UUID) -> RunView | None:
    row = (
        await session.execute(
            text(
                "SELECT status, scope, started_at, finished_at, error_code, error_detail "
                "FROM sync_run WHERE connection_id = :c AND job_type = :job "
                "ORDER BY started_at DESC LIMIT 1"
            ),
            {"c": connection_id, "job": JOB_TYPE},
        )
    ).one_or_none()
    if row is None:
        return None
    body: dict[str, Any] = row.scope or {}
    status = row.status
    error = row.error_detail
    if status == "running" and datetime.now(UTC) - row.started_at > STALE_AFTER:
        status, error = "failed", "The reload did not finish. Try again."
    elif row.error_code == "not_ingestable":
        error = "The connection is not working, or has no export. Check it first."
    return RunView(
        status=status,
        period=parse_month(body["period"]) if body.get("period") else None,
        requested_at=row.started_at,
        finished_at=row.finished_at,
        periods=list(body.get("periods", [])),
        error=error,
    )


# --- the reload ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Figures:
    version: str | None
    rows: int
    effective: Decimal
    billed: Decimal
    resource_rows: int
    resource_effective: Decimal

    def same_as(self, other: _Figures) -> bool:
        return (
            self.version == other.version
            and self.rows == other.rows
            and self.resource_rows == other.resource_rows
            and abs(self.effective - other.effective) <= SAME
            and abs(self.billed - other.billed) <= SAME
            and abs(self.resource_effective - other.resource_effective) <= SAME
        )


def _loaded_figures(loaded: LoadedExport) -> _Figures:
    return _Figures(
        version=loaded.manifest.export_version,
        rows=len(loaded.rows),
        effective=sum((r.effective_cost for r in loaded.rows), Decimal(0)),
        billed=sum((r.billed_cost for r in loaded.rows), Decimal(0)),
        resource_rows=len(loaded.resource_rows),
        resource_effective=sum((r.effective_cost for r in loaded.resource_rows), Decimal(0)),
    )


async def _stored_figures(session: AsyncSession, conn_id: uuid.UUID, period: date) -> _Figures:
    row = (
        await session.execute(
            text(
                "SELECT (SELECT export_version FROM ingest_batch WHERE connection_id = :c "
                "AND billing_period = :p AND status = 'succeeded' "
                "ORDER BY created_at DESC LIMIT 1) AS version, "
                "d.n AS rows, d.effective, d.billed, r.n AS resource_rows, "
                "r.effective AS resource_effective FROM "
                "(SELECT count(*) AS n, coalesce(sum(effective_cost_usd), 0) AS effective, "
                "coalesce(sum(billed_cost_usd), 0) AS billed FROM cost_daily "
                "WHERE connection_id = :c AND billing_period = :p) d, "
                "(SELECT count(*) AS n, coalesce(sum(effective_cost_usd), 0) AS effective "
                "FROM cost_resource_daily WHERE connection_id = :c AND billing_period = :p) r"
            ),
            {"c": conn_id, "p": period},
        )
    ).one()
    return _Figures(
        row.version,
        int(row.rows),
        Decimal(row.effective),
        Decimal(row.billed),
        int(row.resource_rows),
        Decimal(row.resource_effective),
    )


def _status(result: ReloadResult, requested: date | None) -> RunStatus:
    good = [p for p in result.periods if p.outcome in ("reloaded", "unchanged")]
    # Not in the bucket counts against a reload of one month, not against "every month"
    # (older months may be gone from the bucket and stay as loaded).
    bad = [
        p
        for p in result.periods
        if p.outcome == "failed" or (p.outcome == "not_in_bucket" and requested is not None)
    ]
    if result.errors and not result.periods:
        return "failed"
    if bad or result.errors:
        return "partial" if good else "failed"
    return "succeeded" if good else "failed"


async def reload_connection(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, period: date | None = None
) -> ReloadResult:
    """Load the latest export version of ``period`` (every period in the bucket when
    ``None``) again. Each period is stored in its own transaction."""
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        loaded_before = {
            (r.billing_period, r.export_version)
            for r in await session.execute(
                text(
                    "SELECT DISTINCT billing_period, export_version FROM ingest_batch "
                    "WHERE connection_id = :c"
                ),
                {"c": connection_id},
            )
        }
        run_id = await _start_run(session, tenant_id, connection_id, period)
    result = ReloadResult("succeeded", period=period)
    if conn.status not in service.INGESTABLE or not conn.cur_bucket:
        result.status = "skipped"
        await _finish_run(maker, tenant_id, run_id, result)
        return result

    try:
        source = await run_in_threadpool(service._source_factory, conn, tenant_id)
        manifests, result.errors = await run_in_threadpool(service._discover, source, conn)
    except service.LOAD_ERRORS as exc:
        result.status = "failed"
        result.errors.append(service._error_code(exc))
        await _finish_run(maker, tenant_id, run_id, result)
        return result

    in_bucket = {m.billing_period for m in manifests}
    if period is not None:
        manifests = [m for m in manifests if m.billing_period == period]
        missing = set() if period in in_bucket else {period}
    else:
        # Months loaded before whose export is gone from the bucket.
        missing = {p for p, _ in loaded_before} - in_bucket
    settings = get_settings()
    norm_base = (
        Path(settings.normalised_dir) / f"tenant={tenant_id}" if settings.normalised_dir else None
    )
    for manifest in sorted(manifests, key=lambda m: m.billing_period):
        p, version = manifest.billing_period, manifest.export_version
        try:
            loaded = await run_in_threadpool(
                load_export, source, manifest, norm_base, conn.provider
            )
        except service.LOAD_ERRORS as exc:
            result.periods.append(PeriodResult(p, "failed", version, service._error_code(exc)))
            continue
        async with maker() as session, session.begin():
            await scope(session, tenant_id=tenant_id)
            if loaded.ok and _loaded_figures(loaded).same_as(
                await _stored_figures(session, connection_id, p)
            ):
                result.periods.append(PeriodResult(p, "unchanged", version))
                continue
            await service._store(
                session, tenant_id, conn, loaded, reload=(p, version) in loaded_before
            )
        if loaded.ok:
            result.periods.append(PeriodResult(p, "reloaded", version))
            result.records += loaded.row_count
        else:
            failed = sorted(c.id for c in loaded.checks if c.status == "error")
            result.periods.append(
                PeriodResult(p, "failed", version, "Failed the checks: " + ", ".join(failed))
            )
    result.periods.extend(PeriodResult(p, "not_in_bucket") for p in sorted(missing))
    result.periods.sort(key=lambda r: r.period)
    result.status = _status(result, period)
    await _finish_run(maker, tenant_id, run_id, result)
    return result


async def after_reload_inline(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, periods: list[date]
) -> None:
    """What follows new bills, run now (inline jobs): reconciliation of each period, budgets,
    anomalies, then the analysis (which ends with verification). Each in its own
    transactions, so the caller must not hold the tenant's audit-chain lock."""
    from cloudoptics.analysis.engine import run_analysis
    from cloudoptics.anomalies.service import detect_and_alert
    from cloudoptics.budgets.service import alert_budgets
    from cloudoptics.providers import capability_for

    check = await capability_for(tenant_id, connection_id, "reconcile")
    for p in sorted(set(periods)):
        if check is not None:
            await check.reconcile(tenant_id, connection_id, p)
    today = datetime.now(UTC).date()
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await alert_budgets(session, tenant_id, today)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await detect_and_alert(session, tenant_id, today)
    await run_analysis(tenant_id)
