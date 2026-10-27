"""/v1/tenants/{t}/digest (PRD US-6.4) and /reports (US-7.1, docs/09 §3.2 "Reports")."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Path, Query, Request, status
from pydantic import EmailStr, Field
from sqlalchemy import text

from cloudoptics.audit import log as audit
from cloudoptics.identity.deps import AdminAccess, Session, ViewerAccess
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.settings import get_settings
from cloudoptics.reporting import digest, monthly

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


reports_router = APIRouter(prefix="/v1/tenants/{tenant_id}/reports", tags=["reporting"])

MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"


class ReportListItem(ApiModel):
    month: str
    generated_at: datetime
    spend_usd: float
    spend_local: float | None
    currency: str


class ReportList(ApiModel):
    reports: list[ReportListItem]
    #: The last full month, shown as a draft until its report is made.
    latest_month: str


class ReportView(ApiModel):
    report: monthly.MonthlyReport
    #: False: worked out now from the current data (the stored report is made on the 3rd
    #: working day of the next month).
    stored: bool


class ReportSettings(ApiModel):
    """Addresses that get the report besides the members (who can turn it off)."""

    emails: list[EmailStr] = Field(default_factory=list, max_length=20)


class SendResult(ApiModel):
    sent_to: int
    report: monthly.MonthlyReport


async def _local_today(session: Any, tenant_id: uuid.UUID) -> date:
    tz_name = (
        await session.execute(text("SELECT time_zone FROM tenant WHERE id = :t"), {"t": tenant_id})
    ).scalar_one()
    return datetime.now(UTC).astimezone(digest.zone(tz_name)).date()


async def _month(session: Any, tenant_id: uuid.UUID, month: str) -> date:
    first = date.fromisoformat(f"{month}-01")
    if first >= monthly.month_start(await _local_today(session, tenant_id)):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "A report covers a month that has ended."
        )
    return first


@reports_router.get("", response_model=ReportList)
async def list_reports(access: ViewerAccess, session: Session) -> ReportList:
    rows = await session.execute(
        text("SELECT month, generated_at, content FROM monthly_report ORDER BY month DESC")
    )
    today = await _local_today(session, access.tenant_id)
    return ReportList(
        reports=[
            ReportListItem(
                month=f"{r.month:%Y-%m}",
                generated_at=r.generated_at,
                spend_usd=r.content["spend"]["usd"],
                spend_local=r.content["spend"]["local"],
                currency=r.content["currency"],
            )
            for r in rows
        ],
        latest_month=f"{monthly.previous_month(today):%Y-%m}",
    )


@reports_router.get("/settings", response_model=ReportSettings)
async def get_report_settings(access: ViewerAccess, session: Session) -> ReportSettings:
    return ReportSettings(emails=await monthly.extra_emails(session))


@reports_router.put("/settings", response_model=ReportSettings)
async def put_report_settings(
    body: ReportSettings, request: Request, access: AdminAccess, session: Session
) -> ReportSettings:
    emails = sorted({str(e).lower() for e in body.emails})
    await session.execute(
        text(
            "INSERT INTO report_setting (tenant_id, emails, updated_by) VALUES (:t, :e, :u) "
            "ON CONFLICT (tenant_id) DO UPDATE SET emails = EXCLUDED.emails, "
            "updated_by = EXCLUDED.updated_by, updated_at = now()"
        ),
        {"t": access.tenant_id, "e": emails, "u": access.user.id},
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="report_settings.updated",
        target_type="tenant",
        target_id=str(access.tenant_id),
        after={"emails": emails},
    )
    return ReportSettings(emails=emails)


@reports_router.get("/{month}", response_model=ReportView)
async def get_report(
    month: Annotated[str, Path(pattern=MONTH)], access: ViewerAccess, session: Session
) -> ReportView:
    first = await _month(session, access.tenant_id, month)
    saved = await monthly.stored(session, first)
    if saved is not None:
        return ReportView(report=saved, stored=True)
    built = await monthly.build(session, access.tenant_id, first)
    if built is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No bills for this month")
    return ReportView(report=built, stored=False)


@reports_router.post("/{month}/send", response_model=SendResult)
async def send_report(
    month: Annotated[str, Path(pattern=MONTH)],
    request: Request,
    access: AdminAccess,
    session: Session,
) -> SendResult:
    """Make the report again from the current data, store it and email every recipient."""
    first = await _month(session, access.tenant_id, month)
    built = await monthly.build(session, access.tenant_id, first)
    if built is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No bills for this month")
    await monthly.store(session, access.tenant_id, built, access.user.id)
    sent = await monthly.email(session, access.tenant_id, built, again=True)
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="report.sent",
        target_type="report",
        target_id=month,
        after={"sentTo": sent},
    )
    return SendResult(sent_to=sent, report=built)
