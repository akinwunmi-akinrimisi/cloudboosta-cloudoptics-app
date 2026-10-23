"""Background jobs for the resource inventory (ADR-003, docs/11c §1.2)."""

from __future__ import annotations

import uuid

import procrastinate
from sqlalchemy import text

from cloudoptics.inventory.scanner import USABLE, scan_connection
from cloudoptics.platform.db import get_sessionmaker, scheduler_scope
from cloudoptics.platform.jobs import defer_once

blueprint = procrastinate.Blueprint()

SCAN_TASK = "inventory:inventory.scan"


async def defer_scan(app: procrastinate.App, tenant_id: str, connection_id: str) -> None:
    await defer_once(
        app,
        SCAN_TASK,
        f"inventory:{connection_id}",
        tenant_id=tenant_id,
        connection_id=connection_id,
    )


@blueprint.task(name="inventory.scan", queue="inventory", retry=1)
async def scan(tenant_id: str, connection_id: str) -> str:
    status = await scan_connection(uuid.UUID(tenant_id), uuid.UUID(connection_id))
    if status in ("succeeded", "partial"):
        from cloudoptics.metrics.jobs import defer_pull
        from cloudoptics.platform.jobs import get_job_app

        await defer_pull(get_job_app(), tenant_id, connection_id)
    return status


@blueprint.periodic(cron="47 */12 * * *")
@blueprint.task(name="inventory.poll", queue="inventory", queueing_lock="inventory.poll")
async def poll(timestamp: int) -> int:
    """Every 12 hours: rescan each usable connection (docs/11b §1.4)."""
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        rows = (
            await session.execute(
                text("SELECT tenant_id, id FROM cloud_connection WHERE status = ANY(:ok)"),
                {"ok": list(USABLE)},
            )
        ).all()
    from cloudoptics.platform.jobs import get_job_app

    app = get_job_app()
    for tenant_id, connection_id in rows:
        await defer_scan(app, str(tenant_id), str(connection_id))
    return len(rows)
