"""Deleting a connection's data or a whole workspace, with a verification record (ADM-7).

``TABLES`` lists every table that holds workspace data, with how its rows belong to one
connection. A test compares it with the database after all migrations, so a new table with
a ``tenant_id`` column fails CI until it is listed here.

What is deleted:

- **Connection** (after it is disconnected): the connection row, and with it (``ON DELETE
  CASCADE``) its accounts, sync runs, bills, cost tables, resources, metrics, commitments
  and links; its recommendations (with their history) and its accounts' anomalies; tag
  sets no cost row uses any more. Workspace-wide rows stay (members, budgets, settings,
  monthly reports, the email log).
- **Workspace** (after the grace period): the ``tenant`` row, and with it every table in
  ``TABLES`` by ``ON DELETE CASCADE``.

What is kept (``KEPT``): the audit log (append-only and hash-chained; PRD US-8.3 keeps
legal records, docs/09 §2.4 keeps it at least 12 months, ADR-015), and the deletion
records themselves. User accounts (``app_user``) are not workspace data: a person may be
in other workspaces, and signs in again to an empty account otherwise.

Counts before and after run in the workspace's RLS scope, like the deletes. A count that
is not zero after the delete aborts the transaction, so nothing is half-deleted and the
job tries again.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import (
    ColumnElement,
    and_,
    any_,
    bindparam,
    column,
    delete,
    func,
    select,
    table,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Text
from starlette.concurrency import run_in_threadpool

from cloudoptics.platform.settings import get_settings

log = logging.getLogger(__name__)

# How one table's rows belong to a connection:
#   "id" / "connection_id": that column holds the connection's ID;
#   "resource": resource_id is one of the connection's resources;
#   "account": (provider, provider_account_id) is one of the connection's accounts;
#   "recommendation": the connection's recommendations (by account or resource);
#   "recommendation_event": history of those recommendations;
#   None: workspace-wide, kept when a connection's data is deleted.
ConnectionScope = Literal[
    "id",
    "connection_id",
    "resource",
    "account",
    "recommendation",
    "recommendation_event",
]


@dataclass(frozen=True)
class TenantTable:
    connection: ConnectionScope | None
    # The column that holds the workspace ID (``id`` on ``tenant`` itself).
    tenant_column: str = "tenant_id"


TABLES: dict[str, TenantTable] = {
    "tenant": TenantTable(None, tenant_column="id"),
    "membership": TenantTable(None),
    "invite": TenantTable(None),
    "cloud_connection": TenantTable("id"),
    "cloud_account": TenantTable("connection_id"),
    # Where a Google Cloud connection's bill is (migration 0044, ADR-019).
    "gcp_connection": TenantTable("connection_id"),
    "azure_connection": TenantTable("connection_id"),  # ADR-020
    "connection_intent": TenantTable("connection_id"),
    "sync_run": TenantTable("connection_id"),
    "ingest_batch": TenantTable("connection_id"),
    "billing_file": TenantTable("connection_id"),
    "cost_daily": TenantTable("connection_id"),
    "cost_resource_daily": TenantTable("connection_id"),
    "cost_snapshot_ce": TenantTable("connection_id"),
    "cost_reconciliation": TenantTable("connection_id"),
    "resource": TenantTable("connection_id"),
    "resource_metric": TenantTable("resource"),
    "commitment": TenantTable("connection_id"),
    "commitment_snapshot": TenantTable("connection_id"),
    "recommendation": TenantTable("recommendation"),
    "recommendation_event": TenantTable("recommendation_event"),
    "anomaly": TenantTable("account"),
    # Shared by every connection's cost rows; unused ones go with a connection's data.
    "tag_set": TenantTable(None),
    "export_job": TenantTable(None),  # export files hold the workspace's data
    "aws_api_usage": TenantTable(None),
    "budget": TenantTable(None),
    "credit_balance": TenantTable(None),
    "alert_setting": TenantTable(None),
    "rule_setting": TenantTable(None),
    "notification": TenantTable(None),
    "notification_preference": TenantTable(None),
    "monthly_report": TenantTable(None),
    "report_setting": TenantTable(None),
    # Staff break-glass grants and their requests; the audit log keeps the record of each.
    "support_access_grant": TenantTable(None),
    "support_access_request": TenantTable(None),
    # First-time funnel steps and the workspace's first-touch campaign (SITE-15).
    "funnel_event": TenantTable(None),
}

# Tables with a tenant_id that a deletion keeps, and why.
KEPT: dict[str, str] = {
    "audit_log": "append-only, hash-chained record kept for its retention period (ADR-015)",
    "data_deletion": "the deletion requests and their verification records",
    # Migration 0031 (ETags): a workspace ID and a counter, no customer data. Deleting the
    # workspace row itself bumps them at commit, so they would only be re-created.
    "tenant_data_version": "per-workspace data-version counter for ETags; ID and number only",
    "data_version_pending": "commit-time queue for the data-version counter; ID and number only",
}

_UUIDS = ARRAY(UUID(as_uuid=True))
_TEXTS = ARRAY(Text())

CONNECTION_PROVIDER = text("SELECT provider FROM cloud_connection WHERE id = :c")
CONNECTION_ACCOUNTS = text(
    "SELECT a.provider_account_id FROM cloud_account a WHERE a.connection_id = :c "
    "UNION SELECT c.provider_account_id FROM cloud_connection c WHERE c.id = :c "
    "AND NOT EXISTS (SELECT 1 FROM cloud_account o WHERE o.provider = c.provider "
    "AND o.provider_account_id = c.provider_account_id AND o.connection_id <> c.id)"
)
CONNECTION_RESOURCES = text("SELECT id FROM resource WHERE connection_id = :c")
CONNECTION_RECOMMENDATIONS = text(
    "SELECT id FROM recommendation WHERE (provider = :p "
    "AND provider_account_id = ANY(CAST(:a AS text[]))) OR resource_ref = ANY(CAST(:r AS uuid[]))"
)
DELETE_UNUSED_TAG_SETS = text(
    "DELETE FROM tag_set t WHERE NOT EXISTS (SELECT 1 FROM cost_daily d "
    "WHERE d.tenant_id = t.tenant_id AND d.tag_set_hash = t.hash) "
    "AND NOT EXISTS (SELECT 1 FROM cost_resource_daily r "
    "WHERE r.tenant_id = t.tenant_id AND r.tag_set_hash = t.hash)"
)
COUNT_AUDIT = text("SELECT count(*) FROM audit_log WHERE tenant_id = :t")


class DeletionIncompleteError(RuntimeError):
    """Rows were still there after the delete; the transaction is rolled back."""


@dataclass
class ConnectionTargets:
    connection_id: uuid.UUID
    provider: str = "aws"
    accounts: list[str] = field(default_factory=list)
    resources: list[uuid.UUID] = field(default_factory=list)
    recommendations: list[uuid.UUID] = field(default_factory=list)


def _in(name: str, values: list[Any], array: Any) -> ColumnElement[bool]:
    return column(name) == any_(bindparam(None, values, type_=array))


def _account_filter(t: ConnectionTargets) -> ColumnElement[bool]:
    return and_(column("provider") == t.provider, _in("provider_account_id", t.accounts, _TEXTS))


def _connection_filter(spec: TenantTable, t: ConnectionTargets) -> ColumnElement[bool]:
    match spec.connection:
        case "id":
            return column("id") == t.connection_id
        case "connection_id":
            return column("connection_id") == t.connection_id
        case "resource":
            return _in("resource_id", t.resources, _UUIDS)
        case "account":
            return _account_filter(t)
        case "recommendation":
            return _in("id", t.recommendations, _UUIDS)
        case "recommendation_event":
            return _in("recommendation_id", t.recommendations, _UUIDS)
    raise ValueError(f"{spec} has no connection scope")


async def _count(session: AsyncSession, name: str, where: ColumnElement[bool]) -> int:
    query = select(func.count()).select_from(table(name)).where(where)
    return int((await session.execute(query)).scalar_one())


async def tenant_counts(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, int]:
    return {
        name: await _count(session, name, column(spec.tenant_column) == tenant_id)
        for name, spec in TABLES.items()
    }


async def connection_targets(session: AsyncSession, connection_id: uuid.UUID) -> ConnectionTargets:
    targets = ConnectionTargets(connection_id)
    provider = (await session.execute(CONNECTION_PROVIDER, {"c": connection_id})).scalar()
    targets.provider = str(provider or targets.provider)
    targets.accounts = sorted(
        str(a).strip()
        for a in (await session.execute(CONNECTION_ACCOUNTS, {"c": connection_id})).scalars()
    )
    targets.resources = list(
        (await session.execute(CONNECTION_RESOURCES, {"c": connection_id})).scalars()
    )
    targets.recommendations = list(
        (
            await session.execute(
                CONNECTION_RECOMMENDATIONS,
                {"p": targets.provider, "a": targets.accounts, "r": targets.resources},
            )
        ).scalars()
    )
    return targets


async def connection_counts(session: AsyncSession, targets: ConnectionTargets) -> dict[str, int]:
    return {
        name: await _count(session, name, _connection_filter(spec, targets))
        for name, spec in TABLES.items()
        if spec.connection is not None
    }


def _verification(before: dict[str, int], after: dict[str, int]) -> dict[str, Any]:
    left = {name: n for name, n in after.items() if n}
    if left:
        raise DeletionIncompleteError(f"rows left after the delete: {left}")
    return {
        "tables": {name: {"before": before[name], "after": after[name]} for name in before},
        "rowsDeleted": sum(before.values()),
        "verified": True,
    }


async def delete_connection_data(session: AsyncSession, connection_id: uuid.UUID) -> dict[str, Any]:
    """Inside the workspace's scope; the caller checked the connection is disconnected."""
    targets = await connection_targets(session, connection_id)
    before = await connection_counts(session, targets)
    await session.execute(
        delete(table("recommendation")).where(_in("id", targets.recommendations, _UUIDS))
    )
    await session.execute(delete(table("anomaly")).where(_account_filter(targets)))
    await session.execute(delete(table("cloud_connection")).where(column("id") == connection_id))
    tag_sets = await session.execute(DELETE_UNUSED_TAG_SETS)
    after = await connection_counts(session, targets)
    result = _verification(before, after)
    result["accounts"] = targets.accounts
    result["unusedTagSetsDeleted"] = int(getattr(tag_sets, "rowcount", 0) or 0)
    return result


async def delete_tenant_data(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Inside the workspace's scope. Every table in ``TABLES`` goes by cascade from ``tenant``."""
    before = await tenant_counts(session, tenant_id)
    await session.execute(delete(table("tenant")).where(column("id") == tenant_id))
    after = await tenant_counts(session, tenant_id)
    result = _verification(before, after)
    result["kept"] = {
        "audit_log": int((await session.execute(COUNT_AUDIT, {"t": tenant_id})).scalar_one()),
    }
    await _remove_normalised_files(tenant_id)
    return result


async def _remove_normalised_files(tenant_id: uuid.UUID) -> None:
    """Local development writes normalised Parquet per workspace (``normalised_dir``)."""
    base = get_settings().normalised_dir
    if not base:
        return
    folder = Path(base) / f"tenant={tenant_id}"
    if folder.is_dir():
        await run_in_threadpool(shutil.rmtree, folder)
