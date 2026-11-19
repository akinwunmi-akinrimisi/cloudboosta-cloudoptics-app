"""Cloud provider plug-ins (ADR-018). ``base`` has the protocol, ``registry`` the lookup,
and each provider its own package (``aws`` today)."""

from cloudoptics.providers.base import (
    DEFAULT_PROVIDER,
    PROVIDER_NAMES,
    Capability,
    CloudProvider,
    ProviderName,
)
from cloudoptics.providers.registry import (
    UnknownProviderError,
    capability_for,
    connection_provider,
    find,
    get,
    register,
    registered,
)

__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDER_NAMES",
    "Capability",
    "CloudProvider",
    "ProviderName",
    "UnknownProviderError",
    "capability_for",
    "connection_provider",
    "find",
    "get",
    "register",
    "registered",
]
