"""Background jobs for connections (ADR-003)."""

from __future__ import annotations

import uuid

import procrastinate

from cloudoptics.audit.log import Actor
from cloudoptics.connectors.service import run_validation
from cloudoptics.platform.db import get_sessionmaker, scope

blueprint = procrastinate.Blueprint()


@blueprint.task(name="connection.validate", queue="connections", retry=2)
async def validate_connection(tenant_id: str, connection_id: str) -> str:
    tid, cid = uuid.UUID(tenant_id), uuid.UUID(connection_id)
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tid)
        result = await run_validation(
            session, tenant_id=tid, connection_id=cid, actor=Actor(type="system")
        )
    return result.status
