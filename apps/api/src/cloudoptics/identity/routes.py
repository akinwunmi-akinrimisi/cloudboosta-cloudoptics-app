"""/v1/me, /v1/tenants and members (docs/09 §3.2 "Auth & session", "Tenants")."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal
from zoneinfo import available_timezones

from fastapi import APIRouter, Query, Request, status
from pydantic import EmailStr, Field, field_validator
from sqlalchemy import text

from cloudoptics.audit import log as audit
from cloudoptics.identity.deps import AdminAccess, Role, Session, User, ViewerAccess
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.db import scope
from cloudoptics.platform.ids import uuid7

router = APIRouter(prefix="/v1", tags=["identity"])

CurrencyView = Literal["usd", "local", "both"]

# Defaults when a workspace is created; users can change them (PRD Epic E3).
COUNTRY_DEFAULTS: dict[str, tuple[str, str]] = {
    "NG": ("NGN", "Africa/Lagos"),
    "KE": ("KES", "Africa/Nairobi"),
    "GH": ("GHS", "Africa/Accra"),
    "ZA": ("ZAR", "Africa/Johannesburg"),
    "EG": ("EGP", "Africa/Cairo"),
    "RW": ("RWF", "Africa/Kigali"),
    "UG": ("UGX", "Africa/Kampala"),
    "TZ": ("TZS", "Africa/Dar_es_Salaam"),
    "GB": ("GBP", "Europe/London"),
}
SUPPORTED_CURRENCIES = {c for c, _ in COUNTRY_DEFAULTS.values()} | {"USD", "EUR"}


class Membership(ApiModel):
    tenant_id: uuid.UUID
    tenant_name: str
    role: Role


class Me(ApiModel):
    id: uuid.UUID
    email: EmailStr
    display_name: str | None
    preferred_currency_view: CurrencyView
    memberships: list[Membership]


class MePatch(ApiModel):
    preferred_currency_view: CurrencyView


def _check_timezone(value: str | None) -> str | None:
    if value is not None and value not in available_timezones():
        raise ValueError("unknown IANA time zone")
    return value


def _check_currency(value: str | None) -> str | None:
    if value is not None and value not in SUPPORTED_CURRENCIES:
        raise ValueError(f"currency must be one of {sorted(SUPPORTED_CURRENCIES)}")
    return value


class TenantCreate(ApiModel):
    name: str = Field(min_length=1, max_length=120)
    country_code: str = Field(pattern=r"^[A-Z]{2}$")
    display_currency: str | None = None
    time_zone: str | None = None

    _tz = field_validator("time_zone")(_check_timezone)
    _ccy = field_validator("display_currency")(_check_currency)


class TenantPatch(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    display_currency: str | None = None
    time_zone: str | None = None
    mfa_required: bool | None = None

    _tz = field_validator("time_zone")(_check_timezone)
    _ccy = field_validator("display_currency")(_check_currency)


class Tenant(ApiModel):
    id: uuid.UUID
    name: str
    country_code: str
    display_currency: str
    time_zone: str
    mfa_required: bool
    status: str
    created_at: datetime
    role: Role


class Member(ApiModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: Role
    joined_at: datetime


class AuditItem(ApiModel):
    id: int
    actor_type: str
    actor_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: str | None
    before: dict[str, object] | None
    after: dict[str, object] | None
    occurred_at: datetime


# Literal statements only; request values are always bound parameters.
SELECT_TENANT = text(
    "SELECT id, name, country_code, display_currency, time_zone, mfa_required, status, "
    "created_at FROM tenant WHERE id = :t"
)
INSERT_TENANT = text(
    "INSERT INTO tenant (id, name, country_code, display_currency, time_zone) "
    "VALUES (:id, :name, :cc, :ccy, :tz) RETURNING id, name, country_code, display_currency, "
    "time_zone, mfa_required, status, created_at"
)
UPDATE_TENANT = text(
    "UPDATE tenant SET name = COALESCE(:name, name), "
    "display_currency = COALESCE(:display_currency, display_currency), "
    "time_zone = COALESCE(:time_zone, time_zone), "
    "mfa_required = COALESCE(:mfa_required, mfa_required) WHERE id = :t"
)


@router.get("/me", response_model=Me, response_model_by_alias=True)
async def get_me(user: User, session: Session) -> Me:
    pref = (
        await session.execute(
            text("SELECT preferred_currency_view FROM app_user WHERE id = :u"), {"u": user.id}
        )
    ).scalar_one()
    rows = await session.execute(
        text(
            "SELECT m.tenant_id, t.name AS tenant_name, m.role FROM membership m "
            "JOIN tenant t ON t.id = m.tenant_id WHERE m.user_id = :u ORDER BY t.name"
        ),
        {"u": user.id},
    )
    return Me(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        preferred_currency_view=pref,
        memberships=[Membership.model_validate(r._mapping) for r in rows],
    )


@router.patch("/me", response_model=Me, response_model_by_alias=True)
async def patch_me(body: MePatch, user: User, session: Session) -> Me:
    await session.execute(
        text("UPDATE app_user SET preferred_currency_view = :v WHERE id = :u"),
        {"v": body.preferred_currency_view, "u": user.id},
    )
    return await get_me(user, session)


@router.post(
    "/tenants",
    response_model=Tenant,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_tenant(
    body: TenantCreate, request: Request, user: User, session: Session
) -> Tenant:
    currency, tz = COUNTRY_DEFAULTS.get(body.country_code, ("USD", "UTC"))
    tenant_id = uuid7()
    await scope(session, user_id=user.id, tenant_id=tenant_id)
    row = (
        await session.execute(
            INSERT_TENANT,
            {
                "id": tenant_id,
                "name": body.name.strip(),
                "cc": body.country_code,
                "ccy": body.display_currency or currency,
                "tz": body.time_zone or tz,
            },
        )
    ).one()
    await session.execute(
        text("INSERT INTO membership (tenant_id, user_id, role) VALUES (:t, :u, 'admin')"),
        {"t": tenant_id, "u": user.id},
    )
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor=user_actor(request, user.id),
        action="tenant.created",
        target_type="tenant",
        target_id=str(tenant_id),
        after={"name": row.name, "countryCode": row.country_code, "currency": row.display_currency},
    )
    return Tenant(**row._mapping, role="admin")


@router.get("/tenants/{tenant_id}", response_model=Tenant, response_model_by_alias=True)
async def get_tenant(access: ViewerAccess, session: Session) -> Tenant:
    row = (await session.execute(SELECT_TENANT, {"t": access.tenant_id})).one()
    return Tenant(**row._mapping, role=access.role)


@router.patch("/tenants/{tenant_id}", response_model=Tenant, response_model_by_alias=True)
async def patch_tenant(
    body: TenantPatch, request: Request, access: AdminAccess, session: Session
) -> Tenant:
    changes = body.model_dump(exclude_none=True)
    before = (await session.execute(SELECT_TENANT, {"t": access.tenant_id})).one()
    if changes:
        await session.execute(
            UPDATE_TENANT,
            {**body.model_dump(include=set(TenantPatch.model_fields)), "t": access.tenant_id},
        )
        await audit.record(
            session,
            tenant_id=access.tenant_id,
            actor=user_actor(request, access.user.id),
            action="tenant.updated",
            target_type="tenant",
            target_id=str(access.tenant_id),
            before={k: getattr(before, k) for k in changes},
            after=changes,
        )
    return await get_tenant(access, session)


@router.get(
    "/tenants/{tenant_id}/members", response_model=list[Member], response_model_by_alias=True
)
async def list_members(access: ViewerAccess, session: Session) -> list[Member]:
    rows = await session.execute(
        text(
            "SELECT u.id AS user_id, u.email, u.display_name, m.role, m.created_at AS joined_at "
            "FROM membership m JOIN app_user u ON u.id = m.user_id "
            "WHERE m.tenant_id = :t ORDER BY m.created_at"
        ),
        {"t": access.tenant_id},
    )
    return [Member.model_validate(r._mapping) for r in rows]


@router.get(
    "/tenants/{tenant_id}/audit-log", response_model=list[AuditItem], response_model_by_alias=True
)
async def audit_log(
    access: AdminAccess,
    session: Session,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditItem]:
    entries = await audit.list_entries(session, access.tenant_id, limit)
    return [AuditItem.model_validate(e) for e in entries]
