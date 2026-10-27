"""Background jobs for reports (ADR-003, docs/09 §2.4 ``digest.weekly``)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import procrastinate
from sqlalchemy import text

from cloudoptics.platform.db import get_sessionmaker, scheduler_scope, scope
from cloudoptics.platform.jobs import defer_once
from cloudoptics.reporting import monthly
from cloudoptics.reporting.digest import send

blueprint = procrastinate.Blueprint()

DIGEST_TASK = "reporting:digest.weekly"
REPORT_TASK = "reporting:report.monthly"


@blueprint.task(name="digest.weekly", queue="notifications", retry=2)
async def weekly(tenant_id: str) -> int:
    """Send the workspace's weekly digest when it is due (Monday from 07:00 local)."""
    tid = uuid.UUID(tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        return await send(session, tid, datetime.now(UTC))


@blueprint.periodic(cron="7 * * * *")
@blueprint.task(name="digest.poll", queue="notifications", queueing_lock="digest.poll")
async def poll(timestamp: int) -> int:
    """Hourly: every workspace with a connection is checked, so each gets its digest in
    the first hour after 07:00 Monday, and its monthly report after 06:00 on the 3rd
    working day, in its own time zone (failed sends retry hourly that day)."""
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        tenants = (
            await session.execute(
                text(
                    "SELECT DISTINCT tenant_id FROM cloud_connection WHERE status <> 'disconnected'"
                )
            )
        ).scalars()
        tenant_ids = [str(t) for t in tenants]
    from cloudoptics.platform.jobs import get_job_app

    app = get_job_app()
    for tenant_id in tenant_ids:
        await defer_once(app, DIGEST_TASK, f"digest:{tenant_id}", tenant_id=tenant_id)
        await defer_once(app, REPORT_TASK, f"report:{tenant_id}", tenant_id=tenant_id)
    return len(tenant_ids)


@blueprint.task(name="report.monthly", queue="notifications", retry=2)
async def report(tenant_id: str) -> int:
    """Make and email last month's report when it is due (3rd working day from 06:00)."""
    tid = uuid.UUID(tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        return await monthly.send_due(session, tid, datetime.now(UTC))
