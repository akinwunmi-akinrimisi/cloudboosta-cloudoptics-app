"""The single Cloudoptics stack: one-click links and the stack's callback.

A new account starts as a ``connection_intent``: a link with an External ID and no AWS
account yet. The person creates ``cloudoptics-access`` in whichever account they are signed
in to; its setup step calls ``POST /v1/cfn/callback`` with that account, and the intent
becomes a connection. Connections already on the older stacks get a ``c:<id>`` link instead
(same External ID, so their history stays). The callback is not signed in: it proves itself
with the External ID. That value is also readable in the customer's account (IAM trust
policies, the setup function's settings) and stays known to admins who leave, so it is not
enough on its own (security audit run 1): unused links expire after 7 days, can be withdrawn
and stop working when their maker is no longer an admin; a reported delete disconnects only
once the role can no longer be assumed; values reported for an existing connection replace
the stored ones only once their role works; and a disconnected connection comes back only
through an admin (Reconnect or a new link, both with a new External ID).
"""

from __future__ import annotations

import hmac
import json
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cloudoptics.analytics import funnel
from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.connectors import aws
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.routes import access_link, start_removal_check, start_validation
from cloudoptics.identity.deps import AdminAccess, Session
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.db import scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.ratelimit import per_ip
from cloudoptics.platform.settings import get_settings

intents_router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/connections/intents", tags=["connections"]
)
# Not signed in: limited per client address before anything else runs (WBS API-8).
callback_router = APIRouter(
    prefix="/v1/cfn",
    tags=["connections"],
    dependencies=[
        Depends(per_ip("callback", lambda: get_settings().rate_limit_callback_per_minute))
    ],
)


INTENT_LIFETIME_DAYS = 7  # the column default in migration 0028


class Intent(ApiModel):
    id: uuid.UUID
    # waiting: the stack has not reported back yet; connected: it has (see connection_id).
    status: Literal["waiting", "connected"]
    connection_id: uuid.UUID | None
    error: str | None
    # After this the link no longer connects an account (an unused link only).
    expires_at: datetime | None = None


class IntentSummary(ApiModel):
    """One link in the admin's list of links (``GET .../connections/intents``)."""

    id: uuid.UUID
    status: Literal["waiting", "connected", "expired", "revoked"]
    connection_id: uuid.UUID | None
    created_at: datetime
    created_by: uuid.UUID | None
    expires_at: datetime
    revoked_at: datetime | None


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


EXPIRED = "This link has expired. Open a new link from Cloudoptics."
REVOKED = "This link was withdrawn by a workspace admin. Open a new link from Cloudoptics."
NOT_ADMIN = (
    "The person who made this link is no longer an admin of this workspace. "
    "Ask an admin to open a new link from Cloudoptics."
)

LIST_INTENTS = (
    "SELECT id, connection_id, error, created_at, created_by, expires_at, revoked_at, "
    "expires_at <= now() AS expired FROM connection_intent ORDER BY created_at DESC"
)
GET_INTENT = (
    "SELECT id, connection_id, error, created_at, created_by, expires_at, revoked_at, "
    "expires_at <= now() AS expired FROM connection_intent WHERE id = :i"
)


def _unused_problem(row: Any) -> str | None:
    """Why an unused link can no longer connect an account (None: it still can)."""
    if row.connection_id is not None:
        return None
    if row.revoked_at is not None:
        return REVOKED
    if row.expired:
        return EXPIRED
    return None


def _intent(row: Any) -> Intent:
    return Intent(
        id=row.id,
        status="connected" if row.connection_id else "waiting",
        connection_id=row.connection_id,
        error=row.error or _unused_problem(row),
        expires_at=row.expires_at,
    )


def _summary(row: Any) -> IntentSummary:
    state: Literal["waiting", "connected", "expired", "revoked"] = "waiting"
    if row.connection_id:
        state = "connected"
    elif row.revoked_at is not None:
        state = "revoked"
    elif row.expired:
        state = "expired"
    return IntentSummary(
        id=row.id,
        status=state,
        connection_id=row.connection_id,
        created_at=row.created_at,
        created_by=row.created_by,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )


@intents_router.post("", response_model=IntentSetup, status_code=status.HTTP_201_CREATED)
async def create_intent(access: AdminAccess, session: Session) -> IntentSetup:
    settings = get_settings()
    external_id = aws.new_external_id()
    intent_id = uuid7()
    expires_at = (
        await session.execute(
            text(
                "INSERT INTO connection_intent (id, tenant_id, external_id_enc, created_by) "
                "VALUES (:id, :t, :ext, :by) RETURNING expires_at"
            ),
            {
                "id": intent_id,
                "t": access.tenant_id,
                "ext": get_encryptor().encrypt(external_id, access.tenant_id),
                "by": access.user.id,
            },
        )
    ).scalar_one()
    await funnel.emit(session, "connect_started", tenant_id=access.tenant_id)
    ref = f"i:{intent_id}"
    return IntentSetup(
        intent=Intent(
            id=intent_id, status="waiting", connection_id=None, error=None, expires_at=expires_at
        ),
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


@intents_router.get("", response_model=list[IntentSummary])
async def list_intents(access: AdminAccess, session: Session) -> list[IntentSummary]:
    rows = await session.execute(text(LIST_INTENTS))
    return [_summary(r) for r in rows]


@intents_router.get("/{intent_id}", response_model=Intent)
async def get_intent(intent_id: uuid.UUID, access: AdminAccess, session: Session) -> Intent:
    row = (
        await session.execute(
            text(GET_INTENT),
            {"i": intent_id},
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    return _intent(row)


@intents_router.delete("/{intent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_intent(
    intent_id: uuid.UUID, request: Request, access: AdminAccess, session: Session
) -> Response:
    """Withdraw a link that has not connected an account yet (it can then never connect one)."""
    row = (
        await session.execute(
            text("SELECT connection_id, revoked_at FROM connection_intent WHERE id = :i"),
            {"i": intent_id},
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")
    if row.connection_id is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This link already connected an account. Disconnect the connection instead.",
        )
    if row.revoked_at is None:
        await session.execute(
            text(
                "UPDATE connection_intent SET revoked_at = now() "
                "WHERE id = :i AND connection_id IS NULL AND revoked_at IS NULL"
            ),
            {"i": intent_id},
        )
        await audit.record(
            session,
            tenant_id=access.tenant_id,
            actor=user_actor(request, access.user.id),
            action="connection.intent_revoked",
            target_type="connection_intent",
            target_id=str(intent_id),
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
DISCONNECTED = (
    "This AWS account was disconnected in Cloudoptics. A workspace admin can connect it again "
    "from the Connections page, which gives a new link."
)
# A queued check of a stack's new role waits this long, so the role has reached STS.
NEW_ROLE_DELAY_SECONDS = 60


def _same_secret(enc: bytes, tenant_id: uuid.UUID, external_id: str) -> bool:
    try:
        stored = get_encryptor().decrypt(bytes(enc), tenant_id)
    except Exception:  # a bad tenant or ciphertext is simply "unknown"
        return False
    return hmac.compare_digest(stored.encode(), external_id.encode())


def _current_ciphertext(enc: bytes, tenant_id: uuid.UUID) -> bytes:
    """The intent's ciphertext for the new connection, under KMS if it was written before."""
    encryptor = get_encryptor()
    if not encryptor.needs_upgrade(enc):
        return enc
    return encryptor.encrypt(encryptor.decrypt(enc, tenant_id), tenant_id)


async def _connection_from_intent(
    session: Session, body: StackCallback, intent_id: uuid.UUID, created_by: uuid.UUID | None
) -> uuid.UUID:
    """The account the stack is in becomes a connection (or a disconnected one comes back)."""
    existing = (
        await session.execute(
            text(
                "SELECT id, status FROM cloud_connection WHERE provider = 'aws' "
                "AND provider_account_id = :acct"
            ),
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
        "ext": _current_ciphertext(bytes(intent.external_id_enc), body.tenant_id),
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
                "last_error = NULL, last_validated_at = NULL, account_verified_at = NULL, "
                "pending_stack = NULL, created_by = :by WHERE id = :id"
            ),
            {**values, "id": connection_id},
        )
    else:
        connection_id = uuid7()
        try:
            async with session.begin_nested():
                await session.execute(
                    text(
                        "INSERT INTO cloud_connection (id, tenant_id, provider_account_id, "
                        "account_type, role_arn, external_id_enc, stack_name, create_cur_export, "
                        "created_by) VALUES (:id, :t, :acct, :type, :arn, :ext, :stack, true, :by)"
                    ),
                    {**values, "id": connection_id},
                )
        except IntegrityError as exc:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"AWS account {body.account_id} is already connected to this workspace.",
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


def _problem(code: int, detail: str) -> JSONResponse:
    """A refusal that still commits what was written (the waiting page's error, audit rows)."""
    return JSONResponse(
        status_code=code,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": detail,
            "status": code,
            "instance": "/v1/cfn/callback",
        },
    )


async def _intent_refusal(
    session: Session, tenant_id: uuid.UUID, intent: Any
) -> tuple[int, str] | None:
    """Why an unused link may not connect an account now (None: it may).

    A link works for 7 days, until an admin withdraws it, and only while the admin who made
    it is still an admin of the workspace (security audit run 1, leads 2 and 3).
    """
    if intent.revoked_at is not None:
        return status.HTTP_410_GONE, REVOKED
    if intent.expired:
        return status.HTTP_410_GONE, EXPIRED
    role = None
    if intent.created_by is not None:
        role = (
            await session.execute(
                text("SELECT role FROM membership WHERE tenant_id = :t AND user_id = :u"),
                {"t": tenant_id, "u": intent.created_by},
            )
        ).scalar_one_or_none()
    if role != "admin":
        return status.HTTP_403_FORBIDDEN, NOT_ADMIN
    return None


@callback_router.post("/callback", response_model=CallbackResult)
async def stack_callback(body: StackCallback, session: Session) -> CallbackResult | JSONResponse:
    """Called by the stack's setup step on create, update and delete.

    The External ID is the only proof the caller has, and it is readable in the customer's
    account (IAM trust policies, the setup function's settings), so a callback never takes
    effect on its word alone: a delete disconnects only once the role is really gone, and new
    values for an existing connection replace the stored ones only once their role works.
    """
    await scope(session, tenant_id=body.tenant_id)
    kind, ref = body.ref.split(":", 1)
    ref_id = uuid.UUID(ref)
    connection_id: uuid.UUID | None
    fresh = False  # made (or brought back) from an admin's link in this request
    if kind == "i":
        intent = (
            await session.execute(
                text(
                    "SELECT id, external_id_enc, connection_id, created_by, revoked_at, "
                    "expires_at <= now() AS expired FROM connection_intent WHERE id = :i "
                    "FOR UPDATE"
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
            refusal = await _intent_refusal(session, body.tenant_id, intent)
            if refusal is not None:
                await session.execute(
                    text("UPDATE connection_intent SET error = :e WHERE id = :i"),
                    {"e": refusal[1], "i": ref_id},
                )
                return _problem(*refusal)
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
                return _problem(exc.status_code, str(exc.detail))
            fresh = True
    else:
        connection_id = ref_id

    conn = (
        await session.execute(
            text(
                "SELECT id, provider_account_id AS aws_account_id, status, stack_name, "
                "external_id_enc, account_type, "
                "role_arn, stack_version, cur_bucket, cur_prefix "
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
        outcome = "ignored"
        if conn.stack_name == body.stack_name and conn.status != "disconnected":
            outcome = await start_removal_check(session, body.tenant_id, conn, body.stack_name)
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
        return CallbackResult(connection_id=conn.id, status=outcome)

    if conn.status == "disconnected":
        # Only an admin brings a disconnected account back (Reconnect, or a new link), and
        # either gives it a new External ID.
        await audit.record(
            session,
            tenant_id=body.tenant_id,
            actor=Actor(type="system"),
            action="connection.callback_refused",
            target_type="cloud_connection",
            target_id=str(conn.id),
            after={"requestType": body.request_type, "stack": body.stack_name},
        )
        return _problem(status.HTTP_409_CONFLICT, DISCONNECTED)

    reported = {
        "accountType": "management" if body.management else "standalone",
        "roleArn": aws.billing_role_arn(body.account_id, body.stack_name),
        "stackName": body.stack_name,
        "stackVersion": body.template_version,
        "curBucket": body.cur_bucket or conn.cur_bucket,
        "curPrefix": body.cur_prefix or conn.cur_prefix,
    }
    delay = 0
    if fresh:
        # Nothing known-good to keep: the connection was made from this link just now.
        await session.execute(
            text(
                "UPDATE cloud_connection SET stack_version = :version, "
                "cur_bucket = COALESCE(:bucket, cur_bucket), "
                "cur_prefix = COALESCE(:prefix, cur_prefix) WHERE id = :c"
            ),
            {
                "version": body.template_version,
                "bucket": body.cur_bucket,
                "prefix": body.cur_prefix,
                "c": conn.id,
            },
        )
    elif reported != {
        "accountType": conn.account_type,
        "roleArn": conn.role_arn,
        "stackName": conn.stack_name,
        "stackVersion": conn.stack_version,
        "curBucket": conn.cur_bucket,
        "curPrefix": conn.cur_prefix,
    }:
        # Checked by the next validation; the stored values stay until the new role works.
        await session.execute(
            text("UPDATE cloud_connection SET pending_stack = CAST(:p AS jsonb) WHERE id = :c"),
            {"p": json.dumps(reported), "c": conn.id},
        )
        delay = NEW_ROLE_DELAY_SECONDS
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
    await start_validation(
        session, body.tenant_id, conn.id, Actor(type="system"), delay_seconds=delay
    )
    return CallbackResult(connection_id=conn.id, status="accepted")
