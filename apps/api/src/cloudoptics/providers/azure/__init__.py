"""The Microsoft Azure plug-in (ADR-020, multi-cloud plan Phase 2b).

- ``connect``: validation with one check per permission (``validate``).
- ``credentials``: the multi-tenant Entra app's certificate credential, no client secret
  (``credentials``, ``client``).
- ``accounts``: the connection's subscriptions (from the connection and the bill).
- ``billing_source``: the Cost Management FOCUS export in Blob Storage (``billing``).
- ``reconcile``: Cost Management query totals and the month-to-date snapshot.
- ``inventory`` / ``metrics`` / ``native_recs``: Resource Graph, Azure Monitor, Advisor.
- ``rules``: AZ1-AZ5 (``rules``).

No prices or commitments yet: the rules price resources from the bill.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from cloudoptics.providers.base import CloudProvider

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from cloudoptics.audit.log import Actor
    from cloudoptics.ingestion.reload import ReloadResult
    from cloudoptics.providers.azure.billing import IngestResult
    from cloudoptics.providers.azure.validate import AzureValidation

GUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
#: A subscription GUID, or a billing account ID (EA enrollment number, MCA "<id>:<id>_<date>").
ACCOUNT_ID_PATTERN = rf"{GUID_PATTERN}|[0-9A-Za-z:_.-]{{1,128}}"


class Connect:
    async def validate(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        actor: Actor,
    ) -> AzureValidation | None:
        from cloudoptics.providers.azure import validate

        return await validate.validate(
            session, tenant_id=tenant_id, connection_id=connection_id, actor=actor
        )


class Credentials:
    """Client-credentials tokens signed with the app's certificate (ADR-020)."""

    def session_factory(self) -> Any:
        from cloudoptics.providers.azure.client import tokens

        return tokens()


class Accounts:
    async def discover(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        # Validation records the connected subscriptions and the bill names a billing
        # account's ones: nothing else to list. The discovery job queues the scan after it.
        return "succeeded"


class BillingSource:
    async def ingest(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> IngestResult:
        from cloudoptics.providers.azure import billing

        return await billing.ingest_connection(tenant_id, connection_id)

    async def reload(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID, period: date | None
    ) -> ReloadResult:
        from cloudoptics.providers.azure import billing

        result: ReloadResult = await billing.reload_connection(tenant_id, connection_id, period)
        return result


class Reconcile:
    async def snapshot(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.providers.azure import reconcile

        return await reconcile.refresh_snapshot(tenant_id, connection_id)

    async def reconcile(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID, billing_period: date
    ) -> str:
        from cloudoptics.providers.azure import reconcile

        return await reconcile.reconcile_period(tenant_id, connection_id, billing_period)


class Inventory:
    async def scan(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.providers.azure import inventory

        return await inventory.scan_connection(tenant_id, connection_id)


class Metrics:
    async def pull(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.providers.azure import inventory

        return await inventory.pull_connection(tenant_id, connection_id)


class NativeRecs:
    """Azure Advisor's cost recommendations, attached to the resources they are about."""

    async def fetch(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.providers.azure import inventory

        return await inventory.fetch_advisor(tenant_id, connection_id)


def build() -> CloudProvider:
    from cloudoptics.providers.azure.inventory import RESOURCE_TYPES
    from cloudoptics.providers.azure.rules import AZURE_RULES

    return CloudProvider(
        name="azure",
        label="Azure",
        account_noun="subscription",
        account_id_pattern=ACCOUNT_ID_PATTERN,
        # Azure bills in the customer's currency; amounts are kept natively beside USD.
        billing_currencies=None,
        connect=Connect(),
        credentials=Credentials(),
        accounts=Accounts(),
        billing_source=BillingSource(),
        reconcile=Reconcile(),
        inventory=Inventory(),
        metrics=Metrics(),
        native_recs=NativeRecs(),
        prices=None,
        commitments=None,
        rules=AZURE_RULES,
        resource_types=RESOURCE_TYPES,
    )
