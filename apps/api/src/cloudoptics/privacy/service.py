"""Deletion requests: schedule, cancel and carry out, with the emails to workspace admins.

A connection's data is deleted as soon as it is requested (the connection is already
disconnected and the admin typed its account ID). A workspace is deleted after a grace
period (``tenant_deletion_grace_days``, 7 by default; docs/08 §1.7), during which any admin
can cancel; the periodic job ``privacy:deletion.poll`` then runs it on the worker.
"""

from __future__ import annotations

import html
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.audit import log as audit
from cloudoptics.connectors import aws
from cloudoptics.notifications.email import Email, EmailError, get_mailer
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.privacy.deletion import delete_connection_data, delete_tenant_data
from cloudoptics.privacy.removal import Removal, removal_lines, removal_steps
from cloudoptics.providers.naming import connection_label, provider_label

log = logging.getLogger(__name__)

STACKS_URL = (
    f"https://{aws.STACK_REGION}.console.aws.amazon.com/cloudformation/home"
    f"?region={aws.STACK_REGION}#/stacks?filteringText=cloudoptics"
)

SELECT_DELETIONS = text(
    "SELECT d.id, d.kind, d.connection_id, d.target_label, d.status, d.requested_at, "
    "u.email AS requested_by_email, d.scheduled_for, d.cancelled_at, d.completed_at, "
    "d.verification, d.error FROM data_deletion d LEFT JOIN app_user u ON u.id = d.requested_by "
    "WHERE d.tenant_id = :t AND (CAST(:id AS uuid) IS NULL OR d.id = :id) "
    "AND (CAST(:kind AS text) IS NULL OR d.kind = :kind) "
    "AND (CAST(:status AS text) IS NULL OR d.status = :status) "
    "ORDER BY d.requested_at DESC, d.id DESC"
)
INSERT_DELETION = text(
    "INSERT INTO data_deletion (id, tenant_id, kind, connection_id, target_label, requested_by, "
    "scheduled_for) VALUES (:id, :t, :kind, :c, :label, :by, :at)"
)
LOCK_DELETION = text(
    "SELECT id, kind, connection_id, target_label, status, scheduled_for, requested_by "
    "FROM data_deletion WHERE id = :id FOR UPDATE"
)
COMPLETE_DELETION = text(
    "UPDATE data_deletion SET status = 'completed', completed_at = now(), "
    "verification = CAST(:v AS jsonb), error = NULL WHERE id = :id"
)
CANCEL_DELETION = text(
    "UPDATE data_deletion SET status = 'cancelled', cancelled_at = now(), cancelled_by = :by "
    "WHERE id = :id AND status = 'scheduled'"
)
SET_TENANT_STATUS = text("UPDATE tenant SET status = :s WHERE id = :t")
ADMIN_EMAILS = text(
    "SELECT u.email FROM membership m JOIN app_user u ON u.id = m.user_id "
    "WHERE m.tenant_id = :t AND m.role = 'admin' ORDER BY u.email"
)
TENANT_NAME = text("SELECT name, status FROM tenant WHERE id = :t")
STACK_ACCOUNTS = text(
    "SELECT provider_account_id AS aws_account_id, account_type, stack_name "
    "FROM cloud_connection WHERE provider = 'aws' ORDER BY provider_account_id"
)
CONNECTION_FOR_DELETE = text(
    "SELECT id, provider_account_id AS aws_account_id, provider, account_type, status "
    "FROM cloud_connection "
    "WHERE id = :c FOR UPDATE"
)
RECORD_NOTIFICATION = text(
    "INSERT INTO notification (id, tenant_id, kind, dedupe_key, recipients, subject, status, "
    "error) VALUES (:id, :t, :kind, :k, :to, :subject, :status, :error) "
    "ON CONFLICT (tenant_id, dedupe_key) DO NOTHING"
)


@dataclass(frozen=True)
class StackSteps:
    aws_account_id: str
    account_type: str
    stacks: list[str]


def stacks_to_delete(account_type: str, stack_name: str) -> list[str]:
    """The CloudFormation stacks a connection's setup created in the customer's account."""
    if stack_name == aws.ACCESS_STACK_NAME:
        return [aws.ACCESS_STACK_NAME]
    stacks = [stack_name, aws.RESOURCE_STACK_NAME]
    if account_type == "management":
        stacks.append(aws.STACKSET_STACK_NAME)
    return stacks


async def stack_steps(session: AsyncSession) -> list[StackSteps]:
    return [
        StackSteps(r.aws_account_id, r.account_type, stacks_to_delete(r.account_type, r.stack_name))
        for r in await session.execute(STACK_ACCOUNTS)
    ]


def stack_lines(steps: list[StackSteps]) -> list[str]:
    if not steps:
        return []
    lines = [
        "Cloudoptics cannot delete anything in your AWS accounts. To remove our read-only "
        "access, sign in to each account below, open CloudFormation in "
        f"{aws.STACK_REGION} ({STACKS_URL}) and delete these stacks:",
    ]
    lines += [f"- AWS account {s.aws_account_id}: {', '.join(s.stacks)}" for s in steps]
    lines.append(
        "AWS keeps the S3 bucket with your billing export after the stack is deleted. Empty "
        "and delete it in the S3 console if you no longer need the files."
    )
    return lines


def _html(lines: list[str]) -> str:
    paragraphs: list[str] = []
    items: list[str] = []
    for line in lines:
        if line.startswith("- "):
            items.append(f"<li>{html.escape(line[2:])}</li>")
            continue
        if items:
            paragraphs.append(f"<ul>{''.join(items)}</ul>")
            items = []
        if line:
            paragraphs.append(f"<p>{html.escape(line)}</p>")
    if items:
        paragraphs.append(f"<ul>{''.join(items)}</ul>")
    return "".join(paragraphs)


def _email(to: list[str], subject: str, lines: list[str], tag: str) -> Email:
    return Email(to, subject, "\n".join(lines), _html(lines), tag=tag)


def scheduled_email(
    to: list[str],
    workspace: str,
    when: datetime,
    by: str,
    steps: list[StackSteps],
    removals: list[Removal] | None = None,
) -> Email:
    """``steps``: the AWS stacks; ``removals``: what to remove in the other clouds."""
    link = f"{get_settings().app_url.rstrip('/')}/settings?section=workspace"
    lines = [
        f"{by} asked to delete the Cloudoptics workspace {workspace} and all of its data.",
        "",
        f"It will be deleted on {when:%d %B %Y at %H:%M} UTC. Until then any admin can "
        f"cancel it in Settings, Workspace: {link}",
        "",
        *stack_lines(steps),
        *_then(removal_lines(removals or [])),
        "",
        "If you did not expect this, cancel the deletion and check who has the admin role.",
    ]
    return _email(
        to, f"Workspace {workspace} will be deleted on {when:%d %B %Y}", lines, "deletion"
    )


def cancelled_email(to: list[str], workspace: str, by: str) -> Email:
    lines = [
        f"{by} cancelled the deletion of the Cloudoptics workspace {workspace}. "
        "Nothing was deleted."
    ]
    return _email(to, f"Deletion of workspace {workspace} cancelled", lines, "deletion")


def tenant_deleted_email(
    to: list[str],
    workspace: str,
    verification: dict[str, Any],
    steps: list[StackSteps],
    removals: list[Removal] | None = None,
) -> Email:
    lines = [
        f"The Cloudoptics workspace {workspace} has been deleted: "
        f"{verification['rowsDeleted']:,} records across {len(verification['tables'])} tables, "
        "each checked to be empty afterwards.",
        "",
        "Backups age out within 30 days. The workspace's audit log is kept, unchanged, for its "
        "retention period as a security record.",
        "",
        *stack_lines(steps),
        *_then(removal_lines(removals or [])),
    ]
    return _email(to, f"Workspace {workspace} deleted", lines, "deletion")


def _then(lines: list[str]) -> list[str]:
    """A blank line before a block that is there."""
    return ["", *lines] if lines else []


def connection_deleted_email(
    to: list[str],
    account: str,
    verification: dict[str, Any],
    provider: str | None = None,
    account_type: str | None = None,
) -> Email:
    """``provider`` and ``account_type`` name the account ("Azure subscription …"); a
    missing provider is AWS."""
    label = connection_label(provider, account, account_type)
    again = (
        "Reconnecting the account starts again from the bills in your S3 bucket."
        if (provider or "aws") == "aws"
        else f"Connecting it again loads its bills again from {provider_label(provider)}."
    )
    lines = [
        f"Cloudoptics deleted the data it held for {label}: "
        f"{verification['rowsDeleted']:,} records (bills, costs, resources, metrics, "
        "recommendations and anomalies), each table checked to be empty afterwards.",
        "",
        f"The audit log keeps the record of the deletion. {again}",
    ]
    return _email(to, f"Data for {label} deleted", lines, "deletion")


async def admin_emails(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    return [str(e) for e in (await session.execute(ADMIN_EMAILS, {"t": tenant_id})).scalars()]


async def send(
    email: Email, session: AsyncSession | None = None, tenant_id: uuid.UUID | None = None
) -> str:
    """Send to the admins; record it in the email log while the workspace still exists."""
    status, error = "skipped", None
    if email.to:
        try:
            await get_mailer().send(email)
            status = "sent"
        except EmailError as exc:
            status, error = "failed", str(exc)
            log.warning("deletion email not sent: %s", exc)
    if session is not None and tenant_id is not None:
        await session.execute(
            RECORD_NOTIFICATION,
            {
                "id": uuid7(),
                "t": tenant_id,
                "kind": "data_deletion",
                "k": f"deletion:{uuid7()}",
                "to": email.to,
                "subject": email.subject,
                "status": status,
                "error": error,
            },
        )
    return status


async def list_deletions(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    deletion_id: uuid.UUID | None = None,
    kind: str | None = None,
    status: str | None = None,
) -> list[Any]:
    params = {"t": tenant_id, "id": deletion_id, "kind": kind, "status": status}
    return list((await session.execute(SELECT_DELETIONS, params)).all())


async def create_request(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    kind: str,
    label: str,
    by: uuid.UUID,
    at: datetime,
    connection_id: uuid.UUID | None = None,
) -> uuid.UUID:
    deletion_id = uuid7()
    await session.execute(
        INSERT_DELETION,
        {
            "id": deletion_id,
            "t": tenant_id,
            "kind": kind,
            "c": connection_id,
            "label": label,
            "by": by,
            "at": at,
        },
    )
    return deletion_id


def grace_period() -> timedelta:
    return timedelta(days=get_settings().tenant_deletion_grace_days)


async def cancel(
    session: AsyncSession, tenant_id: uuid.UUID, deletion_id: uuid.UUID, by: uuid.UUID
) -> bool:
    done = await session.execute(CANCEL_DELETION, {"id": deletion_id, "by": by})
    if not getattr(done, "rowcount", 0):
        return False
    await session.execute(SET_TENANT_STATUS, {"s": "active", "t": tenant_id})
    return True


async def mark_tenant_deleting(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    await session.execute(SET_TENANT_STATUS, {"s": "deleting", "t": tenant_id})


async def run_connection_deletion(
    session: AsyncSession, tenant_id: uuid.UUID, deletion_id: uuid.UUID, connection_id: uuid.UUID
) -> dict[str, Any]:
    verification = await delete_connection_data(session, connection_id)
    await _complete(session, deletion_id, verification)
    return verification


async def _complete(
    session: AsyncSession, deletion_id: uuid.UUID, verification: dict[str, Any]
) -> None:
    await session.execute(COMPLETE_DELETION, {"id": deletion_id, "v": json.dumps(verification)})


@dataclass(frozen=True)
class Outcome:
    status: str
    email: Email | None = None
    # The email can be logged only while the workspace exists.
    log_email: bool = True


async def run_due(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    deletion_id: uuid.UUID,
    now: datetime,
    actor: audit.Actor | None = None,
) -> Outcome:
    """Carry out one request if it is still scheduled and due, in the workspace's scope.

    The worker runs it as the system; a connection deleted straight from the request runs
    it as the admin who asked, so one audit entry says who and what.
    """
    row = (await session.execute(LOCK_DELETION, {"id": deletion_id})).one_or_none()
    if row is None or row.status != "scheduled":
        return Outcome("skipped")
    if row.scheduled_for > now:
        return Outcome("not_due")
    actor = actor or audit.Actor(type="system")
    if row.kind == "connection":
        conn = (
            await session.execute(CONNECTION_FOR_DELETE, {"c": row.connection_id})
        ).one_or_none()
        if conn is not None and conn.status != "disconnected":
            # Reconnected since the request: its data is in use again.
            await session.execute(CANCEL_DELETION, {"id": deletion_id, "by": None})
            return Outcome("cancelled")
        verification = await run_connection_deletion(
            session, tenant_id, deletion_id, row.connection_id
        )
        await audit.record(
            session,
            tenant_id=tenant_id,
            actor=actor,
            action="connection.data_deleted",
            target_type="cloud_connection",
            target_id=str(row.connection_id),
            # The provider names the account in the audit log ("Deleted an Azure subscription's
            # data"); the connection itself is gone now.
            after={**verification, "provider": conn.provider if conn is not None else "aws"},
        )
        email = connection_deleted_email(
            await admin_emails(session, tenant_id),
            row.target_label,
            verification,
            provider=conn.provider if conn is not None else None,
            account_type=conn.account_type if conn is not None else None,
        )
        return Outcome("completed", email)

    tenant = (await session.execute(TENANT_NAME, {"t": tenant_id})).one_or_none()
    name = tenant.name if tenant is not None else row.target_label
    recipients = await admin_emails(session, tenant_id)
    steps = await stack_steps(session)
    removals = await removal_steps(session, tenant_id)
    verification = await delete_tenant_data(session, tenant_id)
    await _complete(session, deletion_id, verification)
    await audit.record(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="tenant.data_deleted",
        target_type="tenant",
        target_id=str(tenant_id),
        after=verification,
    )
    email = tenant_deleted_email(recipients, name, verification, steps, removals)
    return Outcome("completed", email, False)
