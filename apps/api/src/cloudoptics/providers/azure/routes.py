"""/v1/tenants/{t}/providers/azure/connections: connect an Azure subscription or billing
account (ADR-020). The generic ``/connections`` list, detail and disconnect cover Azure rows
too; this router adds what is Azure's own: create (with the set-up steps), the steps again,
reconnect (a disconnected connection, with a new ownership code) and validation.

``/v1/azure/consent`` is where Entra sends the admin back after consenting: a small page
that says to close the tab. It reads and writes nothing; the next validation sees the
consent by getting a token in the directory.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from cloudoptics.audit import log as audit
from cloudoptics.audit.log import Actor
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.routes import PermissionCheck
from cloudoptics.identity.deps import AdminAccess, Session
from cloudoptics.platform.api import ApiModel, user_actor
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.azure import connection as azconn

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/providers/azure/connections", tags=["connections"]
)
consent_router = APIRouter(prefix="/v1/azure", tags=["connections"])

GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class AzureConnectionCreate(ApiModel):
    directory_id: str = Field(pattern=GUID)
    scope_type: azconn.ScopeType = "subscription"
    subscription_ids: list[str] = Field(default_factory=list, max_length=50)
    billing_account_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[0-9A-Za-z:_.-]+$"
    )
    storage_account: str = Field(pattern=r"^[a-z0-9]{3,24}$")
    container: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
    export_path: str = Field(default="", max_length=512, pattern=r"^[A-Za-z0-9._/-]*$")

    @model_validator(mode="after")
    def _scope(self) -> AzureConnectionCreate:
        bad = [s for s in self.subscription_ids if not re.fullmatch(GUID, s)]
        if bad:
            raise ValueError(
                "Each subscription ID is a GUID like 00000000-0000-0000-0000-000000000000."
            )
        self.subscription_ids = sorted({s.lower() for s in self.subscription_ids})
        self.directory_id = self.directory_id.lower()
        self.export_path = self.export_path.strip("/")
        if "--" in self.container:
            raise ValueError("A container name cannot have two hyphens in a row.")
        if ".." in self.export_path.split("/"):
            raise ValueError("The export folder cannot contain '..'.")
        if self.scope_type == "subscription":
            if len(self.subscription_ids) != 1:
                raise ValueError(
                    "Connect one subscription at a time (each has its own FOCUS export), or "
                    "connect the billing account."
                )
            self.billing_account_id = None
        elif not self.billing_account_id:
            raise ValueError("Enter the billing account ID.")
        return self


class AzureConnection(ApiModel):
    id: uuid.UUID
    provider: Literal["azure"] = "azure"
    status: str
    directory_id: str
    scope_type: azconn.ScopeType
    scope: str
    subscription_ids: list[str]
    billing_account_id: str | None
    storage_account: str
    container: str
    export_path: str
    permission_checks: list[PermissionCheck]
    last_error: dict[str, Any] | None
    last_validated_at: datetime | None
    created_at: datetime


class AzureConnectionSetup(ApiModel):
    connection: AzureConnection
    client_id: str
    consent_url: str
    ownership_tag: str
    ownership_code: str
    role_assignment_command: str
    portal_role_steps: list[str]
    export_portal_url: str
    export_steps: list[str]


def _model(row: Any) -> AzureConnection:
    s = azconn.from_row(row)
    return AzureConnection(
        id=row.id,
        status=row.status,
        directory_id=s.directory_id,
        scope_type=s.scope_type,
        scope=s.scope,
        subscription_ids=list(s.subscription_ids),
        billing_account_id=s.billing_account_id,
        storage_account=s.storage_account,
        container=s.container,
        export_path=s.export_path,
        permission_checks=[PermissionCheck.model_validate(c) for c in row.permission_checks or []],
        last_error=row.last_error,
        last_validated_at=row.last_validated_at,
        created_at=row.created_at,
    )


def _setup(row: Any, code: str) -> AzureConnectionSetup:
    s = azconn.from_row(row)
    return AzureConnectionSetup(
        connection=_model(row),
        client_id=get_settings().azure_client_id,
        consent_url=azconn.consent_url(s.directory_id, row.id),
        ownership_tag=azconn.OWNERSHIP_TAG,
        ownership_code=code,
        role_assignment_command=azconn.role_command(s, code),
        portal_role_steps=azconn.portal_role_steps(s, code),
        export_portal_url=azconn.EXPORTS_PORTAL_URL,
        export_steps=azconn.export_steps(s),
    )


async def _load(session: Session, connection_id: uuid.UUID) -> Any:
    row = await azconn.load(session, connection_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Connection not found")
    return row


EXISTING = text(
    "SELECT id, status FROM cloud_connection WHERE provider = 'azure' "
    "AND provider_account_id = :acct"
)
INSERT_CONNECTION = text(
    "INSERT INTO cloud_connection (id, tenant_id, provider, provider_account_id, account_type, "
    "role_arn, external_id_enc, stack_name, create_cur_export, cur_bucket, cur_prefix, "
    "created_by) VALUES (:id, :t, 'azure', :acct, :type, '', :code, :stack, false, :bucket, "
    ":prefix, :by)"
)
RESET_CONNECTION = text(
    "UPDATE cloud_connection SET status = 'pending', health = NULL, account_type = :type, "
    "external_id_enc = :code, cur_bucket = :bucket, cur_prefix = :prefix, "
    "permission_checks = '[]', last_error = NULL, last_validated_at = NULL, "
    "account_verified_at = NULL, created_by = :by WHERE id = :id"
)
UPSERT_DETAIL = text(
    "INSERT INTO azure_connection (connection_id, tenant_id, directory_id, scope_type, "
    "billing_account_id, subscription_ids, storage_account, container, export_path) VALUES "
    "(:id, :t, :dir, :scope, :billing, :subs, :account, :container, :path) "
    "ON CONFLICT (connection_id) DO UPDATE SET directory_id = EXCLUDED.directory_id, "
    "scope_type = EXCLUDED.scope_type, billing_account_id = EXCLUDED.billing_account_id, "
    "subscription_ids = EXCLUDED.subscription_ids, storage_account = EXCLUDED.storage_account, "
    "container = EXCLUDED.container, export_path = EXCLUDED.export_path, "
    "storage_account_id = NULL, export_name = NULL"
)


@router.post("", response_model=AzureConnectionSetup, status_code=status.HTTP_201_CREATED)
async def create_connection(
    body: AzureConnectionCreate, request: Request, access: AdminAccess, session: Session
) -> AzureConnectionSetup:
    account = (
        body.billing_account_id
        if body.scope_type == "billing_account"
        else (body.subscription_ids[0])
    )
    code = azconn.new_ownership_code()
    params = {
        "t": access.tenant_id,
        "acct": account,
        "type": "management" if body.scope_type == "billing_account" else "standalone",
        "code": get_encryptor().encrypt(code, access.tenant_id),
        "stack": azconn.STACK_NAME,
        "bucket": f"{body.storage_account}/{body.container}",
        "prefix": body.export_path or None,
        "by": access.user.id,
    }
    existing = (await session.execute(EXISTING, {"acct": account})).one_or_none()
    if existing is not None and existing.status != "disconnected":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This Azure subscription or billing account is already connected to this workspace.",
        )
    action = "connection.reconnected" if existing is not None else "connection.created"
    connection_id = existing.id if existing is not None else uuid7()
    try:
        async with session.begin_nested():
            if existing is None:
                await session.execute(INSERT_CONNECTION, {**params, "id": connection_id})
            else:
                await session.execute(RESET_CONNECTION, {**params, "id": connection_id})
            await session.execute(
                UPSERT_DETAIL,
                {
                    "id": connection_id,
                    "t": access.tenant_id,
                    "dir": body.directory_id,
                    "scope": body.scope_type,
                    "billing": body.billing_account_id,
                    "subs": body.subscription_ids,
                    "account": body.storage_account,
                    "container": body.container,
                    "path": body.export_path,
                },
            )
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This Azure subscription or billing account is already connected to this workspace.",
        ) from exc
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action=action,
        target_type="cloud_connection",
        target_id=str(connection_id),
        after={"provider": "azure", "accountId": account, "scopeType": body.scope_type},
    )
    return _setup(await _load(session, connection_id), code)


@router.get("/{connection_id}", response_model=AzureConnectionSetup)
async def get_setup(
    connection_id: uuid.UUID, access: AdminAccess, session: Session
) -> AzureConnectionSetup:
    row = await _load(session, connection_id)
    if row.external_id_enc is None:
        # Disconnecting drops a non-AWS connection's secret (migration 0046): a new one
        # comes with connecting again.
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This connection is disconnected. Connect the subscription again to use it.",
        )
    code = get_encryptor().decrypt(bytes(row.external_id_enc), access.tenant_id)
    return _setup(row, code)


RESET_DETAIL = text(
    "UPDATE azure_connection SET storage_account_id = NULL, export_name = NULL "
    "WHERE connection_id = :id"
)


@router.post("/{connection_id}/reconnect", response_model=AzureConnectionSetup)
async def reconnect(
    connection_id: uuid.UUID, request: Request, access: AdminAccess, session: Session
) -> AzureConnectionSetup:
    """Connect a disconnected subscription or billing account again with the settings it
    had. Disconnecting deleted its ownership code, so it gets a new one: the role script and
    the storage account tag change, and the steps in Azure are done again."""
    row = await _load(session, connection_id)
    if row.status != "disconnected":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This connection is still connected. Use Set up again to see its steps.",
        )
    s = azconn.from_row(row)
    code = azconn.new_ownership_code()
    try:
        async with session.begin_nested():
            await session.execute(
                RESET_CONNECTION,
                {
                    "id": connection_id,
                    "type": row.account_type,
                    "code": get_encryptor().encrypt(code, access.tenant_id),
                    "bucket": f"{s.storage_account}/{s.container}",
                    "prefix": s.export_path or None,
                    "by": access.user.id,
                },
            )
            await session.execute(RESET_DETAIL, {"id": connection_id})
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This Azure subscription or billing account is connected to another workspace.",
        ) from exc
    await audit.record(
        session,
        tenant_id=access.tenant_id,
        actor=user_actor(request, access.user.id),
        action="connection.reconnected",
        target_type="cloud_connection",
        target_id=str(connection_id),
        before={"status": "disconnected"},
        after={"provider": "azure", "accountId": s.account_id, "scopeType": s.scope_type},
    )
    return _setup(await _load(session, connection_id), code)


@router.post("/{connection_id}/validate", response_model=AzureConnection)
async def validate_connection(
    connection_id: uuid.UUID, request: Request, access: AdminAccess, session: Session
) -> AzureConnection:
    """Check every permission now (a few seconds), then load what it can: inline in tests
    and local runs, queued on the worker in staging and production."""
    row = await _load(session, connection_id)
    if row.status == "disconnected":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This connection is disconnected. Connect the subscription again to use it.",
        )
    await run_and_follow_up(access.tenant_id, connection_id, user_actor(request, access.user.id))
    return _model(await _load(session, connection_id))


async def run_and_follow_up(tenant_id: uuid.UUID, connection_id: uuid.UUID, actor: Actor) -> Any:
    """Validation in its own transaction, then the loads it unlocks (as the validate job)."""
    from cloudoptics.providers.azure import validate

    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        result = await validate.validate(
            session, tenant_id=tenant_id, connection_id=connection_id, actor=actor
        )
    if result is None or result.status == "failed":
        return result
    if get_settings().jobs_mode == "inline":
        await _inline_follow_up(tenant_id, connection_id, result.has_billing_export)
        return result
    from cloudoptics.connectors.jobs import defer_discovery
    from cloudoptics.ingestion.jobs import defer_ingest, defer_snapshot
    from cloudoptics.platform.jobs import get_job_app

    app = get_job_app()
    await defer_snapshot(app, str(tenant_id), str(connection_id))
    if result.has_billing_export:
        await defer_ingest(app, str(tenant_id), str(connection_id))
    await defer_discovery(app, str(tenant_id), str(connection_id))
    return result


async def _inline_follow_up(tenant_id: uuid.UUID, connection_id: uuid.UUID, exported: bool) -> None:
    from cloudoptics.analysis.engine import run_analysis
    from cloudoptics.providers.azure import billing, inventory, reconcile

    if exported:
        loaded = await billing.ingest_connection(tenant_id, connection_id)
        for period in loaded.loaded_periods:
            await reconcile.reconcile_period(tenant_id, connection_id, period)
    await reconcile.refresh_snapshot(tenant_id, connection_id)
    if await inventory.scan_connection(tenant_id, connection_id) in ("succeeded", "partial"):
        await inventory.pull_connection(tenant_id, connection_id)
    await run_analysis(tenant_id)


CONSENT_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Cloudoptics</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;
line-height:1.5;color:#111}}</style></head><body><h1>{title}</h1><p>{body}</p></body></html>"""


@consent_router.get("/consent", response_class=HTMLResponse, include_in_schema=False)
async def consent_done(admin_consent: str | None = None, error: str | None = None) -> HTMLResponse:
    if error or (admin_consent or "").lower() != "true":
        page = CONSENT_PAGE.format(
            title="Consent was not given",
            body="Azure did not record the approval. Close this tab, then open the consent link "
            "again as a Global Administrator or Privileged Role Administrator.",
        )
    else:
        page = CONSENT_PAGE.format(
            title="Cloudoptics is approved",
            body="Close this tab and go back to Cloudoptics to finish connecting.",
        )
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})
