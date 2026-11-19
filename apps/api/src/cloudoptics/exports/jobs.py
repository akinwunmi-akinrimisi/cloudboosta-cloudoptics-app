"""Background jobs for exports (docs/09 §2.4 ``export.generate``)."""

from __future__ import annotations

import logging
import uuid

import procrastinate
from pydantic import TypeAdapter
from sqlalchemy import text

from cloudoptics.exports import service
from cloudoptics.platform.db import get_sessionmaker, scheduler_scope, scope

log = logging.getLogger(__name__)

blueprint = procrastinate.Blueprint()

GENERATE_TASK = "exports:export.generate"

START_SQL = (
    "UPDATE export_job SET status = 'running', progress = 5, started_at = now() "
    "WHERE id = :id AND status IN ('queued', 'running') AND expires_at > now() "
    "RETURNING kind, params"
)
PROGRESS_SQL = "UPDATE export_job SET progress = :p WHERE id = :id AND status = 'running'"
EXISTS_SQL = "SELECT status FROM export_job WHERE id = :id"
EXPIRE_SQL = "DELETE FROM export_job WHERE expires_at < now()"

_spec: TypeAdapter[service.ExportSpec] = TypeAdapter(service.ExportSpec)


class ExportNotCommittedError(Exception):
    """The request that made the export has not committed yet: try again shortly."""


async def defer_generate(tenant_id: str, export_id: str) -> None:
    from cloudoptics.platform.jobs import defer_once, get_job_app

    await defer_once(
        get_job_app(),
        GENERATE_TASK,
        f"export:{export_id}",
        tenant_id=tenant_id,
        export_id=export_id,
    )


@blueprint.task(
    name="export.generate",
    queue="exports",
    retry=procrastinate.RetryStrategy(
        max_attempts=5, wait=2, retry_exceptions={ExportNotCommittedError}
    ),
)
async def generate(tenant_id: str, export_id: str) -> str:
    """Write a large export's file, reporting progress as it goes. Returns the status."""
    tid, eid = uuid.UUID(tenant_id), uuid.UUID(export_id)
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tid)
        started = (await session.execute(text(START_SQL), {"id": eid})).one_or_none()
        if started is None:
            state = (await session.execute(text(EXISTS_SQL), {"id": eid})).scalar_one_or_none()
            if state is None:
                # Queued from inside the request's transaction, which may not be visible yet.
                raise ExportNotCommittedError(export_id)
            return str(state)
    spec = _spec.validate_python({"kind": started.kind, **started.params})

    async def progress(share: float) -> None:
        async with maker() as s, s.begin():
            await scope(s, tenant_id=tid)
            await s.execute(text(PROGRESS_SQL), {"id": eid, "p": 5 + int(90 * share)})

    try:
        async with maker() as session, session.begin():
            await scope(session, tenant_id=tid)
            total = await service.count(session, spec)
            built = await service.build(session, tid, spec, total=total, progress=progress)
            await service.finish(session, eid, built)
        return "succeeded"
    except service.ExportError as exc:
        message = str(exc)
    except Exception:  # the user sees a plain failure; the details go to the logs
        log.exception("export %s failed", export_id)
        message = "The export failed. Try again, or contact support if it keeps failing."
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tid)
        await service.fail(session, eid, message)
    return "failed"


@blueprint.periodic(cron="23 3 * * *")
@blueprint.task(name="export.expire", queue="exports", queueing_lock="export.expire")
async def expire(timestamp: int) -> int:
    """Daily: delete exports past their 7 days (downloads already refuse them)."""
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        result = await session.execute(text(EXPIRE_SQL))
        return int(getattr(result, "rowcount", 0) or 0)
