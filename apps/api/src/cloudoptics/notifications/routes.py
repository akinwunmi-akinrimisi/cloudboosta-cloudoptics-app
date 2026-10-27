"""/v1/tenants/{t}/notification-preferences (PRD FR-NOT-03): optional emails, per member
with workspace defaults."""

from __future__ import annotations

from fastapi import APIRouter, Request

from cloudoptics.audit import log as audit
from cloudoptics.identity.deps import AdminAccess, Session, ViewerAccess
from cloudoptics.notifications import preferences
from cloudoptics.platform.api import ApiModel, user_actor

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/notification-preferences", tags=["notifications"]
)


class Choices(ApiModel):
    """Emails a member can turn off. Anomaly alerts go to admins and members only."""

    weekly_digest: bool | None = None
    anomaly_alert: bool | None = None
    monthly_report: bool | None = None

    def changes(self) -> dict[preferences.Kind, bool]:
        return {k: v for k, v in self.model_dump().items() if v is not None}  # type: ignore[misc]


class PreferencesView(ApiModel):
    mine: Choices
    defaults: Choices


def _view(prefs: preferences.Preferences) -> PreferencesView:
    return PreferencesView(
        mine=Choices.model_validate(prefs.mine), defaults=Choices.model_validate(prefs.defaults)
    )


@router.get("", response_model=PreferencesView)
async def get_preferences(access: ViewerAccess, session: Session) -> PreferencesView:
    return _view(await preferences.load(session, access.user.id))


@router.put("/me", response_model=PreferencesView)
async def put_mine(
    body: Choices, request: Request, access: ViewerAccess, session: Session
) -> PreferencesView:
    changes = body.changes()
    await preferences.save(session, access.tenant_id, access.user.id, changes, access.user.id)
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="notification_preferences.updated",
        target_type="user",
        target_id=str(access.user.id),
        after={str(k): v for k, v in changes.items()},
    )
    return _view(await preferences.load(session, access.user.id))


@router.put("/defaults", response_model=PreferencesView)
async def put_defaults(
    body: Choices, request: Request, access: AdminAccess, session: Session
) -> PreferencesView:
    """The workspace default for members who have not chosen themselves."""
    changes = body.changes()
    await preferences.save(session, access.tenant_id, None, changes, access.user.id)
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="notification_defaults.updated",
        target_type="tenant",
        target_id=str(access.tenant_id),
        after={str(k): v for k, v in changes.items()},
    )
    return _view(await preferences.load(session, access.user.id))
