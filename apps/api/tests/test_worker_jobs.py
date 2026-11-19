"""The shared worker: concurrency, job deadlines and stalled-job recovery (audit run 1,
leads 11 and 12)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from cloudoptics import worker
from cloudoptics.platform import jobs
from cloudoptics.platform.settings import Settings


def job_context(task_name: str) -> Any:
    return SimpleNamespace(job=SimpleNamespace(task_name=task_name))


async def test_a_job_past_its_deadline_fails(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(jobs, "TASK_TIMEOUTS", {"ingestion:cur.ingest": 0.05})
    monkeypatch.setattr(jobs, "DEFAULT_TIMEOUT", 5)
    cancelled = asyncio.Event()

    async def slow() -> str:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "done"

    with pytest.raises(TimeoutError, match=r"ingestion:cur.ingest ran for more than"):
        await jobs.deadline_middleware(slow, job_context("ingestion:cur.ingest"), None)
    assert cancelled.is_set()  # the job itself was stopped, not left running

    async def quick() -> str:
        return "done"

    assert await jobs.deadline_middleware(quick, job_context("analysis:analysis.run"), None) == (
        "done"
    )


def test_every_deadline_names_a_registered_task(app) -> None:  # type: ignore[no-untyped-def]
    # Task names as the blueprints register them (an app built twice in one process
    # namespaces them again, so match on the ending).
    registered = set(jobs.get_job_app.__wrapped__().tasks)
    for name in [*jobs.TASK_TIMEOUTS, "platform:jobs.recover_stalled"]:
        assert any(t == name or t.endswith(f":{name}") for t in registered), name
    assert jobs.task_timeout("anything:else") == jobs.DEFAULT_TIMEOUT


async def test_the_worker_runs_jobs_concurrently_with_deadlines(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, Any] = {}

    class FakeApp:
        @contextlib.asynccontextmanager
        async def open_async(self) -> AsyncIterator[None]:
            yield

        async def run_worker_async(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    settings = Settings(worker_concurrency=5)
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "get_job_app", lambda: FakeApp())
    await worker.main()
    assert seen["concurrency"] == 5
    assert seen["worker_middleware"] == [jobs.deadline_middleware]
    assert "notifications" in seen["queues"]
    assert Settings().worker_concurrency == 3
    with pytest.raises(ValidationError):
        Settings(worker_concurrency=0)


INSERT_JOB = (
    "INSERT INTO procrastinate_jobs (queue_name, task_name, lock, queueing_lock, args, "
    "status, attempts) VALUES ('connections', %s, %s, %s, '{}', %s, %s) RETURNING id"
)


async def test_stalled_jobs_are_queued_again_a_bounded_number_of_times(app, owner_sql) -> None:  # type: ignore[no-untyped-def]
    task = "test:stalled.example"
    # Left in 'doing' by a worker that died (no worker row, so no heartbeat).
    [(fresh,)] = owner_sql(INSERT_JOB, task, "lock-fresh", None, "doing", 0)
    [(looping,)] = owner_sql(
        INSERT_JOB, task, "lock-looping", None, "doing", jobs.STALLED_MAX_ATTEMPTS
    )
    [(queued_twice,)] = owner_sql(INSERT_JOB, task, "lock-q1", "same-work", "doing", 0)
    owner_sql(INSERT_JOB, task, "lock-q2", "same-work", "todo", 0)

    job_app = jobs.get_job_app()
    async with job_app.open_async():
        retried = await jobs.recover_stalled_jobs(job_app)
    assert retried >= 1
    status = dict(
        owner_sql("SELECT id, status::text FROM procrastinate_jobs WHERE task_name = %s", task)
    )
    assert status[fresh] == "todo"  # runs again
    assert status[looping] == "failed"  # stalled too often: it may be what kills the worker
    assert status[queued_twice] == "failed"  # the queued copy of the same work runs instead
    owner_sql("DELETE FROM procrastinate_jobs WHERE task_name = %s", task)


async def test_work_on_the_same_rows_runs_one_job_at_a_time(app, owner_sql) -> None:  # type: ignore[no-untyped-def]
    """With several slots, a connection's bill load and reload (and a tenant's analysis and
    verification) share a Procrastinate lock, so they still run one after the other."""
    import uuid

    from cloudoptics.analysis.jobs import defer_analysis
    from cloudoptics.ingestion.jobs import defer_ingest, defer_reload
    from cloudoptics.recommendations.jobs import defer_verification

    tenant, connection = str(uuid.uuid4()), str(uuid.uuid4())
    job_app = jobs.get_job_app()
    await defer_ingest(job_app, tenant, connection)
    await defer_reload(job_app, tenant, connection, None)
    await defer_analysis(job_app, tenant)
    await defer_verification(job_app, tenant)
    rows = owner_sql(
        "SELECT task_name, lock, queueing_lock FROM procrastinate_jobs "
        "WHERE args->>'tenant_id' = %s ORDER BY id",
        tenant,
    )
    assert rows == [
        ("ingestion:cur.ingest", f"bills:{connection}", f"ingest:{connection}"),
        ("ingestion:cur.reload", f"bills:{connection}", f"reload:{connection}"),
        ("analysis:analysis.run", f"recommendations:{tenant}", f"analysis:{tenant}"),
        ("recommendations:verification.run", f"recommendations:{tenant}", f"verification:{tenant}"),
    ]
    owner_sql("DELETE FROM procrastinate_jobs WHERE args->>'tenant_id' = %s", tenant)
