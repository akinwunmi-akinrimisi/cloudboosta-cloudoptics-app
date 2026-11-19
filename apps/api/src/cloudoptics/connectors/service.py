"""Connection lifecycle: create, validate, disconnect."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from cloudoptics.analytics import funnel
from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.connectors.aws import SessionFactory, session_name
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.validation import Check, ValidationResult, validate_billing_access
from cloudoptics.notifications.alerts import should_alert
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

# Literal statements only; request values are always bound parameters.
SELECT_CONNECTION = text(
    "SELECT id, tenant_id, aws_account_id, account_type, role_arn, stack_name, "
    "create_cur_export, cur_bucket, cur_prefix, cur_region, cur_export_arn, status, "
    "permission_checks, last_error, last_validated_at, created_at, external_id_enc, health, "
    "stack_version, pending_stack FROM cloud_connection WHERE id = :c"
)
LIST_CONNECTIONS = text(
    "SELECT id, tenant_id, aws_account_id, account_type, role_arn, stack_name, "
    "create_cur_export, cur_bucket, cur_prefix, cur_region, cur_export_arn, status, "
    "permission_checks, last_error, last_validated_at, created_at, stack_version "
    "FROM cloud_connection ORDER BY created_at"
)

_session_factory: SessionFactory | None = None


def get_session_factory() -> SessionFactory:
    global _session_factory
    if _session_factory is None:
        _session_factory = SessionFactory(
            broker_role_arn=get_settings().aws_broker_role_arn or None
        )
    return _session_factory


def set_session_factory(factory: Any) -> None:
    """Swap the AWS session factory (tests use fakes)."""
    global _session_factory
    _session_factory = factory


@dataclass(frozen=True)
class ConnectionNotFoundError(Exception):
    connection_id: uuid.UUID


async def get_connection(session: AsyncSession, connection_id: uuid.UUID) -> Any:
    row = (
        await session.execute(
            SELECT_CONNECTION,
            {"c": connection_id},
        )
    ).one_or_none()
    if row is None:
        raise ConnectionNotFoundError(connection_id)
    return row


async def _store_result(
    session: AsyncSession, connection_id: uuid.UUID, result: ValidationResult, verified: bool
) -> bool:
    """Store a validation result; False when the connection was disconnected meanwhile.

    Disconnect is final: a validation that was queued (or running) when an admin or the stack
    disconnected never brings the connection back (security audit run 1, lead 4).
    """
    stored = await session.execute(
        text(
            "UPDATE cloud_connection SET status = :status, health = :status, "
            "permission_checks = CAST(:checks AS jsonb), last_error = CAST(:err AS jsonb), "
            "last_validated_at = now(), cur_bucket = COALESCE(:bucket, cur_bucket), "
            "cur_prefix = COALESCE(:prefix, cur_prefix), "
            "cur_region = COALESCE(:region, cur_region), "
            "cur_export_arn = COALESCE(:export_arn, cur_export_arn), "
            "account_verified_at = CASE WHEN :verified THEN coalesce(account_verified_at, now()) "
            "ELSE account_verified_at END WHERE id = :c AND status <> 'disconnected'"
        ),
        {
            "status": result.status,
            "checks": json.dumps(result.checks_json()),
            "err": json.dumps(result.error) if result.error else None,
            "bucket": result.cur_bucket,
            "prefix": result.cur_prefix,
            "region": result.cur_region,
            "export_arn": result.cur_export_arn,
            "verified": verified,
            "c": connection_id,
        },
    )
    return bool(getattr(stored, "rowcount", 0))


async def _apply_stack(
    session: AsyncSession, tenant_id: uuid.UUID, conn: Any, stack: dict[str, Any]
) -> bool:
    """The values a stack reported are proven (its role was assumed), so they replace the
    stored ones. False when the connection was disconnected meanwhile."""
    applied = await session.execute(
        text(
            "UPDATE cloud_connection SET account_type = :type, role_arn = :arn, "
            "stack_name = :stack, stack_version = :version, create_cur_export = true, "
            "cur_bucket = COALESCE(:bucket, cur_bucket), "
            "cur_prefix = COALESCE(:prefix, cur_prefix), pending_stack = NULL "
            "WHERE id = :c AND status <> 'disconnected'"
        ),
        {
            "type": stack["accountType"],
            "arn": stack["roleArn"],
            "stack": stack["stackName"],
            "version": stack["stackVersion"],
            "bucket": stack.get("curBucket"),
            "prefix": stack.get("curPrefix"),
            "c": conn.id,
        },
    )
    if not getattr(applied, "rowcount", 0):
        return False
    if stack["stackName"] != conn.stack_name or stack["roleArn"] != conn.role_arn:
        await audit.record(
            session,
            tenant_id=tenant_id,
            actor=Actor(type="system"),
            action="connection.stack_changed",
            target_type="cloud_connection",
            target_id=str(conn.id),
            before={"stack": conn.stack_name},
            after={"stack": stack["stackName"], "version": stack["stackVersion"]},
        )
    return True


async def run_validation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    actor: Actor,
) -> ValidationResult | None:
    """Validate one connection inside the caller's transaction (tenant scope already set).

    Returns None, without calling AWS, for a disconnected connection. When a stack callback
    reported new values for the connection (``pending_stack``), those are checked first. They
    replace the stored ones only if their role can be assumed; otherwise they are dropped and
    the known-good values are checked instead.
    """
    conn = await get_connection(session, connection_id)
    if conn.status == "disconnected":
        return None
    stack: dict[str, Any] | None = conn.pending_stack
    role_arn = stack["roleArn"] if stack else conn.role_arn
    account_type = stack["accountType"] if stack else conn.account_type
    external_id = get_encryptor().decrypt(bytes(conn.external_id_enc), tenant_id)
    factory = get_session_factory()
    started = datetime.now(UTC)

    def _open() -> Any:
        return factory.assume(role_arn, external_id, session_name(tenant_id, "validate"))

    result = await run_in_threadpool(
        validate_billing_access,
        _open,
        aws_account_id=conn.aws_account_id,
        account_type=account_type,
        create_cur_export=True if stack else conn.create_cur_export,
    )
    if stack is not None:
        if result.status == "failed":
            # Not proven: keep what works. The audit log says what the stack asked for.
            await session.execute(
                text("UPDATE cloud_connection SET pending_stack = NULL WHERE id = :c"),
                {"c": connection_id},
            )
            await audit.record(
                session,
                tenant_id=tenant_id,
                actor=Actor(type="system"),
                action="connection.stack_change_rejected",
                target_type="cloud_connection",
                target_id=str(connection_id),
                before={"stack": conn.stack_name, "accountType": conn.account_type},
                after={
                    "stack": stack["stackName"],
                    "accountType": stack["accountType"],
                    "reason": (result.error or {}).get("code"),
                },
            )
            return await run_validation(
                session, tenant_id=tenant_id, connection_id=connection_id, actor=actor
            )
        if not await _apply_stack(session, tenant_id, conn, stack):
            return None
    # Assuming the role in the account proves control of it: only now is the account reserved
    # for this workspace (migration 0017).
    verified = any(c.id == "assume_role" and c.status == "ok" for c in result.checks)
    try:
        async with session.begin_nested():
            stored = await _store_result(session, connection_id, result, verified)
    except IntegrityError:
        result = ValidationResult(
            "failed",
            [
                Check(
                    "assume_role",
                    "Assume the Cloudoptics role",
                    "error",
                    "This AWS account is already connected to another Cloudoptics workspace.",
                    "Disconnect it there first, or ask that workspace's admin to invite you.",
                )
            ],
            error={"code": "AccountInUse", "check": "assume_role"},
        )
        stored = await _store_result(session, connection_id, result, verified=False)
    if not stored:
        return None
    if result.status != "failed":
        await funnel.emit(session, "connected", tenant_id=tenant_id)
        await session.execute(
            text(
                "INSERT INTO cloud_account (id, tenant_id, connection_id, aws_account_id, "
                "account_type) VALUES (:id, :t, :c, :acct, :type) "
                "ON CONFLICT (tenant_id, aws_account_id) DO NOTHING"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "acct": conn.aws_account_id,
                "type": account_type,
            },
        )
    await session.execute(
        text(
            "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, status, started_at, "
            "finished_at, error_code, est_customer_api_cost_usd) VALUES (:id, :t, :c, "
            "'connection.validate', :status, :started, now(), :code, :cost)"
        ),
        {
            "id": uuid7(),
            "t": tenant_id,
            "c": connection_id,
            "status": "failed" if result.status == "failed" else "succeeded",
            "started": started,
            "code": (result.error or {}).get("code"),
            "cost": result.est_cost_usd,
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
            target_id=str(connection_id),
            before={"status": conn.status},
            after={"status": result.status},
        )
    return result


async def mark_disconnected(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    previous: str,
    actor: Actor,
) -> None:
    # The account is no longer reserved for this workspace (migration 0017): connecting it
    # again needs a fresh assume_role with the new External ID (audit run 1, lead 10).
    await session.execute(
        text(
            "UPDATE cloud_connection SET status = 'disconnected', health = NULL, "
            "account_verified_at = NULL, pending_stack = NULL WHERE id = :c"
        ),
        {"c": connection_id},
    )
    # Its open recommendations can no longer be checked or verified.
    await session.execute(
        text(
            "UPDATE recommendation SET status = 'expired' WHERE status IN ('new', 'assigned', "
            "'in_progress') AND (resource_ref IN (SELECT id FROM resource WHERE connection_id = "
            ":c) OR (resource_ref IS NULL AND account_id IN (SELECT aws_account_id FROM "
            "cloud_account WHERE connection_id = :c)))"
        ),
        {"c": connection_id},
    )
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="connection.disconnected",
        target_type="cloud_connection",
        target_id=str(connection_id),
        before={"status": previous},
        after={"status": "disconnected"},
    )


# STS answers AccessDenied when the role is gone or no longer trusts Cloudoptics with this
# External ID; IAM says NoSuchEntity.
ROLE_GONE = {"AccessDenied", "AccessDeniedException", "NoSuchEntity", "NoSuchEntityException"}
RemovalCheck = Literal["disconnected", "kept", "unknown", "skipped"]


def secret_tag(external_id_enc: bytes) -> str:
    """Names the External ID a check was queued for without revealing it. A reconnect issues
    a new one, and a check queued before it must not touch the reconnected connection."""
    return hashlib.sha256(bytes(external_id_enc)).hexdigest()[:16]


async def check_stack_removed(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    stack_name: str,
    tag: str,
    final: bool,
) -> tuple[RemovalCheck, str | None]:
    """After a stack reported its deletion: disconnect only if Cloudoptics really can no
    longer assume the role. The callback alone proves nothing, since anyone who can read the
    External ID in the customer's account can send it (security audit run 1, leads 1 and 2).

    ``kept`` (the role still works) goes to the audit log when ``final``; ``unknown`` (AWS
    gave another error) is left to the caller to retry or record.
    """
    conn = await get_connection(session, connection_id)
    if (
        conn.status == "disconnected"
        or conn.stack_name != stack_name
        or secret_tag(conn.external_id_enc) != tag
    ):
        return "skipped", None
    external_id = get_encryptor().decrypt(bytes(conn.external_id_enc), tenant_id)
    factory = get_session_factory()

    def _assume() -> str | None:
        try:
            factory.assume(conn.role_arn, external_id, session_name(tenant_id, "verify"))
        except ClientError as exc:
            return str(exc.response.get("Error", {}).get("Code", "ClientError"))
        except BotoCoreError as exc:
            return type(exc).__name__
        return None

    code = await run_in_threadpool(_assume)
    if code in ROLE_GONE:
        await mark_disconnected(
            session, tenant_id, connection_id, conn.status, Actor(type="system")
        )
        return "disconnected", code
    if code is not None:
        return "unknown", code
    if final:
        await record_delete_ignored(session, tenant_id, connection_id, "role_still_assumable")
    return "kept", None


async def record_delete_ignored(
    session: AsyncSession, tenant_id: uuid.UUID, connection_id: uuid.UUID, reason: str
) -> None:
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor=Actor(type="system"),
        action="connection.delete_ignored",
        target_type="cloud_connection",
        target_id=str(connection_id),
        after={"reason": reason},
    )
