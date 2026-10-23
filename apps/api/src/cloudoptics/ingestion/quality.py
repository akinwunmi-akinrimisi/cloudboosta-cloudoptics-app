"""Data-quality checks per export version (docs/11b §2.4, PRD FR-ING-09).

``error`` blocks the load: the previous good version stays live. ``warning`` loads the data
and is kept with the batch for review.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from typing import Any, Literal

import pyarrow as pa
import pyarrow.compute as _pc

from cloudoptics.ingestion.focus import REQUIRED_COLUMNS, unknown_line_item_types

# pyarrow-stubs overloads are narrower than what compute functions accept at runtime.
pc: Any = _pc

Severity = Literal["ok", "warning", "error"]
NEGATIVE_ALLOWED = frozenset({"Credit", "Adjustment"})
NEGATIVE_LINE_TYPES = frozenset({"SavingsPlanNegation"})


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
        self._keys: list[Any] = []
        self._missing: set[str] = set()

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
        self._keys.append(
            pc.binary_join_element_wise(
                pc.fill_null(focus["x_LineItemId"], ""),
                pc.fill_null(focus["x_TimeInterval"], ""),
                "|",
            )
        )

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

        keys = (
            pa.chunked_array(self._keys, pa.string())
            if self._keys
            else pa.chunked_array([], pa.string())
        )
        distinct = pc.count_distinct(keys).as_py() if len(keys) else 0
        if distinct != len(keys):
            checks.append(
                Check(
                    "duplicates",
                    "error",
                    f"{len(keys) - distinct} duplicate line items (same ID and time interval).",
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
