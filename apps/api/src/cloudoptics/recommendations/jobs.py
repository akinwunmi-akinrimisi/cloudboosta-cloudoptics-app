"""Background jobs for recommendations (ADR-003): verification and assignment emails.

Verification also runs at the end of every analysis run, so after every inventory scan;
the daily run covers the 72-hour deadline and measures realised savings as bills arrive.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import procrastinate
from sqlalchemy import text

from cloudoptics.platform.db import get_sessionmaker, scheduler_scope, scope
from cloudoptics.platform.jobs import defer_once
from cloudoptics.recommendations.notify import send_assignment_email
from cloudoptics.recommendations.verification import verify

blueprint = procrastinate.Blueprint()

VERIFY_TASK = "recommendations:verification.run"
ASSIGNED_TASK = "recommendations:assignment.email"


async def defer_verification(app: procrastinate.App, tenant_id: str) -> None:
    await defer_once(
        app,
        VERIFY_TASK,
        f"verification:{tenant_id}",
        run_lock=f"recommendations:{tenant_id}",
        tenant_id=tenant_id,
    )


@blueprint.task(name="verification.run", queue="analysis", retry=1)
async def run(tenant_id: str) -> int:
    """Check done and open recommendations and measure realised savings."""
    tid = uuid.UUID(tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        result = await verify(session, tid, datetime.now(UTC))
    return result["verified"] + result["not_verified"] + result["resolved"]


@blueprint.periodic(cron="23 6 * * *")
@blueprint.task(name="verification.poll", queue="analysis", queueing_lock="verification.poll")
async def poll(timestamp: int) -> int:
    """Daily: verify every workspace with a connection."""
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
        await defer_verification(app, tenant_id)
    return len(tenant_ids)


async def defer_assignment_email(
    app: procrastinate.App,
    tenant_id: str,
    assignee_id: str,
    actor_id: str,
    recommendation_ids: list[str],
    key: str,
) -> None:
    await defer_once(
        app,
        ASSIGNED_TASK,
        f"assigned:{key}",
        tenant_id=tenant_id,
        assignee_id=assignee_id,
        actor_id=actor_id,
        recommendation_ids=",".join(recommendation_ids),
        key=key,
    )


@blueprint.task(name="assignment.email", queue="notifications", retry=3)
async def assignment_email(
    tenant_id: str, assignee_id: str, actor_id: str, recommendation_ids: str, key: str
) -> str:
    tid = uuid.UUID(tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        return await send_assignment_email(
            session,
            tid,
            uuid.UUID(assignee_id),
            uuid.UUID(actor_id),
            [uuid.UUID(i) for i in recommendation_ids.split(",") if i],
            key,
        )
