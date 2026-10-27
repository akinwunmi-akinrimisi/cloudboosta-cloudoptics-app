"""/v1/tenants/{t}/costs/credits (PRD US-2.4, docs/09 §3.2): the runway and the balance."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import Field, model_validator
from sqlalchemy import text

from cloudoptics.audit import log as audit
from cloudoptics.credits import service
from cloudoptics.identity.deps import AdminAccess, Session, ViewerAccess
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.ids import uuid7

router = APIRouter(prefix="/v1/tenants/{tenant_id}/costs/credits", tags=["costs"])


class BalanceOut(ApiModel):
    amount_usd: float
    as_of: date
    expires_on: date | None
    note: str | None
    entered_by: str | None
    entered_at: datetime


class DayCredits(ApiModel):
    date: date
    amount: float


class Credits(ApiModel):
    """The credits runway, in USD. ``status``: ``no_credits`` (none applied, no balance),
    ``no_balance`` (credits applied but no balance entered, so no runway), ``ok``,
    ``ending_soon`` (under 90 days left) or ``used_up``."""

    status: service.Status
    data_through: date | None
    applied_month_usd: float
    burn_usd_day: float
    burn_days: int
    spend_before_credits_usd_month: float
    balance: BalanceOut | None
    used_since_balance_usd: float | None
    remaining_usd: float | None
    days_left: int | None
    depletion_date: date | None
    expires_first: bool
    daily: list[DayCredits]


def _out(r: service.Runway) -> Credits:
    b = r.balance
    return Credits(
        status=r.status,
        data_through=r.data_through,
        applied_month_usd=r.applied_month_usd,
        burn_usd_day=r.burn_usd_day,
        burn_days=r.burn_days,
        spend_before_credits_usd_month=r.spend_before_credits_usd_month,
        balance=None
        if b is None
        else BalanceOut(
            amount_usd=b.amount_usd,
            as_of=b.as_of,
            expires_on=b.expires_on,
            note=b.note,
            entered_by=b.entered_by,
            entered_at=b.created_at,
        ),
        used_since_balance_usd=r.used_since_balance_usd,
        remaining_usd=r.remaining_usd,
        days_left=r.days_left,
        depletion_date=r.depletion_date,
        expires_first=r.expires_first,
        daily=[DayCredits(date=d, amount=a) for d, a in r.daily],
    )


@router.get("", response_model=Credits)
async def get_credits(access: ViewerAccess, session: Session) -> Credits:
    return _out(await service.runway(session, datetime.now(UTC).date()))


class BalanceIn(ApiModel):
    """The remaining balance shown in the AWS Billing console (Credits page) on ``asOf``."""

    amount_usd: float = Field(ge=0, le=1e9)
    as_of: date | None = None
    expires_on: date | None = None
    note: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _check(self) -> BalanceIn:
        if self.note is not None:
            self.note = self.note.strip() or None
        return self


@router.put("/balance", response_model=Credits)
async def put_balance(
    body: BalanceIn, request: Request, access: AdminAccess, session: Session
) -> Credits:
    today = datetime.now(UTC).date()
    as_of = body.as_of or today
    if as_of > today:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "The balance date cannot be in the future."
        )
    if body.expires_on is not None and body.expires_on < as_of:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Credits cannot expire before the balance date."
        )
    await session.execute(
        text(
            "INSERT INTO credit_balance (id, tenant_id, amount_usd, as_of, expires_on, note, "
            "entered_by) VALUES (:id, :t, :amount, :as_of, :expires, :note, :by)"
        ),
        {
            "id": uuid7(),
            "t": access.tenant_id,
            "amount": round(body.amount_usd, 2),
            "as_of": as_of,
            "expires": body.expires_on,
            "note": body.note,
            "by": access.user.id,
        },
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="credits.balance_set",
        target_type="tenant",
        target_id=str(access.tenant_id),
        after={
            "amountUsd": round(body.amount_usd, 2),
            "asOf": as_of.isoformat(),
            "expiresOn": body.expires_on.isoformat() if body.expires_on else None,
        },
    )
    return _out(await service.runway(session, today))
