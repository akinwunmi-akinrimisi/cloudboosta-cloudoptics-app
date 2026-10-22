"""The subset of CUR 2.0 columns the fixture emits.

Column names follow the CUR 2.0 table in BCM Data Exports. Rates are strings in CUR 2.0;
costs and amounts are doubles; ``product`` and ``resource_tags`` are string maps.
"""

import pyarrow as pa

STRING_MAP: pa.DataType = pa.map_(pa.string(), pa.string())
TS: pa.DataType = pa.timestamp("ms", tz="UTC")

CUR2_SCHEMA = pa.schema(
    [
        pa.field("identity_line_item_id", pa.string()),
        pa.field("identity_time_interval", pa.string()),
        pa.field("bill_bill_type", pa.string()),
        pa.field("bill_billing_entity", pa.string()),
        pa.field("bill_billing_period_start_date", TS),
        pa.field("bill_billing_period_end_date", TS),
        pa.field("bill_payer_account_id", pa.string()),
        pa.field("bill_payer_account_name", pa.string()),
        pa.field("line_item_usage_account_id", pa.string()),
        pa.field("line_item_usage_account_name", pa.string()),
        pa.field("line_item_line_item_type", pa.string()),
        pa.field("line_item_product_code", pa.string()),
        pa.field("line_item_usage_type", pa.string()),
        pa.field("line_item_operation", pa.string()),
        pa.field("line_item_resource_id", pa.string()),
        pa.field("line_item_availability_zone", pa.string()),
        pa.field("line_item_usage_start_date", TS),
        pa.field("line_item_usage_end_date", TS),
        pa.field("line_item_usage_amount", pa.float64()),
        pa.field("line_item_unblended_rate", pa.string()),
        pa.field("line_item_unblended_cost", pa.float64()),
        pa.field("line_item_blended_cost", pa.float64()),
        pa.field("line_item_net_unblended_cost", pa.float64()),
        pa.field("line_item_currency_code", pa.string()),
        pa.field("line_item_line_item_description", pa.string()),
        pa.field("pricing_public_on_demand_cost", pa.float64()),
        pa.field("pricing_unit", pa.string()),
        pa.field("product_region_code", pa.string()),
        pa.field("product_instance_type", pa.string()),
        pa.field("product_servicecode", pa.string()),
        pa.field("product", STRING_MAP),
        pa.field("resource_tags", STRING_MAP),
        pa.field("savings_plan_savings_plan_a_r_n", pa.string()),
        pa.field("savings_plan_savings_plan_effective_cost", pa.float64()),
    ]
)

LINE_ITEM_TYPES = frozenset(
    {
        "Usage",
        "SavingsPlanCoveredUsage",
        "SavingsPlanRecurringFee",
        "SavingsPlanNegation",
        "Credit",
        "Tax",
        "Refund",
    }
)
