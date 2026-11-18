"""Savings Plans, Reserved Instances and the R8 Savings Plan advisory (PRD US-4.5,
FR-ANA-10)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fake_aws import client_error
from httpx import AsyncClient
from test_budgets import ACCOUNT, OTHER, workspace
from test_digest import member
from test_invites import Outbox, outbox  # noqa: F401 (fixture)

from cloudoptics.analysis.engine import run_analysis
from cloudoptics.commitments import advisor, aws, jobs, service
from cloudoptics.connectors import service as connectors
from cloudoptics.platform.db import get_sessionmaker, scope

NOW = datetime.now(UTC).replace(microsecond=0)
SP_ARN = f"arn:aws:savingsplans::{ACCOUNT}:savingsplan/sp-1"
MEMBER_SP_ARN = f"arn:aws:savingsplans::{OTHER}:savingsplan/sp-2"


# --- The arithmetic ------------------------------------------------------------------------


def test_percentile_interpolates_like_numpy() -> None:
    assert advisor.percentile([], 10) == 0
    assert advisor.percentile([5.0], 20) == 5
    assert advisor.percentile([1.0, 2.0, 3.0, 4.0, 5.0], 10) == pytest.approx(1.4)
    assert advisor.percentile([4.0, 1.0, 3.0, 2.0], 50) == pytest.approx(2.5)


def test_baseline_nets_other_savings_and_takes_the_conservative_end() -> None:
    # 20 days: USD 48 (2/h) on 15, USD 24 (1/h) on 5; USD 73 a month (0.1/h) netted.
    days = [24.0] * 5 + [48.0] * 15
    b = advisor.baseline(days, 73)
    assert (b.days, b.netted_usd_month) == (20, 73)
    assert (b.p10_hourly_usd, b.p20_hourly_usd) == (0.9, 0.9)
    assert b.average_hourly_usd == pytest.approx(1.65)
    assert b.target_hourly_usd == b.band_low_hourly_usd == pytest.approx(0.54)
    assert b.band_high_hourly_usd == pytest.approx(0.72)
    # Only the last 60 days count, and netting never makes an hour negative.
    assert advisor.baseline([1000.0] * 10 + [24.0] * 60, 0).p20_hourly_usd == 1
    assert advisor.baseline([24.0] * 30, 10_000).average_hourly_usd == 0


def test_options_break_even_and_matrix() -> None:
    o = advisor.option("savings_plan", 1, "all_upfront", 1.0, 0.33)
    assert o.hourly_commitment_usd == 0.67
    assert o.total_commitment_usd == pytest.approx(0.67 * 8760)
    assert (o.upfront_usd, o.monthly_recurring_usd) == (5869.2, 0)
    # Pays back the upfront after 0.67 x 8760 / 730 = 8.04 months of on-demand avoided.
    assert o.break_even_months == 8.0
    assert o.monthly_savings_usd == pytest.approx(730 - 0.67 * 730, abs=0.01)
    partial = advisor.option("reserved_instance", 3, "partial_upfront", 1.0, 0.59)
    assert partial.upfront_usd == pytest.approx(0.41 * 8760 * 3 / 2, abs=0.01)
    assert 0 < partial.break_even_months < 36
    assert advisor.option("savings_plan", 1, "no_upfront", 1.0, 0.28).break_even_months == 0
    # A discount of 0 never pays back.
    assert advisor.option("savings_plan", 1, "all_upfront", 1.0, 0.0).break_even_months == 12

    recs = [
        {"paymentOption": "NO_UPFRONT", "lookbackDays": 30, "hourlyCommitmentUsd": 0.5,
         "estimatedSavingsPercent": 25, "estimatedMonthlySavingsUsd": 120, "upfrontUsd": 0},
        {"paymentOption": "NO_UPFRONT", "lookbackDays": 60, "hourlyCommitmentUsd": 0.4,
         "estimatedSavingsPercent": 30, "estimatedMonthlySavingsUsd": 110, "upfrontUsd": 0},
        {"paymentOption": "ALL_UPFRONT", "lookbackDays": 60, "hourlyCommitmentUsd": 0.38,
         "estimatedSavingsPercent": 34, "estimatedMonthlySavingsUsd": 115,
         "upfrontUsd": 3328.8},
        {"paymentOption": "ALL_UPFRONT", "lookbackDays": 30, "hourlyCommitmentUsd": 0,
         "estimatedSavingsPercent": 0},
        {"paymentOption": "PARTIAL_UPFRONT", "lookbackDays": 30, "hourlyCommitmentUsd": 1},
    ]  # fmt: skip
    assert advisor.aws_discounts(recs) == {"no_upfront": 0.30, "all_upfront": 0.34}
    options = advisor.matrix(0.54, recs)
    ours = [o for o in options if o.source == "ours"]
    theirs = [o for o in options if o.source == "aws"]
    assert len(ours) == 12 and len(theirs) == 3
    default = advisor.default_option(options)
    assert default is not None and default.discount_from_aws
    assert (default.hourly_commitment_usd, default.discount_percent) == (0.378, 30)
    assert default.monthly_savings_usd == pytest.approx(0.54 * 730 - 0.378 * 730, abs=0.01)
    typical = next(o for o in ours if (o.instrument, o.term_years, o.payment)
                   == ("savings_plan", 1, "partial_upfront"))  # fmt: skip
    assert (typical.discount_percent, typical.discount_from_aws) == (31, False)
    aws_all = next(o for o in theirs if o.payment == "all_upfront")
    assert (aws_all.lookback_days, aws_all.upfront_usd, aws_all.monthly_savings_usd) == (
        60, 3328.8, 115
    )  # fmt: skip
    assert aws_all.break_even_months == pytest.approx(3328.8 / (0.38 / 0.66 * 730), abs=0.1)
    assert advisor.default_option(theirs) is None


def test_ladder_fx_and_risk() -> None:
    steps = advisor.ladder(1.0, date(2026, 11, 18))
    assert [s.share_percent for s in steps] == [25, 35, 45, 55, 65, 75, 85, 95, 100]
    assert [s.buy_on for s in steps[:3]] == [
        date(2026, 11, 18), date(2026, 12, 1), date(2027, 1, 1)
    ]  # fmt: skip
    assert steps[0].buy_hourly_usd == 0.25 and steps[1].buy_hourly_usd == 0.1
    assert steps[-1].total_hourly_usd == 1 and steps[-1].buy_hourly_usd == 0.05
    assert sum(s.buy_hourly_usd for s in steps) == pytest.approx(1)
    assert advisor.ladder(0.0005, date(2026, 11, 18)) == []
    # Tiny targets round to whole steps of USD 0.001.
    assert [s.total_hourly_usd for s in advisor.ladder(0.004, date(2026, 1, 1))] == [
        0.001, 0.002, 0.003, 0.004
    ]  # fmt: skip

    o = advisor.option("savings_plan", 1, "no_upfront", 0.54, 0.30)
    view = advisor.fx_view(o, "NGN", 1500, date(2026, 11, 17), False)
    assert [s.weakening_percent for s in view.scenarios] == [0, 20, 40]
    assert [s.rate for s in view.scenarios] == [1500, 1800, 2100]
    assert view.scenarios[0].monthly_local == pytest.approx(0.378 * 730 * 1500, abs=0.01)
    assert view.scenarios[2].total_local == pytest.approx(0.378 * 8760 * 2100, abs=0.01)

    b = advisor.baseline([24.0] * 5 + [48.0] * 15, 73)
    assert advisor.decline_tolerance_percent(b, o) == pytest.approx(67.3, abs=0.1)
    assert advisor.target_coverage_percent(b, o, None, None, 0) == pytest.approx(32.7, abs=0.1)
    # Covered 72 and on-demand 720 over 30 days: 0.1/h + 0.378 against 1 - 0.54.
    assert advisor.target_coverage_percent(b, o, 72, 720, 30) == pytest.approx(51.0, abs=0.1)
    assert advisor.status(b, o) == "ok"
    assert advisor.status(advisor.baseline([], 0), None) == "no_data"
    assert advisor.status(advisor.baseline([24.0] * 5, 0), o) == "not_enough_data"
    tiny = advisor.option("savings_plan", 1, "no_upfront", 0.01, 0.30)
    assert advisor.status(b, tiny) == "no_purchase"
    empty = advisor.baseline([0.0] * 20, 0)
    assert advisor.decline_tolerance_percent(empty, o) is None
    assert advisor.target_coverage_percent(empty, o, None, None, 0) is None


def test_expiry_thresholds() -> None:
    assert [service.due_threshold(d) for d in (90, 61, 60, 45, 30, 16, 15, 0)] == [
        None, None, 60, 60, 30, 30, 15, 15
    ]  # fmt: skip


# --- AWS --------------------------------------------------------------------------------------


class FakeCe:
    def __init__(self, world: World) -> None:
        self.world = world

    def _call(self, name: str) -> None:
        self.world.ce_calls.append(name)
        if name in self.world.ce_fail:
            raise client_error("DataUnavailableException", name)

    def get_savings_plans_purchase_recommendation(self, **kw: Any) -> dict[str, Any]:
        self._call("recommendation")
        assert (kw["SavingsPlansType"], kw["TermInYears"]) == ("COMPUTE_SP", "ONE_YEAR")
        no_upfront = kw["PaymentOption"] == "NO_UPFRONT"
        hourly = (0.4 if no_upfront else 0.38) * (1 if kw["LookbackPeriodInDays"] ==
                                                 "SIXTY_DAYS" else 1.1)  # fmt: skip
        return {
            "Metadata": {"GenerationTimestamp": "2026-11-17T00:00:00Z"},
            "SavingsPlansPurchaseRecommendation": {
                "SavingsPlansPurchaseRecommendationSummary": {
                    "HourlyCommitmentToPurchase": f"{hourly:.3f}",
                    "EstimatedSavingsPercentage": "30" if no_upfront else "34",
                    "EstimatedMonthlySavingsAmount": "110",
                    "CurrentOnDemandSpend": "1100",
                },
                "SavingsPlansPurchaseRecommendationDetails": [
                    {"UpfrontCost": "0" if no_upfront else "1664.4"},
                    {"UpfrontCost": "0" if no_upfront else "1664.4"},
                ],
            },
        }

    def get_savings_plans_coverage(self, **kw: Any) -> dict[str, Any]:
        self._call("coverage")
        return {
            "SavingsPlansCoverages": [
                {"Coverage": {"SpendCoveredBySavingsPlans": "40", "OnDemandCost": "300"}},
                {"Coverage": {"SpendCoveredBySavingsPlans": "32", "OnDemandCost": "420"}},
            ]
        }

    def get_savings_plans_utilization_details(self, **kw: Any) -> dict[str, Any]:
        self._call("utilisation")
        if "NextToken" not in kw:
            return {
                "SavingsPlansUtilizationDetails": [
                    {"SavingsPlanArn": SP_ARN, "Utilization": {"UtilizationPercentage": "97.5"}}
                ],
                "NextToken": "2",
            }
        return {
            "SavingsPlansUtilizationDetails": [
                {
                    "SavingsPlanArn": MEMBER_SP_ARN,
                    "Attributes": {"SavingsPlansType": "EC2Instance", "EndDateTime":
                                   (NOW + timedelta(days=200)).isoformat()},
                    "Utilization": {"UtilizationPercentage": "64"},
                }
            ],
            "Total": {
                "Utilization": {"UtilizationPercentage": "90.1", "UnusedCommitment": "12.5"},
                "Savings": {"NetSavings": "80"},
            },
        }  # fmt: skip

    def get_reservation_utilization(self, **kw: Any) -> dict[str, Any]:
        self._call("ri_utilisation")
        return {
            "UtilizationsByTime": [
                {
                    "Groups": [
                        {"Key": "SUBSCRIPTION_ID", "Value": "12345",
                         "Attributes": {"leaseId": "ri-eu"},
                         "Utilization": {"UtilizationPercentage": "88"}},
                    ]
                }
            ],
            "Total": {"UtilizationPercentage": "88"},
        }  # fmt: skip

    def get_reservation_coverage(self, **kw: Any) -> dict[str, Any]:
        self._call("ri_coverage")
        return {"Total": {"CoverageHours": {"CoverageHoursPercentage": "12.5"}}}


class FakeSavingsPlans:
    def __init__(self, world: World) -> None:
        self.world = world

    def describe_savings_plans(self, **kw: Any) -> dict[str, Any]:
        if self.world.sp_error:
            raise client_error(self.world.sp_error, "DescribeSavingsPlans")
        return {
            "savingsPlans": [
                {
                    "savingsPlanId": "sp-1",
                    "savingsPlanArn": SP_ARN,
                    "savingsPlanType": "Compute",
                    "paymentOption": "No Upfront",
                    "state": "active",
                    "commitment": "0.250",
                    "upfrontPaymentAmount": "0",
                    "termDurationInSeconds": 31536000,
                    "start": (self.world.sp_end - timedelta(days=365)).isoformat(),
                    "end": self.world.sp_end.isoformat(),
                }
            ]
        }


class FakeEc2:
    def __init__(self, region: str, world: World, account: str) -> None:
        self.region, self.world, self.account = region, world, account

    def describe_regions(self) -> dict[str, Any]:
        return {"Regions": [{"RegionName": r} for r in ("eu-west-2", "us-east-1", "af-south-1")]}

    def describe_reserved_instances(self, **kw: Any) -> dict[str, Any]:
        if self.region == "af-south-1":
            raise client_error("UnauthorizedOperation", "DescribeReservedInstances")
        if (self.account, self.region) != (OTHER, "eu-west-2"):
            return {"ReservedInstances": []}
        return {
            "ReservedInstances": [
                {
                    "ReservedInstancesId": "ri-eu",
                    "InstanceType": "m5.large",
                    "InstanceCount": 2,
                    "ProductDescription": "Linux/UNIX",
                    "OfferingClass": "standard",
                    "OfferingType": "Partial Upfront",
                    "State": "active",
                    "Duration": 94608000,
                    "FixedPrice": 1200.0,
                    "UsagePrice": 0.0,
                    "RecurringCharges": [{"Amount": 0.02, "Frequency": "Hourly"}],
                    "Start": NOW - timedelta(days=1000),
                    "End": self.world.ri_end,
                }
            ]
        }


class FakeRds:
    def __init__(self, region: str, account: str) -> None:
        self.region, self.account = region, account

    def describe_reserved_db_instances(self, **kw: Any) -> dict[str, Any]:
        if (self.account, self.region) != (ACCOUNT, "us-east-1"):
            return {"ReservedDBInstances": []}
        if "Marker" not in kw:
            return {
                "ReservedDBInstances": [
                    {"ReservedDBInstanceId": "retired-db", "State": "retired"},
                ],
                "Marker": "next",
            }
        return {
            "ReservedDBInstances": [
                {
                    "ReservedDBInstanceId": "db-ri",
                    "DBInstanceClass": "db.r6g.large",
                    "DBInstanceCount": 1,
                    "ProductDescription": "postgresql",
                    "MultiAZ": True,
                    "OfferingType": "No Upfront",
                    "State": "active",
                    "StartTime": NOW - timedelta(days=300),
                    "Duration": 31536000,
                    "UsagePrice": 0.0,
                    "RecurringCharges": [{"RecurringChargeAmount": 0, "Amount": 0.25,
                                          "Frequency": "Hourly"}],
                }
            ]
        }  # fmt: skip


class World:
    def __init__(self) -> None:
        self.ce_calls: list[str] = []
        self.ce_fail: set[str] = set()
        self.sp_error: str | None = None
        self.no_access: set[str] = set()
        self.assumed: list[tuple[str, str]] = []
        self.sp_end = NOW + timedelta(days=50)
        self.ri_end = NOW + timedelta(days=100)


class Session:
    def __init__(self, world: World, account: str) -> None:
        self.world, self.account = world, account

    def client(self, name: str, region: str = "us-east-1") -> Any:
        if name == "ce":
            return FakeCe(self.world)
        if name == "savingsplans":
            return FakeSavingsPlans(self.world)
        if name == "ec2":
            return FakeEc2(region, self.world, self.account)
        assert name == "rds"
        return FakeRds(region, self.account)


class Factory:
    def __init__(self, world: World) -> None:
        self.world = world

    def assume(self, role_arn: str, external_id: str, name: str) -> Session:
        account, role = role_arn.split(":")[4], role_arn.rsplit("/", 1)[1]
        assert name.endswith("-commitments")
        self.world.assumed.append((account, role))
        if account in self.world.no_access:
            raise client_error("AccessDenied", "AssumeRole")
        return Session(self.world, account)


@pytest.fixture
def world():  # type: ignore[no-untyped-def]
    w = World()
    connectors.set_session_factory(Factory(w))
    yield w
    connectors.set_session_factory(None)


def eligible(owner_sql, tenant: dict[str, Any], connection: str, day: date, usd: float,
             usage_type: str = "EUW2-BoxUsage:m5.large", service_code: str = "AmazonEC2",
             category: str = "Usage") -> None:  # fmt: skip
    owner_sql(
        "INSERT INTO cost_daily (tenant_id, connection_id, usage_date, billing_period, "
        "account_id, service_code, service_name, service_category, region_id, usage_type, "
        "charge_category, billed_cost_usd, effective_cost_usd, net_cost_usd, data_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 'Compute', 'eu-west-2', %s, %s, %s, %s, %s, 'v1')",
        tenant["id"], connection, day, day.replace(day=1), ACCOUNT, service_code, service_code,
        usage_type, category, usd, usd, usd,
    )  # fmt: skip


def open_rec(owner_sql, tenant: dict[str, Any], rule: str, resource_id: str, savings: float,
             status: str = "new", account: str = ACCOUNT) -> None:  # fmt: skip
    rec_id = str(uuid.uuid4())
    owner_sql(
        "INSERT INTO recommendation (id, tenant_id, fingerprint, type, rule_id, rule_version, "
        "rule_config, generation_id, account_id, region_id, resource_id, title, "
        "cost_now_usd_month, cost_after_usd_month, savings_basis, effort, reversibility, "
        "risk_score, risk_level, confidence, priority_score, engine_version, status) VALUES "
        "(%s, %s, %s, 'x', %s, 1, '{}', 1, %s, 'eu-west-2', %s, 't', %s, 0, 'list_price', 'S', "
        "'reversible', 10, 'low', 'high', 1, 'test', %s)",
        rec_id, tenant["id"], rec_id, rule, account, resource_id, savings, status,
    )  # fmt: skip


async def healthy_workspace(client: AsyncClient, login, owner_sql):  # type: ignore[no-untyped-def]
    tenant, admin, connection = await workspace(client, login, owner_sql)
    owner_sql("UPDATE cloud_connection SET status = 'healthy' WHERE id = %s", connection)
    return tenant, admin, connection


async def test_refresh_lists_commitments_and_cost_explorer_view(
    client: AsyncClient, login, owner_sql, world: World
) -> None:
    tenant, admin, connection = await healthy_workspace(client, login, owner_sql)
    tid = tenant["id"]
    _, viewer = await member(client, login, owner_sql, tenant, "viewer")
    url = f"/v1/tenants/{tid}/commitments"
    assert (await client.post(f"{url}/refresh", headers=viewer)).status_code == 403
    assert (await client.get(url, headers=viewer)).json() == {
        "commitments": [], "coverage": [], "expiryAlertDays": [60, 30, 15]
    }  # fmt: skip

    world.no_access.add("999999999999")
    r = await client.post(f"{url}/refresh", headers=admin)
    assert r.json() == {"status": "partial", "connections": 1}  # af-south-1 is not allowed
    assert len(world.ce_calls) == 9  # 4 recommendations, 2 utilisation pages, 3 more
    # The payer's billing role, then each account's resource role.
    assert world.assumed[0] == (ACCOUNT, "CloudopticsBillingReadOnly")
    assert sorted(world.assumed[1:]) == [
        (ACCOUNT, "CloudopticsResourceReadOnly"), (OTHER, "CloudopticsResourceReadOnly")
    ]  # fmt: skip

    body = (await client.get(url, headers=viewer)).json()
    held = {c["commitmentId"]: c for c in body["commitments"]}
    assert set(held) == {SP_ARN, MEMBER_SP_ARN, "ri-eu", "db-ri"}
    sp = held[SP_ARN]
    assert (sp["kind"], sp["hourlyCommitmentUsd"], sp["utilisationPct"]) == (
        "savings_plan", 0.25, 97.5
    )  # fmt: skip
    assert (sp["termMonths"], sp["daysLeft"], sp["current"]) == (12, 50, True)
    assert sp["description"] == "Compute Savings Plan"
    assert body["commitments"][0]["commitmentId"] == SP_ARN  # soonest expiry first
    other = held[MEMBER_SP_ARN]
    assert (other["accountId"], other["utilisationPct"], other["daysLeft"]) == (OTHER, 64, 200)
    ri = held["ri-eu"]
    assert (ri["kind"], ri["accountId"], ri["regionId"], ri["quantity"]) == (
        "ec2_reserved", OTHER, "eu-west-2", 2
    )  # fmt: skip
    assert (ri["hourlyCommitmentUsd"], ri["upfrontUsd"], ri["termMonths"]) == (0.04, 1200, 36)
    assert (ri["utilisationPct"], ri["description"]) == (88, "2 x m5.large Linux/UNIX")
    db = held["db-ri"]
    assert (db["kind"], db["daysLeft"], db["description"]) == (
        "rds_reserved", 65, "1 x db.r6g.large postgresql Multi-AZ"
    )  # fmt: skip
    [cov] = body["coverage"]
    assert (cov["savingsPlansCoveragePercent"], cov["savingsPlansUtilisationPercent"]) == (
        9.1, 90.1
    )  # fmt: skip
    assert (cov["reservationsCoveragePercent"], cov["reservationsUtilisationPercent"]) == (
        12.5, 88
    )  # fmt: skip
    assert cov["errors"] == []

    run = owner_sql(
        "SELECT status, records, error_code, error_detail, est_customer_api_cost_usd "
        "FROM sync_run WHERE connection_id = %s AND job_type = 'commitments.refresh'",
        connection,
    )
    assert run[0][:3] == ("succeeded", 4, "partial")
    assert "af-south-1: UnauthorizedOperation" in str(run[0][3])
    assert float(str(run[0][4])) == 0.09
    audit = owner_sql(
        "SELECT action FROM audit_log WHERE tenant_id = %s AND action LIKE 'commitments.%%'",
        tid,
    )
    assert audit == [("commitments.refresh_requested",)]

    # The daily refresh within 20 hours skips Cost Explorer; a later one keeps old values.
    world.ce_calls.clear()
    world.sp_error = "AccessDeniedException"
    assert await service.refresh(uuid.UUID(tid), uuid.UUID(connection)) == "partial"
    assert world.ce_calls == []
    owner_sql(
        "UPDATE commitment_snapshot SET fetched_at = now() - interval '21 hours' "
        "WHERE connection_id = %s",
        connection,
    )
    world.ce_fail = {"coverage", "ri_coverage"}
    assert await service.refresh(uuid.UUID(tid), uuid.UUID(connection)) == "partial"
    assert len(world.ce_calls) == 9
    [cov] = (await client.get(url, headers=viewer)).json()["coverage"]
    assert cov["savingsPlansCoveragePercent"] is None
    assert cov["errors"] == [
        "savings plans coverage: DataUnavailableException",
        "reservation coverage: DataUnavailableException",
    ]

    # A disconnected connection is not refreshed.
    owner_sql("UPDATE cloud_connection SET status = 'disconnected' WHERE id = %s", connection)
    assert await service.refresh(uuid.UUID(tid), uuid.UUID(connection)) == "skipped"
    r = await client.post(f"{url}/refresh", headers=admin)
    assert r.json() == {"status": "skipped", "connections": 0}


async def test_refresh_failing_to_assume_the_billing_role(
    client: AsyncClient, login, owner_sql, world: World
) -> None:
    tenant, admin, connection = await healthy_workspace(client, login, owner_sql)
    world.no_access.add(ACCOUNT)
    r = await client.post(f"/v1/tenants/{tenant['id']}/commitments/refresh", headers=admin)
    assert r.json() == {"status": "failed", "connections": 1}
    assert owner_sql(
        "SELECT status, error_detail FROM sync_run WHERE connection_id = %s "
        "AND job_type = 'commitments.refresh'",
        connection,
    ) == [("failed", "AccessDenied")]


async def test_refresh_queues_on_the_worker(
    client: AsyncClient, login, owner_sql, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cloudoptics.analysis import jobs as analysis_jobs
    from cloudoptics.platform import jobs as platform_jobs
    from cloudoptics.platform.settings import get_settings

    tenant, admin, connection = await healthy_workspace(client, login, owner_sql)
    queued: list[tuple[str, ...]] = []

    async def fake_defer(app, tenant_id, connection_id):  # type: ignore[no-untyped-def]
        queued.append(("refresh", tenant_id, connection_id))

    async def fake_analysis(app, tenant_id):  # type: ignore[no-untyped-def]
        queued.append(("analysis", tenant_id))

    monkeypatch.setattr(jobs, "defer_refresh", fake_defer)
    monkeypatch.setattr(analysis_jobs, "defer_analysis", fake_analysis)
    monkeypatch.setattr(platform_jobs, "get_job_app", lambda: None)
    monkeypatch.setattr(get_settings(), "jobs_mode", "queue")
    r = await client.post(f"/v1/tenants/{tenant['id']}/commitments/refresh", headers=admin)
    assert r.status_code == 202 and r.json() == {"status": "queued", "connections": 1}
    assert queued == [("refresh", tenant["id"], connection)]

    # The task refreshes, alerts and queues the engine.
    assert await jobs.refresh(tenant["id"], connection) == "partial"
    assert queued[-1] == ("analysis", tenant["id"])
    assert await jobs.poll(0) >= 1
    assert ("refresh", tenant["id"], connection) in queued[2:]


# --- The advisory and R8 ------------------------------------------------------------------------


async def test_advisory_nets_open_savings_and_shows_fx_stress(
    client: AsyncClient, login, owner_sql, world: World
) -> None:
    tenant, admin, connection = await healthy_workspace(client, login, owner_sql)
    tid = tenant["id"]
    url = f"/v1/tenants/{tid}/commitments/savings-plans"
    assert (await client.get(url, headers=admin)).json()["advice"] == []

    last = NOW.date() - timedelta(days=1)
    # 70 days of bills; the advisory uses the last 60. Weekdays USD 48 (2/h), weekends 24.
    for i in range(70):
        day = last - timedelta(days=i)
        eligible(owner_sql, tenant, connection, day, 24 if day.weekday() >= 5 else 48)
        # Not eligible: Spot, storage, credits.
        eligible(owner_sql, tenant, connection, day, 30, usage_type="EUW2-SpotUsage:m5.large")
        eligible(owner_sql, tenant, connection, day, 9, usage_type="EUW2-EBS:VolumeUsage.gp3")
        eligible(owner_sql, tenant, connection, day, -5, category="Credit")
    eligible(owner_sql, tenant, connection, last, 0, service_code="AWSLambda",
             usage_type="EUW2-Lambda-GB-Second")  # fmt: skip

    # USD 73 a month (0.1/h) of open savings on servers is taken off first; others are not.
    open_rec(owner_sql, tenant, "R4_idle_ec2", "i-idle", 43)
    open_rec(owner_sql, tenant, "R7", "i-dev", 30)
    open_rec(owner_sql, tenant, "R5_rightsize_ec2", "i-done", 500, status="dismissed")
    open_rec(owner_sql, tenant, "R4_idle_rds", "db-1", 500)
    open_rec(owner_sql, tenant, "R1_unattached_ebs", "vol-1", 500)
    open_rec(owner_sql, tenant, "R5_rightsize_ec2", "i-elsewhere", 500, account="000000000000")

    me = (await client.get("/v1/me", headers=admin)).json()
    owner_sql(
        "INSERT INTO fx_rate (id, currency, rate_date, basis, rate, source, entered_by) "
        "VALUES (gen_random_uuid(), 'NGN', %s, 'official', 1500, 'manual', %s)",
        NOW.date(),
        me["id"],
    )

    # Before Cost Explorer: our simulation with typical discounts.
    [a] = (await client.get(url, headers=admin)).json()["advice"]
    assert (a["status"], a["payerAccountId"], a["dataThrough"]) == ("ok", ACCOUNT, str(last))
    b = a["baseline"]
    assert (b["days"], b["nettedUsdMonth"], b["p10HourlyUsd"], b["p20HourlyUsd"]) == (
        60, 73, 0.9, 0.9
    )  # fmt: skip
    assert b["targetHourlyUsd"] == pytest.approx(0.54)
    assert len(a["dailyEligible"]) == 60 and a["dailyEligible"][-1]["date"] == str(last)
    default = a["default"]
    assert (default["discountPercent"], default["discountFromAws"]) == (28, False)
    assert default["hourlyCommitmentUsd"] == 0.388
    assert a["currentCoveragePercent"] is None and a["awsFetchedAt"] is None

    await client.post(f"/v1/tenants/{tid}/commitments/refresh", headers=admin)
    # The refresh re-ran the engine, which closed the test's resource-less recommendations.
    reopen = (
        "UPDATE recommendation SET status = 'new' WHERE tenant_id = %s "
        "AND resource_id IN ('i-idle', 'i-dev')"
    )
    owner_sql(reopen, tid)
    body = (await client.get(url, headers=admin)).json()
    assert (body["returnWindowDays"], body["ladderFirstPercent"], body["ladderStepPercent"]) == (
        7, 25, 10
    )  # fmt: skip
    assert body["fxStressPercent"] == [20, 40]
    [a] = body["advice"]
    default = a["default"]
    assert (default["source"], default["instrument"], default["termYears"], default["payment"]) == (
        "ours", "savings_plan", 1, "no_upfront"
    )  # fmt: skip
    assert (default["discountPercent"], default["discountFromAws"]) == (30, True)
    assert default["hourlyCommitmentUsd"] == 0.378
    assert default["monthlySavingsUsd"] == pytest.approx(118.26, abs=0.01)
    assert len([o for o in a["options"] if o["source"] == "ours"]) == 12
    assert {(o["payment"], o["lookbackDays"]) for o in a["options"] if o["source"] == "aws"} == {
        ("no_upfront", 30), ("no_upfront", 60), ("all_upfront", 30), ("all_upfront", 60)
    }  # fmt: skip
    aws_all = next(
        o for o in a["options"] if o["source"] == "aws" and o["payment"] == "all_upfront"
        and o["lookbackDays"] == 60
    )  # fmt: skip
    assert (aws_all["hourlyCommitmentUsd"], aws_all["upfrontUsd"]) == (0.38, 3328.8)
    assert [s["sharePercent"] for s in a["ladder"]][:2] == [25, 35]
    assert a["ladder"][0]["buyHourlyUsd"] == pytest.approx(0.094)
    assert (a["currentCoveragePercent"], a["utilisationPercent"]) == (9.1, 90.1)
    assert a["targetCoveragePercent"] > a["currentCoveragePercent"]
    assert a["fxExposed"] is True
    fx = a["fx"]
    assert (fx["currency"], fx["rate"], fx["estimated"]) == ("NGN", 1500, False)
    assert [s["monthlyLocal"] for s in fx["scenarios"]] == [
        pytest.approx(0.378 * 730 * r, abs=0.01) for r in (1500, 1800, 2100)
    ]

    # R8 joins the recommendations, after the other rules.
    await run_analysis(uuid.UUID(tid))
    recs = (await client.get(f"/v1/tenants/{tid}/recommendations", headers=admin)).json()
    [r8] = [r for r in recs if r["ruleId"] == "R8_savings_plan"]
    assert r8["savingsUsdMonth"] == pytest.approx(118.26, abs=0.01)
    assert (r8["accountId"], r8["regionId"], r8["resourceId"]) == (
        ACCOUNT, "global", "compute-savings-plan"
    )  # fmt: skip
    detail = (await client.get(f"/v1/tenants/{tid}/recommendations/{r8['id']}",
                               headers=admin)).json()  # fmt: skip
    assert detail["reversibility"] == "irreversible"
    assert {f["factor"] for f in detail["riskFactors"]} == {
        "one_year_commitment", "paid_in_dollars"
    }  # fmt: skip
    assert detail["confidence"] == "high"  # AWS recommends at least as much
    assert detail["fix"]["console"]["url"] == service.PURCHASE_URL
    assert "7 days" in detail["fix"]["checklist"][2]
    assert "other_savings_netted" in detail["confidenceNotes"]

    # Turned off, it goes away.
    r = await client.put(
        f"/v1/tenants/{tid}/rule-settings",
        json={"disabledRules": ["R8_savings_plan"]},
        headers=admin,
    )
    assert r.status_code == 200, r.text
    await run_analysis(uuid.UUID(tid))
    status = owner_sql(
        "SELECT status FROM recommendation WHERE tenant_id = %s AND rule_id = 'R8_savings_plan'",
        tid,
    )
    assert status == [("resolved_externally",)]


async def test_advisory_states_and_usd_tenants(
    client: AsyncClient, login, owner_sql, world: World
) -> None:
    tenant, admin, connection = await healthy_workspace(client, login, owner_sql)
    tid = tenant["id"]
    owner_sql("UPDATE tenant SET display_currency = 'USD' WHERE id = %s", tid)
    url = f"/v1/tenants/{tid}/commitments/savings-plans"
    last = NOW.date() - timedelta(days=1)
    for i in range(10):
        eligible(owner_sql, tenant, connection, last - timedelta(days=i), 48)
    [a] = (await client.get(url, headers=admin)).json()["advice"]
    assert (a["status"], a["fxExposed"], a["fx"], a["ladder"]) == (
        "not_enough_data", False, None, []
    )  # fmt: skip
    for i in range(10, 30):
        eligible(owner_sql, tenant, connection, last - timedelta(days=i), 0.5)
    [a] = (await client.get(url, headers=admin)).json()["advice"]
    assert a["status"] == "no_purchase"  # quiet days leave too little to commit safely
    await run_analysis(uuid.UUID(tid))
    assert owner_sql(
        "SELECT count(*) FROM recommendation WHERE tenant_id = %s AND rule_id LIKE 'R8%%'", tid
    ) == [(0,)]


# --- Expiry alerts ------------------------------------------------------------------------------


async def test_expiry_alerts_once_per_threshold(
    client: AsyncClient,
    login,
    owner_sql,
    world: World,
    outbox: Outbox,  # noqa: F811
) -> None:
    tenant, admin, connection = await healthy_workspace(client, login, owner_sql)
    tid = uuid.UUID(tenant["id"])
    me = (await client.get("/v1/me", headers=admin)).json()
    viewer_email, _ = await member(client, login, owner_sql, tenant, "viewer")
    quiet_email, quiet = await member(client, login, owner_sql, tenant, "member")
    r = await client.put(
        f"/v1/tenants/{tid}/notification-preferences/me",
        json={"commitmentExpiry": False},
        headers=quiet,
    )
    assert r.json()["mine"]["commitmentExpiry"] is False
    await service.refresh(tid, uuid.UUID(connection))

    async def alert(at: datetime, seen: bool = True) -> int:
        if seen:  # listed by that day's refresh
            owner_sql("UPDATE commitment SET last_seen_at = %s WHERE tenant_id = %s", at, str(tid))
        async with get_sessionmaker()() as session, session.begin():
            await scope(session, tenant_id=tid)
            return await service.alert_expiries(session, tid, at)

    # Day 0: the payer's plan has 50 days left (the 60-day alert); the rest are further out.
    assert await alert(NOW) == 1
    [mail] = outbox.sent
    assert mail.to == [me["email"].lower()]  # admins and members, not the one who opted out
    assert mail.subject == "A commitment ends in 50 days"
    assert "Compute Savings Plan (account 111122223333, USD 0.250 an hour)" in mail.text
    assert "/commitments" in mail.text and "<li>" in mail.html
    assert await alert(NOW + timedelta(hours=1)) == 0  # once per threshold

    # 21 days later: 29 left (30-day alert), and the RDS reservation reaches 44 (60-day).
    assert await alert(NOW + timedelta(days=21)) == 1
    assert outbox.sent[-1].subject == "2 commitments end soon (first in 29 days)"
    assert await alert(NOW + timedelta(days=22)) == 0
    keys = owner_sql(
        "SELECT dedupe_key, status FROM notification WHERE tenant_id = %s "
        "AND kind = 'commitment_expiry' ORDER BY created_at",
        str(tid),
    )
    assert [k.rsplit(":", 1)[1] for k, _ in keys] == ["60", "30", "60"]
    assert {s for _, s in keys} == {"sent"}

    # A failed send is tried again.
    outbox.fail = True
    assert await alert(NOW + timedelta(days=36)) == 0  # 14 left: the 15-day alert fails
    outbox.fail = False
    assert await alert(NOW + timedelta(days=36, hours=1)) == 1
    # No longer listed by the refreshes: no alerts. Ended: no alert either.
    assert await alert(NOW + timedelta(days=60), seen=False) == 0
    assert await alert(NOW + timedelta(days=60)) == 1
    # The RDS reservation (5 days left) and the EC2 one (40); the payer plan has ended.
    assert outbox.sent[-1].subject == "2 commitments end soon (first in 5 days)"
    assert viewer_email not in outbox.sent[-1].to and quiet_email not in outbox.sent[-1].to


def test_aws_helpers_tolerate_missing_values() -> None:
    assert aws._time(None) is None and aws._time("not a date") is None
    assert aws._time("2026-01-01T00:00:00") == datetime(2026, 1, 1, tzinfo=UTC)
    assert aws._time(datetime(2026, 1, 1)) == datetime(2026, 1, 1, tzinfo=UTC)
    assert aws._float("x") is None and aws._float("") is None
    assert aws._months(None) is None
    assert aws.code(ValueError()) == "ValueError"
