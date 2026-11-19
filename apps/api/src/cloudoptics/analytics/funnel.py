"""Server funnel events (docs/14 §8.4): each step recorded once, the first time it happens.

``emit`` is called from the code path that makes the step happen (sign-in creating the user,
workspace creation, the connect link, validation, bill loading, the analysis). It never
fails the caller: the insert runs in a savepoint and a database error is only logged. A
repeat of a step is a no-op (``ON CONFLICT DO NOTHING`` on the once-per-workspace and
once-per-user indexes).

Only internal IDs are stored. ``workspace_created`` also stores the first-touch campaign the
app sends with the new workspace (``Attribution``), cleaned so that it cannot carry an email
address or control characters.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import Any, Literal

from pydantic import field_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.platform.api import ApiModel

log = logging.getLogger(__name__)

FunnelEvent = Literal[
    "signup_completed",
    "workspace_created",
    "connect_started",
    "connected",
    "first_costs",
    "first_recommendation",
]
# In funnel order.
STEPS: tuple[FunnelEvent, ...] = (
    "signup_completed",
    "workspace_created",
    "connect_started",
    "connected",
    "first_costs",
    "first_recommendation",
)

INSERT_EVENT = text(
    "INSERT INTO funnel_event (event, tenant_id, user_id, detail, source, medium, campaign, "
    "content, term, ref, landing_path) VALUES (:event, :t, :u, :detail, :source, :medium, "
    ":campaign, :content, :term, :ref, :landing_path) ON CONFLICT DO NOTHING"
)

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+")
_PATH = re.compile(r"^/[A-Za-z0-9\-._~/%]*$")


def clean_text(value: object, limit: int, *, lower: bool = False) -> str | None:
    """A campaign value fit to store: printable, trimmed, at most ``limit`` characters, and
    never something that looks like an email address (dropped, not masked)."""
    if not isinstance(value, str):
        return None
    value = " ".join(_CONTROL.sub(" ", value).split())[:limit].strip()
    if not value or _EMAIL.search(value):
        return None
    return value.lower() if lower else value


def clean_path(value: object, limit: int = 200) -> str | None:
    """A site path without query or fragment, or None if it does not look like one."""
    if not isinstance(value, str):
        return None
    value = value.split("?", 1)[0].split("#", 1)[0][:limit]
    return value if _PATH.match(value) and not _EMAIL.search(value) else None


class Attribution(ApiModel):
    """First-touch campaign of a new workspace, as the app captured it from the sign-in URL.

    Lenient on purpose: a bad value is dropped rather than refused, so a mangled campaign
    link never stops someone creating a workspace."""

    source: str | None = None
    medium: str | None = None
    campaign: str | None = None
    content: str | None = None
    term: str | None = None
    ref: str | None = None
    landing_path: str | None = None

    @field_validator("source", "medium", "ref", mode="before")
    @classmethod
    def _lower(cls, value: object) -> str | None:
        return clean_text(value, 100, lower=True)

    @field_validator("campaign", "content", "term", mode="before")
    @classmethod
    def _text(cls, value: object) -> str | None:
        return clean_text(value, 100)

    @field_validator("landing_path", mode="before")
    @classmethod
    def _path(cls, value: object) -> str | None:
        return clean_path(value)


async def emit(
    session: AsyncSession,
    event: FunnelEvent,
    *,
    tenant_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    detail: str | None = None,
    attribution: Attribution | None = None,
) -> None:
    """Record a funnel step in the caller's transaction (RLS scope already set)."""
    a = attribution or Attribution()
    params: dict[str, Any] = {
        "event": event,
        "t": tenant_id,
        "u": user_id,
        "detail": detail,
        "source": a.source,
        "medium": a.medium,
        "campaign": a.campaign,
        "content": a.content,
        "term": a.term,
        "ref": a.ref,
        "landing_path": a.landing_path,
    }
    try:
        async with session.begin_nested():
            await session.execute(INSERT_EVENT, params)
    except SQLAlchemyError as exc:
        log.warning("funnel event %s not recorded: %s", event, exc)
