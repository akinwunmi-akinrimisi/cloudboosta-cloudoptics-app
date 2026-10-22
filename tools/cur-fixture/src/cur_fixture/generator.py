"""Build a deterministic CUR 2.0 table for one billing period.

The fixture models a small African scale-up: a payer account, a prod and a dev member
account, resources in af-south-1, eu-west-1 and the Lagos Local Zone, Savings Plan
coverage, Activate credits, a tax line and (optionally) a restatement.

Prices are illustrative, not AWS list prices. Engines and ingestion tests must assert on
relationships (e.g. "NAT hours billed, zero bytes") rather than on exact AWS prices.
"""

from __future__ import annotations

import calendar
import hashlib
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow as pa

from cur_fixture.schema import CUR2_SCHEMA

PAYER = ("111111111111", "cloudoptics-demo-payer")
PROD = ("222222222222", "demo-prod")
DEV = ("333333333333", "demo-dev")

SP_ARN = "arn:aws:savingsplans::111111111111:savingsplan/0f0e0d0c-demo-compute-sp"
SP_DISCOUNT = 0.28  # covered usage costs 72% of on-demand
SP_UTILISATION = 0.90  # 10% of the commitment is unused
TAX_RATE = 0.075  # illustrative VAT


@dataclass(frozen=True)
class BillingPeriod:
    year: int
    month: int

    @classmethod
    def parse(cls, value: str) -> BillingPeriod:
        year, month = value.split("-")
        return cls(int(year), int(month))

    @property
    def start(self) -> datetime:
        return datetime(self.year, self.month, 1, tzinfo=UTC)

    @property
    def days(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    @property
    def end(self) -> datetime:
        return self.start + timedelta(days=self.days)

    @property
    def label(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True)
class Resource:
    """One billed resource and the recommendation it should trigger (if any)."""

    scenario: str
    account: tuple[str, str]
    region: str
    az: str
    product_code: str
    usage_type: str
    operation: str
    resource_id: str
    unit: str
    rate: float  # USD per unit
    daily_amount: float  # units per day
    tags: dict[str, str] = field(default_factory=dict)
    instance_type: str = ""
    sp_covered: bool = False
    jitter: float = 0.0  # +/- fraction applied per day
    expected_rules: tuple[str, ...] = ()
    note: str = ""


def _resources(period: BillingPeriod) -> list[Resource]:
    gb_month = 1.0 / period.days  # storage is billed in GB-Mo, prorated per day
    return [
        Resource(
            "prod-web-sp-covered",
            PROD,
            "af-south-1",
            "af-south-1a",
            "AmazonEC2",
            "AFS1-BoxUsage:m5.xlarge",
            "RunInstances",
            "i-0prodweb000000001",
            "Hrs",
            0.252,
            24,
            {"env": "prod", "app": "web"},
            "m5.xlarge",
            sp_covered=True,
            note="Covered by the Compute Savings Plan; context for R8",
        ),
        Resource(
            "dev-api-24x7",
            DEV,
            "af-south-1",
            "af-south-1b",
            "AmazonEC2",
            "AFS1-BoxUsage:t3.large",
            "RunInstances",
            "i-0devapi0000000001",
            "Hrs",
            0.1092,
            24,
            {"env": "dev", "app": "api"},
            "t3.large",
            expected_rules=("R7",),
            note="Non-prod running 24/7",
        ),
        Resource(
            "prod-legacy-m4",
            PROD,
            "eu-west-1",
            "eu-west-1a",
            "AmazonEC2",
            "EU-BoxUsage:m4.large",
            "RunInstances",
            "i-0legacym400000001",
            "Hrs",
            0.111,
            24,
            {"env": "prod", "app": "reports"},
            "m4.large",
            expected_rules=("R6",),
            note="Previous generation",
        ),
        Resource(
            "lagos-lz-edge",
            PROD,
            "af-south-1",
            "af-south-1-los-1a",
            "AmazonEC2",
            "AFS1-LOS1-BoxUsage:c6i.large",
            "RunInstances",
            "i-0lagosedge0000001",
            "Hrs",
            0.11,
            24,
            {"env": "prod", "app": "edge"},
            "c6i.large",
            note="Local Zone usage; usage-type prefix to verify against a real export",
        ),
        Resource(
            "dev-unattached-gp2",
            DEV,
            "af-south-1",
            "af-south-1a",
            "AmazonEC2",
            "AFS1-EBS:VolumeUsage.gp2",
            "CreateVolume-Gp2",
            "vol-0devunattached01",
            "GB-Mo",
            0.1309,
            100 * gb_month,
            {"env": "dev"},
            expected_rules=("R1", "R3"),
            note="R1 needs inventory state 'available'; CUR alone cannot show attachment",
        ),
        Resource(
            "prod-db-gp2",
            PROD,
            "af-south-1",
            "af-south-1a",
            "AmazonEC2",
            "AFS1-EBS:VolumeUsage.gp2",
            "CreateVolume-Gp2",
            "vol-0proddata000001",
            "GB-Mo",
            0.1309,
            500 * gb_month,
            {"env": "prod", "app": "db"},
            expected_rules=("R3",),
        ),
        Resource(
            "old-snapshot",
            PROD,
            "af-south-1",
            "",
            "AmazonEC2",
            "AFS1-EBS:SnapshotUsage",
            "CreateSnapshot",
            "snap-0oldorphan0000001",
            "GB-Mo",
            0.0595,
            250 * gb_month,
            {"env": "prod"},
            expected_rules=("R1",),
            note="R1b/old snapshot needs inventory age and source",
        ),
        Resource(
            "idle-eip",
            PROD,
            "af-south-1",
            "",
            "AmazonVPC",
            "AFS1-PublicIPv4:IdleAddress",
            "IdleAddress",
            "eipalloc-0idle000000001",
            "Hrs",
            0.005,
            24,
            {},
            expected_rules=("R2",),
        ),
        Resource(
            "idle-nat-hours",
            DEV,
            "af-south-1",
            "",
            "AmazonEC2",
            "AFS1-NatGateway-Hours",
            "NatGateway",
            "nat-0idle00000000001",
            "Hrs",
            0.065,
            24,
            {"env": "dev"},
            expected_rules=("R10",),
            note="No NatGateway-Bytes rows for this gateway",
        ),
        Resource(
            "busy-nat-hours",
            PROD,
            "af-south-1",
            "",
            "AmazonEC2",
            "AFS1-NatGateway-Hours",
            "NatGateway",
            "nat-0busy00000000001",
            "Hrs",
            0.065,
            24,
            {"env": "prod"},
        ),
        Resource(
            "busy-nat-bytes",
            PROD,
            "af-south-1",
            "",
            "AmazonEC2",
            "AFS1-NatGateway-Bytes",
            "NatGateway",
            "nat-0busy00000000001",
            "GB",
            0.065,
            180,
            {"env": "prod"},
            jitter=0.25,
        ),
        Resource(
            "rds-pg11-instance",
            PROD,
            "af-south-1",
            "af-south-1a",
            "AmazonRDS",
            "AFS1-InstanceUsage:db.m5.large",
            "CreateDBInstance:0014",
            "arn:aws:rds:af-south-1:222222222222:db:ledger-pg11",
            "Hrs",
            0.233,
            24,
            {"env": "prod", "app": "ledger"},
            "db.m5.large",
        ),
        Resource(
            "rds-pg11-extended-support",
            PROD,
            "af-south-1",
            "",
            "AmazonRDS",
            "AFS1-ExtendedSupport:Yr1-Yr2:PostgreSQL11",
            "ExtendedSupport",
            "arn:aws:rds:af-south-1:222222222222:db:ledger-pg11",
            "vCPU-Hrs",
            0.1,
            48,
            {"env": "prod", "app": "ledger"},
            expected_rules=("R6b",),
            note="Usage-type naming to verify against a real export",
        ),
    ]


def _line_id(*parts: object) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:32]


def _base_row(period: BillingPeriod, day_start: datetime, day_end: datetime) -> dict[str, Any]:
    return {
        "identity_time_interval": f"{day_start:%Y-%m-%dT%H:%M:%SZ}/{day_end:%Y-%m-%dT%H:%M:%SZ}",
        "bill_bill_type": "Anniversary",
        "bill_billing_entity": "AWS",
        "bill_billing_period_start_date": period.start,
        "bill_billing_period_end_date": period.end,
        "bill_payer_account_id": PAYER[0],
        "bill_payer_account_name": PAYER[1],
        "line_item_usage_start_date": day_start,
        "line_item_usage_end_date": day_end,
        "line_item_currency_code": "USD",
        "line_item_availability_zone": "",
        "product_instance_type": "",
        "resource_tags": [],
        "savings_plan_savings_plan_a_r_n": "",
        "savings_plan_savings_plan_effective_cost": 0.0,
    }


def _days(period: BillingPeriod) -> Iterator[tuple[datetime, datetime]]:
    for d in range(period.days):
        start = period.start + timedelta(days=d)
        yield start, start + timedelta(days=1)


@dataclass(frozen=True)
class Fixture:
    period: BillingPeriod
    version: int
    table: pa.Table
    expectations: list[dict[str, Any]]


def generate(period: BillingPeriod, seed: int = 7, restate: bool = False) -> Fixture:
    """Generate one billing period. ``restate=True`` produces version 2 with a refund added."""
    rng = random.Random(seed)  # noqa: S311 - test data, not security
    rows: list[dict[str, Any]] = []
    sp_effective_by_day: dict[datetime, float] = {}
    usage_by_account: dict[str, float] = {}

    for res in _resources(period):
        for day_start, day_end in _days(period):
            amount = res.daily_amount
            if res.jitter:
                amount *= 1 + rng.uniform(-res.jitter, res.jitter)
            amount = round(amount, 6)
            cost = round(amount * res.rate, 6)
            line_type = "SavingsPlanCoveredUsage" if res.sp_covered else "Usage"
            row = _base_row(period, day_start, day_end) | {
                "identity_line_item_id": _line_id(res.scenario, res.usage_type, day_start),
                "line_item_usage_account_id": res.account[0],
                "line_item_usage_account_name": res.account[1],
                "line_item_line_item_type": line_type,
                "line_item_product_code": res.product_code,
                "line_item_usage_type": res.usage_type,
                "line_item_operation": res.operation,
                "line_item_resource_id": res.resource_id,
                "line_item_availability_zone": res.az,
                "line_item_usage_amount": amount,
                "line_item_unblended_rate": f"{res.rate:.10f}",
                "line_item_unblended_cost": cost,
                "line_item_blended_cost": cost,
                "line_item_net_unblended_cost": cost,
                "line_item_line_item_description": res.note or res.usage_type,
                "pricing_public_on_demand_cost": cost,
                "pricing_unit": res.unit,
                "product_region_code": res.region,
                "product_instance_type": res.instance_type,
                "product_servicecode": res.product_code,
                "product": [("region", res.region), ("servicecode", res.product_code)],
                "resource_tags": [(f"user_{k}", v) for k, v in sorted(res.tags.items())],
            }
            if res.sp_covered:
                effective = round(cost * (1 - SP_DISCOUNT), 6)
                row["savings_plan_savings_plan_a_r_n"] = SP_ARN
                row["savings_plan_savings_plan_effective_cost"] = effective
                sp_effective_by_day[day_start] = sp_effective_by_day.get(day_start, 0.0) + effective
                rows.append(row)
                rows.append(
                    row
                    | {
                        "identity_line_item_id": _line_id(res.scenario, "negation", day_start),
                        "line_item_line_item_type": "SavingsPlanNegation",
                        "line_item_unblended_cost": -cost,
                        "line_item_blended_cost": -cost,
                        "line_item_net_unblended_cost": -cost,
                        "line_item_line_item_description": "Savings Plan negation",
                        "pricing_public_on_demand_cost": 0.0,
                        "savings_plan_savings_plan_effective_cost": 0.0,
                    }
                )
                continue
            usage_by_account[res.account[0]] = usage_by_account.get(res.account[0], 0.0) + cost
            rows.append(row)

    # Savings Plan recurring fee, owned by the payer: commitment = covered effective / utilisation.
    for day_start, day_end in _days(period):
        fee = round(sp_effective_by_day.get(day_start, 0.0) / SP_UTILISATION, 6)
        rows.append(
            _base_row(period, day_start, day_end)
            | {
                "identity_line_item_id": _line_id("sp-fee", day_start),
                "line_item_usage_account_id": PAYER[0],
                "line_item_usage_account_name": PAYER[1],
                "line_item_line_item_type": "SavingsPlanRecurringFee",
                "line_item_product_code": "ComputeSavingsPlans",
                "line_item_usage_type": "ComputeSP:1yrNoUpfront",
                "line_item_operation": "",
                "line_item_resource_id": SP_ARN,
                "line_item_usage_amount": 24.0,
                "line_item_unblended_rate": f"{fee / 24:.10f}",
                "line_item_unblended_cost": fee,
                "line_item_blended_cost": fee,
                "line_item_net_unblended_cost": fee,
                "line_item_line_item_description": "1 year No Upfront Compute Savings Plan",
                "pricing_public_on_demand_cost": 0.0,
                "pricing_unit": "Hrs",
                "product_region_code": "",
                "product_servicecode": "ComputeSavingsPlans",
                "product": [],
                "savings_plan_savings_plan_a_r_n": SP_ARN,
            }
        )

    # Activate credits applied daily to the dev account's EC2 usage.
    for day_start, day_end in _days(period):
        rows.append(
            _base_row(period, day_start, day_end)
            | {
                "identity_line_item_id": _line_id("credit", day_start),
                "line_item_usage_account_id": DEV[0],
                "line_item_usage_account_name": DEV[1],
                "line_item_line_item_type": "Credit",
                "line_item_product_code": "AmazonEC2",
                "line_item_usage_type": "",
                "line_item_operation": "",
                "line_item_resource_id": "",
                "line_item_usage_amount": 0.0,
                "line_item_unblended_rate": "",
                "line_item_unblended_cost": -2.0,
                "line_item_blended_cost": -2.0,
                "line_item_net_unblended_cost": -2.0,
                "line_item_line_item_description": "AWS Activate credit",
                "pricing_public_on_demand_cost": 0.0,
                "pricing_unit": "",
                "product_region_code": "",
                "product_servicecode": "AmazonEC2",
                "product": [],
            }
        )

    # One tax line per account with usage, at the start of the period.
    first_day = next(_days(period))
    for account in (PROD, DEV):
        tax = round(usage_by_account.get(account[0], 0.0) * TAX_RATE, 6)
        rows.append(
            _base_row(period, *first_day)
            | {
                "identity_line_item_id": _line_id("tax", account[0], period.label),
                "line_item_usage_account_id": account[0],
                "line_item_usage_account_name": account[1],
                "line_item_line_item_type": "Tax",
                "line_item_product_code": "AmazonEC2",
                "line_item_usage_type": "",
                "line_item_operation": "",
                "line_item_resource_id": "",
                "line_item_usage_amount": 0.0,
                "line_item_unblended_rate": "",
                "line_item_unblended_cost": tax,
                "line_item_blended_cost": tax,
                "line_item_net_unblended_cost": tax,
                "line_item_line_item_description": "Illustrative tax",
                "pricing_public_on_demand_cost": 0.0,
                "pricing_unit": "",
                "product_region_code": "",
                "product_servicecode": "AmazonEC2",
                "product": [],
            }
        )

    if restate:
        rows.append(
            _base_row(period, *first_day)
            | {
                "identity_line_item_id": _line_id("refund", period.label),
                "line_item_usage_account_id": PROD[0],
                "line_item_usage_account_name": PROD[1],
                "line_item_line_item_type": "Refund",
                "line_item_product_code": "AmazonEC2",
                "line_item_usage_type": "",
                "line_item_operation": "",
                "line_item_resource_id": "",
                "line_item_usage_amount": 0.0,
                "line_item_unblended_rate": "",
                "line_item_unblended_cost": -25.0,
                "line_item_blended_cost": -25.0,
                "line_item_net_unblended_cost": -25.0,
                "line_item_line_item_description": "Restatement: service credit refund",
                "pricing_public_on_demand_cost": 0.0,
                "pricing_unit": "",
                "product_region_code": "",
                "product_servicecode": "AmazonEC2",
                "product": [],
            }
        )

    table = pa.Table.from_pylist(rows, schema=CUR2_SCHEMA)
    expectations = [
        {
            "scenario": r.scenario,
            "account_id": r.account[0],
            "resource_id": r.resource_id,
            "usage_type": r.usage_type,
            "expected_rules": list(r.expected_rules),
            "note": r.note,
        }
        for r in _resources(period)
    ]
    return Fixture(period, 2 if restate else 1, table, expectations)
