"""One Cloudoptics service account per Google Cloud connection (ADR-019, 20 Nov 2026).

Each connection gets its own service account in Cloudoptics' project
(``CLOUDOPTICS_GCP_SERVICE_ACCOUNT_PROJECT``), named ``co-<12 random hex>``: 15 characters,
within Google's 6-30 and its pattern ``[a-z]([-a-z0-9]*[a-z0-9])``. The customer shares the
billing export with that account and nothing else, and every read for the connection is made
as that account. So data shared with one workspace's account can only be read by that
workspace: typing another customer's dataset gets a 403. This replaced the dataset label.

- **Created** when the connection is (``ensure``), by the broker's federated identity, which
  holds Create Service Accounts on our project (infra/gcp/setup.sh). The account has no roles
  in our project except BigQuery Job User, which setup.sh grants once to every service account
  of the project (``principalSet://cloudresourcemanager.googleapis.com/projects/<number>/type/
  ServiceAccount``), so no IAM policy is written per connection.
- **Deleted** when the connection is disconnected, and for every connection when the workspace
  is deleted (``retire``). Deleting makes every grant the customer gave it stop working at
  once. If Google cannot be reached, the account is marked ``delete_pending`` and retried at
  the connection's or workspace's data deletion; each attempt is in the audit log.
- **Quota**: a project holds a limited number of service accounts (shown as "Service Account
  Count" on its Quotas page; deleted accounts do not count). When it is full, connecting fails
  with ``ServiceAccountQuota`` and a clear message, and the owner raises the quota (runbook).
"""

from __future__ import annotations

import logging
import secrets
import uuid

from fastapi import HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.providers.gcp.client import GcpApi, GcpError
from cloudoptics.providers.gcp.settings import get_gcp_settings

log = logging.getLogger(__name__)

PREFIX = "co-"
#: Google's rule for a service account ID (projects.serviceAccounts.create): 6-30 characters,
#: ``[a-z]([-a-z0-9]*[a-z0-9])``.
ACCOUNT_ID_PATTERN = r"[a-z]([-a-z0-9]*[a-z0-9])"

SELECT_ACCOUNT = text(
    "SELECT service_account_email, service_account_state FROM gcp_connection "
    "WHERE connection_id = :c"
)
SET_ACCOUNT = text(
    "UPDATE gcp_connection SET service_account_email = :email, "
    "service_account_state = 'active', updated_at = now() WHERE connection_id = :c"
)
SET_ROLE = text("UPDATE cloud_connection SET role_arn = :email WHERE id = :c")
SET_STATE = text(
    "UPDATE gcp_connection SET service_account_state = :state, updated_at = now() "
    "WHERE connection_id = :c"
)
LIVE_ACCOUNTS = text(
    "SELECT connection_id FROM gcp_connection WHERE service_account_email IS NOT NULL "
    "AND service_account_state IN ('active', 'delete_pending') ORDER BY connection_id"
)

QUOTA_MESSAGE = (
    "Cloudoptics has reached its Google Cloud limit for connection accounts. We have been "
    "told; try again later today."
)


def new_account_id() -> str:
    return PREFIX + secrets.token_hex(6)


def email_for(account_id: str, project: str) -> str:
    return f"{account_id}@{project}.iam.gserviceaccount.com"


async def ensure(
    session: AsyncSession, tenant_id: uuid.UUID, connection_id: uuid.UUID, actor: Actor
) -> str | None:
    """The connection's service account, created now when it has none (or its last one was
    deleted). None while Google Cloud is not switched on here. Raises an HTTP 503 problem
    when Google refuses, with the quota said plainly."""
    settings = get_gcp_settings()
    row = (await session.execute(SELECT_ACCOUNT, {"c": connection_id})).one_or_none()
    if row is not None and row.service_account_email and row.service_account_state == "active":
        return str(row.service_account_email)
    if not settings.enabled:
        return None
    if row is not None and row.service_account_state == "delete_pending":
        await retire(session, tenant_id, connection_id, actor)
    account_id = new_account_id()
    try:
        created = await GcpApi().create_service_account(
            settings.service_account_project,
            account_id,
            "Cloudoptics connection",
            f"Reads one Cloudoptics connection's Google Cloud data ({connection_id}).",
        )
    except GcpError as exc:
        quota = exc.quota
        log.warning(
            "gcp service account not created",
            extra={"code": exc.code, "quota": quota, "connection_id": str(connection_id)},
        )
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            QUOTA_MESSAGE
            if quota
            else "Google Cloud did not create this connection's account. Try again in a minute.",
        ) from exc
    email = str(created.get("email") or email_for(account_id, settings.service_account_project))
    await session.execute(SET_ACCOUNT, {"email": email, "c": connection_id})
    await session.execute(SET_ROLE, {"email": email, "c": connection_id})
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="gcp.service_account_created",
        target_type="cloud_connection",
        target_id=str(connection_id),
        after={"provider": "gcp", "serviceAccount": email},
    )
    return email


async def retire(
    session: AsyncSession, tenant_id: uuid.UUID, connection_id: uuid.UUID, actor: Actor
) -> str:
    """Delete the connection's service account: ``deleted``, ``delete_pending`` (Google could
    not be reached; retried later) or ``none`` (it had none)."""
    row = (await session.execute(SELECT_ACCOUNT, {"c": connection_id})).one_or_none()
    if row is None or not row.service_account_email or row.service_account_state == "deleted":
        return "none"
    email = str(row.service_account_email)
    outcome = "deleted"
    code: str | None = None
    try:
        await GcpApi().delete_service_account(email)
    except GcpError as exc:
        if not exc.not_found:  # already gone counts as deleted
            outcome, code = "delete_pending", exc.code
    await session.execute(SET_STATE, {"state": outcome, "c": connection_id})
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="gcp.service_account_deleted"
        if outcome == "deleted"
        else "gcp.service_account_delete_failed",
        target_type="cloud_connection",
        target_id=str(connection_id),
        after={"provider": "gcp", "serviceAccount": email, **({"code": code} if code else {})},
    )
    return outcome


async def retire_all(session: AsyncSession, tenant_id: uuid.UUID, actor: Actor) -> list[str]:
    """Every live account of the workspace (tenant scope set), before its data is deleted."""
    ids = [r.connection_id for r in await session.execute(LIVE_ACCOUNTS)]
    return [await retire(session, tenant_id, c, actor) for c in ids]
