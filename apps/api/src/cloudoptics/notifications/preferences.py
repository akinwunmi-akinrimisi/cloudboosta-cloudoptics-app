"""Notification preferences (PRD FR-NOT-03): which optional emails each member gets.

A member's own choice wins, then the workspace default an admin set, then on.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

Kind = Literal["weekly_digest", "anomaly_alert", "monthly_report"]
KINDS: tuple[Kind, ...] = ("weekly_digest", "anomaly_alert", "monthly_report")

SELECT_PREFERENCES = text(
    "SELECT user_id, kind, email FROM notification_preference WHERE user_id IS NULL OR user_id = :u"
)
UPSERT_PREFERENCE = text(
    "INSERT INTO notification_preference (tenant_id, user_id, kind, email, updated_by) "
    "VALUES (:t, :u, :kind, :email, :by) ON CONFLICT (tenant_id, coalesce(user_id, "
    "'00000000-0000-0000-0000-000000000000'::uuid), kind) DO UPDATE SET "
    "email = EXCLUDED.email, updated_by = EXCLUDED.updated_by, updated_at = now()"
)


@dataclass(frozen=True)
class Preferences:
    """Whether each kind is on: the member's effective choice and the workspace default."""

    mine: dict[Kind, bool]
    defaults: dict[Kind, bool]


async def load(session: AsyncSession, user_id: uuid.UUID | None) -> Preferences:
    """Inside a tenant-scoped transaction."""
    rows = (await session.execute(SELECT_PREFERENCES, {"u": user_id})).all()
    defaults: dict[Kind, bool] = {k: True for k in KINDS}
    own: dict[Kind, bool] = {}
    for r in rows:
        if r.user_id is None:
            defaults[r.kind] = r.email
        else:
            own[r.kind] = r.email
    return Preferences(mine={k: own.get(k, defaults[k]) for k in KINDS}, defaults=defaults)


async def save(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID | None,
    choices: dict[Kind, bool],
    by: uuid.UUID,
) -> None:
    """Store a member's choices (``user_id``) or the workspace defaults (``None``)."""
    for kind, email in choices.items():
        await session.execute(
            UPSERT_PREFERENCE,
            {"t": tenant_id, "u": user_id, "kind": kind, "email": email, "by": by},
        )


@dataclass(frozen=True)
class Recipient:
    user_id: uuid.UUID
    email: str
    name: str | None


async def recipients(
    session: AsyncSession, tenant_id: uuid.UUID, kind: Kind, roles: Iterable[str]
) -> list[Recipient]:
    """Members with one of ``roles`` who get ``kind`` by email."""
    rows = await session.execute(
        text(
            "SELECT u.id, u.email, u.display_name FROM membership m "
            "JOIN app_user u ON u.id = m.user_id "
            "LEFT JOIN notification_preference own ON own.user_id = m.user_id "
            "AND own.kind = :kind "
            "LEFT JOIN notification_preference def ON def.user_id IS NULL AND def.kind = :kind "
            "WHERE m.tenant_id = :t AND m.role = ANY(:roles) "
            "AND coalesce(own.email, def.email, true) ORDER BY u.email"
        ),
        {"t": tenant_id, "kind": kind, "roles": list(roles)},
    )
    return [Recipient(r.id, r.email.lower(), r.display_name) for r in rows]
