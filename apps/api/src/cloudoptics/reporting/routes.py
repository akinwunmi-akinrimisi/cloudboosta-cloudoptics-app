"""/v1/tenants/{t}/digest (PRD US-6.4): what the weekly digest says."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from cloudoptics.identity.deps import Session, ViewerAccess
from cloudoptics.platform.api import ApiModel
from cloudoptics.platform.settings import get_settings
from cloudoptics.reporting import digest

router = APIRouter(prefix="/v1/tenants/{tenant_id}/digest", tags=["reporting"])


class DigestPreview(ApiModel):
    week_start: date
    week_end: date
    subject: str
    text: str
    html: str


@router.get("/preview", response_model=DigestPreview)
async def preview(
    access: ViewerAccess,
    session: Session,
    week: Annotated[date | None, Query(description="The week's Monday")] = None,
) -> DigestPreview:
    """The digest for a week (default: the last full week, Monday to Sunday in the
    workspace's time zone), as it would be emailed."""
    if week is not None and week.weekday() != 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "A week starts on a Monday.")
    tz_name = (
        await session.execute(
            text("SELECT time_zone FROM tenant WHERE id = :t"), {"t": access.tenant_id}
        )
    ).scalar_one()
    local = datetime.now(UTC).astimezone(digest.zone(tz_name)).date()
    week_start = week or digest.week_before(local)
    built = await digest.build(session, access.tenant_id, week_start)
    if built is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No bills for last week yet")
    subject, body_text, body_html = digest.render(built, get_settings().app_url)
    return DigestPreview(
        week_start=built.week_start,
        week_end=built.week_end,
        subject=subject,
        text=body_text,
        html=body_html,
    )
