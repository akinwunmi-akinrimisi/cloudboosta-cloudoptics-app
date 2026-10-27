"""Background jobs for budgets (ADR-003, docs/09 §2.4 ``budgets.evaluate``)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import procrastinate
from sqlalchemy import text

from cloudoptics.budgets.service import alert_budgets
from cloudoptics.platform.db import get_sessionmaker, scheduler_scope, scope
from cloudoptics.platform.jobs import defer_once

blueprint = procrastinate.Blueprint()

EVALUATE_TASK = "budgets:budgets.evaluate"


async def defer_evaluate(app: procrastinate.App, tenant_id: str) -> None:
    await defer_once(app, EVALUATE_TASK, f"budgets:{tenant_id}", tenant_id=tenant_id)


@blueprint.task(name="budgets.evaluate", queue="notifications", retry=2)
async def evaluate(tenant_id: str) -> int:
    """Evaluate the workspace's budgets and email thresholds newly crossed."""
    tid = uuid.UUID(tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        return await alert_budgets(session, tid, datetime.now(UTC).date())


@blueprint.periodic(cron="23 * * * *")
@blueprint.task(name="budgets.poll", queue="notifications", queueing_lock="budgets.poll")
async def poll(timestamp: int) -> int:
    """Hourly safety net (docs/11b §5): every workspace with a connection is evaluated, so an
    alert never waits longer than an hour after the data that caused it."""
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
        await defer_evaluate(app, tenant_id)
    return len(tenant_ids)
