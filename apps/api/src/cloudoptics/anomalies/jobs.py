"""Background jobs for anomalies (ADR-003, docs/09 §2.4 ``analysis.run`` anomalies)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import procrastinate
from sqlalchemy import text

from cloudoptics.anomalies.service import detect_and_alert
from cloudoptics.platform.db import get_sessionmaker, scheduler_scope, scope
from cloudoptics.platform.jobs import defer_once

blueprint = procrastinate.Blueprint()

DETECT_TASK = "anomalies:anomalies.detect"


async def defer_detect(app: procrastinate.App, tenant_id: str) -> None:
    await defer_once(app, DETECT_TASK, f"anomalies:{tenant_id}", tenant_id=tenant_id)


@blueprint.task(name="anomalies.detect", queue="analysis", retry=2)
async def detect(tenant_id: str) -> int:
    """Look for anomalies in the workspace's last week of bills and email new ones."""
    tid = uuid.UUID(tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        return await detect_and_alert(session, tid, datetime.now(UTC).date())


@blueprint.periodic(cron="37 * * * *")
@blueprint.task(name="anomalies.poll", queue="analysis", queueing_lock="anomalies.poll")
async def poll(timestamp: int) -> int:
    """Hourly safety net: changed thresholds apply and failed emails are retried within an
    hour; new bills queue a detection straight away (ingestion ``cur.ingest``)."""
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
        await defer_detect(app, tenant_id)
    return len(tenant_ids)
