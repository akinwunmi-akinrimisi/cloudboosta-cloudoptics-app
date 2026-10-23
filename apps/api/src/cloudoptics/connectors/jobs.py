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
    if result.status != "failed":
        from cloudoptics.ingestion.jobs import defer_ingest, defer_snapshot
        from cloudoptics.platform.jobs import get_job_app

        # Provisional costs now; the export is loaded straight away rather than at the next poll.
        await defer_snapshot(get_job_app(), tenant_id, connection_id)
        if result.cur_bucket:
            await defer_ingest(get_job_app(), tenant_id, connection_id)
    return result.status
