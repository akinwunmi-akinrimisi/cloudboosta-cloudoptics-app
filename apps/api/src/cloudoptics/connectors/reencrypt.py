"""Re-encrypt connection secrets written before KMS (ADR-007). Idempotent.

Runs hourly in the worker (``connectors:secrets.reencrypt``) and by hand:

    python -m cloudoptics.connectors.reencrypt          # re-encrypt, print the counts
    python -m cloudoptics.connectors.reencrypt --check  # count only; exit 1 if any remain

Only version 1 ciphertexts (the local data key) are touched. Each row is rewritten only
while it still holds the ciphertext that was read, so it is safe beside the API and other
workers. Tenants are found through their connections (the scheduler may list
``cloud_connection`` across tenants, migration 0003); a tenant's connection links
(``connection_intent``) are re-encrypted with them. A link of a tenant with no connection is
re-encrypted when it is used, and links expire after 7 days anyway. Nothing secret is printed.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.connectors.crypto import KmsEnvelopeEncryptor, get_encryptor
from cloudoptics.platform.db import get_sessionmaker, scheduler_scope, scope

# Version 2 ciphertexts start with 0x02; everything else predates KMS.
LEGACY_TENANTS = text(
    "SELECT DISTINCT tenant_id FROM cloud_connection "
    "WHERE substring(external_id_enc FROM 1 FOR 1) <> decode('02', 'hex')"
)
LEGACY_CONNECTIONS = text(
    "SELECT id, external_id_enc FROM cloud_connection "
    "WHERE substring(external_id_enc FROM 1 FOR 1) <> decode('02', 'hex')"
)
LEGACY_INTENTS = text(
    "SELECT id, external_id_enc FROM connection_intent "
    "WHERE substring(external_id_enc FROM 1 FOR 1) <> decode('02', 'hex')"
)
UPDATE_CONNECTION = text(
    "UPDATE cloud_connection SET external_id_enc = :new WHERE id = :id AND external_id_enc = :old"
)
UPDATE_INTENT = text(
    "UPDATE connection_intent SET external_id_enc = :new WHERE id = :id AND external_id_enc = :old"
)


@dataclass
class Report:
    kms: bool = False
    tenants: int = 0
    connections: int = 0
    links: int = 0
    failed: int = 0


async def _tenant(
    session: AsyncSession, encryptor: KmsEnvelopeEncryptor, tenant_id: uuid.UUID, report: Report
) -> None:
    for select, update, field in (
        (LEGACY_CONNECTIONS, UPDATE_CONNECTION, "connections"),
        (LEGACY_INTENTS, UPDATE_INTENT, "links"),
    ):
        for row in (await session.execute(select)).all():
            old = bytes(row.external_id_enc)
            try:
                new = encryptor.reencrypt(old, tenant_id)
            except Exception:  # a row that does not decrypt is counted, never printed
                report.failed += 1
                continue
            if new is not None:
                await session.execute(update, {"new": new, "old": old, "id": row.id})
                setattr(report, field, getattr(report, field) + 1)


async def _tenants() -> list[uuid.UUID]:
    async with get_sessionmaker()() as session, session.begin():
        await scheduler_scope(session)
        return [r.tenant_id for r in await session.execute(LEGACY_TENANTS)]


async def reencrypt_all(*, dry_run: bool = False) -> Report:
    """Rewrite every version 1 ciphertext under KMS. Without a KMS key, only counts."""
    encryptor = get_encryptor()
    report = Report(kms=isinstance(encryptor, KmsEnvelopeEncryptor))
    for tenant_id in await _tenants():
        report.tenants += 1
        async with get_sessionmaker()() as session, session.begin():
            await scope(session, tenant_id=tenant_id)
            if dry_run or not isinstance(encryptor, KmsEnvelopeEncryptor):
                report.connections += len((await session.execute(LEGACY_CONNECTIONS)).all())
                report.links += len((await session.execute(LEGACY_INTENTS)).all())
                continue
            await _tenant(session, encryptor, tenant_id, report)
    return report


def main(argv: list[str]) -> int:
    check = "--check" in argv
    report = asyncio.run(reencrypt_all(dry_run=check))
    print(json.dumps({**asdict(report), "check": check}))
    if check:
        return 1 if report.connections or report.links else 0
    return 1 if report.failed or not report.kms else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
