"""ETags, conditional GETs and the aggregate query cache (WBS API-8, docs/09 §3.1 "Caching").

The tenant's **data version** (``tenant_data_version``, migration 0031) goes up by one with
every committed transaction that changed the tenant's costs, connections, inventory,
recommendations, budgets, anomalies, reports, credits, commitments or settings; triggers
keep it, so no write path can forget. ``fx_data_version`` does the same for the shared
exchange rates. A heavy read's weak ETag hashes:

- the API version (a deploy can change a response's shape),
- the tenant, the user and their role (responses can depend on who asks: ``assignee=me``),
- both data versions,
- a 30-minute time bucket (``today`` defaults to the current date, UTC or the workspace's
  time zone; 30 minutes covers every whole- and half-hour zone),
- the path and the sorted query string.

``If-None-Match`` with that ETag gets ``304 Not Modified`` after the access check and one
small query, without running the endpoint. Responses carry ``Cache-Control: private,
no-cache`` (browsers may keep tenant data but must revalidate; shared caches must not keep
it) and ``Vary: Authorization``.

``Conditional.cached`` keeps the costliest aggregate responses (costs, trends, reports) in a
small per-process LRU keyed by tenant, both data versions, the time bucket, path and query.
It is only used for responses that do not depend on the user, and only after the RLS-scoped
access check passed, so it can neither cross tenants nor serve data a user could not read:
the rows came from RLS under that tenant, and a new data version is a new key.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypeVar
from urllib.parse import urlencode

from fastapi import Depends, Request, Response
from sqlalchemy import text

from cloudoptics import __version__
from cloudoptics.identity.deps import Session, ViewerAccess
from cloudoptics.platform.settings import get_settings

T = TypeVar("T")

BUCKET_SECONDS = 1800
CACHE_CONTROL = "private, no-cache"

VERSIONS = text(
    "SELECT coalesce((SELECT version FROM tenant_data_version "
    "WHERE tenant_id = app_current_tenant()), 0) AS tenant, "
    "(SELECT version FROM fx_data_version) AS fx"
)


class NotModified(Exception):  # noqa: N818 - named after the HTTP status
    """Answered as ``304 Not Modified`` (main.py) with these headers and no body."""

    def __init__(self, headers: dict[str, str]) -> None:
        super().__init__("not modified")
        self.headers = headers


class QueryCache:
    """A small LRU with a TTL as a safety net; keys always start with the tenant ID."""

    def __init__(self) -> None:
        self._items: OrderedDict[tuple[Any, ...], tuple[float, Any]] = OrderedDict()

    def get(self, key: tuple[Any, ...]) -> Any | None:
        item = self._items.get(key)
        if item is None:
            return None
        expires, value = item
        if expires < time.monotonic():
            del self._items[key]
            return None
        self._items.move_to_end(key)
        return value

    def put(self, key: tuple[Any, ...], value: Any) -> None:
        settings = get_settings()
        if settings.query_cache_entries <= 0:
            return
        self._items[key] = (time.monotonic() + settings.query_cache_ttl_seconds, value)
        self._items.move_to_end(key)
        while len(self._items) > settings.query_cache_entries:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


query_cache = QueryCache()


@dataclass(frozen=True)
class Conditional:
    etag: str
    cache_key: tuple[Any, ...]

    async def cached(self, load: Callable[[], Awaitable[T]]) -> T:
        """``load()``'s result from the query cache, or computed and kept. Only for
        responses that are the same for every member of the tenant. Callers must not
        change the returned value."""
        hit = query_cache.get(self.cache_key)
        if hit is not None:
            return hit  # type: ignore[no-any-return]
        value = await load()
        query_cache.put(self.cache_key, value)
        return value


def etag_matches(header: str | None, etag: str) -> bool:
    """Weak comparison (RFC 9110 §13.1.2): ``W/"x"`` matches ``"x"`` and ``W/"x"``."""
    if not header:
        return False
    opaque = etag.removeprefix("W/")
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate.removeprefix("W/") == opaque:
            return True
    return False


async def conditional(
    request: Request, response: Response, access: ViewerAccess, session: Session
) -> Conditional:
    """Dependency for heavy tenant GETs: sets the ETag and cache headers, or answers 304."""
    versions = (await session.execute(VERSIONS)).one()
    bucket = int(time.time() // BUCKET_SECONDS)
    query = urlencode(sorted(request.query_params.multi_items()))
    shared = (access.tenant_id, versions.tenant, versions.fx, bucket, request.url.path, query)
    material = "|".join(str(part) for part in (__version__, *shared, access.user.id, access.role))
    etag = f'W/"{hashlib.sha256(material.encode()).hexdigest()[:32]}"'
    headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL, "Vary": "Authorization"}
    if etag_matches(request.headers.get("if-none-match"), etag):
        raise NotModified(headers)
    response.headers.update(headers)
    return Conditional(etag=etag, cache_key=(__version__, *shared))


ConditionalGet = Annotated[Conditional, Depends(conditional)]
