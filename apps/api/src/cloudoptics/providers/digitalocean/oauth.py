"""Connect DigitalOcean by signing in (OAuth 2.0 authorization code flow, ADR-021).

DigitalOcean's documented flow (https://docs.digitalocean.com/reference/api/oauth/):

1. ``start``: the browser goes to ``/v1/oauth/authorize`` with our client ID, the registered
   redirect URI, ``response_type=code``, a random ``state``, the scope from settings and a
   PKCE challenge (S256; required for public clients, allowed for confidential ones). The
   customer signs in, picks the team and approves.
2. DigitalOcean sends the browser back to the web app's callback page with ``code`` and
   ``state``. The page posts both to the API with the signed-in user's token. ``consume``
   accepts a state once, only for the user and workspace that started it, within 10 minutes.
3. ``exchange``: ``POST /v1/oauth/token`` (``grant_type=authorization_code``) with the client
   secret and the PKCE verifier. The answer has ``access_token``, ``refresh_token``,
   ``expires_in`` (30 days for confidential clients), ``scope`` and ``info`` with
   ``team_uuid`` and ``team_name``. The user's name and email in ``info`` are dropped here.
4. ``refresh_connection``: before expiry, ``POST /v1/oauth/token``
   (``grant_type=refresh_token``). Each refresh token works once and the refresh invalidates
   the access token it was issued with, so two refreshes must never race: the connection
   row is locked (``SELECT ... FOR UPDATE``) for the whole exchange, and the second job sees
   the new expiry and does nothing.
5. ``revoke``: ``POST /v1/oauth/revoke`` with the access token (bearer header and ``token``).

Both tokens are kept together, as one JSON document, in ``cloud_connection.external_id_enc``
(the tenant-bound encrypted secret column, ADR-007), so the KMS re-encryption, the wipe on
disconnect and the token-leak test cover them unchanged. No token is logged, returned, put
in an exception or written to the audit log.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.service import read_external_id
from cloudoptics.notifications.alerts import should_alert
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.digitalocean import TEAM_ID
from cloudoptics.providers.digitalocean.client import TIMEOUT, get_transport

log = logging.getLogger(__name__)

STATE_MINUTES = 10
SECRET_VERSION = "do-oauth-1"  # noqa: S105 - a format name
#: The token formats are not documented beyond the examples (``doo_v1_…``, ``dor_v1_…``).
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9._~-]{20,512}")
#: Confidential clients get 30-day tokens; a missing or odd ``expires_in`` is read as 1 hour.
MAX_LIFETIME = timedelta(days=90)
DEFAULT_LIFETIME = timedelta(hours=1)


class OAuthError(Exception):
    """DigitalOcean refused or could not answer an OAuth request. Never holds a token."""

    def __init__(self, status: int | None, code: str) -> None:
        super().__init__(f"{code} ({status})" if status else code)
        self.status = status
        self.code = code

    @property
    def refused(self) -> bool:
        """A definite no (``invalid_grant``, ``invalid_client``…): retrying will not help."""
        return self.status is not None and 400 <= self.status < 500 and self.status != 429


class StateRefusedError(Exception):
    """The ``state`` is unknown, used, expired, or another user's or workspace's."""


@dataclass(frozen=True, repr=False)
class Tokens:
    access: str
    refresh: str
    expires_at: datetime
    scope: str
    team_uuid: str | None
    team_name: str | None

    def __repr__(self) -> str:
        return f"<digitalocean.Tokens team={self.team_uuid} expires={self.expires_at:%Y-%m-%d}>"


# --- the stored secret ---------------------------------------------------------------------


def encode(tokens: Tokens) -> str:
    return json.dumps({"v": SECRET_VERSION, "access": tokens.access, "refresh": tokens.refresh})


def is_oauth_secret(secret: str) -> bool:
    return secret.startswith("{")


def access_token(secret: str) -> str:
    """The bearer token in a stored secret: a pasted token as is, or the OAuth access token."""
    if not is_oauth_secret(secret):
        return secret
    return str(json.loads(secret)["access"])


def refresh_token(secret: str) -> str:
    return str(json.loads(secret)["refresh"])


# --- the documented requests (synchronous; callers use a worker thread) ---------------------


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def state_hash(state: str) -> bytes:
    return hashlib.sha256(b"cloudoptics/oauth-state:" + state.encode()).digest()


def scope_param() -> str:
    return " ".join(get_settings().do_oauth_scope.split())


def authorize_url(state: str, verifier: str | None) -> str:
    settings = get_settings()
    params = {
        "client_id": settings.do_oauth_client_id,
        "redirect_uri": settings.do_oauth_callback_url,
        "response_type": "code",
        "state": state,
    }
    # Leaving the scope out shows DigitalOcean's "Manage permissions" choice instead.
    if scope_param():
        params["scope"] = scope_param()
    if verifier:
        params["code_challenge"] = challenge(verifier)
        params["code_challenge_method"] = "S256"
    return f"{settings.do_oauth_authorize_url}?{urlencode(params)}"


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    code = body.get("error") if isinstance(body, dict) else None
    if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code):
        return code
    return "server_error" if response.status_code >= 500 else "client_error"


def _post(path: str, data: dict[str, str], bearer: str | None = None) -> dict[str, Any]:
    settings = get_settings()
    headers = {"Accept": "application/json", "User-Agent": "cloudoptics"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    with httpx.Client(
        base_url=settings.do_oauth_base_url,
        transport=get_transport(),
        timeout=TIMEOUT,
        follow_redirects=False,
    ) as http:
        try:
            response = http.post(path, data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise OAuthError(None, f"network:{type(exc).__name__}") from None
    if response.status_code >= 400:
        raise OAuthError(response.status_code, _error_code(response))
    try:
        body = response.json()
    except ValueError:
        raise OAuthError(response.status_code, "invalid_json") from None
    return body if isinstance(body, dict) else {}


def _client_auth() -> dict[str, str]:
    settings = get_settings()
    return {
        "client_id": settings.do_oauth_client_id,
        "client_secret": settings.do_oauth_client_secret.get_secret_value(),
    }


def _tokens(body: dict[str, Any], now: datetime) -> Tokens:
    access, refresh = body.get("access_token"), body.get("refresh_token")
    if not (isinstance(access, str) and TOKEN_SHAPE.fullmatch(access)):
        raise OAuthError(None, "no_access_token")
    if not (isinstance(refresh, str) and TOKEN_SHAPE.fullmatch(refresh)):
        raise OAuthError(None, "no_refresh_token")
    try:
        lifetime = timedelta(seconds=int(body.get("expires_in") or 0))
    except (TypeError, ValueError):
        lifetime = timedelta(0)
    if lifetime <= timedelta(0) or lifetime > MAX_LIFETIME:
        lifetime = DEFAULT_LIFETIME
    raw = body.get("info")
    # Only the team is kept; the user's name and email are dropped here.
    info: dict[str, Any] = raw if isinstance(raw, dict) else {}
    team = str(info.get("team_uuid") or "")
    name = info.get("team_name")
    return Tokens(
        access,
        refresh,
        now + lifetime,
        str(body.get("scope") or ""),
        team if re.fullmatch(TEAM_ID, team) else None,
        name[:200] if isinstance(name, str) and name else None,
    )


def exchange(code: str, verifier: str | None) -> Tokens:
    """Trade the code from the callback for tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": get_settings().do_oauth_callback_url,
        **_client_auth(),
    }
    if verifier:
        data["code_verifier"] = verifier
    return _tokens(_post("/v1/oauth/token", data), datetime.now(UTC))


def refresh(token: str) -> Tokens:
    """A new access and refresh token; the old ones stop working."""
    data = {"grant_type": "refresh_token", "refresh_token": token, **_client_auth()}
    return _tokens(_post("/v1/oauth/token", data), datetime.now(UTC))


def revoke(token: str) -> bool:
    """Revoke an access token at DigitalOcean; False when it could not be done."""
    try:
        _post("/v1/oauth/revoke", {"token": token}, bearer=token)
    except OAuthError as exc:
        log.warning("DigitalOcean token revocation failed: %s", exc.code)
        return False
    return True


# --- the sign-in state ---------------------------------------------------------------------

PURGE_STATES = text("DELETE FROM oauth_state WHERE expires_at < now() - interval '1 day'")
INSERT_STATE = text(
    "INSERT INTO oauth_state (id, tenant_id, user_id, provider, state_hash, verifier_enc, "
    "expires_at) VALUES (:id, :t, :u, 'digitalocean', :h, :v, "
    "now() + make_interval(mins => :minutes))"
)
# Single use: the update wins once. Another workspace's row is invisible (RLS); another
# user's, a used one and an expired one do not match.
CONSUME_STATE = text(
    "UPDATE oauth_state SET used_at = now() WHERE state_hash = :h AND user_id = :u "
    "AND provider = 'digitalocean' AND used_at IS NULL AND expires_at > now() "
    "RETURNING verifier_enc"
)


async def start(session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Record a new state for this user and workspace; the URL to send the browser to."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48) if get_settings().do_oauth_pkce else None
    await session.execute(PURGE_STATES)
    await session.execute(
        INSERT_STATE,
        {
            "id": uuid7(),
            "t": tenant_id,
            "u": user_id,
            "h": state_hash(state),
            "v": get_encryptor().encrypt(verifier, tenant_id) if verifier else None,
            "minutes": STATE_MINUTES,
        },
    )
    return authorize_url(state, verifier)


async def consume(tenant_id: uuid.UUID, user_id: uuid.UUID, state: str) -> str | None:
    """Use a state up (committed at once, so a failed exchange cannot be replayed). Returns
    its PKCE verifier (None without PKCE); raises ``StateRefusedError``."""
    async with get_sessionmaker()() as s, s.begin():
        await scope(s, tenant_id=tenant_id, user_id=user_id)
        row = (await s.execute(CONSUME_STATE, {"h": state_hash(state), "u": user_id})).one_or_none()
    if row is None:
        raise StateRefusedError
    if row.verifier_enc is None:
        return None
    return get_encryptor().decrypt(bytes(row.verifier_enc), tenant_id)


# --- renewal -------------------------------------------------------------------------------

LOCK_CONNECTION = text(
    "SELECT id, status, health, credential_type, token_expires_at, external_id_enc "
    "FROM cloud_connection WHERE id = :c FOR UPDATE"
)
SAVE_TOKENS = text(
    "UPDATE cloud_connection SET external_id_enc = :enc, token_expires_at = :exp WHERE id = :c"
)
REFRESH_FAILED = text(
    "UPDATE cloud_connection SET status = 'failed', health = 'failed', "
    "permission_checks = CAST(:checks AS jsonb), last_error = CAST(:err AS jsonb), "
    "last_validated_at = now() WHERE id = :c AND status <> 'disconnected'"
)
RECORD_REFRESH = text(
    "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, status, started_at, "
    "finished_at, error_code) VALUES (:id, :t, :c, 'connection.refresh', :status, :started, "
    "now(), :code)"
)
DUE_CONNECTIONS = text(
    "SELECT tenant_id, id FROM cloud_connection WHERE credential_type = 'oauth' "
    "AND status <> 'disconnected' AND external_id_enc IS NOT NULL "
    "AND (token_expires_at IS NULL OR token_expires_at <= now() + make_interval(days => :d))"
)

RENEW_CHECK = {
    "id": "account",
    "label": "Renew access",
    "status": "error",
    "message": "DigitalOcean refused to renew Cloudoptics' access.",
    "hint": "Reconnect DigitalOcean on the Connections page and approve again.",
}


def due(expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:
        return True
    return expires_at <= now + timedelta(days=get_settings().do_oauth_refresh_days)


async def refresh_connection(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, *, force: bool = False
) -> str:
    """Renew one connection's tokens: ``refreshed``, ``fresh`` (not due, or a racing job did
    it), ``failed`` (refused: the connection fails and its admins are told) or ``skipped``.
    A network error or 5xx raises, so the job retries with the old, still valid tokens."""
    started = datetime.now(UTC)
    system = Actor(type="system")
    worsened = False
    async with get_sessionmaker()() as s, s.begin():
        await scope(s, tenant_id=tenant_id)
        # Held until commit: a second refresh waits here, then sees the new expiry.
        row = (await s.execute(LOCK_CONNECTION, {"c": connection_id})).one_or_none()
        if (
            row is None
            or row.status == "disconnected"
            or row.credential_type != "oauth"
            or row.external_id_enc is None
        ):
            return "skipped"
        if not force and not due(row.token_expires_at, started):
            return "fresh"
        secret = await read_external_id(s, row, tenant_id)
        try:
            tokens = await run_in_threadpool(refresh, refresh_token(secret))
        except OAuthError as exc:
            if not exc.refused:
                raise
            await s.execute(
                REFRESH_FAILED,
                {
                    "checks": json.dumps([RENEW_CHECK]),
                    "err": json.dumps({"code": "refresh_refused", "check": "account"}),
                    "c": connection_id,
                },
            )
            await _record(s, tenant_id, connection_id, "failed", started, exc.code)
            await audit.record(
                s,
                tenant_id=tenant_id,
                actor=system,
                action="connection.token_refresh_failed",
                target_type="cloud_connection",
                target_id=str(connection_id),
                before={"status": row.status},
                after={"status": "failed", "code": exc.code},
            )
            worsened = should_alert(row.health, "failed")
            outcome = "failed"
        else:
            await s.execute(
                SAVE_TOKENS,
                {
                    "enc": get_encryptor().encrypt(encode(tokens), tenant_id),
                    "exp": tokens.expires_at,
                    "c": connection_id,
                },
            )
            await _record(s, tenant_id, connection_id, "succeeded", started, None)
            await audit.record(
                s,
                tenant_id=tenant_id,
                actor=system,
                action="connection.token_refreshed",
                target_type="cloud_connection",
                target_id=str(connection_id),
                after={"expiresAt": tokens.expires_at.isoformat()},
            )
            outcome = "refreshed"
    if worsened:
        await _alert(tenant_id, connection_id)
    return outcome


async def _record(
    s: AsyncSession,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    status: str,
    started: datetime,
    code: str | None,
) -> None:
    await s.execute(
        RECORD_REFRESH,
        {
            "id": uuid7(),
            "t": tenant_id,
            "c": connection_id,
            "status": status,
            "started": started,
            "code": code,
        },
    )


async def _alert(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """The existing connection-health alert (FR-NOT-02): inline locally, else queued."""
    if get_settings().jobs_mode == "inline":
        from cloudoptics.notifications.alerts import send_connection_alert

        async with get_sessionmaker()() as s, s.begin():
            await scope(s, tenant_id=tenant_id)
            await send_connection_alert(s, tenant_id, connection_id)
        return
    from cloudoptics.notifications.jobs import defer_connection_alert
    from cloudoptics.platform.jobs import get_job_app

    await defer_connection_alert(get_job_app(), str(tenant_id), str(connection_id))


async def revoke_stored(session: AsyncSession, tenant_id: uuid.UUID, conn: Any) -> bool | None:
    """Revoke a signed-in connection's access token before it is wiped. None for a pasted
    token (the customer deletes that one in DigitalOcean)."""
    if getattr(conn, "credential_type", "static") != "oauth" or conn.external_id_enc is None:
        return None
    secret = await read_external_id(session, conn, tenant_id)
    return await run_in_threadpool(revoke, access_token(secret))
