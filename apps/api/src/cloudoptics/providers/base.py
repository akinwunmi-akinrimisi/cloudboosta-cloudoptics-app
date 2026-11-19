"""The provider protocol (ADR-018, docs/11b §1.2): what a cloud plug-in can do.

A provider is a ``CloudProvider`` value with a name, how its accounts are named and
validated, which billing currencies its bills may use, and a set of optional capabilities.
Each capability is a small protocol; a provider that lacks one (``None``) is skipped by the
jobs that need it, so a provider can start with costs only and add inventory, metrics,
native recommendations, rules or commitments later.

Capabilities take the tenant and connection IDs and open their own tenant-scoped sessions,
as the AWS modules always have; their results are the existing result types, read through
the small protocols below.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from cloudoptics.analysis.pricing import PriceBook
    from cloudoptics.analysis.registry import RuleSpec
    from cloudoptics.audit.log import Actor

#: Every provider the schema accepts (the ``provider`` CHECK of migration 0042).
ProviderName = Literal["aws", "gcp", "azure", "digitalocean"]
PROVIDER_NAMES: tuple[ProviderName, ...] = get_args(ProviderName)
DEFAULT_PROVIDER: ProviderName = "aws"

Capability = Literal[
    "connect",
    "credentials",
    "accounts",
    "billing_source",
    "reconcile",
    "inventory",
    "metrics",
    "native_recs",
    "prices",
    "commitments",
]


# Results, as the jobs read them.
class ValidationOutcome(Protocol):
    @property
    def status(self) -> str: ...
    @property
    def health_worsened(self) -> bool: ...
    @property
    def has_billing_export(self) -> bool: ...


class IngestOutcome(Protocol):
    @property
    def status(self) -> str: ...
    @property
    def loaded_periods(self) -> list[date]: ...


class ReloadOutcome(Protocol):
    @property
    def status(self) -> str: ...
    @property
    def reloaded(self) -> list[date]: ...


# Capabilities.
class Connect(Protocol):
    """Checks a connection's access and records its health."""

    async def validate(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        connection_id: uuid.UUID,
        actor: Actor,
    ) -> ValidationOutcome | None: ...


class Credentials(Protocol):
    """Short-lived access into the customer's cloud (no stored long-lived secrets)."""

    def session_factory(self) -> Any: ...


class Accounts(Protocol):
    """Lists the accounts, projects or subscriptions a connection covers."""

    async def discover(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str: ...


class BillingSource(Protocol):
    """Loads the bill (FOCUS-shaped) into the cost tables."""

    async def ingest(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> IngestOutcome: ...

    async def reload(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID, period: date | None
    ) -> ReloadOutcome: ...


class Reconcile(Protocol):
    """Provisional month-to-date costs and the check of loaded bills against the provider's
    own total."""

    async def snapshot(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str: ...

    async def reconcile(
        self, tenant_id: uuid.UUID, connection_id: uuid.UUID, billing_period: date
    ) -> str: ...


class Inventory(Protocol):
    async def scan(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str: ...


class Metrics(Protocol):
    async def pull(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str: ...


class NativeRecs(Protocol):
    """The provider's own recommendations (Cost Optimization Hub, Recommender, Advisor)."""

    async def fetch(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str: ...


class Prices(Protocol):
    async def load(self, session: AsyncSession) -> PriceBook: ...


class Commitments(Protocol):
    async def refresh(self, tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str: ...


@dataclass(frozen=True)
class CloudProvider:
    name: ProviderName
    label: str
    #: What the provider calls the thing a connection's costs are split by.
    account_noun: Literal["account", "project", "subscription"]
    #: The form of its account IDs (the database CHECKs AWS's; others are checked here).
    account_id_pattern: str
    #: Currencies its bills may be in; None accepts any ISO 4217 code (stored natively).
    billing_currencies: frozenset[str] | None
    connect: Connect | None = None
    credentials: Credentials | None = None
    accounts: Accounts | None = None
    billing_source: BillingSource | None = None
    reconcile: Reconcile | None = None
    inventory: Inventory | None = None
    metrics: Metrics | None = None
    native_recs: NativeRecs | None = None
    prices: Prices | None = None
    commitments: Commitments | None = None
    #: Its savings rules, in evaluation order (analysis/registry.py).
    rules: tuple[RuleSpec, ...] = ()
    #: The resource types its inventory stores (``resource.resource_type``).
    resource_types: frozenset[str] = frozenset()

    def valid_account_id(self, value: str) -> bool:
        return re.fullmatch(self.account_id_pattern, value) is not None

    def accepts_currency(self, currency: str) -> bool:
        return self.billing_currencies is None or currency in self.billing_currencies

    def capability(self, name: Capability) -> Any:
        return getattr(self, name)
