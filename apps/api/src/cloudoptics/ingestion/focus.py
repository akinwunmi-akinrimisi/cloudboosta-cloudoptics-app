"""Map CUR 2.0 record batches to the FOCUS-aligned line-item schema (docs/09 §2.2.1).

The mapping is versioned (``MAPPING_VERSION``): every batch and every ``cost_daily`` row
records the version that produced it, so a mapping change can be replayed per period.

Cost metrics (PRD FR-ING-04):

- ``BilledCost``: unblended cost, what the invoice shows.
- ``EffectiveCost``: amortised. Savings Plan / RI covered usage carries its effective cost,
  negations carry zero, upfront fees are spread into covered usage (zero here), and the
  recurring commitment fee keeps only its unused part. When the export has no commitment
  columns, the fee is marked (``x_CommitmentKind``) and ``aggregate`` takes the used part
  off per day.
- ``x_NetCost``: net unblended cost (after discounts). Credits and refunds are their own
  ``Credit`` lines, so summing every category gives spend after credits.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pyarrow as pa
import pyarrow.compute as _pc

# pyarrow-stubs overloads are narrower than what compute functions accept at runtime.
pc: Any = _pc

MAPPING_VERSION = "cur2-focus-1"

# pyarrow-stubs overloads are narrower than what compute functions accept; arrays flowing
# through compute chains are typed loosely.
Arrow = Any

REQUIRED_COLUMNS: tuple[str, ...] = (
    "identity_line_item_id",
    "identity_time_interval",
    "bill_payer_account_id",
    "line_item_usage_account_id",
    "line_item_line_item_type",
    "line_item_usage_start_date",
    "line_item_unblended_cost",
    "line_item_currency_code",
)

OPTIONAL_COLUMNS: tuple[str, ...] = (
    "bill_billing_period_start_date",
    "line_item_usage_account_name",
    "line_item_product_code",
    "line_item_usage_type",
    "line_item_operation",
    "line_item_resource_id",
    "line_item_availability_zone",
    "line_item_usage_end_date",
    "line_item_usage_amount",
    "line_item_net_unblended_cost",
    "line_item_line_item_description",
    "pricing_public_on_demand_cost",
    "pricing_unit",
    "product_region_code",
    "product_servicecode",
    "product",
    "resource_tags",
    "savings_plan_savings_plan_a_r_n",
    "savings_plan_savings_plan_effective_cost",
    "savings_plan_total_commitment_to_date",
    "savings_plan_used_commitment",
    "reservation_reservation_a_r_n",
    "reservation_effective_cost",
)

CHARGE_CATEGORY: Mapping[str, str] = {
    "Usage": "Usage",
    "DiscountedUsage": "Usage",
    "SavingsPlanCoveredUsage": "Usage",
    "SavingsPlanNegation": "Usage",
    "Fee": "Purchase",
    "RIFee": "Purchase",
    "SavingsPlanRecurringFee": "Purchase",
    "SavingsPlanUpfrontFee": "Purchase",
    "Tax": "Tax",
    "Credit": "Credit",
    "Refund": "Credit",
    "Discount": "Adjustment",
    "BundledDiscount": "Adjustment",
    "EdpDiscount": "Adjustment",
    "PrivateRateDiscount": "Adjustment",
    "DistributorDiscount": "Adjustment",
}
UNKNOWN_CHARGE_CATEGORY = "Adjustment"

# Cross-cloud taxonomy (docs/09 §2.3 item 3); anything unlisted is "Other".
SERVICE_CATEGORY: Mapping[str, str] = {
    "AmazonEC2": "Compute",
    "AWSLambda": "Compute",
    "AmazonECS": "Compute",
    "AmazonEKS": "Compute",
    "AWSFargate": "Compute",
    "AmazonLightsail": "Compute",
    "ComputeSavingsPlans": "Compute",
    "AmazonS3": "Storage",
    "AmazonEFS": "Storage",
    "AmazonFSx": "Storage",
    "AWSBackup": "Storage",
    "AmazonGlacier": "Storage",
    "AmazonRDS": "Databases",
    "AmazonDynamoDB": "Databases",
    "AmazonElastiCache": "Databases",
    "AmazonRedshift": "Databases",
    "AmazonDocDB": "Databases",
    "AmazonVPC": "Networking",
    "AmazonCloudFront": "Networking",
    "AmazonRoute53": "Networking",
    "AWSELB": "Networking",
    "AWSDataTransfer": "Networking",
    "AmazonApiGateway": "Networking",
    "AmazonCloudWatch": "Management and Governance",
    "AWSCloudTrail": "Management and Governance",
    "AWSConfig": "Management and Governance",
    "awskms": "Security",
    "AWSSecretsManager": "Security",
    "AmazonGuardDuty": "Security",
    "AWSSecurityHub": "Security",
    "awswaf": "Security",
    "AmazonSageMaker": "AI and Machine Learning",
    "AmazonBedrock": "AI and Machine Learning",
    "AmazonSNS": "Integration",
    "AmazonSQS": "Integration",
    "AmazonKinesis": "Analytics",
    "AmazonAthena": "Analytics",
    "AWSGlue": "Analytics",
    "AWSSupportBusiness": "Support",
    "AWSSupportEnterprise": "Support",
}

# Local Zone availability zones (af-south-1-los-1a) bill as their own region id
# (af-south-1-los-1).
_LOCAL_ZONE_AZ = r"^([a-z]{2}-[a-z]+-\d-[a-z]{3}-\d)[a-z]$"

_FOCUS_FIELDS: list[Any] = [
    pa.field("BillingAccountId", pa.string()),
    pa.field("SubAccountId", pa.string()),
    pa.field("SubAccountName", pa.string()),
    pa.field("BillingPeriodStart", pa.timestamp("ms", tz="UTC")),
    pa.field("ChargePeriodStart", pa.timestamp("ms", tz="UTC")),
    pa.field("ChargePeriodEnd", pa.timestamp("ms", tz="UTC")),
    pa.field("ChargeDate", pa.date32()),
    pa.field("ChargeCategory", pa.string()),
    pa.field("ChargeClass", pa.string()),
    pa.field("ChargeDescription", pa.string()),
    pa.field("ProviderName", pa.string()),
    pa.field("ServiceName", pa.string()),
    pa.field("ServiceCategory", pa.string()),
    pa.field("RegionId", pa.string()),
    pa.field("AvailabilityZone", pa.string()),
    pa.field("ResourceId", pa.string()),
    pa.field("PricingUnit", pa.string()),
    pa.field("ConsumedQuantity", pa.float64()),
    pa.field("BilledCost", pa.float64()),
    pa.field("EffectiveCost", pa.float64()),
    pa.field("ListCost", pa.float64()),
    pa.field("BillingCurrency", pa.string()),
    pa.field("CommitmentDiscountId", pa.string()),
    pa.field("CommitmentDiscountType", pa.string()),
    pa.field("Tags", pa.map_(pa.string(), pa.string())),
    pa.field("x_NetCost", pa.float64()),
    pa.field("x_ServiceCode", pa.string()),
    pa.field("x_UsageType", pa.string()),
    pa.field("x_Operation", pa.string()),
    pa.field("x_LineItemType", pa.string()),
    pa.field("x_LineItemId", pa.string()),
    pa.field("x_TimeInterval", pa.string()),
    pa.field("x_CommitmentKind", pa.string()),
]
FOCUS_SCHEMA = pa.schema(_FOCUS_FIELDS)


def _col(batch: pa.RecordBatch, name: str, type_: Any) -> Arrow:
    """The column cast to ``type_``, or nulls when the export does not include it."""
    idx = batch.schema.get_field_index(name)
    if idx < 0:
        return pa.nulls(batch.num_rows, type_)
    arr = batch.column(idx)
    return arr if arr.type == type_ else pc.cast(arr, type_)


def _text(batch: pa.RecordBatch, name: str) -> Arrow:
    """String column with empty strings read as null (CUR writes "" for "not applicable")."""
    arr = _col(batch, name, pa.string())
    return pc.if_else(pc.equal(arr, ""), pa.scalar(None, pa.string()), arr)


def _ts(batch: pa.RecordBatch, name: str) -> Arrow:
    idx = batch.schema.get_field_index(name)
    if idx < 0:
        return pa.nulls(batch.num_rows, pa.timestamp("ms", tz="UTC"))
    arr = batch.column(idx)
    if pa.types.is_timestamp(arr.type) and arr.type.tz is None:
        arr = pc.assume_timezone(arr, "UTC")
    return pc.cast(arr, pa.timestamp("ms", tz="UTC"))


def _map(batch: pa.RecordBatch, name: str) -> Arrow:
    type_ = pa.map_(pa.string(), pa.string())
    idx = batch.schema.get_field_index(name)
    if idx < 0 or not pa.types.is_map(batch.column(idx).type):
        return pa.nulls(batch.num_rows, type_)
    return _col(batch, name, type_)


def _lookup(arr: Arrow, mapping: Mapping[str, str], default: str) -> Arrow:
    encoded = pc.dictionary_encode(pc.fill_null(arr, ""))
    values = [mapping.get(v, default) for v in encoded.dictionary.to_pylist()]
    return pc.take(pa.array(values, pa.string()), encoded.indices)


def _is(arr: Arrow, *values: str) -> Arrow:
    return pc.fill_null(pc.is_in(arr, pa.array(values, pa.string())), False)


def _coalesce(*arrays: Arrow) -> Arrow:
    return pc.coalesce(*arrays)


def unknown_line_item_types(batch: pa.RecordBatch) -> set[str]:
    types = set(pc.unique(_col(batch, "line_item_line_item_type", pa.string())).to_pylist())
    return {t for t in types if t is not None and t not in CHARGE_CATEGORY}


def normalise(batch: pa.RecordBatch) -> pa.Table:
    """One CUR 2.0 record batch as a FOCUS-aligned table (``FOCUS_SCHEMA``)."""
    n = batch.num_rows
    f64 = pa.float64()
    zero = pa.scalar(0.0, f64)
    null_text = pa.scalar(None, pa.string())

    line_type = _col(batch, "line_item_line_item_type", pa.string())
    billed = pc.fill_null(_col(batch, "line_item_unblended_cost", f64), zero)
    net = _coalesce(_col(batch, "line_item_net_unblended_cost", f64), billed)

    sp_arn = _text(batch, "savings_plan_savings_plan_a_r_n")
    ri_arn = _text(batch, "reservation_reservation_a_r_n")
    sp_effective = pc.fill_null(_col(batch, "savings_plan_savings_plan_effective_cost", f64), zero)
    ri_effective = pc.fill_null(_col(batch, "reservation_effective_cost", f64), zero)
    sp_total = _col(batch, "savings_plan_total_commitment_to_date", f64)
    sp_used = _col(batch, "savings_plan_used_commitment", f64)
    sp_unused = pc.subtract(sp_total, pc.fill_null(sp_used, zero))
    has_sp_commitment = pc.is_valid(sp_total)

    is_sp_usage = _is(line_type, "SavingsPlanCoveredUsage")
    is_ri_usage = _is(line_type, "DiscountedUsage")
    is_sp_fee = _is(line_type, "SavingsPlanRecurringFee")
    is_ri_fee = _is(line_type, "RIFee")
    is_amortised_away = _is(line_type, "SavingsPlanNegation", "SavingsPlanUpfrontFee")

    effective = billed
    effective = pc.if_else(pc.and_(is_sp_fee, has_sp_commitment), sp_unused, effective)
    effective = pc.if_else(is_amortised_away, zero, effective)
    effective = pc.if_else(is_ri_usage, ri_effective, effective)
    effective = pc.if_else(is_sp_usage, sp_effective, effective)

    kind = pa.nulls(n, pa.string())
    kind = pc.if_else(is_sp_usage, pa.scalar("sp_used"), kind)
    kind = pc.if_else(is_ri_usage, pa.scalar("ri_used"), kind)
    kind = pc.if_else(pc.and_(is_sp_fee, pc.invert(has_sp_commitment)), pa.scalar("sp_fee"), kind)
    kind = pc.if_else(is_ri_fee, pa.scalar("ri_fee"), kind)

    commitment_type = pc.if_else(
        pc.is_valid(sp_arn),
        pa.scalar("Savings Plan"),
        pc.if_else(pc.is_valid(ri_arn), pa.scalar("Reservation"), null_text),
    )

    service_code = _coalesce(
        _text(batch, "product_servicecode"), _text(batch, "line_item_product_code")
    )
    service_code = pc.fill_null(service_code, "Unknown")
    product = _map(batch, "product")
    product_name = pc.map_lookup(product, "product_name", "first")
    product_name = pc.if_else(pc.equal(product_name, ""), null_text, product_name)

    az = _text(batch, "line_item_availability_zone")
    local_zone = pc.fill_null(pc.match_substring_regex(az, _LOCAL_ZONE_AZ), False)
    region = pc.if_else(
        local_zone,
        pc.replace_substring_regex(az, _LOCAL_ZONE_AZ, r"\1"),
        _text(batch, "product_region_code"),
    )

    start = _ts(batch, "line_item_usage_start_date")
    category = _lookup(line_type, CHARGE_CATEGORY, UNKNOWN_CHARGE_CATEGORY)

    columns: dict[str, Arrow] = {
        "BillingAccountId": _col(batch, "bill_payer_account_id", pa.string()),
        "SubAccountId": _col(batch, "line_item_usage_account_id", pa.string()),
        "SubAccountName": _text(batch, "line_item_usage_account_name"),
        "BillingPeriodStart": _ts(batch, "bill_billing_period_start_date"),
        "ChargePeriodStart": start,
        "ChargePeriodEnd": _ts(batch, "line_item_usage_end_date"),
        "ChargeDate": pc.cast(start, pa.date32()),
        "ChargeCategory": category,
        "ChargeClass": pc.if_else(_is(line_type, "Refund"), pa.scalar("Correction"), null_text),
        "ChargeDescription": _text(batch, "line_item_line_item_description"),
        "ProviderName": pa.array(["AWS"] * n, pa.string()),
        "ServiceName": _coalesce(product_name, service_code),
        "ServiceCategory": _lookup(service_code, SERVICE_CATEGORY, "Other"),
        "RegionId": region,
        "AvailabilityZone": az,
        "ResourceId": _text(batch, "line_item_resource_id"),
        "PricingUnit": _text(batch, "pricing_unit"),
        "ConsumedQuantity": _col(batch, "line_item_usage_amount", f64),
        "BilledCost": billed,
        "EffectiveCost": effective,
        "ListCost": _col(batch, "pricing_public_on_demand_cost", f64),
        "BillingCurrency": _col(batch, "line_item_currency_code", pa.string()),
        "CommitmentDiscountId": _coalesce(sp_arn, ri_arn),
        "CommitmentDiscountType": commitment_type,
        "Tags": _map(batch, "resource_tags"),
        "x_NetCost": net,
        "x_ServiceCode": service_code,
        "x_UsageType": _text(batch, "line_item_usage_type"),
        "x_Operation": _text(batch, "line_item_operation"),
        "x_LineItemType": line_type,
        "x_LineItemId": _col(batch, "identity_line_item_id", pa.string()),
        "x_TimeInterval": _col(batch, "identity_time_interval", pa.string()),
        "x_CommitmentKind": kind,
    }
    return pa.Table.from_pydict(columns, schema=FOCUS_SCHEMA)
