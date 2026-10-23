"""/v1/tenants/{t}/costs and the manual ingest trigger (docs/09 §3.2 "Cost data")."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import Field

from cloudoptics.connectors.service import ConnectionNotFoundError, get_connection
from cloudoptics.identity.deps import AdminAccess, Session, ViewerAccess
from cloudoptics.ingestion import queries
from cloudoptics.ingestion.service import ingest_connection
from cloudoptics.platform.api import ApiModel
from cloudoptics.platform.settings import get_settings

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["costs"])


class MonthToDate(ApiModel):
    effective: float
    billed: float
    credits: float


class Forecast(ApiModel):
    amount: float
    method: Literal["run_rate"]
    days_covered: int
    days_in_month: int


Source = Literal["cur", "cost_explorer"]


class Reconciliation(ApiModel):
    """``mismatch``: CUR totals differ from Cost Explorer by over 1% (show a banner)."""

    status: Literal["ok", "mismatch", "failed"]
    month: str
    difference_pct: float | None
    accounts_mismatched: int
    checked_at: datetime


class CostSummary(ApiModel):
    """``provisional``: no CUR data for the month yet, figures come from Cost Explorer."""

    status: Literal["no_connection", "waiting", "provisional", "ready"]
    source: Source | None
    month: str
    currency: str
    data_through: date | None
    last_load_at: datetime | None
    last_load_status: str | None
    month_to_date: MonthToDate | None
    forecast: Forecast | None
    previous_month: float | None
    reconciliation: Reconciliation | None = None


class DailyPoint(ApiModel):
    date: date
    amount: float


class DailyCosts(ApiModel):
    source: Source | None
    currency: str
    points: list[DailyPoint]


class BreakdownItem(ApiModel):
    key: str
    label: str
    amount: float
    share: float


class Breakdown(ApiModel):
    source: Source | None
    month: str
    dimension: Literal["service", "account", "region"]
    currency: str
    total: float
    items: list[BreakdownItem]


class IngestOutcome(ApiModel):
    connection_id: uuid.UUID
    status: Literal["queued", "skipped", "up_to_date", "succeeded", "partial", "failed"]
    loaded: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)


@router.get("/costs/summary", response_model=CostSummary)
async def cost_summary(
    access: ViewerAccess, session: Session, today: date | None = None
) -> CostSummary:
    """``today`` (UTC) defaults to now; it exists so tests and demos can pin the month."""
    result = await queries.summary(session, today or datetime.now(UTC).date())
    return CostSummary.model_validate(result)


@router.get("/costs/daily", response_model=DailyCosts)
async def cost_daily(
    access: ViewerAccess,
    session: Session,
    today: date | None = None,
    days: int = Query(30, ge=7, le=92),
) -> DailyCosts:
    """Daily amortised cost after credits, for the trend chart."""
    result = await queries.daily(session, today or datetime.now(UTC).date(), days)
    return DailyCosts.model_validate(result)


@router.get("/costs/breakdown", response_model=Breakdown)
async def cost_breakdown(
    access: ViewerAccess,
    session: Session,
    dimension: Literal["service", "account", "region"] = "service",
    today: date | None = None,
) -> Breakdown:
    """This month's spend excluding tax, by service, account or region (top 8 plus the rest)."""
    result = await queries.breakdown(session, today or datetime.now(UTC).date(), dimension)
    return Breakdown.model_validate(result)


@router.post(
    "/connections/{connection_id}/ingest",
    response_model=IngestOutcome,
    responses={202: {"model": IngestOutcome}},
)
async def ingest_now(
    connection_id: uuid.UUID, response: Response, access: AdminAccess, session: Session
) -> IngestOutcome:
    try:
        await get_connection(session, connection_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found") from exc

    if get_settings().jobs_mode == "inline":
        result = await ingest_connection(access.tenant_id, connection_id)
        return IngestOutcome(
            connection_id=connection_id,
            status=result.status,
            loaded=result.loaded,
            rejected=result.rejected,
        )

    from cloudoptics.ingestion.jobs import defer_ingest
    from cloudoptics.platform.jobs import get_job_app

    await defer_ingest(get_job_app(), str(access.tenant_id), str(connection_id))
    response.status_code = status.HTTP_202_ACCEPTED
    return IngestOutcome(connection_id=connection_id, status="queued")
