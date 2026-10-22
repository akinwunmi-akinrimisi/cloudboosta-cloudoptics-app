"""Connection lifecycle: create, validate, disconnect."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.connectors.aws import SessionFactory, session_name
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.validation import ValidationResult, validate_billing_access
from cloudoptics.platform.ids import uuid7

CONNECTION_COLUMNS = (
    "id, tenant_id, aws_account_id, account_type, role_arn, stack_name, create_cur_export, "
    "cur_bucket, cur_prefix, cur_region, cur_export_arn, status, permission_checks, "
    "last_error, last_validated_at, created_at"
)

_session_factory: SessionFactory | None = None


def get_session_factory() -> SessionFactory:
    global _session_factory
    if _session_factory is None:
        _session_factory = SessionFactory()
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
            text(
                f"SELECT {CONNECTION_COLUMNS}, external_id_enc FROM cloud_connection "  # noqa: S608
                "WHERE id = :c"
            ),
            {"c": connection_id},
        )
    ).one_or_none()
    if row is None:
        raise ConnectionNotFoundError(connection_id)
    return row


async def run_validation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    actor: Actor,
) -> ValidationResult:
    """Validate one connection inside the caller's transaction (tenant scope already set)."""
    conn = await get_connection(session, connection_id)
    external_id = get_encryptor().decrypt(bytes(conn.external_id_enc), tenant_id)
    factory = get_session_factory()
    started = datetime.now(UTC)

    def _open() -> Any:
        return factory.assume(conn.role_arn, external_id, session_name(tenant_id, "validate"))

    result = await run_in_threadpool(
        validate_billing_access,
        _open,
        aws_account_id=conn.aws_account_id,
        account_type=conn.account_type,
        create_cur_export=conn.create_cur_export,
    )
    await session.execute(
        text(
            "UPDATE cloud_connection SET status = :status, "
            "permission_checks = CAST(:checks AS jsonb), last_error = CAST(:err AS jsonb), "
            "last_validated_at = now(), cur_bucket = COALESCE(:bucket, cur_bucket), "
            "cur_prefix = COALESCE(:prefix, cur_prefix), "
            "cur_region = COALESCE(:region, cur_region), "
            "cur_export_arn = COALESCE(:export_arn, cur_export_arn) WHERE id = :c"
        ),
        {
            "status": result.status,
            "checks": json.dumps(result.checks_json()),
            "err": json.dumps(result.error) if result.error else None,
            "bucket": result.cur_bucket,
            "prefix": result.cur_prefix,
            "region": result.cur_region,
            "export_arn": result.cur_export_arn,
            "c": connection_id,
        },
    )
    if result.status != "failed":
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
                "type": conn.account_type,
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
