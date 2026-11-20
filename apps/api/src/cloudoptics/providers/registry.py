"""The provider registry (ADR-018): which plug-in serves a connection.

Jobs ask ``capability_for(tenant, connection, "billing_source")`` and call what comes back;
``None`` means the connection's provider has no plug-in yet, or a plug-in without that
capability, and the job skips the connection. AWS and Google Cloud (ADR-019) are registered;
``azure`` and ``digitalocean`` are accepted by the schema and get plug-ins in later phases.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal, overload

from sqlalchemy import text

from cloudoptics.providers.base import (
    DEFAULT_PROVIDER,
    PROVIDER_NAMES,
    Accounts,
    BillingSource,
    Capability,
    CloudProvider,
    Commitments,
    Connect,
    Credentials,
    Inventory,
    Metrics,
    NativeRecs,
    Prices,
    Reconcile,
)

_REGISTRY: dict[str, CloudProvider] = {}
_BUILTINS_LOADED = False

CONNECTION_PROVIDER = text("SELECT provider FROM cloud_connection WHERE id = :c")


class UnknownProviderError(LookupError):
    """No plug-in is registered for this provider."""


def register(provider: CloudProvider) -> CloudProvider:
    if provider.name not in PROVIDER_NAMES:
        raise ValueError(f"{provider.name!r} is not a provider the schema accepts")
    _REGISTRY[provider.name] = provider
    return provider


def unregister(name: str) -> None:
    """Tests only: remove a plug-in registered for the test; a built-in one comes back."""
    _REGISTRY.pop(name, None)
    if _BUILTINS_LOADED and name in _BUILTINS:
        register(_build(name))


#: The built-in plug-ins, in registration order (module with a ``build()``).
_BUILTINS = {"aws": "cloudoptics.providers.aws", "gcp": "cloudoptics.providers.gcp"}


def _build(name: str) -> CloudProvider:
    import importlib

    built: CloudProvider = importlib.import_module(_BUILTINS[name]).build()
    return built


def _load_builtins() -> None:
    global _BUILTINS_LOADED
    if not _BUILTINS_LOADED:
        _BUILTINS_LOADED = True
        for name in _BUILTINS:
            register(_build(name))


def registered() -> tuple[CloudProvider, ...]:
    """The registered plug-ins, built-ins first."""
    _load_builtins()
    return tuple(_REGISTRY.values())


def get(name: str) -> CloudProvider:
    _load_builtins()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownProviderError(name) from None


def find(name: str) -> CloudProvider | None:
    try:
        return get(name)
    except UnknownProviderError:
        return None


async def connection_provider(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    """The connection's provider; the default (AWS) when the connection is gone, so the AWS
    code reports the missing connection exactly as it always has."""
    from cloudoptics.platform.db import get_sessionmaker, scope

    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        name = (await session.execute(CONNECTION_PROVIDER, {"c": connection_id})).scalar()
    return str(name or DEFAULT_PROVIDER)


@overload
async def capability_for(
    tenant_id: uuid.UUID | str, connection_id: uuid.UUID | str, name: Literal["connect"]
) -> Connect | None: ...


@overload
async def capability_for(
    tenant_id: uuid.UUID | str, connection_id: uuid.UUID | str, name: Literal["credentials"]
) -> Credentials | None: ...


@overload
async def capability_for(
    tenant_id: uuid.UUID | str, connection_id: uuid.UUID | str, name: Literal["accounts"]
) -> Accounts | None: ...


@overload
async def capability_for(
    tenant_id: uuid.UUID | str, connection_id: uuid.UUID | str, name: Literal["billing_source"]
) -> BillingSource | None: ...


@overload
async def capability_for(
    tenant_id: uuid.UUID | str, connection_id: uuid.UUID | str, name: Literal["reconcile"]
) -> Reconcile | None: ...


@overload
async def capability_for(
    tenant_id: uuid.UUID | str, connection_id: uuid.UUID | str, name: Literal["inventory"]
) -> Inventory | None: ...


@overload
async def capability_for(
    tenant_id: uuid.UUID | str, connection_id: uuid.UUID | str, name: Literal["metrics"]
) -> Metrics | None: ...


@overload
async def capability_for(
    tenant_id: uuid.UUID | str, connection_id: uuid.UUID | str, name: Literal["native_recs"]
) -> NativeRecs | None: ...


@overload
async def capability_for(
    tenant_id: uuid.UUID | str, connection_id: uuid.UUID | str, name: Literal["prices"]
) -> Prices | None: ...


@overload
async def capability_for(
    tenant_id: uuid.UUID | str, connection_id: uuid.UUID | str, name: Literal["commitments"]
) -> Commitments | None: ...


async def capability_for(
    tenant_id: uuid.UUID | str, connection_id: uuid.UUID | str, name: Capability
) -> Any:
    """The connection's provider's ``name`` capability, or None when it has none."""
    provider = find(
        await connection_provider(uuid.UUID(str(tenant_id)), uuid.UUID(str(connection_id)))
    )
    return None if provider is None else provider.capability(name)
