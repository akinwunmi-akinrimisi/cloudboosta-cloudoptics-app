"""Procrastinate app (ADR-003). Business modules register tasks on blueprints; this module
assembles them so the queue can be swapped without touching those modules."""

from __future__ import annotations

import contextlib
from functools import lru_cache

import procrastinate
from procrastinate.exceptions import AlreadyEnqueued, AppNotOpen

from cloudoptics.platform.settings import get_settings


def _conninfo(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_job_app() -> procrastinate.App:
    from cloudoptics.analysis.jobs import blueprint as analysis_jobs
    from cloudoptics.anomalies.jobs import blueprint as anomaly_jobs
    from cloudoptics.budgets.jobs import blueprint as budget_jobs
    from cloudoptics.connectors.jobs import blueprint as connector_jobs
    from cloudoptics.fx.feed import blueprint as fx_jobs
    from cloudoptics.ingestion.jobs import blueprint as ingestion_jobs
    from cloudoptics.inventory.jobs import blueprint as inventory_jobs
    from cloudoptics.metrics.jobs import blueprint as metrics_jobs
    from cloudoptics.notifications.jobs import blueprint as notification_jobs

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
    return app


async def defer_once(app: procrastinate.App, name: str, lock: str, **kwargs: str) -> None:
    """Queue a task once; a second request while one is queued is a no-op.

    Works from a worker (the app is open) and from anywhere else (opened for the call).
    """
    task = app.configure_task(name, queueing_lock=lock)
    with contextlib.suppress(AlreadyEnqueued):
        try:
            await task.defer_async(**kwargs)
        except AppNotOpen:
            async with app.open_async():
                await task.defer_async(**kwargs)
