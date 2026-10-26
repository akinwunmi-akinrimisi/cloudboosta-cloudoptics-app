"""List prices for every AWS region from the public AWS Price List (WBS IN-9, PRD FR-REC-02).

``python -m cloudoptics.analysis.price_list`` streams the on-demand offer files (AmazonEC2,
AWSELB, AmazonVPC and AmazonRDS, CSV, no AWS credentials needed) for each AWS Region and
writes ``migrations/data/price_list.json``: ``{region: {item: usd}}`` for the items in
``price_catalog``. A migration loads it. Run it again and add a migration to refresh prices.

Instance items hold the ``large`` hourly price of each family (Linux, shared tenancy; RDS
Single-AZ PostgreSQL and MySQL). A family with no ``large`` size in a region takes another
size's price scaled to ``large``, as the rules scale it back.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cloudoptics.analysis.compute import size_factor, split_type

BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws"
OFFERS = ("AmazonEC2", "AWSELB", "AmazonVPC", "AmazonRDS")
# AWS Regions only: Local Zones and Wavelength Zones (``us-east-1-bos-1``) have offer files
# of their own but their resources report the parent region.
REGION = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
# Usage types start with a region code (``EUW2-``, ``EU-`` for eu-west-1), except in
# us-east-1. Other prefixes (``TS-`` trust stores, ``Outposts-``) are different products.
USAGE_PREFIX = re.compile(r"^(?:[A-Z]{2,4}\d|EU)-")
OUT = Path(__file__).resolve().parents[3] / "migrations" / "data" / "price_list.json"

# Usage type (region prefix stripped) -> (item, divisor). Throughput is priced per
# GiBps-month; the catalogue holds MiBps-month.
USAGE_ITEMS = {
    "EBS:VolumeUsage.gp2": ("ebs_gp2_gb_month", 1),
    "EBS:VolumeUsage.gp3": ("ebs_gp3_gb_month", 1),
    "EBS:VolumeP-IOPS.gp3": ("ebs_gp3_iops_month", 1),
    "EBS:VolumeP-Throughput.gp3": ("ebs_gp3_throughput_month", 1024),
    "EBS:VolumeUsage.piops": ("ebs_io1_gb_month", 1),
    "EBS:VolumeP-IOPS.piops": ("ebs_io1_iops_month", 1),
    "EBS:VolumeUsage.io2": ("ebs_io2_gb_month", 1),
    "EBS:VolumeUsage.st1": ("ebs_st1_gb_month", 1),
    "EBS:VolumeUsage.sc1": ("ebs_sc1_gb_month", 1),
    "EBS:VolumeUsage": ("ebs_standard_gb_month", 1),
    "EBS:SnapshotUsage": ("ebs_snapshot_gb_month", 1),
    "EBS:SnapshotArchiveStorage": ("ebs_snapshot_archive_gb_month", 1),
    "PublicIPv4:IdleAddress": ("public_ipv4_hour", 1),
}
LB_ITEMS = {
    "Load Balancer": "clb_hour",
    "Load Balancer-Application": "alb_hour",
    "Load Balancer-Network": "nlb_hour",
    "Load Balancer-Gateway": "gwlb_hour",
}
RDS_ENGINES = {"PostgreSQL": "postgres", "MySQL": "mysql"}


def rows(text: Iterable[str]) -> Iterator[dict[str, str]]:
    """Rows of an offer CSV: five metadata lines, then a header."""
    reader = csv.reader(text)
    for _ in range(5):
        next(reader, None)
    header = next(reader, None) or []
    for row in reader:
        yield dict(zip(header, row, strict=False))


def _usage(row: dict[str, str]) -> str:
    return USAGE_PREFIX.sub("", row.get("usageType", ""), count=1)


def items(offer_rows: Iterable[dict[str, str]]) -> dict[str, float]:
    """Catalogue items from the rows of one region's offer files."""
    found: dict[str, float] = {}
    # family item -> (distance of the size from large, price scaled to large)
    families: dict[str, tuple[float, float]] = {}
    for row in offer_rows:
        if row.get("TermType") != "OnDemand" or row.get("Location Type") != "AWS Region":
            continue
        if row.get("StartingRange", "0") not in ("", "0"):
            continue
        try:
            price = float(row.get("PricePerUnit") or "")
        except ValueError:
            continue
        family = row.get("Product Family", "")
        usage = _usage(row)
        prefix = None
        if usage in USAGE_ITEMS:
            item, divisor = USAGE_ITEMS[usage]
            found[item] = price / divisor
        elif family in LB_ITEMS and usage == "LoadBalancerUsage":
            found[LB_ITEMS[family]] = price
        elif (
            family == "Compute Instance"
            and usage.startswith("BoxUsage:")
            and row.get("Tenancy") == "Shared"
            and row.get("Operating System") == "Linux"
            and row.get("Pre Installed S/W") == "NA"
            and row.get("CapacityStatus") == "Used"
            and row.get("License Model") == "No License required"
        ):
            prefix = "ec2_"
        elif (
            family == "Database Instance"
            and usage.startswith("InstanceUsage:")
            and row.get("Database Engine") in RDS_ENGINES
            and row.get("Deployment Option") == "Single-AZ"
        ):
            prefix = f"rds_{RDS_ENGINES[row['Database Engine']]}_"
        if prefix is None or price <= 0:
            continue
        split = split_type(row.get("Instance Type"))
        factor = size_factor(split[1]) if split else None
        if split is None or factor is None:
            continue
        key = f"{prefix}{split[0]}_large_hour"
        distance = abs(factor - 1)
        if key not in families or distance < families[key][0]:
            families[key] = (distance, price / factor)
    found.update({k: round(v, 8) for k, (_, v) in families.items()})
    return {k: round(v, 8) for k, v in sorted(found.items())}


def _offer_rows(offer: str, region: str) -> Iterator[dict[str, str]]:
    url = f"{BASE}/{offer}/current/{region}/index.csv"
    try:
        response = urllib.request.urlopen(url, timeout=300)  # noqa: S310 (fixed https URL)
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 404):  # the service is not offered in this region
            return
        raise
    with response:
        yield from rows(io.TextIOWrapper(response, encoding="utf-8", newline=""))


def region_items(region: str) -> dict[str, float]:
    return items(row for offer in OFFERS for row in _offer_rows(offer, region))


def regions() -> list[str]:
    url = f"{BASE}/AmazonEC2/current/region_index.json"
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310
        return sorted(r for r in json.load(response)["regions"] if REGION.match(r))


def main() -> None:
    wanted = regions()
    with ThreadPoolExecutor(max_workers=6) as pool:
        prices = dict(zip(wanted, pool.map(region_items, wanted), strict=True))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(prices, indent=1, sort_keys=True) + "\n")
    count = sum(len(v) for v in prices.values())
    print(f"{count} prices in {len(prices)} regions -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
