"""Provider names in what people read: emails, digests, reports and API messages.

A connection is named by its provider and what it reads: "AWS account 123456789012",
"Google Cloud billing account 012345-6789AB-CDEF01", "Azure subscription <GUID>" or
"Azure billing account <ID>", "DigitalOcean team <UUID>". Workspace-wide text (a digest's
subject, a report's notes) names the one provider a workspace uses, or speaks of its clouds
when it has several. A missing provider is AWS, as everywhere else (ADR-018).
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.providers.base import DEFAULT_PROVIDER, PROVIDER_NAMES

#: The registry's labels (tests/test_provider_naming.py keeps the two the same).
LABELS: dict[str, str] = {
    "aws": "AWS",
    "gcp": "Google Cloud",
    "azure": "Azure",
    "digitalocean": "DigitalOcean",
}

WORKSPACE_PROVIDERS = text("SELECT DISTINCT provider FROM cloud_connection")


def provider_label(provider: str | None) -> str:
    name = provider or DEFAULT_PROVIDER
    return LABELS.get(name, name)


def account_kind(provider: str | None, account_type: str | None = None) -> str:
    """What one connection reads: account, billing account, subscription or team."""
    name = provider or DEFAULT_PROVIDER
    if name == "gcp":
        return "billing account"
    if name == "azure":
        # An Azure billing-account connection is stored as a management (payer) connection.
        return "billing account" if account_type == "management" else "subscription"
    if name == "digitalocean":
        return "team"
    return "account"


def connection_label(provider: str | None, account_id: str, account_type: str | None = None) -> str:
    """ "AWS account 123456789012", "Azure subscription …", "DigitalOcean team …"."""
    return f"{provider_label(provider)} {account_kind(provider, account_type)} {account_id.strip()}"


def ordered(providers: Iterable[str | None]) -> list[str]:
    """The distinct providers in the schema's order; none at all reads as AWS."""
    seen = {p or DEFAULT_PROVIDER for p in providers}
    return [p for p in PROVIDER_NAMES if p in seen] or [DEFAULT_PROVIDER]


def aws_only(providers: Iterable[str | None]) -> bool:
    return ordered(providers) == ["aws"]


def single_label(providers: Iterable[str | None]) -> str | None:
    """The provider's label when the workspace uses exactly one, else None."""
    names = ordered(providers)
    return provider_label(names[0]) if len(names) == 1 else None


def clouds(providers: Iterable[str | None], several: str = "cloud") -> str:
    """ "AWS" or "Google Cloud" for one provider; ``several`` for more than one."""
    return single_label(providers) or several


def where(providers: Iterable[str | None]) -> str:
    """Where a change is checked: "AWS", "Azure", or "your cloud accounts"."""
    return clouds(providers, "your cloud accounts")


def credits_title(providers: Iterable[str | None]) -> str:
    one = single_label(providers)
    return f"{one} credits" if one else "Credits"


def bills_source(providers: Iterable[str | None]) -> str:
    """Where the figures come from, for a report's notes."""
    names = ordered(providers)
    if names == ["aws"]:
        return "the AWS bills (CUR 2.0)"
    if len(names) == 1:
        return f"the {provider_label(names[0])} bills"
    return (
        "the bills of "
        + ", ".join(provider_label(n) for n in names[:-1])
        + (f" and {provider_label(names[-1])}")
    )


def provider_total(providers: Iterable[str | None]) -> str:
    """What loaded bills are reconciled against."""
    names = ordered(providers)
    if names == ["aws"]:
        return "Cost Explorer"
    if len(names) == 1:
        return f"{provider_label(names[0])}'s own total"
    return "each provider's own total"


async def workspace_providers(session: AsyncSession) -> list[str]:
    """The providers of the workspace's connections (tenant scope already set)."""
    return ordered(str(p) for p in (await session.execute(WORKSPACE_PROVIDERS)).scalars())
