"""Background job for the metrics puller (docs/11c §1.2)."""

from __future__ import annotations

import uuid

import procrastinate

from cloudoptics.metrics.puller import pull_connection
from cloudoptics.platform.jobs import defer_once

blueprint = procrastinate.Blueprint()

PULL_TASK = "metrics:metrics.pull"


async def defer_pull(app: procrastinate.App, tenant_id: str, connection_id: str) -> None:
    await defer_once(
        app,
        PULL_TASK,
        f"metrics:{connection_id}",
        tenant_id=tenant_id,
        connection_id=connection_id,
    )


@blueprint.task(name="metrics.pull", queue="metrics", retry=1)
async def pull(tenant_id: str, connection_id: str) -> str:
    """After each inventory scan: pull usage, then re-run the rules with it."""
    status = await pull_connection(uuid.UUID(tenant_id), uuid.UUID(connection_id))
    from cloudoptics.analysis.jobs import defer_analysis
    from cloudoptics.platform.jobs import get_job_app

    await defer_analysis(get_job_app(), tenant_id)
    return status
