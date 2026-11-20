"""The daily DigitalOcean invoice poll (ADR-021). Each connection's ingest is the shared
``ingestion:cur.ingest`` task, which runs the DigitalOcean ``billing_source``; month to date
comes from the shared daily ``ce.poll`` and the scan from ``inventory.poll``."""

from __future__ import annotations

import procrastinate
from sqlalchemy import text

from cloudoptics.ingestion.service import INGESTABLE
from cloudoptics.platform.db import get_sessionmaker, scheduler_scope

blueprint = procrastinate.Blueprint()

CONNECTIONS = text(
    "SELECT tenant_id, id FROM cloud_connection WHERE provider = 'digitalocean' "
    "AND status = ANY(:ok)"
)


@blueprint.periodic(cron="29 5 * * *")
@blueprint.task(name="invoices.poll", queue="ingestion", queueing_lock="digitalocean.poll")
async def poll(timestamp: int) -> int:
    """Daily: load any invoice DigitalOcean closed since the last poll (one a month)."""
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        rows = (await session.execute(CONNECTIONS, {"ok": list(INGESTABLE)})).all()
    from cloudoptics.ingestion.jobs import defer_ingest
    from cloudoptics.platform.jobs import get_job_app

    app = get_job_app()
    for tenant_id, connection_id in rows:
        await defer_ingest(app, str(tenant_id), str(connection_id))
    return len(rows)
