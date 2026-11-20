"""The DigitalOcean plug-in (ADR-021): a customer-made read-only token, invoices and month to
date into the shared cost tables, the resource scan and five rules.

Capabilities: ``connect`` (token checks), ``billing_source`` (invoices), ``reconcile``
(month to date and the invoice check) and ``inventory`` (with Droplet CPU). There is no
``credentials`` (the token is the credential, stored encrypted on the connection), no
``accounts`` (a token reads one team), no separate ``metrics`` (CPU is read by the scan),
and no ``prices``, ``commitments`` or ``native_recs``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from cloudoptics.providers.base import CloudProvider

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from cloudoptics.audit.log import Actor

#: DigitalOcean bills in US dollars.
BILLING_CURRENCIES = frozenset({"USD"})
TEAM_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class Connect:
    async def validate(
        self, session: AsyncSession, *, tenant_id: uuid.UUID, connection_id: uuid.UUID, actor: Actor
    ) -> Any:
        from cloudoptics.providers.digitalocean import connect

        return await connect.validate(
            session, tenant_id=tenant_id, connection_id=connection_id, actor=actor
        )


class BillingSource:
    async def ingest(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> Any:
        from cloudoptics.providers.digitalocean import billing

        return await billing.ingest_connection(tenant_id, connection_id)

    async def reload(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID, period: date | None
    ) -> Any:
        from cloudoptics.providers.digitalocean import billing

        return await billing.reload_connection(tenant_id, connection_id, period)


class Reconcile:
    async def snapshot(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.providers.digitalocean import billing

        return await billing.refresh_snapshot(tenant_id, connection_id)

    async def reconcile(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID, billing_period: date
    ) -> str:
        from cloudoptics.providers.digitalocean import billing

        return await billing.reconcile_period(tenant_id, connection_id, billing_period)


class Inventory:
    async def scan(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.providers.digitalocean import inventory

        return await inventory.scan_connection(tenant_id, connection_id)


def build() -> CloudProvider:
    from cloudoptics.providers.digitalocean.inventory import RESOURCE_TYPES
    from cloudoptics.providers.digitalocean.rules import DO_RULES

    return CloudProvider(
        name="digitalocean",
        label="DigitalOcean",
        account_noun="account",
        account_id_pattern=TEAM_UUID,
        billing_currencies=BILLING_CURRENCIES,
        connect=Connect(),
        billing_source=BillingSource(),
        reconcile=Reconcile(),
        inventory=Inventory(),
        rules=DO_RULES,
        resource_types=RESOURCE_TYPES,
    )
