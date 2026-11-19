"""The AWS plug-in (ADR-018): the existing AWS modules behind the provider protocol.

Phase 1 wraps rather than moves: each capability calls the module that has always done the
work (``connectors``, ``ingestion``, ``inventory``, ``metrics``, ``commitments``,
``analysis``), looked up when called, so behaviour, the audit trail and the jobs' results
are exactly what they were. Moving the AWS-only modules under ``providers/aws`` can follow
one at a time without touching the jobs.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import TYPE_CHECKING, Any

from cloudoptics.providers.base import CloudProvider

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from cloudoptics.analysis.pricing import PriceBook
    from cloudoptics.audit.log import Actor
    from cloudoptics.connectors.validation import ValidationResult
    from cloudoptics.ingestion.reload import ReloadResult
    from cloudoptics.ingestion.service import IngestResult

#: ``resource.resource_type`` values the AWS inventory stores (the CHECK of 0027/0042).
RESOURCE_TYPES = frozenset(
    {"ec2_instance", "ebs_volume", "ebs_snapshot", "eip", "elb", "rds_instance", "nat_gateway"}
)
#: AWS bills in USD; another currency blocks the load (ingestion/quality.py).
BILLING_CURRENCIES = frozenset({"USD"})


class Connect:
    async def validate(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        actor: Actor,
    ) -> ValidationResult | None:
        from cloudoptics.connectors import service

        return await service.run_validation(
            session, tenant_id=tenant_id, connection_id=connection_id, actor=actor
        )


class Credentials:
    """Roles Anywhere broker, then the customer role with its External ID (ADR-007)."""

    def session_factory(self) -> Any:
        from cloudoptics.connectors import service

        return service.get_session_factory()


class Accounts:
    async def discover(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.connectors import accounts

        return await accounts.discover_accounts(tenant_id, connection_id)


class BillingSource:
    """CUR 2.0 from the customer's S3 bucket, mapped to FOCUS (``cur2-focus-1``)."""

    async def ingest(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> IngestResult:
        from cloudoptics.ingestion import service

        return await service.ingest_connection(tenant_id, connection_id)

    async def reload(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID, period: date | None
    ) -> ReloadResult:
        from cloudoptics.ingestion import reload

        return await reload.reload_connection(tenant_id, connection_id, period)


class Reconcile:
    """Cost Explorer: the month-to-date snapshot and the check of each loaded month."""

    async def snapshot(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.ingestion import explorer

        return await explorer.refresh_snapshot(tenant_id, connection_id)

    async def reconcile(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID, billing_period: date
    ) -> str:
        from cloudoptics.ingestion import reconcile

        return await reconcile.reconcile_period(tenant_id, connection_id, billing_period)


class Inventory:
    async def scan(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.inventory import scanner

        return await scanner.scan_connection(tenant_id, connection_id)


class Metrics:
    """CloudWatch ``GetMetricData`` for the rules' candidates."""

    async def pull(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.metrics import puller

        return await puller.pull_connection(tenant_id, connection_id)


class Prices:
    """AWS list prices from ``price_catalog`` (provider ``aws``)."""

    async def load(self, session: AsyncSession) -> PriceBook:
        from cloudoptics.analysis import pricing

        return await pricing.load_price_book(session)


class Commitments:
    """Savings Plans and reservations, their coverage and utilisation (Cost Explorer)."""

    async def refresh(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
        from cloudoptics.commitments import service

        return await service.refresh(tenant_id, connection_id)


def build() -> CloudProvider:
    from cloudoptics.providers.aws.rules import AWS_RULES

    return CloudProvider(
        name="aws",
        label="AWS",
        account_noun="account",
        account_id_pattern=r"[0-9]{12}",
        billing_currencies=BILLING_CURRENCIES,
        connect=Connect(),
        credentials=Credentials(),
        accounts=Accounts(),
        billing_source=BillingSource(),
        reconcile=Reconcile(),
        inventory=Inventory(),
        metrics=Metrics(),
        # Cost Optimization Hub is not read yet (R5 notes Compute Optimizer's view itself).
        native_recs=None,
        prices=Prices(),
        commitments=Commitments(),
        rules=AWS_RULES,
        resource_types=RESOURCE_TYPES,
    )
