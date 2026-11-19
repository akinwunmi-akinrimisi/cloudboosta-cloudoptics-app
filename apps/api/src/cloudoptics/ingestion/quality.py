"""Data-quality checks per export version (docs/11b §2.4, PRD FR-ING-09).

``error`` blocks the load: the previous good version stays live. ``warning`` loads the data
and is kept with the batch for review.

The duplicate check keeps no per-row strings in memory: each line item key (ID and time
interval) becomes a 16-byte keyed BLAKE2b fingerprint, written to one of ``KEY_PARTITIONS``
temporary files by its first byte; ``finish`` counts the distinct fingerprints one file at a
time. Memory is one partition's fingerprints, and a false duplicate needs two different keys
with the same 128-bit fingerprint, which does not happen in practice.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Literal

import pyarrow as pa
import pyarrow.compute as _pc

from cloudoptics.ingestion.focus import REQUIRED_COLUMNS, unknown_line_item_types

# pyarrow-stubs overloads are narrower than what compute functions accept at runtime.
pc: Any = _pc

Severity = Literal["ok", "warning", "error"]
NEGATIVE_ALLOWED = frozenset({"Credit", "Adjustment"})
NEGATIVE_LINE_TYPES = frozenset({"SavingsPlanNegation"})
KEY_PARTITIONS = 16
FINGERPRINT_BYTES = 16


@dataclass(frozen=True)
class Check:
    id: str
    status: Severity
    message: str

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def missing_columns(schema: pa.Schema) -> list[str]:
    return [c for c in REQUIRED_COLUMNS if schema.get_field_index(c) < 0]


class QualityChecks:
    """Accumulates per-batch evidence; ``finish`` turns it into checks."""

    def __init__(self, billing_period: date) -> None:
        self._period_start = billing_period
        nxt = (billing_period.replace(day=28) + timedelta(days=4)).replace(day=1)
        self._period_end = nxt
        self.rows = 0
        self._null_key_rows = 0
        self._out_of_period = 0
        self._unexpected_negative = 0.0
        self._currencies: set[str] = set()
        self._unknown_types: set[str] = set()
        self._missing: set[str] = set()
        self._key_count = 0
        self._hash_key = os.urandom(16)
        self._spill: tempfile.TemporaryDirectory[str] | None = None
        self._parts: list[BinaryIO] = []

    def schema(self, schema: pa.Schema) -> None:
        self._missing.update(missing_columns(schema))

    def update(self, raw: pa.RecordBatch, focus: pa.Table) -> None:
        self.rows += focus.num_rows
        self._unknown_types |= unknown_line_item_types(raw)
        nulls = pc.or_(
            pc.or_(pc.is_null(focus["BilledCost"]), pc.is_null(focus["ChargePeriodStart"])),
            pc.is_null(focus["SubAccountId"]),
        )
        self._null_key_rows += pc.sum(pc.cast(nulls, pa.int64())).as_py() or 0
        # BilledCost nulls were filled with 0 in normalise; count the raw column too.
        cost_idx = raw.schema.get_field_index("line_item_unblended_cost")
        if cost_idx >= 0:
            self._null_key_rows += raw.column(cost_idx).null_count

        day = focus["ChargeDate"]
        lo = pa.scalar(self._period_start - timedelta(days=1), pa.date32())
        hi = pa.scalar(self._period_end + timedelta(days=1), pa.date32())
        outside = pc.or_(pc.less(day, lo), pc.greater_equal(day, hi))
        self._out_of_period += (
            pc.sum(pc.cast(pc.fill_null(outside, False), pa.int64())).as_py() or 0
        )

        negative = pc.and_(
            pc.less(focus["BilledCost"], 0),
            pc.invert(
                pc.or_(
                    pc.is_in(focus["ChargeCategory"], pa.array(sorted(NEGATIVE_ALLOWED))),
                    pc.is_in(focus["x_LineItemType"], pa.array(sorted(NEGATIVE_LINE_TYPES))),
                )
            ),
        )
        neg_sum = pc.sum(pc.filter(focus["BilledCost"], negative)).as_py()
        self._unexpected_negative += neg_sum or 0.0

        self._currencies |= {c for c in pc.unique(focus["BillingCurrency"]).to_pylist() if c}
        keys = pc.binary_join_element_wise(
            pc.fill_null(focus["x_LineItemId"], ""),
            pc.fill_null(focus["x_TimeInterval"], ""),
            "|",
        )
        self._spill_keys(keys.to_pylist())

    def _spill_keys(self, keys: list[str | None]) -> None:
        if not self._parts:
            self._spill = tempfile.TemporaryDirectory(prefix="cur-keys-")
            self._parts = [
                (Path(self._spill.name) / f"{i:02d}.bin").open("wb") for i in range(KEY_PARTITIONS)
            ]
        buffers = [bytearray() for _ in range(KEY_PARTITIONS)]
        for key in keys:
            digest = hashlib.blake2b(
                (key or "").encode(), digest_size=FINGERPRINT_BYTES, key=self._hash_key
            ).digest()
            buffers[digest[0] % KEY_PARTITIONS] += digest
        for part, buffer in zip(self._parts, buffers, strict=True):
            part.write(buffer)
        self._key_count += len(keys)

    def _distinct_keys(self) -> int:
        """Distinct fingerprints, one partition in memory at a time."""
        distinct = 0
        try:
            for part in self._parts:
                part.close()
                data = Path(part.name).read_bytes()
                n = len(data) // FINGERPRINT_BYTES
                if n:
                    array = pa.Array.from_buffers(
                        pa.binary(FINGERPRINT_BYTES), n, [None, pa.py_buffer(data)]
                    )
                    distinct += pc.count_distinct(array).as_py()
        finally:
            self.close()
        return distinct

    def close(self) -> None:
        """Remove the fingerprint files (``finish`` does it)."""
        for part in self._parts:
            part.close()
        self._parts = []
        if self._spill is not None:
            self._spill.cleanup()
            self._spill = None

    def finish(self, manifest_rows: int | None) -> list[Check]:
        checks: list[Check] = []
        if self._missing:
            checks.append(
                Check("schema", "error", f"Missing columns: {', '.join(sorted(self._missing))}.")
            )
        else:
            checks.append(Check("schema", "ok", "All required columns are present."))

        if manifest_rows is not None and manifest_rows != self.rows:
            checks.append(
                Check(
                    "completeness",
                    "error",
                    f"Read {self.rows} rows; the manifest lists {manifest_rows}.",
                )
            )
        else:
            checks.append(Check("completeness", "ok", f"{self.rows} rows read."))

        if self._null_key_rows:
            checks.append(
                Check(
                    "nulls",
                    "error",
                    f"{self._null_key_rows} rows have no cost, charge period or account.",
                )
            )
        else:
            checks.append(Check("nulls", "ok", "No missing key values."))

        distinct = self._distinct_keys()
        if distinct != self._key_count:
            checks.append(
                Check(
                    "duplicates",
                    "error",
                    f"{self._key_count - distinct} duplicate line items "
                    "(same ID and time interval).",
                )
            )
        else:
            checks.append(Check("duplicates", "ok", "No duplicate line items."))

        if self._currencies - {"USD"}:
            checks.append(
                Check(
                    "currency",
                    "error",
                    "Billing currency "
                    f"{', '.join(sorted(self._currencies - {'USD'}))} is not supported yet.",
                )
            )

        if self._out_of_period:
            checks.append(
                Check(
                    "period_bounds",
                    "warning",
                    f"{self._out_of_period} rows are dated outside the billing period.",
                )
            )
        if self._unexpected_negative < 0:
            checks.append(
                Check(
                    "negative_costs",
                    "warning",
                    f"USD {self._unexpected_negative:.2f} of negative cost outside credits "
                    "and discounts.",
                )
            )
        if self._unknown_types:
            checks.append(
                Check(
                    "line_item_types",
                    "warning",
                    "Unmapped line item types (counted as Adjustment): "
                    f"{', '.join(sorted(self._unknown_types))}.",
                )
            )
        return checks


def blocked(checks: list[Check]) -> bool:
    return any(c.status == "error" for c in checks)
