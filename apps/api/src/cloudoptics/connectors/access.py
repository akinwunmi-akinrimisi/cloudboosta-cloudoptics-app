"""The single Cloudoptics stack: one-click links and the stack's callback.

A new account starts as a ``connection_intent``: a link with an External ID and no AWS
account yet. The person creates ``cloudoptics-access`` in whichever account they are signed
in to; its setup step calls ``POST /v1/cfn/callback`` with that account, and the intent
becomes a connection. Connections already on the older stacks get a ``c:<id>`` link instead
(same External ID, so their history stays). The callback is not signed in: it proves itself
with the External ID, which only Cloudoptics and the stack know.
"""

from __future__ import annotations

import hmac
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.connectors import aws
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.routes import access_link, mark_disconnected, start_validation
from cloudoptics.identity.deps import AdminAccess, Session
from cloudoptics.platform.api import ApiModel
from cloudoptics.platform.db import scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

intents_router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/connections/intents", tags=["connections"]
)
callback_router = APIRouter(prefix="/v1/cfn", tags=["connections"])


class Intent(ApiModel):
    id: uuid.UUID
    # waiting: the stack has not reported back yet; connected: it has (see connection_id).
    status: Literal["waiting", "connected"]
    connection_id: uuid.UUID | None
    error: str | None


class IntentSetup(ApiModel):
    intent: Intent
    quick_create_url: str | None
    # For setting it up by hand when quick-create links are off.
    template_url: str
    template_download_url: str
    external_id: str
    principal_arn: str
    resource_template_url: str
    callback_url: str
    connection_ref: str
    stack_region: str = aws.STACK_REGION


def _intent(row: object) -> Intent:
    connection_id = getattr(row, "connection_id", None)
    return Intent(
        id=row.id,  # type: ignore[attr-defined]
        status="connected" if connection_id else "waiting",
        connection_id=connection_id,
        error=getattr(row, "error", None),
    )


@intents_router.post("", response_model=IntentSetup, status_code=status.HTTP_201_CREATED)
async def create_intent(access: AdminAccess, session: Session) -> IntentSetup:
    settings = get_settings()
    external_id = aws.new_external_id()
    intent_id = uuid7()
    await session.execute(
        text(
            "INSERT INTO connection_intent (id, tenant_id, external_id_enc, created_by) "
            "VALUES (:id, :t, :ext, :by)"
        ),
        {
            "id": intent_id,
            "t": access.tenant_id,
            "ext": get_encryptor().encrypt(external_id, access.tenant_id),
            "by": access.user.id,
        },
    )
    ref = f"i:{intent_id}"
    return IntentSetup(
        intent=Intent(id=intent_id, status="waiting", connection_id=None, error=None),
        quick_create_url=access_link(
            settings, external_id=external_id, connection_ref=ref, tenant_id=access.tenant_id
        ),
        template_url=settings.cfn_access_template_url,
        template_download_url=f"{settings.cfn_template_download_base}/cloudoptics-access.yaml",
        external_id=external_id,
        principal_arn=settings.cloudoptics_principal_arn,
        resource_template_url=settings.cfn_resource_template_url,
        callback_url=f"{settings.public_api_url}/v1/cfn/callback",
        connection_ref=ref,
    )


@intents_router.get("/{intent_id}", response_model=Intent)
async def get_intent(intent_id: uuid.UUID, access: AdminAccess, session: Session) -> Intent:
    row = (
        await session.execute(
            text("SELECT id, connection_id, error FROM connection_intent WHERE id = :i"),
            {"i": intent_id},
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    return _intent(row)


class StackCallback(ApiModel):
    request_type: Literal["Create", "Update", "Delete"]
    ref: str = Field(pattern=r"^[ic]:[0-9a-f-]{36}$")
    tenant_id: uuid.UUID
    external_id: str = Field(min_length=32, max_length=128)
    account_id: str = Field(pattern=r"^\d{12}$")
    management: bool
    cur_bucket: str | None = Field(default=None, max_length=63)
    cur_prefix: str | None = Field(default=None, max_length=512)
    stack_name: str = Field(max_length=128)
    template_version: str = Field(pattern=r"^\d+(\.\d+){0,3}$")
    # How rolling the resource role out to member accounts went ("started", "failed: ...").
    member_rollout: str | None = Field(default=None, max_length=1000)


class CallbackResult(ApiModel):
    connection_id: uuid.UUID | None
    status: str


UNKNOWN = "This stack's link is not known to Cloudoptics. Open a new link from Cloudoptics."


def _same_secret(enc: bytes, tenant_id: uuid.UUID, external_id: str) -> bool:
    try:
        stored = get_encryptor().decrypt(bytes(enc), tenant_id)
    except Exception:  # a bad tenant or ciphertext is simply "unknown"
        return False
    return hmac.compare_digest(stored.encode(), external_id.encode())


async def _connection_from_intent(
    session: Session, body: StackCallback, intent_id: uuid.UUID, created_by: uuid.UUID | None
) -> uuid.UUID:
    """The account the stack is in becomes a connection (or a disconnected one comes back)."""
    existing = (
        await session.execute(
            text("SELECT id, status FROM cloud_connection WHERE aws_account_id = :acct"),
            {"acct": body.account_id},
        )
    ).one_or_none()
    if existing is not None and existing.status != "disconnected":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"AWS account {body.account_id} is already connected to this workspace. "
            "Delete this stack, then use Set up again on the Connections page.",
        )
    intent = (
        await session.execute(
            text("SELECT external_id_enc FROM connection_intent WHERE id = :i"), {"i": intent_id}
        )
    ).one()
    values = {
        "t": body.tenant_id,
        "acct": body.account_id,
        "type": "management" if body.management else "standalone",
        "arn": aws.billing_role_arn(body.account_id, body.stack_name),
        "ext": bytes(intent.external_id_enc),
        "stack": body.stack_name,
        "by": created_by,
    }
    connection_id: uuid.UUID
    if existing is not None:
        connection_id = existing.id
        await session.execute(
            text(
                "UPDATE cloud_connection SET status = 'pending', health = NULL, "
                "account_type = :type, role_arn = :arn, external_id_enc = :ext, "
                "stack_name = :stack, create_cur_export = true, permission_checks = '[]', "
                "last_error = NULL, last_validated_at = NULL, created_by = :by WHERE id = :id"
            ),
            {**values, "id": connection_id},
        )
    else:
        connection_id = uuid7()
        try:
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO cloud_connection (id, tenant_id, aws_account_id, "
                        "account_type, role_arn, external_id_enc, stack_name, create_cur_export, "
                        "created_by) VALUES (:id, :t, :acct, :type, :arn, :ext, :stack, true, :by)"
                    ),
                    {**values, "id": connection_id},
                )
        except IntegrityError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"AWS account {body.account_id} is already connected to another Cloudoptics "
                "workspace.",
            ) from exc
    await session.execute(
        text("UPDATE connection_intent SET connection_id = :c, error = NULL WHERE id = :i"),
        {"c": connection_id, "i": intent_id},
    )
    await audit.record(
        session,
        tenant_id=body.tenant_id,
        actor=Actor(type="user", id=created_by) if created_by else Actor(type="system"),
        action="connection.created" if existing is None else "connection.reconnected",
        target_type="cloud_connection",
        target_id=str(connection_id),
        after={
            "awsAccountId": body.account_id,
            "accountType": values["type"],
            "stack": body.stack_name,
        },
    )
    return connection_id


@callback_router.post("/callback", response_model=CallbackResult)
async def stack_callback(body: StackCallback, session: Session) -> CallbackResult | JSONResponse:
    """Called by the stack's setup step on create, update and delete."""
    await scope(session, tenant_id=body.tenant_id)
    kind, ref = body.ref.split(":", 1)
    ref_id = uuid.UUID(ref)
    connection_id: uuid.UUID | None
    if kind == "i":
        intent = (
            await session.execute(
                text(
                    "SELECT id, external_id_enc, connection_id, created_by "
                    "FROM connection_intent WHERE id = :i"
                ),
                {"i": ref_id},
            )
        ).one_or_none()
        if intent is None or not _same_secret(
            intent.external_id_enc, body.tenant_id, body.external_id
        ):
            raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN)
        connection_id = intent.connection_id
        if connection_id is None:
            if body.request_type == "Delete":  # a stack that never connected
                return CallbackResult(connection_id=None, status="ignored")
            try:
                async with session.begin_nested():
                    connection_id = await _connection_from_intent(
                        session, body, ref_id, intent.created_by
                    )
            except HTTPException as exc:
                # Kept for the waiting page, so the answer is a response, not a rollback.
                await session.execute(
                    text("UPDATE connection_intent SET error = :e WHERE id = :i"),
                    {"e": str(exc.detail), "i": ref_id},
                )
                return JSONResponse(
                    status_code=exc.status_code,
                    media_type="application/problem+json",
                    content={
                        "type": "about:blank",
                        "title": exc.detail,
                        "status": exc.status_code,
                        "instance": "/v1/cfn/callback",
                    },
                )
    else:
        connection_id = ref_id

    conn = (
        await session.execute(
            text(
                "SELECT id, aws_account_id, status, stack_name, external_id_enc "
                "FROM cloud_connection WHERE id = :c"
            ),
            {"c": connection_id},
        )
    ).one_or_none()
    if conn is None or not _same_secret(conn.external_id_enc, body.tenant_id, body.external_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, UNKNOWN)
    if conn.aws_account_id.strip() != body.account_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"This link is for AWS account {conn.aws_account_id}, but the stack is in "
            f"{body.account_id}. Sign in to {conn.aws_account_id} and open the link again.",
        )

    if body.request_type == "Delete":
        if conn.stack_name == body.stack_name and conn.status != "disconnected":
            await mark_disconnected(
                session, body.tenant_id, conn.id, conn.status, Actor(type="system")
            )
        if body.member_rollout and body.member_rollout.startswith("failed"):
            # The stack is gone but member accounts may still have a role trusting Cloudoptics.
            await audit.record(
                session,
                tenant_id=body.tenant_id,
                actor=Actor(type="system"),
                action="connection.member_roles_left",
                target_type="cloud_connection",
                target_id=str(conn.id),
                after={"reason": body.member_rollout},
            )
        return CallbackResult(connection_id=conn.id, status="disconnected")

    await session.execute(
        text(
            "UPDATE cloud_connection SET account_type = :type, role_arn = :arn, "
            "stack_name = :stack, "
            "stack_version = :version, create_cur_export = true, "
            "cur_bucket = COALESCE(:bucket, cur_bucket), "
            "cur_prefix = COALESCE(:prefix, cur_prefix), "
            "status = CASE WHEN status = 'disconnected' THEN 'pending' ELSE status END "
            "WHERE id = :c"
        ),
        {
            "type": "management" if body.management else "standalone",
            "arn": aws.billing_role_arn(body.account_id, body.stack_name),
            "stack": body.stack_name,
            "version": body.template_version,
            "bucket": body.cur_bucket,
            "prefix": body.cur_prefix,
            "c": conn.id,
        },
    )
    if conn.stack_name != body.stack_name or conn.status == "disconnected":
        await audit.record(
            session,
            tenant_id=body.tenant_id,
            actor=Actor(type="system"),
            action="connection.stack_changed",
            target_type="cloud_connection",
            target_id=str(conn.id),
            before={"stack": conn.stack_name},
            after={"stack": body.stack_name, "version": body.template_version},
        )
    if body.member_rollout and body.member_rollout.startswith(("failed", "skipped")):
        # Shown in the audit log, so the reason is visible without the customer's AWS logs.
        await audit.record(
            session,
            tenant_id=body.tenant_id,
            actor=Actor(type="system"),
            action="connection.member_rollout_failed",
            target_type="cloud_connection",
            target_id=str(conn.id),
            after={"reason": body.member_rollout},
        )
    await start_validation(session, body.tenant_id, conn.id, Actor(type="system"))
    return CallbackResult(connection_id=conn.id, status="accepted")
