"""/v1/tenants/{t}/exports (docs/09 §3.2 "Reports & exports", PRD US-7.2).

``POST`` makes an export. Up to 50,000 rows the file is written during the request and the
answer is ``201`` with a finished export; larger exports answer ``202`` and the worker
writes the file, reporting ``progress`` (the export is its own operation resource). Each
user sees and downloads only the exports they made, while they are still a member of the
workspace, for 7 days. Every role can export: each export holds only what every member can
already see in the app.
"""

from __future__ import annotations

import json
import uuid
import zlib
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.audit import log as audit
from cloudoptics.exports import service
from cloudoptics.identity.deps import Session, ViewerAccess
from cloudoptics.platform.api import ApiModel, ApiProblem, user_actor
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

router = APIRouter(prefix="/v1/tenants/{tenant_id}/exports", tags=["exports"])

#: docs/09 §3.1 "Rate limiting": exports 10 an hour (per user, per workspace).
EXPORTS_PER_HOUR = 10

RECENT_SQL = text(
    "SELECT count(*) AS n, min(created_at) AS oldest FROM export_job "
    "WHERE requested_by = :u AND created_at > now() - interval '1 hour'"
)
INSERT_SQL = text(
    "INSERT INTO export_job (id, tenant_id, requested_by, kind, params, file_name, expires_at) "
    "VALUES (:id, :t, :u, :kind, CAST(:params AS jsonb), :name, "
    "now() + make_interval(days => :keep))"
)
GET_SQL = text(
    "SELECT id, kind, params, status, progress, row_count, file_name, size_bytes, error, "
    "created_at, started_at, finished_at, expires_at, expires_at <= now() AS expired "
    "FROM export_job WHERE id = :id AND requested_by = :u"
)
LIST_SQL = text(
    "SELECT id, kind, params, status, progress, row_count, file_name, size_bytes, error, "
    "created_at, started_at, finished_at, expires_at, expires_at <= now() AS expired "
    "FROM export_job WHERE requested_by = :u AND expires_at > now() "
    "ORDER BY created_at DESC LIMIT 50"
)
CONTENT_SQL = text(
    "SELECT content, file_name FROM export_job WHERE id = :id AND requested_by = :u "
    "AND status = 'succeeded' AND expires_at > now()"
)

Status = Literal["queued", "running", "succeeded", "failed", "expired"]


class Export(ApiModel):
    """An export and its file. ``downloadUrl`` (an API path; send the same sign-in) is set
    once the file is ready and until it expires."""

    id: uuid.UUID
    kind: Literal["costs", "recommendations", "anomalies", "budgets"]
    params: dict[str, Any]
    status: Status
    progress: int
    row_count: int | None
    file_name: str
    size_bytes: int | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    expires_at: datetime
    download_url: str | None


class ExportList(ApiModel):
    exports: list[Export]


def _export(row: Any, tenant_id: uuid.UUID) -> Export:
    state: Status = "expired" if row.expired else row.status
    ready = state == "succeeded"
    return Export(
        id=row.id,
        kind=row.kind,
        params=row.params,
        status=state,
        progress=row.progress,
        row_count=row.row_count,
        file_name=row.file_name,
        size_bytes=row.size_bytes,
        error=row.error,
        created_at=row.created_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        expires_at=row.expires_at,
        download_url=f"/v1/tenants/{tenant_id}/exports/{row.id}/download" if ready else None,
    )


async def _load(session: AsyncSession, export_id: uuid.UUID, user_id: uuid.UUID) -> Any:
    row = (await session.execute(GET_SQL, {"id": export_id, "u": user_id})).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Export not found")
    return row


async def _check_rate(session: AsyncSession, user_id: uuid.UUID) -> None:
    recent = (await session.execute(RECENT_SQL, {"u": user_id})).one()
    if recent.n >= EXPORTS_PER_HOUR:
        wait = 3600 - int((datetime.now(UTC) - recent.oldest).total_seconds())
        raise ApiProblem(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            f"You can make {EXPORTS_PER_HOUR} exports an hour. Try again later.",
            headers={"Retry-After": str(max(wait, 1))},
        )


@router.post(
    "",
    response_model=Export,
    status_code=status.HTTP_201_CREATED,
    responses={202: {"model": Export, "description": "Queued: the worker writes the file"}},
)
async def create_export(
    spec: Annotated[service.ExportSpec, Body()],
    request: Request,
    response: Response,
    access: ViewerAccess,
    session: Session,
) -> Export:
    await _check_rate(session, access.user.id)
    rows = await service.count(session, spec)
    if rows is not None and rows > service.MAX_ROWS:
        raise ApiProblem(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "export_too_large",
            f"This export would have {rows:,} rows; the most is {service.MAX_ROWS:,}. "
            "Choose a shorter period or fewer dimensions.",
        )
    export_id = uuid7()
    params = spec.model_dump(mode="json", by_alias=True, exclude={"kind"})
    await session.execute(
        INSERT_SQL,
        {
            "id": export_id,
            "t": access.tenant_id,
            "u": access.user.id,
            "kind": spec.kind,
            "params": json.dumps(params),
            "name": service.file_name(spec, datetime.now(UTC).date()),
            "keep": service.KEEP_DAYS,
        },
    )
    queued = rows is not None and rows > service.SYNC_ROWS and get_settings().jobs_mode == "queue"
    if not queued:
        try:
            built = await service.build(session, access.tenant_id, spec)
        except service.ExportError as exc:
            raise ApiProblem(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "export_too_large", str(exc)
            ) from exc
        await service.finish(session, export_id, built)
    # The audit chain takes a lock: write it after the file is made.
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="export.created",
        target_type="export",
        target_id=str(export_id),
        after={"kind": spec.kind, "params": params, "rows": rows, "queued": queued},
    )
    if queued:
        from cloudoptics.exports.jobs import defer_generate

        await defer_generate(str(access.tenant_id), str(export_id))
        response.status_code = status.HTTP_202_ACCEPTED
    return _export(await _load(session, export_id, access.user.id), access.tenant_id)


@router.get("", response_model=ExportList)
async def list_exports(access: ViewerAccess, session: Session) -> ExportList:
    """Your exports from the last 7 days, newest first."""
    rows = await session.execute(LIST_SQL, {"u": access.user.id})
    return ExportList(exports=[_export(r, access.tenant_id) for r in rows])


@router.get("/{export_id}", response_model=Export)
async def get_export(export_id: uuid.UUID, access: ViewerAccess, session: Session) -> Export:
    return _export(await _load(session, export_id, access.user.id), access.tenant_id)


def _inflate(content: bytes) -> Iterator[bytes]:
    stream = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16)
    for i in range(0, len(content), 64 * 1024):
        chunk = stream.decompress(content[i : i + 64 * 1024])
        if chunk:
            yield chunk
    tail = stream.flush()
    if tail:
        yield tail


@router.get(
    "/{export_id}/download",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}}}, 409: {}, 410: {}},
)
async def download_export(
    export_id: uuid.UUID, request: Request, access: ViewerAccess, session: Session
) -> Response:
    """The CSV file. Membership is checked again on every download, and a file is not
    served after it expires (410) or before it is ready (409)."""
    row = await _load(session, export_id, access.user.id)
    if row.expired:
        raise HTTPException(status.HTTP_410_GONE, "This export has expired. Export again.")
    if row.status != "succeeded":
        raise HTTPException(status.HTTP_409_CONFLICT, "This export is not ready")
    stored = (
        await session.execute(CONTENT_SQL, {"id": export_id, "u": access.user.id})
    ).one_or_none()
    if stored is None or stored.content is None:  # expired between the two reads
        raise HTTPException(status.HTTP_410_GONE, "This export has expired. Export again.")
    content = bytes(stored.content)
    headers = {
        "Content-Disposition": service.content_disposition(stored.file_name),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    media_type = "text/csv; charset=utf-8"
    if "gzip" in request.headers.get("accept-encoding", "").lower():
        # Stored compressed: send it as it is.
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
        return Response(content, media_type=media_type, headers=headers)
    return StreamingResponse(_inflate(content), media_type=media_type, headers=headers)
