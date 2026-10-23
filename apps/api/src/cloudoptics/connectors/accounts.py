"""AWS Organizations account discovery and account coverage (WBS CC-7, PRD US-1.3).

For a management (payer) connection, ``organizations:ListAccounts`` lists every linked
account with its name and status. Discovery runs after the connection validates and every
12 hours (docs/11b §1.4). An account that drops out of the list keeps its row (its cost
history stays) and is marked ``NOT_LISTED``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from cloudoptics.connectors.aws import session_name
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.service import get_connection, get_session_factory
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7

USABLE = ("healthy", "degraded")
NOT_LISTED = "NOT_LISTED"

DiscoveryStatus = Literal["skipped", "succeeded", "failed"]


@dataclass(frozen=True)
class OrgAccount:
    aws_account_id: str
    name: str | None
    status: str


def list_org_accounts(org: Any) -> list[OrgAccount]:
    accounts, token = [], None
    while True:
        page = org.list_accounts(**({"NextToken": token} if token else {}))
        for a in page.get("Accounts", []):
            # AWS is replacing Status with State; read whichever is present.
            status = a.get("State") or a.get("Status") or "ACTIVE"
            accounts.append(OrgAccount(a["Id"], a.get("Name"), str(status).upper()))
        token = page.get("NextToken")
        if not token:
            return accounts


async def discover_accounts(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> DiscoveryStatus:
    """List the organization's accounts for one management connection and store them."""
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
    if conn.status not in USABLE or conn.account_type != "management":
        return "skipped"

    external_id = get_encryptor().decrypt(bytes(conn.external_id_enc), tenant_id)

    def _list() -> list[OrgAccount]:
        aws = get_session_factory().assume(
            conn.role_arn, external_id, session_name(tenant_id, "orgs")
        )
        return list_org_accounts(aws.client("organizations"))

    started = datetime.now(UTC)
    accounts: list[OrgAccount] = []
    error: str | None = None
    try:
        accounts = await run_in_threadpool(_list)
    except ClientError as exc:
        error = str(exc.response.get("Error", {}).get("Code", "ClientError"))
    except (BotoCoreError, KeyError) as exc:
        error = type(exc).__name__

    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        if error is None:
            await _store(session, tenant_id, conn, accounts)
        await session.execute(
            text(
                "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, "
                "started_at, finished_at, records, error_code) VALUES (:id, :t, :c, "
                "'accounts.discover', CAST(:scope AS jsonb), :status, :started, now(), "
                ":records, :code)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "scope": json.dumps({"accounts": len(accounts)}),
                "status": "failed" if error else "succeeded",
                "started": started,
                "records": len(accounts),
                "code": error,
            },
        )
    return "failed" if error else "succeeded"


async def _store(
    session: AsyncSession, tenant_id: uuid.UUID, conn: Any, accounts: list[OrgAccount]
) -> None:
    if accounts:
        await session.execute(
            text(
                "INSERT INTO cloud_account (id, tenant_id, connection_id, aws_account_id, name, "
                "account_type, org_status, org_seen_at) VALUES (:id, :t, :c, :acct, :name, "
                ":type, :status, now()) ON CONFLICT (tenant_id, aws_account_id) DO UPDATE SET "
                "name = COALESCE(EXCLUDED.name, cloud_account.name), "
                "org_status = EXCLUDED.org_status, org_seen_at = now()"
            ),
            [
                {
                    "id": uuid7(),
                    "t": tenant_id,
                    "c": conn.id,
                    "acct": a.aws_account_id,
                    "name": a.name,
                    "type": "management" if a.aws_account_id == conn.aws_account_id else "member",
                    "status": a.status,
                }
                for a in accounts
            ],
        )
    await session.execute(
        text(
            "UPDATE cloud_account SET org_status = :gone WHERE connection_id = :c "
            "AND org_status IS NOT NULL AND org_status <> :gone "
            "AND NOT (aws_account_id = ANY(:listed))"
        ),
        {"gone": NOT_LISTED, "c": conn.id, "listed": [a.aws_account_id for a in accounts]},
    )


@dataclass(frozen=True)
class AccountRow:
    aws_account_id: str
    name: str | None
    account_type: str
    org_status: str | None
    org_seen_at: datetime | None
    resource_access: bool
    has_cost_data: bool


@dataclass(frozen=True)
class Coverage:
    accounts: int
    with_cost_data: int
    with_resource_access: int


async def list_accounts(session: AsyncSession, today: date) -> tuple[list[AccountRow], Coverage]:
    """Every known account, with whether this or last month has CUR data for it.

    Accounts no longer in the organization are listed but left out of the coverage counts.
    """
    since = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    rows = [
        AccountRow(**r._mapping)
        for r in await session.execute(
            text(
                "SELECT a.aws_account_id, a.name, a.account_type, a.org_status, a.org_seen_at, "
                "a.resource_access, EXISTS (SELECT 1 FROM cost_daily d WHERE "
                "d.account_id = a.aws_account_id AND d.usage_date >= :since) AS has_cost_data "
                "FROM cloud_account a JOIN cloud_connection c ON c.id = a.connection_id "
                "WHERE c.status <> 'disconnected' "
                "ORDER BY a.account_type = 'management' DESC, lower(coalesce(a.name, '')), "
                "a.aws_account_id"
            ),
            {"since": since},
        )
    ]
    current = [r for r in rows if r.org_status != NOT_LISTED]
    return rows, Coverage(
        accounts=len(current),
        with_cost_data=sum(r.has_cost_data for r in current),
        with_resource_access=sum(r.resource_access for r in current),
    )
