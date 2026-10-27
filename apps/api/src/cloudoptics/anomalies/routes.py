"""/v1/tenants/{t}/anomalies and /alert-settings (PRD US-6.3, docs/09 §3.2)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import EmailStr, Field, model_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.anomalies import service
from cloudoptics.audit import log as audit
from cloudoptics.identity.deps import AdminAccess, MemberAccess, Session, ViewerAccess
from cloudoptics.platform.api import ApiModel, user_actor

router = APIRouter(prefix="/v1/tenants/{tenant_id}/anomalies", tags=["anomalies"])
settings_router = APIRouter(prefix="/v1/tenants/{tenant_id}/alert-settings", tags=["anomalies"])

# One query for both: a single anomaly (:id) or the list (:since, optional :status).
SELECT_ANOMALIES = text(
    "SELECT a.id, a.usage_date, a.account_id, coalesce(c.name, a.account_id) AS account_label, "
    "a.service_code, a.service_name, a.actual_usd, a.expected_usd, a.impact_usd, "
    "a.contributors, a.status, a.suppressed_by, a.note, a.status_changed_at, a.detected_at, "
    "u.display_name AS changed_by_name, EXISTS (SELECT 1 FROM notification n "
    "WHERE n.dedupe_key = 'anomaly:' || CAST(a.id AS text) AND n.status = 'sent') AS alerted "
    "FROM anomaly a LEFT JOIN cloud_account c ON c.tenant_id = a.tenant_id "
    "AND c.aws_account_id = a.account_id LEFT JOIN app_user u ON u.id = a.status_changed_by "
    "WHERE (CAST(:id AS uuid) IS NULL OR a.id = CAST(:id AS uuid)) "
    "AND (CAST(:since AS date) IS NULL OR a.usage_date >= CAST(:since AS date)) "
    "AND (CAST(:status AS text) IS NULL OR a.status = CAST(:status AS text)) "
    "ORDER BY a.usage_date DESC, a.impact_usd DESC LIMIT 500"
)


class Contributor(ApiModel):
    usage_type: str
    region: str | None
    actual_usd: float
    expected_usd: float
    impact_usd: float


class Anomaly(ApiModel):
    """A day's spend for one account and service well above its baseline, in USD.
    ``deviationPercent`` is null when nothing was expected (new spend)."""

    id: uuid.UUID
    date: date
    account_id: str
    account_name: str
    service_code: str
    service_name: str
    actual_usd: float
    expected_usd: float
    impact_usd: float
    deviation_percent: float | None
    contributors: list[Contributor]
    status: service.Status
    suppressed: bool
    note: str | None
    status_changed_at: datetime | None
    status_changed_by: str | None
    detected_at: datetime
    alerted: bool


class Point(ApiModel):
    date: date
    actual: float
    expected: float | None
    upper: float | None


class AnomalyDetail(Anomaly):
    series: list[Point]


def _model(row: Any) -> dict[str, Any]:
    expected = float(row.expected_usd)
    impact = float(row.impact_usd)
    return {
        "id": row.id,
        "date": row.usage_date,
        "account_id": row.account_id.strip(),
        "account_name": row.account_label.strip(),
        "service_code": row.service_code,
        "service_name": row.service_name,
        "actual_usd": round(float(row.actual_usd), 2),
        "expected_usd": round(expected, 2),
        "impact_usd": round(impact, 2),
        "deviation_percent": None if expected < 0.005 else round(100 * impact / expected, 1),
        "contributors": [Contributor.model_validate(c) for c in row.contributors or []],
        "status": row.status,
        "suppressed": row.suppressed_by is not None,
        "note": row.note,
        "status_changed_at": row.status_changed_at,
        "status_changed_by": row.changed_by_name,
        "detected_at": row.detected_at,
        "alerted": row.alerted,
    }


async def _load(session: AsyncSession, anomaly_id: uuid.UUID) -> Any:
    row = (
        await session.execute(SELECT_ANOMALIES, {"id": anomaly_id, "since": None, "status": None})
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Anomaly not found")
    return row


async def _detail(session: AsyncSession, row: Any) -> AnomalyDetail:
    points = await service.series(session, row, await service.load_settings(session))
    return AnomalyDetail(
        **_model(row),
        series=[
            Point(date=p.date, actual=p.actual, expected=p.expected, upper=p.upper) for p in points
        ],
    )


@router.get("", response_model=list[Anomaly])
async def list_anomalies(
    access: ViewerAccess,
    session: Session,
    status_filter: Annotated[service.Status | None, Query(alias="status")] = None,
    days: Annotated[int, Query(ge=1, le=400)] = 90,
) -> list[Anomaly]:
    since = datetime.now(UTC).date() - timedelta(days=days)
    rows = await session.execute(
        SELECT_ANOMALIES, {"id": None, "since": since, "status": status_filter}
    )
    return [Anomaly(**_model(r)) for r in rows]


@router.get("/{anomaly_id}", response_model=AnomalyDetail)
async def get_anomaly(
    anomaly_id: uuid.UUID, access: ViewerAccess, session: Session
) -> AnomalyDetail:
    return await _detail(session, await _load(session, anomaly_id))


class AnomalyChange(ApiModel):
    status: service.Status
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _strip(self) -> AnomalyChange:
        if self.note is not None:
            self.note = self.note.strip() or None
        return self


@router.patch("/{anomaly_id}", response_model=AnomalyDetail)
async def change_anomaly(
    anomaly_id: uuid.UUID,
    body: AnomalyChange,
    request: Request,
    access: MemberAccess,
    session: Session,
) -> AnomalyDetail:
    """Open, Investigating, Expected or Resolved; Expected suppresses new anomalies for the
    same account and service for a week."""
    row = await _load(session, anomaly_id)
    await session.execute(
        text(
            "UPDATE anomaly SET status = :status, note = :note, status_changed_by = :by, "
            "status_changed_at = now(), updated_at = now() WHERE id = :id"
        ),
        {"id": anomaly_id, "status": body.status, "note": body.note, "by": access.user.id},
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="anomaly.status_changed",
        target_type="anomaly",
        target_id=str(anomaly_id),
        before={"status": row.status},
        after={"status": body.status, "note": body.note},
    )
    return await _detail(session, await _load(session, anomaly_id))


class AlertSettingsBody(ApiModel):
    """Anomaly thresholds: a day is flagged only when it is both ``anomalyMinPercent`` above
    its baseline and at least ``anomalyMinImpact`` (per day, in USD or the workspace
    currency) above it."""

    anomaly_min_impact: float = Field(default=service.DEFAULT_MIN_IMPACT, ge=1, le=1e9)
    anomaly_min_impact_currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    anomaly_min_percent: int = Field(default=service.DEFAULT_MIN_PERCENT, ge=10, le=500)
    anomaly_emails: list[EmailStr] = Field(default_factory=list, max_length=20)


class AlertSettingsView(ApiModel):
    settings: AlertSettingsBody
    #: The minimum impact in USD used now; null when the workspace currency has no rate yet
    #: (then the USD default applies).
    min_impact_usd: float | None
    defaults: AlertSettingsBody


async def _view(session: AsyncSession, current: service.AlertSettings) -> AlertSettingsView:
    minimum = await service.min_impact_usd(session, current, datetime.now(UTC).date())
    return AlertSettingsView(
        settings=AlertSettingsBody.model_validate(current.to_json()),
        min_impact_usd=minimum.usd if minimum.rate is not None else None,
        defaults=AlertSettingsBody(),
    )


@settings_router.get("", response_model=AlertSettingsView)
async def get_alert_settings(access: ViewerAccess, session: Session) -> AlertSettingsView:
    return await _view(session, await service.load_settings(session))


@settings_router.put("", response_model=AlertSettingsView)
async def put_alert_settings(
    body: AlertSettingsBody, request: Request, access: AdminAccess, session: Session
) -> AlertSettingsView:
    local = (
        await session.execute(
            text("SELECT display_currency FROM tenant WHERE id = :t"), {"t": access.tenant_id}
        )
    ).scalar_one()
    if body.anomaly_min_impact_currency not in ("USD", local.strip()):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"The minimum is in USD or the workspace currency ({local.strip()}).",
        )
    before = (await service.load_settings(session)).to_json()
    saved = service.AlertSettings.from_json(body.model_dump(by_alias=True, mode="json"))
    after = saved.to_json()
    await session.execute(
        text(
            "INSERT INTO alert_setting (tenant_id, settings, updated_by) VALUES (:t, "
            "CAST(:s AS jsonb), :u) ON CONFLICT (tenant_id) DO UPDATE SET "
            "settings = EXCLUDED.settings, updated_by = EXCLUDED.updated_by, updated_at = now()"
        ),
        {"t": access.tenant_id, "s": json.dumps(after), "u": access.user.id},
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="alert_settings.updated",
        target_type="tenant",
        target_id=str(access.tenant_id),
        before=before,
        after=after,
    )
    return await _view(session, saved)
