"""Daily aggregates for ``cost_daily`` (docs/11b §2.6, PRD FR-ING-08).

Grain: day x account x service x region x usage type x charge category x tag set. Batches
are aggregated as they stream in and merged, so a period never has to fit in memory.
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
