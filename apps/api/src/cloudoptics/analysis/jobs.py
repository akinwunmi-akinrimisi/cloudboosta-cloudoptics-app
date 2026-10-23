"""Background job for the recommendation engine (docs/09 §3.3)."""

from __future__ import annotations

import uuid

import procrastinate

from cloudoptics.analysis.engine import run_analysis
from cloudoptics.platform.jobs import defer_once

blueprint = procrastinate.Blueprint()

RUN_TASK = "analysis:analysis.run"


async def defer_analysis(app: procrastinate.App, tenant_id: str) -> None:
    await defer_once(app, RUN_TASK, f"analysis:{tenant_id}", tenant_id=tenant_id)


@blueprint.task(name="analysis.run", queue="analysis", retry=1)
async def run(tenant_id: str) -> int:
    """After each inventory scan: re-evaluate every rule for the tenant."""
    result = await run_analysis(uuid.UUID(tenant_id))
    return result["recommendations"]
