"""Background jobs for connections (ADR-003)."""

from __future__ import annotations

import contextlib
import uuid
from datetime import timedelta

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued, AppNotOpen
from procrastinate.types import JSONValue
from sqlalchemy import text

from cloudoptics.audit.log import Actor
from cloudoptics.connectors.accounts import USABLE, discover_accounts
from cloudoptics.connectors.service import check_stack_removed, run_validation
from cloudoptics.connectors.validation import ValidationResult
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


async def _validate(tenant_id: str, connection_id: str) -> ValidationResult | None:
    """None when the connection was disconnected after the job was queued: nothing runs."""
    tid, cid = uuid.UUID(tenant_id), uuid.UUID(connection_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        result = await run_validation(
            session, tenant_id=tid, connection_id=cid, actor=Actor(type="system")
        )
    if result is not None and result.health_worsened:
        from cloudoptics.notifications.jobs import defer_connection_alert
        from cloudoptics.platform.jobs import get_job_app

        await defer_connection_alert(get_job_app(), tenant_id, connection_id)
    return result


@blueprint.task(name="connection.validate", queue="connections", retry=2)
async def validate_connection(tenant_id: str, connection_id: str) -> str:
    result = await _validate(tenant_id, connection_id)
    if result is None:
        return "disconnected"
    if result.status != "failed":
        from cloudoptics.ingestion.jobs import defer_ingest, defer_snapshot
        from cloudoptics.platform.jobs import get_job_app

        app = get_job_app()
        # Provisional costs now; the export is loaded straight away rather than at the next poll.
        await defer_snapshot(app, tenant_id, connection_id)
        if result.cur_bucket:
            await defer_ingest(app, tenant_id, connection_id)
        # Discovery runs first so the inventory scan it queues covers every member account.
        await defer_discovery(app, tenant_id, connection_id)
    return result.status


CHECK_TASK = "connectors:connection.check"


@blueprint.task(name="connection.check", queue="connections", retry=1)
async def check_connection(tenant_id: str, connection_id: str) -> str:
    """A scheduled health check: validation only, without the follow-up loads."""
    result = await _validate(tenant_id, connection_id)
    return "disconnected" if result is None else result.status


REMOVAL_TASK = "connectors:connection.verify_removed"
# When the stack reports its deletion, its roles are still there (CloudFormation deletes the
# setup step before the roles it depends on), so the role is checked after these delays.
REMOVAL_CHECK_DELAYS = (timedelta(minutes=5), timedelta(minutes=25))


async def defer_removal_check(
    app: procrastinate.App,
    tenant_id: str,
    connection_id: str,
    stack_name: str,
    tag: str,
    *,
    attempt: int,
) -> None:
    delay = REMOVAL_CHECK_DELAYS[attempt - 1]
    task = app.configure_task(
        REMOVAL_TASK,
        queueing_lock=f"removed:{connection_id}",
        schedule_in={"seconds": int(delay.total_seconds())},
    )
    kwargs: dict[str, JSONValue] = {
        "tenant_id": tenant_id,
        "connection_id": connection_id,
        "stack_name": stack_name,
        "tag": tag,
        "attempt": attempt,
    }
    with contextlib.suppress(AlreadyEnqueued):  # one queued check is enough
        try:
            await task.defer_async(**kwargs)
        except AppNotOpen:
            async with app.open_async():
                await task.defer_async(**kwargs)


@blueprint.task(name="connection.verify_removed", queue="connections", retry=3)
async def verify_removed(
    tenant_id: str, connection_id: str, stack_name: str, tag: str, attempt: int = 1
) -> str:
    """The stack reported its deletion: disconnect if the role can no longer be assumed.

    A role that still works is checked once more later (a stack delete can be slow); if it
    still works then, the report is ignored and the audit log says so.
    """
    final = attempt >= len(REMOVAL_CHECK_DELAYS)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=uuid.UUID(tenant_id))
        outcome, code = await check_stack_removed(
            session,
            tenant_id=uuid.UUID(tenant_id),
            connection_id=uuid.UUID(connection_id),
            stack_name=stack_name,
            tag=tag,
            final=final,
        )
    if outcome == "unknown":
        raise RemovalCheckError(code or "unknown")  # retried by the queue
    if outcome == "kept" and not final:
        from cloudoptics.platform.jobs import get_job_app

        await defer_removal_check(
            get_job_app(), tenant_id, connection_id, stack_name, tag, attempt=attempt + 1
        )
    return outcome


class RemovalCheckError(RuntimeError):
    """AWS answered with something other than 'role gone' or 'role works'."""


@blueprint.periodic(cron="17 */6 * * *")
@blueprint.task(name="connections.health", queue="connections", queueing_lock="connections.health")
async def health_check(timestamp: int) -> int:
    """Every 6 hours: re-check every connection that has been validated (FR-CON-09), so a
    broken role or export is noticed and its admins are told."""
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        rows = (
            await session.execute(
                text(
                    "SELECT tenant_id, id FROM cloud_connection WHERE health IS NOT NULL "
                    "AND status NOT IN ('disconnected', 'validating')"
                )
            )
        ).all()
    from cloudoptics.platform.jobs import get_job_app

    app = get_job_app()
    for tenant_id, connection_id in rows:
        await defer_once(
            app,
            CHECK_TASK,
            f"check:{connection_id}",
            tenant_id=str(tenant_id),
            connection_id=str(connection_id),
        )
    return len(rows)


@blueprint.task(name="accounts.discover", queue="connections", retry=2)
async def discover(tenant_id: str, connection_id: str) -> str:
    status = await discover_accounts(uuid.UUID(tenant_id), uuid.UUID(connection_id))
    from cloudoptics.inventory.jobs import defer_scan
    from cloudoptics.platform.jobs import get_job_app

    await defer_scan(get_job_app(), tenant_id, connection_id)
    return status


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
