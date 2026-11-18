"""/v1/tenants/{t}/commitments (docs/09 §3.2, PRD US-4.5): the Savings Plans and Reserved
Instances held, and the Savings Plan advisory."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, Request, Response, status
from sqlalchemy import text

from cloudoptics.audit import log as audit
from cloudoptics.commitments import advisor, service
from cloudoptics.identity.deps import AdminAccess, Session, ViewerAccess
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.settings import get_settings

router = APIRouter(prefix="/v1/tenants/{tenant_id}/commitments", tags=["commitments"])


class CommitmentOut(ApiModel):
    """``kind``: ``savings_plan``, ``ec2_reserved`` or ``rds_reserved``. ``current`` is
    false once it ended or the daily refresh stopped listing it."""

    id: uuid.UUID
    connection_id: uuid.UUID
    kind: str
    commitment_id: str
    account_id: str | None
    region_id: str | None
    description: str
    plan_type: str | None
    term_months: int | None
    payment_option: str | None
    state: str | None
    quantity: int | None
    hourly_commitment_usd: float | None
    upfront_usd: float | None
    starts_at: datetime | None
    ends_at: datetime | None
    utilisation_pct: float | None
    days_left: int | None
    current: bool
    last_seen_at: datetime


class PayerCoverage(ApiModel):
    """Cost Explorer's view over the last 30 days, per connection (payer)."""

    connection_id: uuid.UUID
    savings_plans_coverage_percent: float | None
    savings_plans_utilisation_percent: float | None
    reservations_coverage_percent: float | None
    reservations_utilisation_percent: float | None
    fetched_at: datetime
    errors: list[str]


class Commitments(ApiModel):
    commitments: list[CommitmentOut]
    coverage: list[PayerCoverage]
    expiry_alert_days: list[int]


def _pct(block: object) -> float | None:
    if not isinstance(block, dict) or block.get("percent") is None:
        return None
    return float(block["percent"])


@router.get("", response_model=Commitments)
async def list_commitments(access: ViewerAccess, session: Session) -> Commitments:
    now = datetime.now(UTC)
    snaps = await service.snapshots(session)
    return Commitments(
        commitments=[CommitmentOut.model_validate(h) for h in await service.held(session, now)],
        coverage=[
            PayerCoverage(
                connection_id=s.connection_id,
                savings_plans_coverage_percent=_pct(s.coverage.get("savingsPlans")),
                savings_plans_utilisation_percent=_pct(s.utilisation.get("savingsPlans")),
                reservations_coverage_percent=_pct(s.coverage.get("reservations")),
                reservations_utilisation_percent=_pct(s.utilisation.get("reservations")),
                fetched_at=s.fetched_at,
                errors=s.errors,
            )
            for s in snaps.values()
        ],
        expiry_alert_days=list(service.EXPIRY_THRESHOLDS),
    )


class BaselineOut(ApiModel):
    days: int
    netted_usd_month: float
    average_hourly_usd: float
    p10_hourly_usd: float
    p20_hourly_usd: float
    target_hourly_usd: float
    band_low_hourly_usd: float
    band_high_hourly_usd: float


class OptionOut(ApiModel):
    """One cell of the matrix. ``source``: ``ours`` (our simulation) or ``aws`` (AWS's
    recommendation). Break-even in months; 0 = saves from the first hour."""

    source: Literal["ours", "aws"]
    instrument: Literal["savings_plan", "reserved_instance"]
    term_years: int
    payment: Literal["no_upfront", "partial_upfront", "all_upfront"]
    lookback_days: int | None
    discount_percent: float
    hourly_commitment_usd: float
    covered_on_demand_hourly_usd: float
    monthly_savings_usd: float
    upfront_usd: float
    monthly_recurring_usd: float
    total_commitment_usd: float
    break_even_months: float
    discount_from_aws: bool


class LadderStepOut(ApiModel):
    month: int
    buy_on: date
    buy_hourly_usd: float
    total_hourly_usd: float
    share_percent: int


class FxScenarioOut(ApiModel):
    weakening_percent: int
    rate: float
    monthly_local: float
    upfront_local: float
    total_local: float


class FxOut(ApiModel):
    currency: str
    rate: float
    rate_date: date
    estimated: bool
    scenarios: list[FxScenarioOut]


class DayAmount(ApiModel):
    date: date
    amount: float


class AdviceOut(ApiModel):
    """``status``: ``no_data``, ``not_enough_data`` (under 14 days of bills),
    ``no_purchase`` (the safe commitment is too small to be worth it) or ``ok``."""

    connection_id: uuid.UUID
    payer_account_id: str
    status: advisor.Status
    data_through: date | None
    baseline: BaselineOut
    daily_eligible: list[DayAmount]
    options: list[OptionOut]
    default: OptionOut | None
    ladder: list[LadderStepOut]
    fx: FxOut | None
    fx_exposed: bool
    current_coverage_percent: float | None
    target_coverage_percent: float | None
    utilisation_percent: float | None
    decline_tolerance_percent: float | None
    aws_fetched_at: datetime | None
    aws_errors: list[str]


class SavingsPlanAdvisory(ApiModel):
    advice: list[AdviceOut]
    return_window_days: int
    ladder_first_percent: int
    ladder_step_percent: int
    fx_stress_percent: list[int]


def _advice_out(a: service.Advice) -> AdviceOut:
    data = {k: getattr(a, k) for k in a.__dataclass_fields__ if k != "daily_eligible"}
    return AdviceOut.model_validate(
        {**data, "daily_eligible": [DayAmount(date=d, amount=v) for d, v in a.daily_eligible]}
    )


@router.get("/savings-plans", response_model=SavingsPlanAdvisory)
async def savings_plans(access: ViewerAccess, session: Session) -> SavingsPlanAdvisory:
    today = datetime.now(UTC).date()
    return SavingsPlanAdvisory(
        advice=[_advice_out(a) for a in await service.advice(session, today)],
        return_window_days=advisor.RETURN_WINDOW_DAYS,
        ladder_first_percent=round(advisor.LADDER_FIRST * 100),
        ladder_step_percent=round(advisor.LADDER_STEP * 100),
        fx_stress_percent=[round(p * 100) for p in advisor.FX_STRESS],
    )


class RefreshResult(ApiModel):
    status: Literal["queued", "skipped", "succeeded", "partial", "failed"]
    connections: int


@router.post("/refresh", response_model=RefreshResult)
async def refresh(
    request: Request, response: Response, access: AdminAccess, session: Session
) -> RefreshResult:
    """Read the commitments and ask Cost Explorer again now (about USD 0.08 a connection,
    billed to the AWS account), instead of waiting for the daily refresh."""
    ids = list(
        (
            await session.execute(
                text("SELECT id FROM cloud_connection WHERE status = ANY(:ok) ORDER BY created_at"),
                {"ok": list(service.USABLE)},
            )
        ).scalars()
    )

    async def record() -> None:
        # Written last: the inline refresh and analysis run in their own transactions and
        # append to the audit chain too (verification), which this one would otherwise
        # hold locked until the request ends.
        await audit.record(
            session,
            tenant_id=access.tenant_id,
            actor=user_actor(request, access.user.id),
            action="commitments.refresh_requested",
            target_type="tenant",
            target_id=str(access.tenant_id),
            after={"connections": len(ids)},
        )

    if not ids:
        await record()
        return RefreshResult(status="skipped", connections=0)
    if get_settings().jobs_mode == "inline":
        # The refresh uses its own transactions; this one keeps only the audit row.
        from cloudoptics.analysis.engine import run_analysis

        results = [await service.refresh(access.tenant_id, c, force=True) for c in ids]
        from cloudoptics.platform.db import get_sessionmaker, scope

        async with get_sessionmaker()() as s, s.begin():
            await scope(s, tenant_id=access.tenant_id)
            await service.alert_expiries(s, access.tenant_id, datetime.now(UTC))
        await run_analysis(access.tenant_id)
        worst: Literal["succeeded", "partial", "failed", "skipped"] = (
            "failed"
            if all(r == "failed" for r in results)
            else "partial"
            if any(r in ("partial", "failed") for r in results)
            else "succeeded"
        )
        await record()
        return RefreshResult(status=worst, connections=len(ids))
    from cloudoptics.commitments.jobs import defer_refresh
    from cloudoptics.platform.jobs import get_job_app

    for c in ids:
        await defer_refresh(get_job_app(), str(access.tenant_id), str(c))
    await record()
    response.status_code = status.HTTP_202_ACCEPTED
    return RefreshResult(status="queued", connections=len(ids))
