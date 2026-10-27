"""Exchange rates: the workspace's current rate, history, and staff rate entry (docs/09 §3.2)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import Field, model_validator
from sqlalchemy import text

from cloudoptics.audit.log import Actor
from cloudoptics.fx import service
from cloudoptics.identity.deps import Session, User, ViewerAccess
from cloudoptics.platform.api import ApiModel
from cloudoptics.platform.settings import get_settings

router = APIRouter(prefix="/v1", tags=["fx"])

CURRENCY = Field(pattern=r"^[A-Z]{3}$")


class Quote(ApiModel):
    currency: str
    rate: float
    rate_date: date
    basis: Literal["official", "market"]
    source: Literal["manual", "feed"]
    estimated: bool


class CurrentRate(ApiModel):
    """``quote`` is null when no usable rate exists (show USD only)."""

    currency: str
    basis: Literal["official", "market"]
    quote: Quote | None


class RateEntry(ApiModel):
    currency: str
    rate_date: date
    basis: Literal["official", "market"]
    rate: float
    source: Literal["manual", "feed"]
    source_note: str | None
    reason: str | None
    entered_by: str | None
    created_at: datetime


class RateCreate(ApiModel):
    currency: str = CURRENCY
    rate: Decimal = Field(gt=0, max_digits=18, decimal_places=8)
    rate_date: date | None = None
    basis: Literal["official", "market"] = "official"
    source_note: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=500)
    confirm: bool = False

    @model_validator(mode="after")
    def _checks(self) -> RateCreate:
        if self.currency == "USD":
            raise ValueError("rates are per 1 USD; USD itself is always 1")
        today = datetime.now(UTC).date()
        if self.rate_date and self.rate_date < today and not (self.reason or "").strip():
            raise ValueError("a back-dated rate needs a reason")
        return self


async def staff_user(user: User, request: Request) -> Actor:
    """Cloudoptics staff only (``CLOUDOPTICS_PLATFORM_ADMIN_EMAILS``), with the email confirmed:
    anyone can sign up with an address they do not own."""
    if not user.email_verified or user.email.lower() not in get_settings().platform_admins:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cloudoptics staff only")
    return Actor(
        type="staff",
        id=user.id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


Staff = Annotated[Actor, Depends(staff_user)]


@router.get("/tenants/{tenant_id}/fx/current", response_model=CurrentRate)
async def current_rate(
    access: ViewerAccess, session: Session, day: date | None = None
) -> CurrentRate:
    """The rate for the workspace's display currency and basis, for ``day`` (default today)."""
    tenant = (
        await session.execute(
            text("SELECT display_currency, fx_rate_basis FROM tenant WHERE id = :t"),
            {"t": access.tenant_id},
        )
    ).one()
    found = await service.quote(
        session, tenant.display_currency, day or datetime.now(UTC).date(), tenant.fx_rate_basis
    )
    return CurrentRate(
        currency=tenant.display_currency,
        basis=tenant.fx_rate_basis,
        quote=Quote.model_validate(found) if found else None,
    )


@router.get("/fx/rates", response_model=list[RateEntry])
async def rate_history(
    user: User,
    session: Session,
    currency: Annotated[str, Query(pattern=r"^[A-Z]{3}$")],
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
) -> list[RateEntry]:
    """Manual and feed rates for one currency (default: the last 12 months)."""
    end = end or datetime.now(UTC).date()
    start = start or end - timedelta(days=365)
    rows = await service.history(session, currency, start, end)
    return [RateEntry.model_validate(r) for r in rows]


@router.post("/admin/fx/rates", response_model=Quote, status_code=status.HTTP_201_CREATED)
async def set_rate(body: RateCreate, actor: Staff, session: Session) -> Quote:
    """Set a manual rate (staff). A move over 5% is refused with 409 until ``confirm``."""
    try:
        result = await service.set_rate(
            session,
            actor=actor,
            currency=body.currency,
            rate=body.rate,
            rate_date=body.rate_date or datetime.now(UTC).date(),
            basis=body.basis,
            source_note=body.source_note,
            reason=body.reason,
            confirm=body.confirm,
        )
    except service.LargeRateChangeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return Quote.model_validate(result)
