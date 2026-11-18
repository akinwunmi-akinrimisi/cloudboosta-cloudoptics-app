"""Background jobs for commitments (PRD FR-CON-07 "Savings Plans: daily", FR-ANA-10)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import procrastinate
from sqlalchemy import text

from cloudoptics.commitments import service
from cloudoptics.platform.db import get_sessionmaker, scheduler_scope, scope
from cloudoptics.platform.jobs import defer_once

blueprint = procrastinate.Blueprint()

REFRESH_TASK = "commitments:commitments.refresh"


async def defer_refresh(app: procrastinate.App, tenant_id: str, connection_id: str) -> None:
    await defer_once(
        app,
        REFRESH_TASK,
        f"commitments:{connection_id}",
        tenant_id=tenant_id,
        connection_id=connection_id,
    )


async def after_refresh(tenant_id: uuid.UUID) -> int:
    """Expiry alerts, then the engine again so R8 follows the new figures."""
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        sent = await service.alert_expiries(session, tenant_id, datetime.now(UTC))
    from cloudoptics.analysis.jobs import defer_analysis
    from cloudoptics.platform.jobs import get_job_app

    await defer_analysis(get_job_app(), str(tenant_id))
    return sent


@blueprint.task(name="commitments.refresh", queue="inventory", retry=1)
async def refresh(tenant_id: str, connection_id: str) -> str:
    status = await service.refresh(uuid.UUID(tenant_id), uuid.UUID(connection_id))
    if status != "skipped":
        await after_refresh(uuid.UUID(tenant_id))
    return status


@blueprint.periodic(cron="17 6 * * *")
@blueprint.task(name="commitments.poll", queue="inventory", queueing_lock="commitments.poll")
async def poll(timestamp: int) -> int:
    """Daily: every usable connection (Cost Explorer itself at most every 20 hours)."""
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        rows = (
            await session.execute(
                text("SELECT tenant_id, id FROM cloud_connection WHERE status = ANY(:ok)"),
                {"ok": list(service.USABLE)},
            )
        ).all()
    from cloudoptics.platform.jobs import get_job_app

    app = get_job_app()
    for tenant_id, connection_id in rows:
        await defer_refresh(app, str(tenant_id), str(connection_id))
    return len(rows)
