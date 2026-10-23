"""Daily reference exchange rates (ADR-010: shown beside staff rates, never preferred)."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
import procrastinate

from cloudoptics.fx.service import store_feed
from cloudoptics.platform.db import get_sessionmaker
from cloudoptics.platform.settings import get_settings

blueprint = procrastinate.Blueprint()

# Currencies Cloudoptics shows (PRD FR-FX-01) plus the other tenant display currencies.
CURRENCIES = frozenset({"NGN", "KES", "ZAR", "EGP", "GHS", "GBP", "EUR", "RWF", "UGX", "TZS"})


class FeedError(Exception):
    pass


def parse_feed(body: object) -> dict[str, float]:
    if not isinstance(body, dict) or not isinstance(body.get("rates"), dict):
        raise FeedError("no rates in feed response")
    base = body.get("base_code") or body.get("base") or "USD"
    if base != "USD":
        raise FeedError(f"feed base is {base}, expected USD")
    rates: dict[str, float] = {}
    for ccy, value in body["rates"].items():
        if ccy in CURRENCIES and isinstance(value, int | float) and value > 0:
            rates[ccy] = float(value)
    return rates


async def fetch_feed(url: str, client: httpx.AsyncClient | None = None) -> dict[str, float]:
    async with client or httpx.AsyncClient(timeout=20) as http:
        response = await http.get(url)
        response.raise_for_status()
        return parse_feed(response.json())


@blueprint.periodic(cron="0 6 * * *")
@blueprint.task(name="fx.fetch", queue="fx", queueing_lock="fx.fetch", retry=3)
async def fetch(timestamp: int) -> int:
    """Daily 06:00 UTC (docs/09 §1.3): store today's reference rates, if a feed is set."""
    url = get_settings().fx_feed_url
    if not url:
        return 0
    rates = await fetch_feed(url)
    async with get_sessionmaker()() as session, session.begin():
        return await store_feed(
            session, rates, datetime.now(UTC).date(), f"feed:{urlsplit(url).hostname}"
        )
