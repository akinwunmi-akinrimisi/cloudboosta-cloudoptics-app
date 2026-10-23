"""Procrastinate app (ADR-003). Business modules register tasks on blueprints; this module
assembles them so the queue can be swapped without touching those modules."""

from __future__ import annotations

from functools import lru_cache

import procrastinate

from cloudoptics.platform.settings import get_settings


def _conninfo(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_job_app() -> procrastinate.App:
    from cloudoptics.connectors.jobs import blueprint as connector_jobs
    from cloudoptics.fx.feed import blueprint as fx_jobs
    from cloudoptics.ingestion.jobs import blueprint as ingestion_jobs

    app = procrastinate.App(
        connector=procrastinate.PsycopgConnector(
            conninfo=_conninfo(get_settings().app_database_url)
        )
    )
    app.add_tasks_from(connector_jobs, namespace="connectors")
    app.add_tasks_from(ingestion_jobs, namespace="ingestion")
    app.add_tasks_from(fx_jobs, namespace="fx")
    return app
