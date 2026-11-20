"""Connecting a DigitalOcean team with a read-only token (ADR-021).

The customer creates a personal access token with custom read scopes and pastes it once.
Every check below is one read request; the token's team is the connection's account.

DigitalOcean has no endpoint that lists a token's scopes, so each scope is tested by using
it, and write access by two create requests that cannot succeed: an empty body to
``POST /v2/droplets`` and an empty tag name to ``POST /v2/tags``. A token without write
scopes is refused with 403 before the body is read; a token with them gets 422 (the body is
invalid) and nothing is created. A token that can write is refused: Cloudoptics only reads,
and a stored write token would be a much larger secret to hold.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from cloudoptics.analytics import funnel
from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.connectors.service import get_connection, read_external_id
from cloudoptics.notifications.alerts import should_alert
from cloudoptics.platform.ids import uuid7
from cloudoptics.providers.digitalocean.client import Client, DigitalOceanError

CheckStatus = Literal["ok", "warning", "error", "skipped"]
Status = Literal["healthy", "degraded", "failed"]

TOKEN_PAGE = "https://cloud.digitalocean.com/account/api/tokens"  # noqa: S105 - a URL
#: The scopes the customer ticks (Custom scopes). ``billing:read`` and ``account:read`` are
#: required; the others turn on the resource scan and its rules.
REQUIRED_SCOPES = ("account:read", "billing:read")
RESOURCE_SCOPES = (
    "droplet:read",
    "block_storage:read",
    "snapshot:read",
    "reserved_ip:read",
    "load_balancer:read",
    "database:read",
    "monitoring:read",
    "tag:read",
)
WRITE_PROBES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("/v2/droplets", {}),
    ("/v2/tags", {"name": ""}),
)
REFUSED = (401, 403)
#: The checks whose scopes the resource scan reads.
RESOURCE_CHECKS = frozenset(
    {"droplets", "volumes", "snapshots", "reserved_ips", "load_balancers", "databases"}
)

WRITE_MESSAGE = (
    "This token can change your DigitalOcean resources. Cloudoptics only reads: create a "
    "token with Custom scopes, tick only the read scopes listed, and paste that one."
)


@dataclass
class Check:
    id: str
    label: str
    status: CheckStatus
    message: str
    hint: str | None = None


# (check id, label, scope, path, required). ``{team}`` and the dates are filled in per run.
SCOPE_CHECKS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("billing", "Read the balance", "billing:read", "/v2/customers/my/balance", True),
    ("invoices", "Read invoices", "billing:read", "/v2/customers/my/invoices", True),
    (
        "insights",
        "Read daily spend",
        "billing:read",
        "/v2/billing/do:team:{team}/insights/{start}/{end}",
        False,
    ),
    ("droplets", "List Droplets", "droplet:read", "/v2/droplets", False),
    ("volumes", "List volumes", "block_storage:read", "/v2/volumes", False),
    ("snapshots", "List snapshots", "snapshot:read", "/v2/snapshots", False),
    ("reserved_ips", "List reserved IPs", "reserved_ip:read", "/v2/reserved_ips", False),
    ("load_balancers", "List load balancers", "load_balancer:read", "/v2/load_balancers", False),
    ("databases", "List managed databases", "database:read", "/v2/databases", False),
    ("monitoring", "Read Droplet CPU", "monitoring:read", "/v2/monitoring/alerts", False),
    ("tags", "Read tags", "tag:read", "/v2/tags", False),
)


@dataclass
class Inspection:
    """What a token can do, and whose it is. Never holds the token."""

    status: Status
    checks: list[Check]
    team_uuid: str | None = None
    team_name: str | None = None
    error: dict[str, str] | None = None
    calls: int = 0
    # Set by ``validate``: the connection got worse (its admins are told).
    health_worsened: bool = False
    #: The provider protocol (ADR-018): a working token can always load invoices.
    has_billing_export: bool = True
    #: The checks that worked (``billing``, ``insights``, ``droplets``...).
    readable: frozenset[str] = field(default_factory=frozenset)

    def checks_json(self) -> list[dict[str, Any]]:
        return [asdict(c) for c in self.checks]

    @property
    def can_write(self) -> bool:
        return any(c.id == "read_only" and c.status == "error" for c in self.checks)


def _skipped(reason: str) -> list[Check]:
    return [
        Check(cid, label, "skipped", reason, f"Scope {scope}.")
        for cid, label, scope, _, _ in SCOPE_CHECKS
    ]


def inspect_token(client: Client, today: date | None = None) -> Inspection:
    """Run every check with the token (synchronous; callers use a worker thread)."""
    today = today or datetime.now(UTC).date()
    checks: list[Check] = []
    try:
        account = (client.get("/v2/account") or {}).get("account") or {}
    except DigitalOceanError as exc:
        if exc.unauthorized:
            message, hint = (
                "DigitalOcean did not accept this token.",
                "Check it was copied whole and has not expired or been deleted.",
            )
        elif exc.forbidden:
            message, hint = (
                "The token cannot read the team.",
                "Tick account:read when you create the token.",
            )
        else:
            message, hint = f"DigitalOcean did not answer ({exc.code}).", "Try again shortly."
        checks.append(Check("account", "Read the team", "error", message, hint))
        checks.extend(_skipped("Not checked: the token was not accepted."))
        return Inspection(
            "failed", checks, error={"code": exc.code, "check": "account"}, calls=client.calls
        )
    team = account.get("team") or {}
    team_uuid = str(team.get("uuid") or account.get("uuid") or "")
    team_name = team.get("name") or None
    if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", team_uuid):
        checks.append(
            Check(
                "account",
                "Read the team",
                "error",
                "DigitalOcean did not name the token's team.",
                None,
            )
        )
        return Inspection(
            "failed", checks, error={"code": "no_team", "check": "account"}, calls=client.calls
        )
    checks.append(Check("account", "Read the team", "ok", f"Team {team_name or team_uuid}.", None))

    readable: set[str] = set()
    start = today.replace(day=1)
    for cid, label, scope, path, required in SCOPE_CHECKS:
        url = path.format(team=team_uuid, start=start.isoformat(), end=today.isoformat())
        try:
            client.get(url, {"per_page": 1})
        except DigitalOceanError as exc:
            tip: str | None
            if exc.forbidden or exc.unauthorized:
                message = f"The token cannot use {scope}."
                tip = f"Create a new token and tick {scope}."
            elif cid == "insights" and exc.status in (400, 404):
                message, tip = "Daily spend is not available for this team yet.", None
            else:
                message, tip = f"DigitalOcean did not answer ({exc.code}).", "Try again later."
            outcome: CheckStatus = "error" if required else "warning"
            if not required:
                message += " Costs still load; " + (
                    "month-to-date spend shows the balance only."
                    if cid == "insights"
                    else "this part of the resource scan is skipped."
                )
            checks.append(Check(cid, label, outcome, message, tip))
            continue
        checks.append(Check(cid, label, "ok", f"Works ({scope}).", None))
        readable.add(cid)

    checks.append(_read_only(client))
    if any(c.status == "error" for c in checks):
        failed = next(c for c in checks if c.status == "error")
        return Inspection(
            "failed",
            checks,
            team_uuid,
            team_name,
            error={
                "code": "write_access" if failed.id == "read_only" else "missing_scope",
                "check": failed.id,
            },
            calls=client.calls,
            readable=frozenset(readable),
        )
    overall: Status = "degraded" if any(c.status == "warning" for c in checks) else "healthy"
    return Inspection(
        overall, checks, team_uuid, team_name, calls=client.calls, readable=frozenset(readable)
    )


def _read_only(client: Client) -> Check:
    label = "Token is read-only"
    for path, body in WRITE_PROBES:
        try:
            code = client.status_of("POST", path, body)
        except DigitalOceanError as exc:
            return Check(
                "read_only",
                label,
                "warning",
                f"Could not check for write access ({exc.code}).",
                None,
            )
        if code not in REFUSED:
            return Check(
                "read_only", label, "error", WRITE_MESSAGE, "Delete this token in DigitalOcean too."
            )
    return Check("read_only", label, "ok", "DigitalOcean refuses changes with this token.", None)


def inspect(token: str) -> Inspection:
    with Client(token) as client:
        return inspect_token(client)


# --- storing a result ----------------------------------------------------------------------

STORE_RESULT = text(
    "UPDATE cloud_connection SET status = :status, health = :status, "
    "permission_checks = CAST(:checks AS jsonb), last_error = CAST(:err AS jsonb), "
    "last_validated_at = now(), account_verified_at = CASE WHEN :verified THEN "
    "coalesce(account_verified_at, now()) ELSE account_verified_at END "
    "WHERE id = :c AND status <> 'disconnected'"
)
ADD_ACCOUNT = text(
    "INSERT INTO cloud_account (id, tenant_id, connection_id, provider, provider_account_id, "
    "name, account_type, resource_access) VALUES (:id, :t, :c, 'digitalocean', :acct, :name, "
    "'standalone', :access) ON CONFLICT (tenant_id, provider, provider_account_id) DO UPDATE "
    "SET name = COALESCE(EXCLUDED.name, cloud_account.name), "
    "resource_access = EXCLUDED.resource_access, connection_id = EXCLUDED.connection_id"
)
RECORD_RUN = text(
    "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, status, started_at, "
    "finished_at, error_code) VALUES (:id, :t, :c, 'connection.validate', :status, :started, "
    "now(), :code)"
)


def in_use() -> Inspection:
    return Inspection(
        "failed",
        [
            Check(
                "account",
                "Read the team",
                "error",
                "This DigitalOcean team is already connected to another Cloudoptics workspace.",
                "Disconnect it there first, or ask that workspace's admin to invite you.",
            )
        ],
        error={"code": "AccountInUse", "check": "account"},
    )


async def store(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    conn: Any,
    result: Inspection,
    actor: Actor,
    started: datetime,
) -> Inspection | None:
    """Store a check of ``conn`` inside the caller's transaction; None when it was
    disconnected meanwhile. The token proves control of its team, so a passing check
    reserves the team for this workspace (one verified workspace per account, ADR-017)."""
    if result.team_uuid and result.team_uuid != conn.aws_account_id:
        result = Inspection(
            "failed",
            [
                Check(
                    "account",
                    "Read the team",
                    "error",
                    "This token belongs to a different DigitalOcean team.",
                    "Replace it with a token from the team this connection reads.",
                )
            ],
            error={"code": "team_changed", "check": "account"},
        )
    verified = result.status != "failed"
    params = {
        "status": result.status,
        "checks": json.dumps(result.checks_json()),
        "err": json.dumps(result.error) if result.error else None,
        "verified": verified,
        "c": conn.id,
    }
    try:
        async with session.begin_nested():
            stored = await session.execute(STORE_RESULT, params)
    except IntegrityError:
        result = in_use()
        stored = await session.execute(
            STORE_RESULT,
            {
                **params,
                "status": "failed",
                "verified": False,
                "checks": json.dumps(result.checks_json()),
                "err": json.dumps(result.error),
            },
        )
    if not getattr(stored, "rowcount", 0):
        return None
    if result.status != "failed":
        await funnel.emit(session, "connected", tenant_id=tenant_id)
        await session.execute(
            ADD_ACCOUNT,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": conn.id,
                "acct": conn.aws_account_id,
                "name": result.team_name,
                "access": bool(result.readable & RESOURCE_CHECKS),
            },
        )
    await session.execute(
        RECORD_RUN,
        {
            "id": uuid7(),
            "t": tenant_id,
            "c": conn.id,
            "status": "failed" if result.status == "failed" else "succeeded",
            "started": started,
            "code": (result.error or {}).get("code"),
        },
    )
    result.health_worsened = should_alert(conn.health, result.status)
    if result.status != conn.status:
        await audit.record(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="connection.status_changed",
            target_type="cloud_connection",
            target_id=str(conn.id),
            before={"status": conn.status},
            after={"status": result.status},
        )
    return result


async def validate(
    session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, actor: Actor
) -> Inspection | None:
    """Check a stored token again (the ``connect`` capability): None once disconnected."""
    conn = await get_connection(session, connection_id)
    if conn.status == "disconnected" or conn.external_id_enc is None:
        return None
    token = await read_external_id(session, conn, tenant_id)
    started = datetime.now(UTC)
    result = await run_in_threadpool(inspect, token)
    return await store(session, tenant_id, conn, result, actor, started)


async def usable_token(session: AsyncSession, tenant_id: uuid.UUID, conn: Any) -> str | None:
    """The stored token, or None when there is none to use (disconnected)."""
    if conn.status == "disconnected" or conn.external_id_enc is None:
        return None
    return await read_external_id(session, conn, tenant_id)
