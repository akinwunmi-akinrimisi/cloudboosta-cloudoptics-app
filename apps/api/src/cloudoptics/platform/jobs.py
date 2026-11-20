"""Procrastinate app (ADR-003). Business modules register tasks on blueprints; this module
assembles them so the queue can be swapped without touching those modules.

One worker serves every tenant, so no job may hold it for long:

- the worker runs ``worker_concurrency`` jobs at a time (``cloudoptics.worker``);
- every job has a deadline (``TASK_TIMEOUTS``, else ``DEFAULT_TIMEOUT``) after which it is
  cancelled and fails (its task's ``retry`` then applies). Cancelling stops the job at its
  next ``await``: work already handed to a thread finishes in that thread, which is why the
  heavy steps also have limits of their own (bill file caps, the pattern time limit);
- ``platform:jobs.recover_stalled`` (every 10 minutes) puts back in the queue the jobs a
  worker was running when it died (no heartbeat), at most ``STALLED_MAX_ATTEMPTS`` times;
  after that the job is marked failed, so a job that kills its worker cannot loop.

Jobs queued through ``defer_once`` also take a Procrastinate ``lock`` (``run_lock``, by
default the queueing lock), so with several slots the same work never runs twice at once,
and work that writes the same rows (a connection's bill loads and reloads, a tenant's
analysis and verification) runs one job at a time as it did with one slot. A job queued
again after a stall re-reads what it needs from the database, as it does on its own retry.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued, AppNotOpen, UniqueViolation
from procrastinate.jobs import Status

from cloudoptics.platform.settings import get_settings

log = logging.getLogger(__name__)

#: Seconds a job may run, by task. Bill loads and scans of an Organization take longest.
DEFAULT_TIMEOUT = 15 * 60
TASK_TIMEOUTS: dict[str, int] = {
    "ingestion:cur.ingest": 60 * 60,
    "ingestion:cur.reload": 60 * 60,
    "inventory:inventory.scan": 60 * 60,
    "metrics:metrics.pull": 30 * 60,
    "commitments:commitments.refresh": 30 * 60,
}
#: A worker with no heartbeat for this long is gone (it beats every 10 s).
STALLED_AFTER_SECONDS = 120
#: Runs a job may have had before a stall is no longer retried.
STALLED_MAX_ATTEMPTS = 2

blueprint = procrastinate.Blueprint()


def task_timeout(task_name: str) -> int:
    return TASK_TIMEOUTS.get(task_name, DEFAULT_TIMEOUT)


async def deadline_middleware(
    call_next: Callable[[], Awaitable[Any]], context: Any, worker: Any
) -> Any:
    """Worker middleware: fail a job that runs past its task's deadline."""
    name = context.job.task_name
    seconds = task_timeout(name)
    try:
        return await asyncio.wait_for(call_next(), timeout=seconds)
    except TimeoutError as exc:
        raise TimeoutError(f"{name} ran for more than {seconds} s and was stopped") from exc


async def recover_stalled_jobs(app: procrastinate.App) -> int:
    """Queue again the jobs of dead workers; fail those that already stalled too often.
    Returns how many were queued again."""
    manager = app.job_manager
    retried = 0
    for job in await manager.get_stalled_jobs(seconds_since_heartbeat=STALLED_AFTER_SECONDS):
        assert job.id is not None
        if job.attempts >= STALLED_MAX_ATTEMPTS:
            log.warning("Stalled job %s (%s) failed: stalled too often", job.id, job.task_name)
            await manager.finish_job_by_id_async(job.id, status=Status.FAILED, delete_job=False)
            continue
        try:
            await manager.retry_job(job)
        except UniqueViolation:
            # The same work is queued again already (queueing lock): that copy runs.
            await manager.finish_job_by_id_async(job.id, status=Status.FAILED, delete_job=False)
            continue
        log.warning("Stalled job %s (%s) queued again", job.id, job.task_name)
        retried += 1
    return retried


@blueprint.periodic(cron="*/10 * * * *")
@blueprint.task(
    name="jobs.recover_stalled",
    queue="connections",
    queueing_lock="jobs.recover_stalled",
    pass_context=True,
)
async def recover_stalled(context: procrastinate.JobContext, timestamp: int) -> int:
    return await recover_stalled_jobs(context.app)


def _conninfo(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_job_app() -> procrastinate.App:
    from cloudoptics.analysis.jobs import blueprint as analysis_jobs
    from cloudoptics.anomalies.jobs import blueprint as anomaly_jobs
    from cloudoptics.budgets.jobs import blueprint as budget_jobs
    from cloudoptics.commitments.jobs import blueprint as commitment_jobs
    from cloudoptics.connectors.jobs import blueprint as connector_jobs
    from cloudoptics.exports.jobs import blueprint as export_jobs
    from cloudoptics.fx.feed import blueprint as fx_jobs
    from cloudoptics.ingestion.jobs import blueprint as ingestion_jobs
    from cloudoptics.inventory.jobs import blueprint as inventory_jobs
    from cloudoptics.metrics.jobs import blueprint as metrics_jobs
    from cloudoptics.notifications.jobs import blueprint as notification_jobs
    from cloudoptics.privacy.jobs import blueprint as privacy_jobs
    from cloudoptics.recommendations.jobs import blueprint as recommendation_jobs
    from cloudoptics.reporting.jobs import blueprint as reporting_jobs

    app = procrastinate.App(
        connector=procrastinate.PsycopgConnector(
            conninfo=_conninfo(get_settings().app_database_url)
        )
    )
    app.add_tasks_from(connector_jobs, namespace="connectors")
    app.add_tasks_from(ingestion_jobs, namespace="ingestion")
    app.add_tasks_from(fx_jobs, namespace="fx")
    app.add_tasks_from(inventory_jobs, namespace="inventory")
    app.add_tasks_from(metrics_jobs, namespace="metrics")
    app.add_tasks_from(analysis_jobs, namespace="analysis")
    app.add_tasks_from(notification_jobs, namespace="notifications")
    app.add_tasks_from(budget_jobs, namespace="budgets")
    app.add_tasks_from(anomaly_jobs, namespace="anomalies")
    app.add_tasks_from(reporting_jobs, namespace="reporting")
    app.add_tasks_from(recommendation_jobs, namespace="recommendations")
    app.add_tasks_from(commitment_jobs, namespace="commitments")
    app.add_tasks_from(privacy_jobs, namespace="privacy")
    app.add_tasks_from(export_jobs, namespace="exports")
    app.add_tasks_from(blueprint, namespace="platform")
    return app


async def defer_once(
    app: procrastinate.App, name: str, lock: str, run_lock: str | None = None, **kwargs: str
) -> None:
    """Queue a task once; a second request while one is queued is a no-op. Jobs with the
    same ``run_lock`` (default ``lock``) run one at a time.

    Works from a worker (the app is open) and from anywhere else (opened for the call).
    """
    task = app.configure_task(name, queueing_lock=lock, lock=run_lock or lock)
    with contextlib.suppress(AlreadyEnqueued):
        try:
            await task.defer_async(**kwargs)
        except AppNotOpen:
            async with app.open_async():
                await task.defer_async(**kwargs)
