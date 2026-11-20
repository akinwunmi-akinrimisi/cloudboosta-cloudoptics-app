"""The Google Cloud plug-in (ADR-019): costs from the BigQuery FOCUS export, resources and
Recommender per project, and the starter rules GCP1-GCP7.

Credentials: Workload Identity Federation from the AWS broker identity to the Cloudoptics
service account (``client``); no Google key is stored. Each capability opens its own
tenant-scoped sessions, as the AWS modules do.
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
    from cloudoptics.providers.gcp.billing import GcpIngestResult
    from cloudoptics.providers.gcp.connect import GcpValidation

#: Google Cloud project IDs (6-30 characters, lowercase, digits and hyphens).
PROJECT_ID_PATTERN = r"[a-z][a-z0-9-]{4,28}[a-z0-9]"


class Connect:
    async def validate(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        actor: Actor,
    ) -> GcpValidation | None:
        from cloudoptics.providers.gcp import connect

        return await connect.run_validation(
            session, tenant_id=tenant_id, connection_id=connection_id, actor=actor
        )


class Credentials:
    """Federated, short-lived Google access tokens (no stored secret)."""

    def session_factory(self) -> Any:
        from cloudoptics.providers.gcp.client import GcpApi

        return GcpApi


class Accounts:
    async def discover(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.providers.gcp import billing

        return await billing.discover(tenant_id, connection_id)


class BillingSource:
    """FOCUS rows from BigQuery (``gcp-focus-1``)."""

    async def ingest(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> GcpIngestResult:
        from cloudoptics.providers.gcp import billing

        return await billing.ingest(tenant_id, connection_id)

    async def reload(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID, period: date | None
    ) -> ReloadResult:
        from cloudoptics.providers.gcp import billing

        return await billing.reload(tenant_id, connection_id, period)


class Reconcile:
    async def snapshot(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        """No separate month-to-date source: the export itself is near real time and this
        month is loaded at every poll (ADR-019)."""
        return "skipped"

    async def reconcile(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID, billing_period: date
    ) -> str:
        from cloudoptics.providers.gcp import billing

        return await billing.reconcile(tenant_id, connection_id, billing_period)


class Inventory:
    async def scan(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.providers.gcp import inventory

        return await inventory.scan(tenant_id, connection_id)


class Metrics:
    async def pull(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.providers.gcp import inventory

        return await inventory.pull(tenant_id, connection_id)


class NativeRecs:
    """Recommender, read with each inventory scan and kept on the resources it is about."""

    async def fetch(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.providers.gcp import inventory

        return await inventory.scan(tenant_id, connection_id)


def build() -> CloudProvider:
    from cloudoptics.providers.gcp.inventory import RESOURCE_TYPES
    from cloudoptics.providers.gcp.rules import GCP_RULES

    return CloudProvider(
        name="gcp",
        label="Google Cloud",
        account_noun="project",
        account_id_pattern=PROJECT_ID_PATTERN,
        # Any ISO 4217 currency: stored natively and converted to USD (ADR-019).
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
        rules=GCP_RULES,
        resource_types=RESOURCE_TYPES,
    )
