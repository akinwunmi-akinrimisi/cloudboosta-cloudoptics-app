"""Background jobs for notifications (ADR-003)."""

from __future__ import annotations

import uuid

import procrastinate

from cloudoptics.notifications.alerts import send_connection_alert
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.jobs import defer_once

blueprint = procrastinate.Blueprint()

ALERT_TASK = "notifications:connection.alert"


async def defer_connection_alert(
    app: procrastinate.App, tenant_id: str, connection_id: str
) -> None:
    await defer_once(
        app,
        ALERT_TASK,
        f"alert:{connection_id}",
        tenant_id=tenant_id,
        connection_id=connection_id,
    )


@blueprint.task(name="connection.alert", queue="notifications", retry=3)
async def connection_alert(tenant_id: str, connection_id: str) -> str:
    tid = uuid.UUID(tenant_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        return await send_connection_alert(session, tid, uuid.UUID(connection_id))
