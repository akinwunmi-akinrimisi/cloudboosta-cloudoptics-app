"""Daily aggregates for ``cost_daily`` (docs/11b §2.6, PRD FR-ING-08).

Grain: day x account x service x region x usage type x charge category x tag set. Batches
are aggregated as they stream in and merged, so a period never has to fit in memory.

Usage cost per resource and day (``cost_resource_daily``) is kept alongside, for the
before and after comparison of realised savings (PRD US-5.2).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

KEY_COLUMNS = (
    "ChargeDate",
    "SubAccountId",
    "x_ServiceCode",
    "ServiceName",
    "ServiceCategory",
    "RegionId",
    "x_UsageType",
    "ChargeCategory",
    "_tag_hash",
    "x_CommitmentKind",
)
SUM_COLUMNS = ("BilledCost", "EffectiveCost", "x_NetCost", "ListCost", "ConsumedQuantity")
_KIND = KEY_COLUMNS.index("x_CommitmentKind")

# Usage cost per resource, day and usage type: read by the billing-only rules (R6b, R10)
# and by verification's before/after comparison (realised savings).
RESOURCE_KEY_COLUMNS = (
    "ChargeDate",
    "SubAccountId",
    "x_ServiceCode",
    "RegionId",
    "x_UsageType",
    "ResourceId",
    "_tag_hash",
)


@dataclass(frozen=True)
class DailyCost:
    usage_date: date
    account_id: str
    service_code: str
    service_name: str
    service_category: str
    region_id: str | None
    usage_type: str | None
    charge_category: str
    tag_set_hash: bytes | None
    billed_cost: Decimal
    effective_cost: Decimal
    net_cost: Decimal
    list_cost: Decimal | None
    usage_quantity: Decimal | None


@dataclass(frozen=True)
class ResourceDailyCost:
    usage_date: date
    account_id: str
    service_code: str
    region_id: str | None
    usage_type: str
    resource_id: str
    tag_set_hash: bytes | None
    effective_cost: Decimal
    usage_quantity: Decimal


@dataclass
class _Sums:
    billed: float = 0.0
    effective: float = 0.0
    net: float = 0.0
    list_cost: float | None = None
    quantity: float | None = None

    def add(self, other: _Sums) -> None:
        self.billed += other.billed
        self.effective += other.effective
        self.net += other.net
        if other.list_cost is not None:
            self.list_cost = (self.list_cost or 0.0) + other.list_cost
        if other.quantity is not None:
            self.quantity = (self.quantity or 0.0) + other.quantity


def tag_hash(tags: list[tuple[str, str]] | None) -> tuple[bytes | None, dict[str, str]]:
    """SHA-256 of the sorted tag pairs; ``None`` for untagged rows."""
    if not tags:
        return None, {}
    pairs = dict(sorted(tags))
    canonical = json.dumps(pairs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).digest(), pairs


def _money(value: float) -> Decimal:
    return Decimal(f"{value:.6f}")


@dataclass
class DailyAggregator:
    tag_sets: dict[bytes, dict[str, str]] = field(default_factory=dict)
    accounts: dict[str, str | None] = field(default_factory=dict)
    _sums: dict[tuple[Any, ...], _Sums] = field(default_factory=dict)
    _resource_sums: dict[tuple[Any, ...], list[float]] = field(default_factory=dict)

    def update(self, focus: pa.Table) -> None:
        hashes: list[bytes | None] = []
        for tags in focus["Tags"].to_pylist():
            digest, pairs = tag_hash(tags)
            if digest is not None:
                self.tag_sets.setdefault(digest, pairs)
            hashes.append(digest)
        table = focus.append_column("_tag_hash", pa.array(hashes, pa.binary()))

        for acct, name in zip(
            table["SubAccountId"].to_pylist(), table["SubAccountName"].to_pylist(), strict=True
        ):
            if acct and (name or acct not in self.accounts):
                self.accounts[acct] = name

        grouped = table.group_by(list(KEY_COLUMNS), use_threads=False).aggregate(
            [(c, "sum") for c in SUM_COLUMNS]
            + [("ListCost", "count"), ("ConsumedQuantity", "count")]
        )
        columns = [grouped[c].to_pylist() for c in KEY_COLUMNS]
        billed, effective, net, list_cost, quantity = (
            grouped[f"{c}_sum"].to_pylist() for c in SUM_COLUMNS
        )
        list_n = grouped["ListCost_count"].to_pylist()
        quantity_n = grouped["ConsumedQuantity_count"].to_pylist()
        for i, key in enumerate(zip(*columns, strict=True)):
            sums = _Sums(
                billed[i] or 0.0,
                effective[i] or 0.0,
                net[i] or 0.0,
                list_cost[i] if list_n[i] else None,
                quantity[i] if quantity_n[i] else None,
            )
            if key in self._sums:
                self._sums[key].add(sums)
            else:
                self._sums[key] = sums
        self._update_resources(table)

    def _update_resources(self, table: pa.Table) -> None:
        no: Any = pa.scalar(False)
        named: Any = pc.not_equal(table["ResourceId"], pa.scalar(""))
        account: Any = pc.is_valid(table["SubAccountId"])
        wanted = pc.and_(
            pc.and_(pc.fill_null(named, no), account),
            pc.equal(table["ChargeCategory"], pa.scalar("Usage")),
        )
        rows = table.filter(wanted)
        if rows.num_rows == 0:
            return
        grouped = rows.group_by(list(RESOURCE_KEY_COLUMNS), use_threads=False).aggregate(
            [("EffectiveCost", "sum"), ("ConsumedQuantity", "sum")]
        )
        columns = [grouped[c].to_pylist() for c in RESOURCE_KEY_COLUMNS]
        cost = grouped["EffectiveCost_sum"].to_pylist()
        quantity = grouped["ConsumedQuantity_sum"].to_pylist()
        for i, key in enumerate(zip(*columns, strict=True)):
            sums = self._resource_sums.setdefault(key, [0.0, 0.0])
            sums[0] += cost[i] or 0.0
            sums[1] += quantity[i] or 0.0

    def resource_rows(self) -> list[ResourceDailyCost]:
        rows = [
            ResourceDailyCost(
                usage_date=key[0],
                account_id=key[1],
                service_code=key[2],
                region_id=key[3],
                usage_type=key[4],
                resource_id=key[5],
                tag_set_hash=key[6],
                effective_cost=_money(cost),
                usage_quantity=_money(quantity),
            )
            for key, (cost, quantity) in self._resource_sums.items()
        ]
        rows.sort(key=lambda r: (r.usage_date, r.account_id, r.resource_id, r.usage_type))
        return rows

    def _amortise_commitment_fees(self) -> None:
        """Take each day's used commitment off that day's recurring fees.

        Used when the export lacks commitment columns (see ``focus``): covered usage already
        carries its effective cost, so the fee keeps only the unused part.
        """
        used: dict[tuple[date, str], float] = defaultdict(float)
        fees: dict[tuple[date, str], list[_Sums]] = defaultdict(list)
        for key, sums in self._sums.items():
            kind = key[_KIND]
            if kind in ("sp_used", "ri_used"):
                used[(key[0], kind[:2])] += sums.effective
            elif kind in ("sp_fee", "ri_fee"):
                fees[(key[0], kind[:2])].append(sums)
        for slot, fee_rows in fees.items():
            total_fee = sum(s.effective for s in fee_rows)
            take = min(used.get(slot, 0.0), total_fee)
            if total_fee <= 0 or take <= 0:
                continue
            for s in fee_rows:
                s.effective -= take * s.effective / total_fee

    def finish(self) -> list[DailyCost]:
        self._amortise_commitment_fees()
        merged: dict[tuple[Any, ...], _Sums] = {}
        for key, sums in self._sums.items():
            merged.setdefault(key[:_KIND], _Sums()).add(sums)
        rows = [
            DailyCost(
                usage_date=key[0],
                account_id=key[1],
                service_code=key[2],
                service_name=key[3],
                service_category=key[4],
                region_id=key[5],
                usage_type=key[6],
                charge_category=key[7],
                tag_set_hash=key[8],
                billed_cost=_money(s.billed),
                effective_cost=_money(s.effective),
                net_cost=_money(s.net),
                list_cost=None if s.list_cost is None else _money(s.list_cost),
                usage_quantity=None if s.quantity is None else _money(s.quantity),
            )
            for key, s in merged.items()
        ]
        rows.sort(key=lambda r: (r.usage_date, r.account_id, r.service_code, r.charge_category))
        return rows
