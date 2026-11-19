"""Rate limits (WBS API-8, docs/09 §3.1 "Rate limiting", docs/10 T5/T9).

Fixed one-minute windows counted in PostgreSQL (``rate_limit_counter``, migration 0031), so
every API instance shares the same counts and no extra service is needed. Each hit is one
upsert on its own short autocommit connection, never inside the request's transaction, so a
counter row is locked only for that statement. A fixed window lets a client send up to twice
the limit across a window boundary; that is acceptable for abuse protection and keeps a hit
to a single indexed write (a sliding window needs the previous window's count too).

If the counter cannot be written (the database is unreachable), the request is let through:
it will fail on its own database work anyway, and the limiter must not be the outage.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import math
import random
from collections.abc import Awaitable, Callable

from fastapi import Request, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from cloudoptics.platform.api import ApiProblem, client_ip
from cloudoptics.platform.db import get_side_engine
from cloudoptics.platform.settings import get_settings

log = logging.getLogger(__name__)

WINDOW_SECONDS = 60
# Counters older than this are deleted by a small share of hits (any instance).
_KEEP = "10 minutes"
_CLEANUP_SHARE = 0.01

HIT = text(
    "INSERT INTO rate_limit_counter AS c (bucket, window_start, hits) "
    "VALUES (:bucket, to_timestamp(floor(extract(epoch FROM now()) / :w) * :w), 1) "
    "ON CONFLICT (bucket, window_start) DO UPDATE SET hits = c.hits + 1 "
    "RETURNING hits, extract(epoch FROM c.window_start + make_interval(secs => :w) - now()) "
    "AS remaining"
)
CLEANUP = text(
    "DELETE FROM rate_limit_counter WHERE window_start < now() - CAST(:keep AS interval)"
)


def rate_limited_problem(scope: str, limit: int, retry_after: int) -> ApiProblem:
    """429 in the problem format, with ``Retry-After`` and the IETF RateLimit headers."""
    return ApiProblem(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "rate_limited",
        f"Too many requests. Try again in {retry_after} seconds.",
        headers={
            "Retry-After": str(retry_after),
            "RateLimit-Policy": f'"{scope}";q={limit};w={WINDOW_SECONDS}',
            "RateLimit": f'"{scope}";r=0;t={retry_after}',
        },
    )


async def hit(bucket: str, limit: int, scope: str) -> None:
    """Count one request against ``bucket``; raise 429 once it is over ``limit``."""
    if not get_settings().rate_limit_enabled or limit <= 0:
        return
    try:
        async with get_side_engine().connect() as conn:
            row = (await conn.execute(HIT, {"bucket": bucket, "w": WINDOW_SECONDS})).one()
            if random.random() < _CLEANUP_SHARE:  # noqa: S311 - load spreading, not security
                await conn.execute(CLEANUP, {"keep": _KEEP})
            await conn.commit()
    except (SQLAlchemyError, OSError) as exc:
        log.warning("rate limit not checked for %s: %s", scope, exc)
        return
    if row.hits > limit:
        retry_after = max(1, math.ceil(float(row.remaining)))
        raise rate_limited_problem(scope, limit, retry_after)


def _ip_key(ip: str | None) -> str:
    """Count IPv6 clients per /64 (one subscriber's block), IPv4 per address. Stored as a
    hash: counters need no readable address."""
    try:
        addr = ipaddress.ip_address(ip or "")
    except ValueError:
        raw = "unknown"
    else:
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if isinstance(addr, ipaddress.IPv6Address):
            raw = str(ipaddress.ip_network(f"{addr}/64", strict=False))
        else:
            raw = str(addr)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def per_ip(route: str, limit: Callable[[], int]) -> Callable[[Request], Awaitable[None]]:
    """Dependency for public routes: at most ``limit()`` requests a minute per client."""

    async def dependency(request: Request) -> None:
        await hit(f"ip:{route}:{_ip_key(client_ip(request))}", limit(), f"ip-{route}")

    return dependency


async def per_user(subject: str) -> None:
    """Per signed-in user, keyed by the identity provider's subject (hashed)."""
    key = hashlib.sha256(subject.encode()).hexdigest()[:32]
    await hit(f"user:{key}", get_settings().rate_limit_user_per_minute, "user")


async def per_tenant(tenant_id: object) -> None:
    await hit(f"tenant:{tenant_id}", get_settings().rate_limit_tenant_per_minute, "tenant")
