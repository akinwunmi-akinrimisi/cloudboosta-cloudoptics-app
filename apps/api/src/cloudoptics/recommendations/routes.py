"""/v1/tenants/{t}/recommendations: what the engine found, best first (docs/09 §3.3)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import Field, model_validator
from sqlalchemy import text

from cloudoptics.audit import log as audit
from cloudoptics.identity.deps import MemberAccess, Session, ViewerAccess
from cloudoptics.platform.api import ApiModel, user_actor

router = APIRouter(prefix="/v1/tenants/{tenant_id}/recommendations", tags=["recommendations"])

OPEN = ["new", "assigned", "in_progress"]
# Literal statements only; request values are always bound parameters.
LIST_RECOMMENDATIONS = text(
    "SELECT id, type, rule_id, account_id, region_id, resource_id, title, current_config, "
    "recommended_config, cost_now_usd_month, cost_after_usd_month, savings_usd_month, "
    "savings_basis, effort, reversibility, restart_required, risk_score, risk_level, "
    "risk_factors, confidence, confidence_notes, priority_score, evidence, fix, primary_of, "
    "status, first_detected_at, last_evaluated_at FROM recommendation "
    "WHERE (NOT :open_only OR status = ANY(:open)) "
    "AND (CAST(:rule AS text) IS NULL OR rule_id = :rule) "
    "AND (CAST(:account AS text) IS NULL OR account_id = :account) "
    "AND (:alternatives OR primary_of IS NULL) "
    "ORDER BY priority_score DESC, savings_usd_month DESC, id LIMIT 500"
)
GET_RECOMMENDATION = text(
    "SELECT id, type, rule_id, account_id, region_id, resource_id, title, current_config, "
    "recommended_config, cost_now_usd_month, cost_after_usd_month, savings_usd_month, "
    "savings_basis, effort, reversibility, restart_required, risk_score, risk_level, "
    "risk_factors, confidence, confidence_notes, priority_score, evidence, fix, primary_of, "
    "status, first_detected_at, last_evaluated_at FROM recommendation "
    "WHERE id = :id"
)


class Recommendation(ApiModel):
    id: uuid.UUID
    type: str
    rule_id: str
    account_id: str
    region_id: str
    resource_id: str
    title: str
    current_config: dict[str, Any]
    recommended_config: dict[str, Any]
    cost_now_usd_month: float
    cost_after_usd_month: float
    savings_usd_month: float
    savings_basis: Literal["effective_rate", "list_price"]
    effort: Literal["S", "M", "L"]
    reversibility: Literal["reversible", "with_snapshot", "irreversible"]
    restart_required: bool
    risk_score: int
    risk_level: Literal["low", "medium", "high"]
    risk_factors: list[dict[str, Any]]
    confidence: Literal["high", "medium", "low"]
    confidence_notes: list[str]
    priority_score: float
    evidence: list[dict[str, Any]]
    fix: dict[str, Any]
    primary_of: uuid.UUID | None
    status: str
    first_detected_at: datetime
    last_evaluated_at: datetime


class RuleTotal(ApiModel):
    rule_id: str
    count: int
    savings_usd_month: float


class RecommendationSummary(ApiModel):
    """Open recommendations; alternatives (``primaryOf`` set) are not counted twice."""

    open_count: int
    savings_usd_month: float
    by_rule: list[RuleTotal]
    last_evaluated_at: datetime | None


def _model(row: Any) -> Recommendation:
    data = dict(row._mapping)
    data["account_id"] = data["account_id"].strip()
    return Recommendation.model_validate(data)


@router.get("", response_model=list[Recommendation])
async def list_recommendations(
    access: ViewerAccess,
    session: Session,
    state: Annotated[Literal["open", "all"], Query(alias="status")] = "open",
    rule_id: Annotated[str | None, Query(alias="ruleId")] = None,
    account_id: Annotated[str | None, Query(alias="accountId")] = None,
    alternatives: bool = False,
) -> list[Recommendation]:
    rows = await session.execute(
        LIST_RECOMMENDATIONS,
        {
            "open_only": state == "open",
            "open": OPEN,
            "rule": rule_id,
            "account": account_id,
            "alternatives": alternatives,
        },
    )
    return [_model(r) for r in rows]


@router.get("/summary", response_model=RecommendationSummary)
async def recommendation_summary(access: ViewerAccess, session: Session) -> RecommendationSummary:
    rows = (
        await session.execute(
            text(
                "SELECT rule_id, count(*) AS n, sum(savings_usd_month) AS savings "
                "FROM recommendation WHERE status = ANY(:open) AND primary_of IS NULL "
                "GROUP BY rule_id ORDER BY sum(savings_usd_month) DESC"
            ),
            {"open": OPEN},
        )
    ).all()
    last = (
        await session.execute(text("SELECT max(last_evaluated_at) FROM recommendation"))
    ).scalar_one()
    return RecommendationSummary(
        open_count=sum(r.n for r in rows),
        savings_usd_month=round(sum(float(r.savings) for r in rows), 2),
        by_rule=[RuleTotal(rule_id=r.rule_id, count=r.n, savings_usd_month=float(r.savings))
                 for r in rows],
        last_evaluated_at=last,
    )  # fmt: skip


@router.get("/{recommendation_id}", response_model=Recommendation)
async def get_recommendation(
    recommendation_id: uuid.UUID, access: ViewerAccess, session: Session
) -> Recommendation:
    row = (await session.execute(GET_RECOMMENDATION, {"id": recommendation_id})).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recommendation_not_found")
    return _model(row)


class StatusChange(ApiModel):
    """Members move a recommendation through its lifecycle (PRD FR-REC-07).

    ``done`` means the fix was applied; the next analysis run confirms it. Dismissing needs
    a reason, which is kept for the audit trail and for tuning the rules.
    """

    status: Literal["new", "in_progress", "done", "dismissed"]
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _reason_for_dismissal(self) -> StatusChange:
        if self.status == "dismissed" and not (self.reason or "").strip():
            raise ValueError("A reason is required to dismiss a recommendation.")
        return self


@router.patch("/{recommendation_id}", response_model=Recommendation)
async def change_status(
    recommendation_id: uuid.UUID,
    body: StatusChange,
    request: Request,
    access: MemberAccess,
    session: Session,
) -> Recommendation:
    before = (
        await session.execute(
            text("SELECT status FROM recommendation WHERE id = :id FOR UPDATE"),
            {"id": recommendation_id},
        )
    ).scalar_one_or_none()
    if before is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recommendation_not_found")
    if before != body.status:
        await session.execute(
            text("UPDATE recommendation SET status = :s WHERE id = :id"),
            {"s": body.status, "id": recommendation_id},
        )
        await audit.record(
            session,
            tenant_id=access.tenant_id,
            actor=user_actor(request, access.user.id),
            action="recommendation.status_changed",
            target_type="recommendation",
            target_id=str(recommendation_id),
            before={"status": before},
            after={"status": body.status, "reason": body.reason},
        )
    return await get_recommendation(recommendation_id, access, session)
