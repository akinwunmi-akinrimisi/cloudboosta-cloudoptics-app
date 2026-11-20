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

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import Field, SecretStr
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from cloudoptics.audit import log as audit
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.routes import Connection, _to_model
from cloudoptics.connectors.service import (
    ConnectionNotFoundError,
    get_connection,
    read_external_id,
)
from cloudoptics.identity.deps import AdminAccess, Session
from cloudoptics.platform.api import ApiModel, ApiProblem, user_actor
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.digitalocean import connect
from cloudoptics.providers.digitalocean import oauth as oauth_flow
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
    "role_arn, external_id_enc, stack_name, create_cur_export, credential_type, "
    "token_expires_at) VALUES (:id, :t, 'digitalocean', :team, 'standalone', '', :enc, :stack, "
    "false, :kind, :exp)"
)
RECONNECT = text(
    "UPDATE cloud_connection SET status = 'pending', health = NULL, external_id_enc = :enc, "
    "credential_type = :kind, token_expires_at = :exp, "
    "permission_checks = '[]', last_error = NULL, last_validated_at = NULL, "
    "account_verified_at = NULL, created_by = NULL WHERE id = :id"
)
# In the request's own transaction: it holds the signed-in user's row (sign-in updates it),
# which the connection's foreign key would otherwise wait for in the connect transaction.
SET_CREATED_BY = text("UPDATE cloud_connection SET created_by = :by WHERE id = :id")
# A pasted token replaces a sign-in too (the old tokens are revoked).
SET_TOKEN = text(
    "UPDATE cloud_connection SET external_id_enc = :enc, credential_type = 'static', "
    "token_expires_at = NULL WHERE id = :id"
)

OAUTH_RETRY = "Click Connect DigitalOcean and approve read-only access."
OAUTH_WRITE_MESSAGE = (
    "This sign-in gave Cloudoptics write access. Cloudoptics only reads: click Connect "
    "DigitalOcean again and approve read access only. Nothing was kept."
)


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
    return await _save(request, access, session, secret=token, result=result, oauth=None)


async def _save(
    request: Request,
    access: AdminAccess,
    session: Session,
    *,
    secret: str,
    result: connect.Inspection,
    oauth: oauth_flow.Tokens | None,
) -> Connection:
    """Store a checked secret as a new connection or a reconnected one. Signing in again to
    a team this workspace already reads (``oauth``) gives the same connection new tokens; a
    pasted token for such a team is refused (Replace token is the way)."""
    team = str(result.team_uuid)
    actor = user_actor(request, access.user.id)
    kind = "oauth" if oauth else "static"
    expires = oauth.expires_at if oauth else None
    old_secret: str | None = None
    # Its own transaction, committed before the first loads run (in their own sessions).
    async with get_sessionmaker()() as s, s.begin():
        await scope(s, tenant_id=access.tenant_id, user_id=access.user.id)
        existing = (await s.execute(FIND_TEAM, {"team": team})).one_or_none()
        if existing is not None and existing.status != "disconnected":
            if oauth is None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "This DigitalOcean team is already connected to this workspace. Use "
                    "Replace token on the Connections page to change its token.",
                )
            old = await get_connection(s, existing.id)
            if old.credential_type == "oauth" and old.external_id_enc is not None:
                old_secret = await read_external_id(s, old, access.tenant_id)
        enc = get_encryptor().encrypt(secret, access.tenant_id)
        del secret
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
                    "kind": kind,
                    "exp": expires,
                },
            )
        else:
            connection_id, action = existing.id, "connection.reconnected"
            await s.execute(
                RECONNECT, {"id": connection_id, "enc": enc, "kind": kind, "exp": expires}
            )
        conn = await get_connection(s, connection_id)
        stored = await connect.store(s, access.tenant_id, conn, result, actor, _now())
        if stored is not None and (stored.error or {}).get("code") == "AccountInUse":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This DigitalOcean team is already connected to another Cloudoptics workspace. "
                "Disconnect it there first, or ask that workspace's admin to invite you.",
            )
    if old_secret is not None:
        # The tokens just replaced are no longer needed by anyone.
        await run_in_threadpool(oauth_flow.revoke, oauth_flow.access_token(old_secret))
        del old_secret
    await _follow_up(access.tenant_id, connection_id)
    await session.execute(SET_CREATED_BY, {"id": connection_id, "by": access.user.id})
    # Written last: the inline loads append to the audit chain too (verification).
    after: dict[str, Any] = {"provider": "digitalocean", "accountId": team, "status": result.status}
    if oauth:
        after |= {"method": "sign_in", "teamName": result.team_name, "scope": oauth.scope}
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=actor,
        action=action,
        target_type="cloud_connection",
        target_id=str(connection_id),
        before={"status": existing.status} if existing is not None else None,
        after=after,
    )
    return await _connection(access.tenant_id, connection_id)


# --- signing in (OAuth) --------------------------------------------------------------------


class OAuthConfig(ApiModel):
    #: False until the OAuth app's client ID and secret are set: only the token paste is shown.
    enabled: bool


class OAuthStart(ApiModel):
    authorize_url: str


class OAuthCallback(ApiModel):
    code: str = Field(min_length=1, max_length=512, pattern=r"^[A-Za-z0-9._~-]+$")
    state: str = Field(min_length=20, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def _not_configured() -> ApiProblem:
    return ApiProblem(
        status.HTTP_404_NOT_FOUND,
        "oauth_not_configured",
        "Signing in with DigitalOcean is not set up here. Use an API token instead.",
    )


@router.get("/digitalocean/oauth", response_model=OAuthConfig)
async def oauth_config(access: AdminAccess) -> OAuthConfig:
    return OAuthConfig(enabled=get_settings().do_oauth_enabled)


@router.post(
    "/digitalocean/oauth/start", response_model=OAuthStart, status_code=status.HTTP_201_CREATED
)
async def oauth_start(access: AdminAccess, session: Session) -> OAuthStart:
    """A one-time sign-in link for this admin and workspace (10 minutes)."""
    if not get_settings().do_oauth_enabled:
        raise _not_configured()
    url = await oauth_flow.start(session, access.tenant_id, access.binding_id)
    return OAuthStart(authorize_url=url)


@router.post(
    "/digitalocean/oauth/callback",
    response_model=Connection,
    status_code=status.HTTP_201_CREATED,
)
async def oauth_callback(
    body: OAuthCallback, request: Request, access: AdminAccess, session: Session
) -> Connection:
    """DigitalOcean sent the browser back: check the state, trade the code for tokens, run
    the same checks as a pasted token, then store the tokens encrypted."""
    if not get_settings().do_oauth_enabled:
        raise _not_configured()
    try:
        verifier = await oauth_flow.consume(access.tenant_id, access.binding_id, body.state)
    except oauth_flow.StateRefusedError:
        raise ApiProblem(
            status.HTTP_400_BAD_REQUEST,
            "oauth_state",
            "This sign-in has expired or was already used. Click Connect DigitalOcean again.",
        ) from None
    try:
        tokens = await run_in_threadpool(oauth_flow.exchange, body.code, verifier)
    except oauth_flow.OAuthError as exc:
        raise _refuse(
            "DigitalOcean did not complete the sign-in. Click Connect DigitalOcean again.",
            "oauth_refused" if exc.refused else "oauth_unavailable",
        ) from None
    result = await run_in_threadpool(connect.inspect, tokens.access)
    problem: ApiProblem | None = None
    if result.can_write:
        problem = _refuse(OAUTH_WRITE_MESSAGE, "token_can_write")
    elif result.status == "failed":
        failed = next((c for c in result.checks if c.status == "error"), None)
        words = failed.message if failed else "DigitalOcean did not accept the sign-in."
        problem = _refuse(f"{words} {OAUTH_RETRY}")
    elif tokens.team_uuid and result.team_uuid != tokens.team_uuid:
        problem = _refuse(f"DigitalOcean named two different teams. {OAUTH_RETRY}")
    if problem is not None:
        # Nothing is kept: give the access back at once.
        await run_in_threadpool(oauth_flow.revoke, tokens.access)
        raise problem
    if tokens.team_name and not result.team_name:
        result.team_name = tokens.team_name
    try:
        return await _save(
            request, access, session, secret=oauth_flow.encode(tokens), result=result, oauth=tokens
        )
    except HTTPException:
        await run_in_threadpool(oauth_flow.revoke, tokens.access)
        raise


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
    revoked = await oauth_flow.revoke_stored(session, access.tenant_id, conn)
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
        after={"provider": "digitalocean", "status": result.status}
        | ({"signInRevoked": revoked} if revoked is not None else {}),
    )
    return _to_model(await get_connection(session, connection_id))


def _now() -> datetime:
    return datetime.now(UTC)
