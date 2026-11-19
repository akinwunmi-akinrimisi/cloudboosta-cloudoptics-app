"""The platform audit chain (``tenant_id IS NULL``), written and verified as the app role.

Platform rows come from staff actions (break-glass grants, exchange rates) in sessions scoped
to a workspace or to none. Row-level security hides platform rows from those sessions, so the
writer must still find the chain's head, and a verifier that cannot see the chain must fail
rather than pass on an empty result.
"""

from __future__ import annotations

import itertools
import json
import uuid
from datetime import UTC

import test_support_access as support
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from test_support_access import audit_entries, grant, workspace

from cloudoptics.audit import log as audit
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.support import service

BASE = "/v1/admin/support-access"
staff_email = support.staff_email  # fixtures
outbox = support.outbox

PLATFORM_CHAIN = "SELECT id, prev_hash, row_hash FROM audit_log WHERE tenant_id IS NULL ORDER BY id"
PLATFORM_COUNT = text("SELECT count(*) FROM audit_log WHERE tenant_id IS NULL")
LEGACY_ROW = text(
    "INSERT INTO audit_log (tenant_id, actor_type, action, target_type, target_id, after, "
    "occurred_at, prev_hash, row_hash) VALUES (NULL, 'staff', :action, 'fx_rate', :target, "
    "CAST(:after AS jsonb), :at, NULL, :hash)"
)


async def test_platform_rows_link_through_the_app_role(
    client: AsyncClient, login, owner_sql, staff_email, outbox
) -> None:
    tenant, admin, _ = await workspace(client, login)
    staff = await login(staff_email, mfa=True)
    # Interleaved: platform + tenant row (grant), a tenant row, platform + tenant row (end).
    g = await grant(client, staff, tenant)
    r = await client.patch(f"/v1/tenants/{tenant}", json={"name": "Renamed"}, headers=admin)
    assert r.status_code == 200, r.text
    assert (await client.post(f"{BASE}/{g['id']}/end", headers=staff)).status_code == 200

    rows = owner_sql(PLATFORM_CHAIN)
    ours = [i for i, row in enumerate(rows) if row[0] in _ids(owner_sql, g["id"])]
    assert len(ours) == 2
    # Every platform row links to the one before it, across sessions scoped to the workspace.
    assert rows[0][1] is None
    for before, after in itertools.pairwise(rows):
        assert after[1] == before[2], f"platform row {after[0]} does not link to {before[0]}"

    # The staff console sees the whole chain and it verifies.
    async with get_sessionmaker()() as s, s.begin():
        await s.execute(service.CONSOLE_SCOPE)
        assert await audit.verify_chain(s, None)
    # Elsewhere the app role sees no platform rows, and verification fails instead of
    # passing on an empty chain.
    async with get_sessionmaker()() as s, s.begin():
        await scope(s, tenant_id=uuid.UUID(tenant))
        assert (await s.execute(PLATFORM_COUNT)).scalar_one() == 0
        assert not await audit.verify_chain(s, None)
    # The workspace's own chain is unchanged and still verifies.
    assert [e["action"] for e in await audit_entries(client, tenant, admin)][:3] == [
        "support_access.ended",
        "tenant.updated",
        "support_access.granted",
    ]


def _ids(owner_sql, grant_id: str) -> set[int]:  # type: ignore[no-untyped-def]
    found = owner_sql(
        "SELECT id FROM audit_log WHERE tenant_id IS NULL AND target_id = %s", grant_id
    )
    return {int(r[0]) for r in found}  # type: ignore[call-overload]


async def test_rows_written_before_the_fix_still_verify(database: dict[str, str]) -> None:
    """Rows written before migration 0037 never linked (each has ``prev_hash`` NULL). They
    are checked one by one; the first linked row follows the last of them; an unlinked row
    after that is a break. Runs as the superuser in a transaction that is rolled back."""
    engine = create_async_engine(database["owner"])
    try:
        async with engine.connect() as conn:
            trans = await conn.begin()
            s = AsyncSession(bind=conn)
            await s.execute(text("DELETE FROM audit_log WHERE tenant_id IS NULL"))
            for n in range(2):
                await _legacy(s, n)
            actor = audit.Actor(type="staff", id=uuid.uuid4())
            await audit.record(s, tenant_id=None, actor=actor, action="fx_rate.set")
            await audit.record(s, tenant_id=None, actor=actor, action="fx_rate.set")
            linked = (await s.execute(text(PLATFORM_CHAIN))).all()
            assert [r.prev_hash is None for r in linked] == [True, True, False, False]
            assert bytes(linked[2].prev_hash) == bytes(linked[1].row_hash)
            assert await audit.verify_chain(s, None)

            # A changed legacy row is still detected.
            await s.execute(text("SAVEPOINT tamper"))
            await s.execute(
                text("UPDATE audit_log SET action = 'x' WHERE id = :id"), {"id": linked[0].id}
            )
            assert not await audit.verify_chain(s, None)
            await s.execute(text("ROLLBACK TO SAVEPOINT tamper"))

            # An unlinked row once the chain links is a break.
            await _legacy(s, 3)
            assert not await audit.verify_chain(s, None)
            await trans.rollback()
    finally:
        await engine.dispose()


async def _legacy(s: AsyncSession, n: int) -> None:
    """A platform row as the code before 0037 wrote it: hashed with no predecessor."""
    at = (await s.execute(text("SELECT now()"))).scalar_one()
    after = {"rate": str(n)}
    rec = {
        "tenant_id": None,
        "actor_type": "staff",
        "actor_id": None,
        "action": "fx_rate.set",
        "target_type": "fx_rate",
        "target_id": f"legacy-{n}",
        "before": None,
        "after": after,
        "occurred_at": at.astimezone(UTC).isoformat(),
    }
    await s.execute(
        LEGACY_ROW,
        {
            "action": "fx_rate.set",
            "target": f"legacy-{n}",
            "after": json.dumps(after),
            "at": at,
            "hash": audit._row_hash(None, rec),
        },
    )
