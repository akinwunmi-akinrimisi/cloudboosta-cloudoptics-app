"""Background jobs for billing ingestion (ADR-003, docs/11c §1.1 F1)."""

from __future__ import annotations

import contextlib
import uuid

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued, AppNotOpen
from sqlalchemy import text

from cloudoptics.ingestion.service import INGESTABLE, ingest_connection
from cloudoptics.platform.db import get_sessionmaker, scheduler_scope

blueprint = procrastinate.Blueprint()

INGEST_TASK = "ingestion:cur.ingest"


async def defer_ingest(app: procrastinate.App, tenant_id: str, connection_id: str) -> None:
    """Queue one ingest per connection; a second request while one is queued is a no-op.

    Works from a worker (the app is open) and from anywhere else (opened for the call).
    """
    task = app.configure_task(INGEST_TASK, queueing_lock=f"ingest:{connection_id}")
    with contextlib.suppress(AlreadyEnqueued):
        try:
            await task.defer_async(tenant_id=tenant_id, connection_id=connection_id)
        except AppNotOpen:
            async with app.open_async():
                await task.defer_async(tenant_id=tenant_id, connection_id=connection_id)


@blueprint.task(name="cur.ingest", queue="ingestion", retry=2)
async def ingest(tenant_id: str, connection_id: str) -> str:
    result = await ingest_connection(uuid.UUID(tenant_id), uuid.UUID(connection_id))
    return result.status


@blueprint.periodic(cron="17 */6 * * *")
@blueprint.task(name="cur.poll", queue="ingestion", queueing_lock="cur.poll")
async def poll(timestamp: int) -> int:
    """Every 6 hours: queue an ingest for each connection with an export (docs/11c §3.1)."""
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        rows = (
            await session.execute(
                text(
                    "SELECT tenant_id, id FROM cloud_connection "
                    "WHERE status = ANY(:ok) AND cur_bucket IS NOT NULL"
                ),
                {"ok": list(INGESTABLE)},
            )
        ).all()
    from cloudoptics.platform.jobs import get_job_app

    app = get_job_app()
    for tenant_id, connection_id in rows:
        await defer_ingest(app, str(tenant_id), str(connection_id))
    return len(rows)
