"""Background jobs for connections (ADR-003)."""

from __future__ import annotations

import uuid

import procrastinate
from sqlalchemy import text

from cloudoptics.audit.log import Actor
from cloudoptics.connectors.accounts import USABLE, discover_accounts
from cloudoptics.connectors.service import run_validation
from cloudoptics.platform.db import get_sessionmaker, scheduler_scope, scope
from cloudoptics.platform.jobs import defer_once

blueprint = procrastinate.Blueprint()

DISCOVER_TASK = "connectors:accounts.discover"


async def defer_discovery(app: procrastinate.App, tenant_id: str, connection_id: str) -> None:
    await defer_once(
        app,
        DISCOVER_TASK,
        f"accounts:{connection_id}",
        tenant_id=tenant_id,
        connection_id=connection_id,
    )


@blueprint.task(name="connection.validate", queue="connections", retry=2)
async def validate_connection(tenant_id: str, connection_id: str) -> str:
    tid, cid = uuid.UUID(tenant_id), uuid.UUID(connection_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        result = await run_validation(
            session, tenant_id=tid, connection_id=cid, actor=Actor(type="system")
        )
    if result.status != "failed":
        from cloudoptics.ingestion.jobs import defer_ingest, defer_snapshot
        from cloudoptics.platform.jobs import get_job_app

        app = get_job_app()
        # Provisional costs now; the export is loaded straight away rather than at the next poll.
        await defer_snapshot(app, tenant_id, connection_id)
        if result.cur_bucket:
            await defer_ingest(app, tenant_id, connection_id)
        await defer_discovery(app, tenant_id, connection_id)
    return result.status


@blueprint.task(name="accounts.discover", queue="connections", retry=2)
async def discover(tenant_id: str, connection_id: str) -> str:
    return await discover_accounts(uuid.UUID(tenant_id), uuid.UUID(connection_id))


@blueprint.periodic(cron="23 */12 * * *")
@blueprint.task(name="accounts.poll", queue="connections", queueing_lock="accounts.poll")
async def poll_accounts(timestamp: int) -> int:
    """Every 12 hours: rediscover the accounts of each management connection."""
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        rows = (
            await session.execute(
                text(
                    "SELECT tenant_id, id FROM cloud_connection WHERE status = ANY(:ok) "
                    "AND account_type = 'management'"
                ),
                {"ok": list(USABLE)},
            )
        ).all()
    from cloudoptics.platform.jobs import get_job_app

    app = get_job_app()
    for tenant_id, connection_id in rows:
        await defer_discovery(app, str(tenant_id), str(connection_id))
    return len(rows)
