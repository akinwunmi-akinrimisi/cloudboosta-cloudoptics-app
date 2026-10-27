"""/v1/tenants/{t}/budgets (PRD US-6.1, docs/09 §3.2 "Budgets & alerts")."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import EmailStr, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.audit import log as audit
from cloudoptics.budgets import service
from cloudoptics.identity.deps import MemberAccess, Session, ViewerAccess
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.ids import uuid7

router = APIRouter(prefix="/v1/tenants/{tenant_id}/budgets", tags=["budgets"])

SELECT_BUDGETS = (
    "SELECT id, name, scope, amount, currency, thresholds, recipients, is_active, created_at "
    "FROM budget"
)


class Scope(ApiModel):
    """The whole workspace, or some of its AWS accounts."""

    type: Literal["tenant", "accounts"] = "tenant"
    accounts: list[str] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _accounts(self) -> Scope:
        if self.type == "tenant":
            self.accounts = []
        elif not self.accounts:
            raise ValueError("choose at least one account")
        if any(len(a) != 12 or not a.isdigit() for a in self.accounts):
            raise ValueError("account IDs have 12 digits")
        self.accounts = sorted(set(self.accounts))
        return self


class ThresholdIn(ApiModel):
    kind: Literal["actual", "forecast"]
    percent: int = Field(ge=1, le=1000)


class Recipients(ApiModel):
    users: list[uuid.UUID] = Field(default_factory=list, max_length=50)
    emails: list[EmailStr] = Field(default_factory=list, max_length=20)


def _thresholds_ok(thresholds: list[ThresholdIn]) -> list[ThresholdIn]:
    if len({(t.kind, t.percent) for t in thresholds}) != len(thresholds):
        raise ValueError("each threshold once")
    return sorted(thresholds, key=lambda t: (t.kind != "actual", t.percent))


class BudgetIn(ApiModel):
    name: str = Field(min_length=1, max_length=100)
    scope: Scope = Field(default_factory=Scope)
    amount: Decimal = Field(gt=0, le=Decimal("1e12"), decimal_places=2)
    currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    thresholds: list[ThresholdIn] = Field(
        default_factory=lambda: [ThresholdIn(**t) for t in service.DEFAULT_THRESHOLDS],
        min_length=1,
        max_length=10,
    )
    recipients: Recipients = Field(default_factory=Recipients)

    @model_validator(mode="after")
    def _check(self) -> BudgetIn:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("name is required")
        self.thresholds = _thresholds_ok(self.thresholds)
        return self


class BudgetPatch(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    scope: Scope | None = None
    amount: Decimal | None = Field(default=None, gt=0, le=Decimal("1e12"), decimal_places=2)
    currency: str | None = Field(default=None, pattern="^[A-Z]{3}$")
    thresholds: list[ThresholdIn] | None = Field(default=None, min_length=1, max_length=10)
    recipients: Recipients | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> BudgetPatch:
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("name is required")
        if self.thresholds is not None:
            self.thresholds = _thresholds_ok(self.thresholds)
        return self


class ThresholdOut(ApiModel):
    kind: Literal["actual", "forecast"]
    percent: int
    crossed: bool
    alerted: bool


class Budget(ApiModel):
    """A budget and how this month is going. Amounts in the budget's currency, with the USD
    figures they come from; the rate is the budget currency per USD (null: no usable rate,
    status ``no_rate``)."""

    id: uuid.UUID
    name: str
    scope: Scope
    amount: float
    currency: str
    recipients: Recipients
    is_active: bool
    created_at: datetime
    status: service.Status
    period: date
    data_through: date | None
    rate: float | None
    amount_usd: float | None
    actual: float | None
    actual_usd: float
    forecast: float | None
    forecast_usd: float | None
    actual_percent: float | None
    forecast_percent: float | None
    thresholds: list[ThresholdOut]


async def _out(session: AsyncSession, row: Any) -> Budget:
    ev = await service.evaluate(session, row, datetime.now(UTC).date())
    return Budget(
        id=row.id,
        name=row.name,
        scope=Scope.model_validate(row.scope),
        amount=float(row.amount),
        currency=row.currency.strip(),
        recipients=Recipients.model_validate(row.recipients),
        is_active=row.is_active,
        created_at=row.created_at,
        status=ev.status,
        period=ev.period,
        data_through=ev.data_through,
        rate=ev.rate,
        amount_usd=ev.amount_usd,
        actual=ev.in_budget_currency(ev.actual_usd),
        actual_usd=ev.actual_usd,
        forecast=ev.in_budget_currency(ev.forecast_usd),
        forecast_usd=ev.forecast_usd,
        actual_percent=ev.actual_percent,
        forecast_percent=ev.forecast_percent,
        thresholds=[
            ThresholdOut(kind=t.kind, percent=t.percent, crossed=t.crossed, alerted=t.alerted)
            for t in ev.thresholds
        ],
    )


async def _load(session: AsyncSession, budget_id: uuid.UUID) -> Any:
    row = (
        await session.execute(text(f"{SELECT_BUDGETS} WHERE id = :id"), {"id": budget_id})
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Budget not found")
    return row


async def _validate(
    session: AsyncSession, tenant_id: uuid.UUID, currency: str, scope: Scope, people: Recipients
) -> None:
    """Checks that need the workspace: its currency, accounts and members."""
    local = (
        await session.execute(
            text("SELECT display_currency FROM tenant WHERE id = :t"), {"t": tenant_id}
        )
    ).scalar_one()
    if currency not in ("USD", local.strip()):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"A budget is in USD or the workspace currency ({local.strip()}).",
        )
    if scope.accounts:
        known = {
            r.aws_account_id.strip()
            for r in await session.execute(text("SELECT aws_account_id FROM cloud_account"))
        }
        unknown = sorted(set(scope.accounts) - known)
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"Not an account in this workspace: {', '.join(unknown)}.",
            )
    if people.users:
        members = {
            r.user_id
            for r in await session.execute(
                text("SELECT user_id FROM membership WHERE tenant_id = :t"), {"t": tenant_id}
            )
        }
        if set(people.users) - members:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "Every recipient chosen by name must be a member of this workspace.",
            )


def _dump(model: Any) -> str:
    return json.dumps(model.model_dump(mode="json"))


@router.get("", response_model=list[Budget])
async def list_budgets(access: ViewerAccess, session: Session) -> list[Budget]:
    rows = await session.execute(text(f"{SELECT_BUDGETS} ORDER BY created_at"))
    return [await _out(session, r) for r in rows.all()]


@router.post("", response_model=Budget, status_code=status.HTTP_201_CREATED)
async def create_budget(
    body: BudgetIn, request: Request, access: MemberAccess, session: Session
) -> Budget:
    await _validate(session, access.tenant_id, body.currency, body.scope, body.recipients)
    budget_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO budget (id, tenant_id, name, scope, amount, currency, thresholds, "
            "recipients, created_by) VALUES (:id, :t, :name, CAST(:scope AS jsonb), :amount, "
            ":ccy, CAST(:thresholds AS jsonb), CAST(:recipients AS jsonb), :by)"
        ),
        {
            "id": budget_id,
            "t": access.tenant_id,
            "name": body.name,
            "scope": _dump(body.scope),
            "amount": body.amount,
            "ccy": body.currency,
            "thresholds": json.dumps([t.model_dump(mode="json") for t in body.thresholds]),
            "recipients": _dump(body.recipients),
            "by": access.user.id,
        },
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="budget.created",
        target_type="budget",
        target_id=str(budget_id),
        after={"name": body.name, "amount": str(body.amount), "currency": body.currency},
    )
    return await _out(session, await _load(session, budget_id))


@router.get("/{budget_id}", response_model=Budget)
async def get_budget(budget_id: uuid.UUID, access: ViewerAccess, session: Session) -> Budget:
    return await _out(session, await _load(session, budget_id))


@router.patch("/{budget_id}", response_model=Budget)
async def update_budget(
    budget_id: uuid.UUID,
    body: BudgetPatch,
    request: Request,
    access: MemberAccess,
    session: Session,
) -> Budget:
    row = await _load(session, budget_id)
    scope = body.scope or Scope.model_validate(row.scope)
    people = body.recipients or Recipients.model_validate(row.recipients)
    currency = body.currency or row.currency.strip()
    await _validate(session, access.tenant_id, currency, scope, people)
    changes = body.model_dump(exclude_unset=True, mode="json")
    await session.execute(
        text(
            "UPDATE budget SET name = :name, scope = CAST(:scope AS jsonb), amount = :amount, "
            "currency = :ccy, thresholds = CAST(:thresholds AS jsonb), "
            "recipients = CAST(:recipients AS jsonb), is_active = :active, updated_at = now() "
            "WHERE id = :id"
        ),
        {
            "id": budget_id,
            "name": (body.name or row.name).strip(),
            "scope": _dump(scope),
            "amount": body.amount if body.amount is not None else row.amount,
            "ccy": currency,
            "thresholds": json.dumps(
                [t.model_dump(mode="json") for t in body.thresholds]
                if body.thresholds is not None
                else row.thresholds
            ),
            "recipients": _dump(people),
            "active": row.is_active if body.is_active is None else body.is_active,
        },
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="budget.updated",
        target_type="budget",
        target_id=str(budget_id),
        after=changes,
    )
    return await _out(session, await _load(session, budget_id))


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_budget(
    budget_id: uuid.UUID, request: Request, access: MemberAccess, session: Session
) -> Response:
    row = await _load(session, budget_id)
    await session.execute(text("DELETE FROM budget WHERE id = :id"), {"id": budget_id})
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="budget.deleted",
        target_type="budget",
        target_id=str(budget_id),
        before={"name": row.name},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
