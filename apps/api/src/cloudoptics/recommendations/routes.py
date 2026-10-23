"""/v1/tenants/{t}/recommendations: what the engine found, best first (docs/09 §3.3)."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import Field, model_validator
from sqlalchemy import text

from cloudoptics.analysis.settings import (
    DEFAULT_IGNORE_TAG,
    RULE_IDS,
    RuleSettings,
    load_rule_settings,
)
from cloudoptics.audit import log as audit
from cloudoptics.identity.deps import AdminAccess, MemberAccess, Session, ViewerAccess
from cloudoptics.metrics.puller import usage
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

router = APIRouter(prefix="/v1/tenants/{tenant_id}/recommendations", tags=["recommendations"])

OPEN = ["new", "assigned", "in_progress"]
# Literal statements only; request values are always bound parameters.
LIST_RECOMMENDATIONS = text(
    "SELECT id, type, rule_id, account_id, region_id, resource_id, title, current_config, "
    "recommended_config, cost_now_usd_month, cost_after_usd_month, savings_usd_month, "
    "savings_basis, effort, reversibility, restart_required, risk_score, risk_level, "
    "risk_factors, confidence, confidence_notes, priority_score, evidence, fix, primary_of, "
    "status, assignee_id, first_detected_at, last_evaluated_at FROM recommendation "
    "WHERE (NOT :open_only OR status = ANY(:open)) "
    "AND (CAST(:rule AS text) IS NULL OR rule_id = :rule) "
    "AND (CAST(:account AS text) IS NULL OR account_id = :account) "
    "AND (CAST(:risk AS text) IS NULL OR risk_level = :risk) "
    "AND (CAST(:assignee AS uuid) IS NULL OR assignee_id = :assignee) "
    "AND (:alternatives OR primary_of IS NULL) "
    "ORDER BY priority_score DESC, savings_usd_month DESC, id LIMIT 500"
)
GET_RECOMMENDATION = text(
    "SELECT id, type, rule_id, account_id, region_id, resource_id, title, current_config, "
    "recommended_config, cost_now_usd_month, cost_after_usd_month, savings_usd_month, "
    "savings_basis, effort, reversibility, restart_required, risk_score, risk_level, "
    "risk_factors, confidence, confidence_notes, priority_score, evidence, fix, primary_of, "
    "status, assignee_id, first_detected_at, last_evaluated_at FROM recommendation "
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
    assignee_id: uuid.UUID | None
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
    risk_level: Annotated[Literal["low", "medium", "high"] | None, Query(alias="risk")] = None,
    assignee: Annotated[str | None, Query(description="A user ID, or 'me'")] = None,
    alternatives: bool = False,
) -> list[Recommendation]:
    assignee_id: uuid.UUID | None = None
    if assignee == "me":
        assignee_id = access.user.id
    elif assignee:
        try:
            assignee_id = uuid.UUID(assignee)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "invalid_assignee") from exc
    rows = await session.execute(
        LIST_RECOMMENDATIONS,
        {
            "open_only": state == "open",
            "open": OPEN,
            "rule": rule_id,
            "account": account_id,
            "risk": risk_level,
            "assignee": assignee_id,
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


class RecommendationChange(ApiModel):
    """Members move a recommendation through its lifecycle (PRD FR-REC-07) and assign it.

    ``done`` means the fix was applied; the next analysis run confirms it. Dismissing needs
    a reason, which is kept for the audit trail and for tuning the rules. Assigning a new
    recommendation makes it ``assigned``; ``assigneeId: null`` unassigns it.
    """

    status: Literal["new", "in_progress", "done", "dismissed"] | None = None
    reason: str | None = Field(default=None, max_length=500)
    assignee_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check(self) -> RecommendationChange:
        if self.status is None and "assignee_id" not in self.model_fields_set:
            raise ValueError("Change the status, the assignee or both.")
        if self.status == "dismissed" and not (self.reason or "").strip():
            raise ValueError("A reason is required to dismiss a recommendation.")
        return self


async def _apply(
    session: Any,
    request: Request,
    access: Any,
    recommendation_id: uuid.UUID,
    change: RecommendationChange,
) -> bool:
    """Apply one change with its history and audit entries; False when not found."""
    row = (
        await session.execute(
            text("SELECT status, assignee_id FROM recommendation WHERE id = :id FOR UPDATE"),
            {"id": recommendation_id},
        )
    ).one_or_none()
    if row is None:
        return False
    actor = user_actor(request, access.user.id)
    new_status = change.status
    if "assignee_id" in change.model_fields_set and change.assignee_id != row.assignee_id:
        await session.execute(
            text("UPDATE recommendation SET assignee_id = :a WHERE id = :id"),
            {"a": change.assignee_id, "id": recommendation_id},
        )
        await _event(session, access, recommendation_id, "assigned", assignee=change.assignee_id)
        await audit.record(
            session,
            tenant_id=access.tenant_id,
            actor=actor,
            action="recommendation.assigned",
            target_type="recommendation",
            target_id=str(recommendation_id),
            before={"assigneeId": str(row.assignee_id) if row.assignee_id else None},
            after={"assigneeId": str(change.assignee_id) if change.assignee_id else None},
        )
        if new_status is None and change.assignee_id and row.status == "new":
            new_status = "assigned"  # type: ignore[assignment]
        if new_status is None and change.assignee_id is None and row.status == "assigned":
            new_status = "new"
    if new_status is not None and new_status != row.status:
        await session.execute(
            text("UPDATE recommendation SET status = :s WHERE id = :id"),
            {"s": new_status, "id": recommendation_id},
        )
        await _event(
            session,
            access,
            recommendation_id,
            "status_changed",
            from_status=row.status,
            to_status=new_status,
            body=change.reason,
        )
        await audit.record(
            session,
            tenant_id=access.tenant_id,
            actor=actor,
            action="recommendation.status_changed",
            target_type="recommendation",
            target_id=str(recommendation_id),
            before={"status": row.status},
            after={"status": new_status, "reason": change.reason},
        )
    return True


async def _event(
    session: Any,
    access: Any,
    recommendation_id: uuid.UUID,
    kind: str,
    *,
    from_status: str | None = None,
    to_status: str | None = None,
    assignee: uuid.UUID | None = None,
    body: str | None = None,
) -> uuid.UUID:
    event_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO recommendation_event (id, tenant_id, recommendation_id, actor_id, kind, "
            "from_status, to_status, assignee_id, body) VALUES (:id, :t, :r, :u, :kind, :f, "
            ":to, :a, :body)"
        ),
        {
            "id": event_id,
            "t": access.tenant_id,
            "r": recommendation_id,
            "u": access.user.id,
            "kind": kind,
            "f": from_status,
            "to": to_status,
            "a": assignee,
            "body": body,
        },
    )
    return event_id


async def _check_assignee(session: Any, access: Any, change: RecommendationChange) -> None:
    if change.assignee_id is None:
        return
    member = (
        await session.execute(
            text("SELECT 1 FROM membership WHERE tenant_id = :t AND user_id = :u"),
            {"t": access.tenant_id, "u": change.assignee_id},
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "assignee_not_a_member")


@router.patch("/{recommendation_id}", response_model=Recommendation)
async def change_recommendation(
    recommendation_id: uuid.UUID,
    body: RecommendationChange,
    request: Request,
    access: MemberAccess,
    session: Session,
) -> Recommendation:
    await _check_assignee(session, access, body)
    if not await _apply(session, request, access, recommendation_id, body):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recommendation_not_found")
    return await get_recommendation(recommendation_id, access, session)


class BulkChange(RecommendationChange):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class BulkResult(ApiModel):
    updated: int


@router.post("/bulk", response_model=BulkResult)
async def change_many(
    body: BulkChange, request: Request, access: MemberAccess, session: Session
) -> BulkResult:
    """The same change for up to 200 recommendations at once (PRD US-3.4)."""
    await _check_assignee(session, access, body)
    change = RecommendationChange.model_validate(
        body.model_dump(include=body.model_fields_set - {"ids"})
    )
    updated = 0
    for rid in dict.fromkeys(body.ids):
        updated += await _apply(session, request, access, rid, change)
    return BulkResult(updated=updated)


class Comment(ApiModel):
    body: str = Field(min_length=1, max_length=2000)


class RecommendationEvent(ApiModel):
    id: uuid.UUID
    kind: Literal["status_changed", "assigned", "commented"]
    actor_id: uuid.UUID | None
    actor_name: str | None
    from_status: str | None
    to_status: str | None
    assignee_id: uuid.UUID | None
    assignee_name: str | None
    body: str | None
    created_at: datetime


EVENTS = text(
    "SELECT e.id, e.kind, e.actor_id, coalesce(a.display_name, a.email::text) AS actor_name, "
    "e.from_status, e.to_status, e.assignee_id, "
    "coalesce(s.display_name, s.email::text) AS assignee_name, e.body, e.created_at "
    "FROM recommendation_event e LEFT JOIN app_user a ON a.id = e.actor_id "
    "LEFT JOIN app_user s ON s.id = e.assignee_id WHERE e.recommendation_id = :r "
    "ORDER BY e.created_at, e.id"
)


@router.get("/{recommendation_id}/events", response_model=list[RecommendationEvent])
async def list_events(
    recommendation_id: uuid.UUID, access: ViewerAccess, session: Session
) -> list[RecommendationEvent]:
    """The history: who changed, assigned or commented on it, oldest first."""
    await get_recommendation(recommendation_id, access, session)
    rows = await session.execute(EVENTS, {"r": recommendation_id})
    return [RecommendationEvent.model_validate(r._mapping) for r in rows]


@router.post(
    "/{recommendation_id}/comments",
    response_model=list[RecommendationEvent],
    status_code=status.HTTP_201_CREATED,
)
async def add_comment(
    recommendation_id: uuid.UUID, body: Comment, access: MemberAccess, session: Session
) -> list[RecommendationEvent]:
    await get_recommendation(recommendation_id, access, session)
    await _event(session, access, recommendation_id, "commented", body=body.body.strip())
    return await list_events(recommendation_id, access, session)


class UsageDay(ApiModel):
    day: date
    average: float | None
    maximum: float | None


class UsageSeries(ApiModel):
    metric: str
    days: list[UsageDay]


@router.get("/{recommendation_id}/usage", response_model=list[UsageSeries])
async def recommendation_usage(
    recommendation_id: uuid.UUID, access: ViewerAccess, session: Session
) -> list[UsageSeries]:
    """Daily CPU, network or connection use of the resource, for the evidence charts.
    Empty for resources without usage data (volumes, addresses, load balancers)."""
    resource = (
        await session.execute(
            text("SELECT resource_ref FROM recommendation WHERE id = :id"),
            {"id": recommendation_id},
        )
    ).one_or_none()
    if resource is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "recommendation_not_found")
    if resource.resource_ref is None:
        return []
    return [
        UsageSeries(
            metric=s.metric,
            days=[UsageDay(day=d, average=avg, maximum=peak) for d, avg, peak in s.days],
        )
        for s in await usage(session, resource.resource_ref)
    ]


settings_router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/rule-settings", tags=["recommendations"]
)


class RuleSettingsBody(ApiModel):
    """What the rules look for (PRD FR-REC-08). Saving re-runs the rules straight away."""

    disabled_rules: list[str] = Field(default_factory=list, max_length=len(RULE_IDS))
    ignore_tag: str = Field(
        default=DEFAULT_IGNORE_TAG, min_length=1, max_length=128, pattern=r"^[\w.:/=+\-@ ]+$"
    )
    idle_cpu_percent: float = Field(default=5.0, ge=1, le=20)
    idle_days: int = Field(default=14, ge=7, le=30)

    @model_validator(mode="after")
    def _known_rules(self) -> RuleSettingsBody:
        unknown = sorted(set(self.disabled_rules) - set(RULE_IDS))
        if unknown:
            raise ValueError(f"Unknown rules: {', '.join(unknown)}")
        return self


class RuleSettingsView(ApiModel):
    rules: list[str]
    settings: RuleSettingsBody


@settings_router.get("", response_model=RuleSettingsView)
async def get_rule_settings(access: ViewerAccess, session: Session) -> RuleSettingsView:
    current = await load_rule_settings(session)
    return RuleSettingsView(
        rules=RULE_IDS, settings=RuleSettingsBody.model_validate(current.to_json())
    )


@settings_router.put("", response_model=RuleSettingsView)
async def put_rule_settings(
    body: RuleSettingsBody, request: Request, access: AdminAccess, session: Session
) -> RuleSettingsView:
    before = (await load_rule_settings(session)).to_json()
    after = RuleSettings.from_json(body.model_dump(by_alias=True)).to_json()
    await session.execute(
        text(
            "INSERT INTO rule_setting (tenant_id, settings, updated_by) VALUES (:t, "
            "CAST(:s AS jsonb), :u) ON CONFLICT (tenant_id) DO UPDATE SET "
            "settings = EXCLUDED.settings, updated_by = EXCLUDED.updated_by, updated_at = now()"
        ),
        {"t": access.tenant_id, "s": json.dumps(after), "u": access.user.id},
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="rule_settings.updated",
        target_type="tenant",
        target_id=str(access.tenant_id),
        before=before,
        after=after,
    )
    saved = RuleSettings.from_json(after)
    if get_settings().jobs_mode == "inline":
        from cloudoptics.analysis.engine import run_analysis

        # Its own transaction cannot see this one's row yet, so it gets the settings.
        await run_analysis(access.tenant_id, settings=saved)
    else:
        from cloudoptics.analysis.jobs import defer_analysis
        from cloudoptics.platform.jobs import get_job_app

        await defer_analysis(get_job_app(), str(access.tenant_id))
    return RuleSettingsView(rules=RULE_IDS, settings=RuleSettingsBody.model_validate(after))
