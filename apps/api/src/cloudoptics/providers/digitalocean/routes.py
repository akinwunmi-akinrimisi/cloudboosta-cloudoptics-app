"""Connect a DigitalOcean team, replace its token (ADR-021). Scan now, check again, load now,
reload and disconnect are the shared connection routes, which dispatch by provider.

    POST /v1/tenants/{t}/connections/digitalocean       {"token": "dop_v1_..."}
    PUT  /v1/tenants/{t}/connections/{c}/token          {"token": "dop_v1_..."}

The token is checked before anything is stored, and stored only encrypted (tenant-bound,
``connectors.crypto``). It is never returned, logged or written to the audit log. A token
DigitalOcean refuses, one without ``billing:read`` or ``account:read``, and one that can
write are refused with 422 and not kept. Disconnect is the shared ``DELETE`` route; it wipes
the token (``connectors.service.mark_disconnected``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import Field, SecretStr
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from cloudoptics.audit import log as audit
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.routes import Connection, _to_model
from cloudoptics.connectors.service import ConnectionNotFoundError, get_connection
from cloudoptics.identity.deps import AdminAccess, Session
from cloudoptics.platform.api import ApiModel, ApiProblem, user_actor
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.digitalocean import connect
from cloudoptics.providers.digitalocean.client import valid_token

router = APIRouter(prefix="/v1/tenants/{tenant_id}/connections", tags=["connections"])

STACK_NAME = "digitalocean-token"  # cloud_connection.stack_name is NOT NULL; no stack here
BAD_FORMAT = (
    "This does not look like a DigitalOcean token. Copy the whole token: it starts with "
    "dop_v1_ and is shown once, when you create it."
)

FIND_TEAM = text(
    "SELECT id, status FROM cloud_connection WHERE provider = 'digitalocean' "
    "AND provider_account_id = :team"
)
INSERT_CONNECTION = text(
    "INSERT INTO cloud_connection (id, tenant_id, provider, provider_account_id, account_type, "
    "role_arn, external_id_enc, stack_name, create_cur_export) VALUES (:id, :t, "
    "'digitalocean', :team, 'standalone', '', :enc, :stack, false)"
)
RECONNECT = text(
    "UPDATE cloud_connection SET status = 'pending', health = NULL, external_id_enc = :enc, "
    "permission_checks = '[]', last_error = NULL, last_validated_at = NULL, "
    "account_verified_at = NULL, created_by = NULL WHERE id = :id"
)
# In the request's own transaction: it holds the signed-in user's row (sign-in updates it),
# which the connection's foreign key would otherwise wait for in the connect transaction.
SET_CREATED_BY = text("UPDATE cloud_connection SET created_by = :by WHERE id = :id")
SET_TOKEN = text("UPDATE cloud_connection SET external_id_enc = :enc WHERE id = :id")


class TokenBody(ApiModel):
    token: SecretStr = Field(min_length=20, max_length=200)


def _refuse(message: str, code: str = "token_refused") -> ApiProblem:
    return ApiProblem(status.HTTP_422_UNPROCESSABLE_CONTENT, code, message)


async def _inspect(body: TokenBody) -> tuple[str, connect.Inspection]:
    token = body.token.get_secret_value().strip()
    if not valid_token(token):
        raise _refuse(BAD_FORMAT, "token_format")
    result = await run_in_threadpool(connect.inspect, token)
    if result.can_write:
        raise _refuse(connect.WRITE_MESSAGE, "token_can_write")
    if result.status == "failed":
        failed = next((c for c in result.checks if c.status == "error"), None)
        words = failed.message if failed else "DigitalOcean did not accept this token."
        if failed and failed.hint:
            words = f"{words} {failed.hint}"
        raise _refuse(words)
    return token, result


async def _follow_up(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Costs first: month to date and the closed invoices, then the resource scan."""
    if get_settings().jobs_mode == "inline":
        from cloudoptics.analysis.engine import run_analysis
        from cloudoptics.providers.digitalocean import billing, inventory

        await billing.refresh_snapshot(tenant_id, connection_id, force=True)
        result = await billing.ingest_connection(tenant_id, connection_id)
        for period in sorted(set(result.loaded_periods)):
            await billing.reconcile_period(tenant_id, connection_id, period)
        if await inventory.scan_connection(tenant_id, connection_id) in ("succeeded", "partial"):
            await run_analysis(tenant_id)
        return
    from cloudoptics.ingestion.jobs import defer_ingest, defer_snapshot
    from cloudoptics.inventory.jobs import defer_scan
    from cloudoptics.platform.jobs import get_job_app

    app = get_job_app()
    await defer_snapshot(app, str(tenant_id), str(connection_id))
    await defer_ingest(app, str(tenant_id), str(connection_id))
    await defer_scan(app, str(tenant_id), str(connection_id))


async def _connection(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> Connection:
    async with get_sessionmaker()() as s, s.begin():
        await scope(s, tenant_id=tenant_id)
        return _to_model(await get_connection(s, connection_id))


@router.post("/digitalocean", response_model=Connection, status_code=status.HTTP_201_CREATED)
async def connect_digitalocean(
    body: TokenBody, request: Request, access: AdminAccess, session: Session
) -> Connection:
    token, result = await _inspect(body)
    team = str(result.team_uuid)
    actor = user_actor(request, access.user.id)
    # Its own transaction, committed before the first loads run (in their own sessions).
    async with get_sessionmaker()() as s, s.begin():
        await scope(s, tenant_id=access.tenant_id, user_id=access.user.id)
        existing = (await s.execute(FIND_TEAM, {"team": team})).one_or_none()
        if existing is not None and existing.status != "disconnected":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This DigitalOcean team is already connected to this workspace. Use Replace "
                "token on the Connections page to change its token.",
            )
        enc = get_encryptor().encrypt(token, access.tenant_id)
        del token
        if existing is None:
            connection_id, action = uuid7(), "connection.created"
            await s.execute(
                INSERT_CONNECTION,
                {
                    "id": connection_id,
                    "t": access.tenant_id,
                    "team": team,
                    "enc": enc,
                    "stack": STACK_NAME,
                },
            )
        else:
            connection_id, action = existing.id, "connection.reconnected"
            await s.execute(RECONNECT, {"id": connection_id, "enc": enc})
        conn = await get_connection(s, connection_id)
        stored = await connect.store(s, access.tenant_id, conn, result, actor, _now())
        if stored is not None and (stored.error or {}).get("code") == "AccountInUse":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This DigitalOcean team is already connected to another Cloudoptics workspace. "
                "Disconnect it there first, or ask that workspace's admin to invite you.",
            )
    await _follow_up(access.tenant_id, connection_id)
    await session.execute(SET_CREATED_BY, {"id": connection_id, "by": access.user.id})
    # Written last: the inline loads append to the audit chain too (verification).
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=actor,
        action=action,
        target_type="cloud_connection",
        target_id=str(connection_id),
        before={"status": "disconnected"} if action == "connection.reconnected" else None,
        after={"provider": "digitalocean", "accountId": team, "status": result.status},
    )
    return await _connection(access.tenant_id, connection_id)


@router.put("/{connection_id}/token", response_model=Connection)
async def replace_token(
    connection_id: uuid.UUID,
    body: TokenBody,
    request: Request,
    access: AdminAccess,
    session: Session,
) -> Connection:
    """Rotate a DigitalOcean token: the new one must read the same team."""
    try:
        conn = await get_connection(session, connection_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found") from exc
    if conn.provider != "digitalocean":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only DigitalOcean connections use a token.")
    if conn.status == "disconnected":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This connection is disconnected. Connect DigitalOcean again with the new token.",
        )
    token, result = await _inspect(body)
    if result.team_uuid != conn.aws_account_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This token is for a different DigitalOcean team. Create it in the team this "
            "connection reads, or connect the other team separately.",
        )
    actor = user_actor(request, access.user.id)
    # Nothing runs in other sessions here, so the request's own transaction does it all.
    await session.execute(
        SET_TOKEN, {"id": connection_id, "enc": get_encryptor().encrypt(token, access.tenant_id)}
    )
    del token
    await connect.store(
        session,
        access.tenant_id,
        await get_connection(session, connection_id),
        result,
        actor,
        _now(),
    )
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=actor,
        action="connection.token_replaced",
        target_type="cloud_connection",
        target_id=str(connection_id),
        after={"provider": "digitalocean", "status": result.status},
    )
    return _to_model(await get_connection(session, connection_id))


def _now() -> datetime:
    return datetime.now(UTC)
