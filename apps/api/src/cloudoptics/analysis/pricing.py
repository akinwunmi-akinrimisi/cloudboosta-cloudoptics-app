"""Unit prices for savings estimates (PRD FR-REC-02).

The tenant's own effective rate from its CUR comes first (it carries discounts and private
pricing): cost divided by usage over the last 30 days of data, per account, region and
usage type. Items with no usage of their own take the list price scaled by the discount
seen on the account's other EBS usage in that region. Without CUR data, list prices from
``price_catalog`` are used: the region's own row, else the us-east-1 fallback (``*``),
which is then flagged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

HOURS_PER_MONTH = 730

# CUR usage types (region prefix stripped) that measure a catalogue item directly.
USAGE_ITEMS = {
    "EBS:VolumeUsage.gp2": "ebs_gp2_gb_month",
    "EBS:VolumeUsage.gp3": "ebs_gp3_gb_month",
    "EBS:VolumeUsage.piops": "ebs_io1_gb_month",
    "EBS:VolumeUsage.io2": "ebs_io2_gb_month",
    "EBS:VolumeUsage.st1": "ebs_st1_gb_month",
    "EBS:VolumeUsage.sc1": "ebs_sc1_gb_month",
    "EBS:VolumeUsage": "ebs_standard_gb_month",
    "EBS:SnapshotUsage": "ebs_snapshot_gb_month",
    "PublicIPv4:IdleAddress": "public_ipv4_hour",
    "PublicIPv4:InUseAddress": "public_ipv4_hour",
}
_PREFIX = re.compile(r"^[A-Z]{2,4}\d?(?:-[A-Z0-9]+)?-(?=[A-Za-z0-9]+:)")

Basis = Literal["effective_rate", "list_price"]


def usage_item(usage_type: str | None) -> str | None:
    if not usage_type:
        return None
    return USAGE_ITEMS.get(_PREFIX.sub("", usage_type))


@dataclass(frozen=True)
class Price:
    usd: float
    basis: Basis
    fallback_region: bool = False


@dataclass
class PriceBook:
    """Prices for one tenant: ``(account, region, item) -> effective $/unit`` and list."""

    list_prices: dict[tuple[str, str], float]
    effective: dict[tuple[str, str, str], float] = field(default_factory=dict)

    def list_price(self, region: str, item: str) -> tuple[float, bool]:
        if (region, item) in self.list_prices:
            return self.list_prices[(region, item)], False
        return self.list_prices[("*", item)], True

    def price(self, account: str, region: str, item: str) -> Price:
        listed, fallback = self.list_price(region, item)
        own = self.effective.get((account, region, item))
        if own is not None:
            return Price(own, "effective_rate")
        if item.startswith("ebs_"):
            refs = [
                (rate, *self.list_price(region, other))
                for (a, r, other), rate in self.effective.items()
                if a == account and r == region and other.startswith("ebs_")
            ]
            if refs:
                factor = sum(rate / ref for rate, ref, _ in refs) / len(refs)
                # Against us-east-1 fallback prices the ratio also carries the region's price
                # level (London is about 16% dearer), so it is only a discount, capped at 1,
                # when the region's own list prices are known.
                if not (fallback and all(ref_fallback for _, _, ref_fallback in refs)):
                    factor = min(1.0, factor)
                return Price(listed * factor, "effective_rate")
        return Price(listed, "list_price", fallback)


async def load_price_book(session: AsyncSession) -> PriceBook:
    """Inside a tenant-scoped transaction."""
    book = PriceBook(
        list_prices={
            (r.region_id, r.item): float(r.price_usd)
            for r in await session.execute(
                text("SELECT region_id, item, price_usd FROM price_catalog")
            )
        }
    )
    last = (await session.execute(text("SELECT max(usage_date) FROM cost_daily"))).scalar_one()
    if last is None:
        return book
    totals: dict[tuple[str, str, str], list[float]] = {}
    for r in await session.execute(
        text(
            "SELECT account_id, region_id, usage_type, sum(effective_cost_usd) AS cost, "
            "sum(usage_quantity) AS qty FROM cost_daily WHERE usage_date > :since "
            "AND charge_category = 'Usage' AND region_id IS NOT NULL "
            "AND (usage_type LIKE '%EBS:%' OR usage_type LIKE '%PublicIPv4:%') "
            "GROUP BY account_id, region_id, usage_type"
        ),
        {"since": last - timedelta(days=30)},
    ):
        item = usage_item(r.usage_type)
        if item is None or not r.qty:
            continue
        key = (r.account_id.strip(), r.region_id, item)
        acc = totals.setdefault(key, [0.0, 0.0])
        acc[0] += float(r.cost)
        acc[1] += float(r.qty)
    book.effective = {k: cost / qty for k, (cost, qty) in totals.items() if qty > 0 and cost > 0}
    return book
