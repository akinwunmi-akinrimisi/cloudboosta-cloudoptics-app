"""Background jobs for billing ingestion (ADR-003, docs/11c §1.1 F1)."""

from __future__ import annotations

import uuid
from datetime import date

import procrastinate
from sqlalchemy import text

from cloudoptics.ingestion.explorer import refresh_snapshot
from cloudoptics.ingestion.reconcile import reconcile_period
from cloudoptics.ingestion.service import INGESTABLE, ingest_connection
from cloudoptics.platform.db import get_sessionmaker, scheduler_scope
from cloudoptics.platform.jobs import defer_once

blueprint = procrastinate.Blueprint()

INGEST_TASK = "ingestion:cur.ingest"
SNAPSHOT_TASK = "ingestion:ce.snapshot"
RECONCILE_TASK = "ingestion:cost.reconcile"


async def defer_ingest(app: procrastinate.App, tenant_id: str, connection_id: str) -> None:
    """Queue one ingest per connection."""
    await defer_once(
        app,
        INGEST_TASK,
        f"ingest:{connection_id}",
        tenant_id=tenant_id,
        connection_id=connection_id,
    )


async def defer_snapshot(app: procrastinate.App, tenant_id: str, connection_id: str) -> None:
    """Queue one Cost Explorer snapshot per connection."""
    await defer_once(
        app,
        SNAPSHOT_TASK,
        f"ce:{connection_id}",
        tenant_id=tenant_id,
        connection_id=connection_id,
    )


@blueprint.task(name="cur.ingest", queue="ingestion", retry=2)
async def ingest(tenant_id: str, connection_id: str) -> str:
    result = await ingest_connection(uuid.UUID(tenant_id), uuid.UUID(connection_id))
    if result.loaded_periods:
        from cloudoptics.platform.jobs import get_job_app

        app = get_job_app()
        for period in sorted(set(result.loaded_periods)):
            await defer_once(
                app,
                RECONCILE_TASK,
                f"reconcile:{connection_id}:{period:%Y-%m}",
                tenant_id=tenant_id,
                connection_id=connection_id,
                billing_period=period.isoformat(),
            )
    return result.status


@blueprint.task(name="cost.reconcile", queue="ingestion", retry=1)
async def reconcile(tenant_id: str, connection_id: str, billing_period: str) -> str:
    return await reconcile_period(
        uuid.UUID(tenant_id), uuid.UUID(connection_id), date.fromisoformat(billing_period)
    )


async def _connections(with_export: bool) -> list[tuple[str, str]]:
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        rows = (
            await session.execute(
                text(
                    "SELECT tenant_id, id FROM cloud_connection WHERE status = ANY(:ok) "
                    "AND (cur_bucket IS NOT NULL OR NOT :with_export)"
                ),
                {"ok": list(INGESTABLE), "with_export": with_export},
            )
        ).all()
    return [(str(t), str(c)) for t, c in rows]


@blueprint.periodic(cron="17 */6 * * *")
@blueprint.task(name="cur.poll", queue="ingestion", queueing_lock="cur.poll")
async def poll(timestamp: int) -> int:
    """Every 6 hours: queue an ingest for each connection with an export (docs/11c §3.1)."""
    rows = await _connections(with_export=True)
    from cloudoptics.platform.jobs import get_job_app

    app = get_job_app()
    for tenant_id, connection_id in rows:
        await defer_ingest(app, tenant_id, connection_id)
    return len(rows)


@blueprint.task(name="ce.snapshot", queue="ingestion", retry=1)
async def snapshot(tenant_id: str, connection_id: str) -> str:
    return await refresh_snapshot(uuid.UUID(tenant_id), uuid.UUID(connection_id))


@blueprint.periodic(cron="41 6 * * *")
@blueprint.task(name="ce.poll", queue="ingestion", queueing_lock="ce.poll")
async def poll_explorer(timestamp: int) -> int:
    """Daily: refresh provisional costs; connections with this month's CUR skip the call."""
    rows = await _connections(with_export=False)
    from cloudoptics.platform.jobs import get_job_app

    app = get_job_app()
    for tenant_id, connection_id in rows:
        await defer_snapshot(app, tenant_id, connection_id)
    return len(rows)
