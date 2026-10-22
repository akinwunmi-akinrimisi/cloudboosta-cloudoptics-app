"""Append-only, hash-chained audit log (docs/09 §1.5, threat T6).

Each tenant (and the platform, tenant ``NULL``) has its own chain:
``row_hash = sha256(prev_hash || canonical_json(record))``. A transaction-scoped advisory
lock serialises appends per chain, so concurrent writers cannot fork it. The app role has
INSERT and SELECT only, so rows cannot be edited; ``verify_chain`` detects tampering by a
privileged user.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ActorType = Literal["user", "system", "staff"]


@dataclass(frozen=True)
class Actor:
    type: ActorType
    id: uuid.UUID | None = None
    ip: str | None = None
    user_agent: str | None = None


def _canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()


def _row_hash(prev_hash: bytes | None, record: dict[str, Any]) -> bytes:
    return hashlib.sha256((prev_hash or b"") + _canonical(record)).digest()


async def record(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    actor: Actor,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    chain = str(tenant_id) if tenant_id else "platform"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"audit:{chain}"}
    )
    prev = (
        await session.execute(
            text(
                "SELECT row_hash FROM audit_log WHERE tenant_id IS NOT DISTINCT FROM :t "
                "ORDER BY id DESC LIMIT 1"
            ),
            {"t": tenant_id},
        )
    ).scalar_one_or_none()
    occurred_at = (await session.execute(text("SELECT now()"))).scalar_one()
    rec = {
        "tenant_id": str(tenant_id) if tenant_id else None,
        "actor_type": actor.type,
        "actor_id": str(actor.id) if actor.id else None,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "before": before,
        "after": after,
        "occurred_at": occurred_at.astimezone(UTC).isoformat(),
    }
    await session.execute(
        text(
            "INSERT INTO audit_log (tenant_id, actor_type, actor_id, action, target_type, "
            "target_id, before, after, ip, user_agent, occurred_at, prev_hash, row_hash) "
            "VALUES (:tenant_id, :actor_type, :actor_id, :action, :target_type, :target_id, "
            "CAST(:before AS jsonb), CAST(:after AS jsonb), CAST(:ip AS inet), :ua, "
            ":occurred_at, :prev_hash, :row_hash)"
        ),
        {
            "tenant_id": tenant_id,
            "actor_type": actor.type,
            "actor_id": actor.id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "before": json.dumps(before, default=str) if before is not None else None,
            "after": json.dumps(after, default=str) if after is not None else None,
            "ip": actor.ip,
            "ua": actor.user_agent,
            "occurred_at": occurred_at,
            "prev_hash": prev,
            "row_hash": _row_hash(prev, rec),
        },
    )


@dataclass(frozen=True)
class AuditEntry:
    id: int
    actor_type: str
    actor_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    occurred_at: datetime


async def list_entries(
    session: AsyncSession, tenant_id: uuid.UUID, limit: int = 50
) -> list[AuditEntry]:
    rows = await session.execute(
        text(
            "SELECT id, actor_type, actor_id, action, target_type, target_id, before, after, "
            "occurred_at FROM audit_log WHERE tenant_id = :t ORDER BY id DESC LIMIT :n"
        ),
        {"t": tenant_id, "n": limit},
    )
    return [AuditEntry(**r._mapping) for r in rows]


async def verify_chain(session: AsyncSession, tenant_id: uuid.UUID | None) -> bool:
    """Recompute every hash in a chain; False if any row was altered, removed or reordered."""
    rows = await session.execute(
        text(
            "SELECT tenant_id, actor_type, actor_id, action, target_type, target_id, before, "
            "after, occurred_at, prev_hash, row_hash FROM audit_log "
            "WHERE tenant_id IS NOT DISTINCT FROM :t ORDER BY id"
        ),
        {"t": tenant_id},
    )
    prev: bytes | None = None
    for r in rows:
        if (bytes(r.prev_hash) if r.prev_hash is not None else None) != prev:
            return False
        rec = {
            "tenant_id": str(r.tenant_id) if r.tenant_id else None,
            "actor_type": r.actor_type,
            "actor_id": str(r.actor_id) if r.actor_id else None,
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "before": r.before,
            "after": r.after,
            "occurred_at": r.occurred_at.astimezone(UTC).isoformat(),
        }
        if _row_hash(prev, rec) != bytes(r.row_hash):
            return False
        prev = bytes(r.row_hash)
    return True
