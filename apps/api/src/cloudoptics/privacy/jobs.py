"""Background jobs for data deletion (docs/09 §2.4 ``tenant.delete``).

The poll finds due requests across workspaces with the read-only scheduler policy on
``data_deletion`` (migration 0029), as other polls do for ``cloud_connection``; each
deletion then runs in its own workspace's RLS scope.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import procrastinate
from sqlalchemy import text

from cloudoptics.platform.db import get_sessionmaker, scheduler_scope, scope
from cloudoptics.platform.jobs import defer_once
from cloudoptics.privacy import service

blueprint = procrastinate.Blueprint()

RUN_TASK = "privacy:deletion.run"

DUE = text(
    "SELECT id, tenant_id FROM data_deletion WHERE status = 'scheduled' "
    "AND scheduled_for <= now() ORDER BY scheduled_for"
)


async def defer_deletion(app: procrastinate.App, tenant_id: str, deletion_id: str) -> None:
    await defer_once(
        app, RUN_TASK, f"deletion:{deletion_id}", tenant_id=tenant_id, deletion_id=deletion_id
    )


async def execute(tenant_id: uuid.UUID, deletion_id: uuid.UUID, now: datetime) -> str:
    """Run one request in its own transaction, then email the admins once it is committed."""
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        outcome = await service.run_due(session, tenant_id, deletion_id, now)
    if outcome.email is not None:
        if outcome.log_email:
            async with get_sessionmaker()() as session, session.begin():
                await scope(session, tenant_id=tenant_id)
                await service.send(outcome.email, session, tenant_id)
        else:
            await service.send(outcome.email)
    return outcome.status


@blueprint.task(name="deletion.run", queue="privacy", retry=3)
async def run(tenant_id: str, deletion_id: str) -> str:
    return await execute(uuid.UUID(tenant_id), uuid.UUID(deletion_id), datetime.now(UTC))


async def due() -> list[tuple[str, str]]:
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        return [(str(r.tenant_id), str(r.id)) for r in await session.execute(DUE)]


@blueprint.periodic(cron="*/15 * * * *")
@blueprint.task(name="deletion.poll", queue="privacy", queueing_lock="deletion.poll")
async def poll(timestamp: int) -> int:
    """Every 15 minutes: start each deletion whose grace period has ended."""
    from cloudoptics.platform.jobs import get_job_app

    found = await due()
    app = get_job_app()
    for tenant_id, deletion_id in found:
        await defer_deletion(app, tenant_id, deletion_id)
    return len(found)
