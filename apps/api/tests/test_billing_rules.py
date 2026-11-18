"""Billing-only rules R6b (Extended Support) and R10 (idle NAT gateways): the per-resource
aggregates they read, the rules, and end to end from a CUR export to the API."""

import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest
from httpx import AsyncClient
from test_ingestion import bucket, connected, ingest  # noqa: F401 - pytest fixture
from test_recommendations import LIST

from cloudoptics.analysis import billing, engine, rules
from cloudoptics.analysis.pricing import HOURS_PER_MONTH, PriceBook
from cloudoptics.analysis.settings import RULE_IDS, RuleSettings
from cloudoptics.ingestion.aggregate import DailyAggregator, ResourceDailyCost
from cloudoptics.ingestion.focus import normalise
from cur_fixture import BillingPeriod, generate, write_export

SEPT = BillingPeriod(2026, 9)
NOW = datetime(2026, 10, 2, 12, tzinfo=UTC)
RDS_ARN = "arn:aws:rds:af-south-1:222222222222:db:ledger-pg11"
EKS_ARN = "arn:aws:eks:af-south-1:222222222222:cluster/payments"


def resource_rows(table: pa.Table) -> list[ResourceDailyCost]:
    aggregator = DailyAggregator()
    for batch in table.to_batches(max_chunksize=500):
        aggregator.update(normalise(batch))
    return aggregator.resource_rows()


def as_billed(
    rows: list[ResourceDailyCost], tags: dict[str, str] | None = None
) -> list[rules.BilledUsage]:
    return [
        rules.BilledUsage(
            day=r.usage_date,
            account_id=r.account_id,
            region_id=r.region_id or "global",
            service_code=r.service_code,
            usage_type=r.usage_type,
            resource_id=r.resource_id,
            cost=float(r.effective_cost),
            quantity=float(r.usage_quantity),
            tags=tags if tags is not None else {"env": "prod"},
        )
        for r in rows
    ]


FIXTURE_BILL = as_billed(resource_rows(generate(SEPT).table))


def run(
    bill: list[rules.BilledUsage],
    resources: list[rules.Resource] | None = None,
    settings: RuleSettings | None = None,
) -> list[engine.Scored]:
    ctx = rules.Context(
        now=NOW,
        prices=PriceBook(list_prices=LIST),
        billing=bill,
        settings=settings or RuleSettings(),
    )
    return engine.evaluate(ctx, resources or [])


def line(
    day: date,
    usage_type: str,
    resource_id: str,
    cost: float,
    qty: float,
    service: str = "AmazonEC2",
) -> rules.BilledUsage:
    return rules.BilledUsage(day, "111122223333", "eu-west-1", service, usage_type, resource_id,
                             cost, qty, {"env": "dev"})  # fmt: skip


def days(n: int, end: date = date(2026, 9, 30)) -> list[date]:
    return [end - timedelta(days=i) for i in range(n)]


def test_aggregates_keep_every_resource_line() -> None:
    rows = resource_rows(generate(SEPT).table)
    kept = {(r.resource_id, r.usage_type) for r in rows}
    # Every usage line with a resource ID is kept (verification compares them before and
    # after a change); the billing rules filter to their usage types when they read.
    assert all(r.resource_id for r in rows)
    assert kept >= {
        ("nat-0idle00000000001", "AFS1-NatGateway-Hours"),
        ("nat-0busy00000000001", "AFS1-NatGateway-Hours"),
        ("nat-0busy00000000001", "AFS1-NatGateway-Bytes"),
        (RDS_ARN, "AFS1-ExtendedSupport:Yr1-Yr2:PostgreSQL11"),
        (EKS_ARN, "AFS1-AmazonEKS-Hours:extendedSupport"),
    }
    idle = [r for r in rows if r.resource_id == "nat-0idle00000000001"]
    assert sum(float(r.usage_quantity) for r in idle) == pytest.approx(24 * 30)
    assert all(r.tag_set_hash is not None and r.region_id == "af-south-1" for r in idle)
    # Batches are merged: the same day split over two batches is one row.
    keys = {(r.usage_date, r.account_id, r.region_id, r.resource_id, r.usage_type) for r in rows}
    assert len(keys) == len(rows)


def test_idle_nat_gateway() -> None:
    scored = [s for s in run(FIXTURE_BILL) if s.candidate.rule_id == "R10_idle_nat"]
    assert [s.candidate.resource.resource_id for s in scored] == ["nat-0idle00000000001"]
    s = scored[0]
    c = s.candidate
    assert c.resource.resource_type == "nat_gateway" and not c.resource.in_inventory
    assert c.savings == pytest.approx(0.065 * HOURS_PER_MONTH)
    assert c.basis == "effective_rate" and c.confidence == "high"
    assert c.confidence_notes == ["route_tables_not_checked"]
    # Production 25 + routes not checked 15: medium; a dev gateway would be low.
    assert (s.risk_score, s.risk_level) == (40, "medium")
    assert c.fix["steps"][0].startswith("aws ec2 describe-nat-gateways --nat-gateway-ids "
                                        "nat-0idle00000000001 --region af-south-1 > ")  # fmt: skip
    assert "Name=route.nat-gateway-id,Values=nat-0idle00000000001" in c.fix["steps"][1]
    assert c.fix["steps"][-1] == (
        "aws ec2 delete-nat-gateway --nat-gateway-id nat-0idle00000000001 --region af-south-1"
    )
    assert c.fix["rollback"][0].startswith("aws ec2 create-nat-gateway")
    assert rules.console_url(c.resource) == (
        "https://af-south-1.console.aws.amazon.com/vpc/home?region=af-south-1"
        "#NatGatewayDetails:natGatewayId=nat-0idle00000000001"
    )
    row = engine._row(s, 1)
    assert row["resource_ref"] is None
    assert '"console"' in row["fix"]


def test_nat_gateways_with_traffic_short_history_arns_and_deleted_ones() -> None:
    arn = "arn:aws:ec2:eu-west-1:111122223333:natgateway/nat-arn"
    bill = [
        *(line(d, "EU-NatGateway-Hours", "nat-busy", 1.08, 24) for d in days(30)),
        line(date(2026, 9, 3), "EU-NatGateway-Bytes", "nat-busy", 0.01, 0.2),
        # Billed by ARN, and only for the last ten days: low confidence.
        *(line(d, "EU-NatGateway-Hours", arn, 1.08, 24) for d in days(10)),
        # Deleted a week before the bill's last day.
        *(
            line(d, "EU-NatGateway-Hours", "nat-gone", 1.08, 24)
            for d in days(20, date(2026, 9, 23))
        ),
    ]
    scored = [s for s in run(bill) if s.candidate.rule_id == "R10_idle_nat"]
    assert [s.candidate.resource.resource_id for s in scored] == ["nat-arn"]
    c = scored[0].candidate
    assert c.confidence == "low"
    assert c.confidence_notes == ["route_tables_not_checked", "short_billing_history"]
    assert scored[0].risk_level == "low"  # dev
    assert c.savings == pytest.approx(0.045 * HOURS_PER_MONTH)


def test_extended_support_for_rds_and_eks() -> None:
    scored = {
        s.candidate.resource.resource_type: s
        for s in run(FIXTURE_BILL)
        if s.candidate.rule_id == "R6b_extended_support"
    }
    assert set(scored) == {"rds_instance", "eks_cluster"}

    db = scored["rds_instance"]
    c = db.candidate
    assert c.resource.resource_id == "ledger-pg11" and c.resource.arn == RDS_ARN
    assert c.title == "Upgrade database ledger-pg11 off PostgreSQL 11 Extended Support"
    # The charge over the last 30 days: 48 vCPU-hours a day at 0.10.
    assert c.savings == pytest.approx(48 * 0.1 * 30)
    assert c.current_config == {"service": "rds", "engine": "postgres", "version": "11",
                                "supportYears": "Yr1-Yr2"}  # fmt: skip
    assert (db.risk_score, db.risk_level) == (60, "medium")  # production + upgrade
    assert c.effort == "M" and c.reversibility == "with_snapshot" and c.restart_required
    assert c.fix["steps"][0] == (
        "aws rds describe-db-engine-versions --engine postgres --engine-version 11 --query "
        "'DBEngineVersions[].ValidUpgradeTarget[].EngineVersion' --region af-south-1"
    )
    assert "create-db-snapshot --db-instance-identifier ledger-pg11" in c.fix["steps"][1]
    assert "--allow-major-version-upgrade" in c.fix["steps"][3]
    assert c.fix["rollback"] == [
        "aws rds restore-db-instance-from-db-snapshot --db-instance-identifier "
        "ledger-pg11-restored --db-snapshot-identifier ledger-pg11-before-upgrade "
        "--region af-south-1"
    ]
    assert rules.console_url(c.resource) == (
        "https://af-south-1.console.aws.amazon.com/rds/home?region=af-south-1"
        "#database:id=ledger-pg11;is-cluster=false"
    )

    eks = scored["eks_cluster"].candidate
    assert eks.resource.resource_id == "payments"
    assert eks.title == "Upgrade EKS cluster payments off Kubernetes Extended Support"
    assert eks.savings == pytest.approx(0.5 * 24 * 30)
    assert eks.reversibility == "irreversible" and eks.fix["rollback"] == []
    assert "update-cluster-version --name payments" in eks.fix["steps"][2]
    assert rules.console_url(eks.resource) == (
        "https://af-south-1.console.aws.amazon.com/eks/home?region=af-south-1#/clusters/payments"
    )


def test_extended_support_uses_the_inventory_and_skips_what_it_should() -> None:
    cluster_arn = "arn:aws:rds:eu-west-1:111122223333:cluster:orders"
    bill = [
        *(line(d, "EU-ExtendedSupport:Yr3:MySQL5.7", "arn:aws:rds:eu-west-1:111122223333:db:shop",
               2.0, 4, "AmazonRDS") for d in days(30)),
        *(line(d, "EU-Aurora:ExtendedSupport:Yr1-Yr2:PostgreSQL11", cluster_arn, 1.0, 8,
               "AmazonRDS") for d in days(15)),
        # Upgraded already: the charge stopped ten days before the bill's last day.
        *(line(d, "EU-ExtendedSupport:Yr1-Yr2:MySQL5.7", "arn:aws:rds:eu-west-1:111122223333:db:o",
               2.0, 4, "AmazonRDS") for d in days(10, date(2026, 9, 20))),
        # Not a MySQL or PostgreSQL charge.
        *(line(d, "EU-ExtendedSupport:Yr1-Yr2:Oracle19", "arn:aws:rds:eu-west-1:111122223333:db:x",
               2.0, 4, "AmazonRDS") for d in days(30)),
    ]  # fmt: skip
    shop = rules.Resource(
        id=uuid.uuid4(), account_id="111122223333", region_id="eu-west-1",
        resource_type="rds_instance", resource_id="shop", name=None, state="available",
        config={"engine": "mysql", "engineVersion": "5.7.44"}, tags={"env": "test"},
        state_since=NOW, first_seen_at=NOW,
        arn="arn:aws:rds:eu-west-1:111122223333:db:shop",
    )  # fmt: skip
    scored = {s.candidate.resource.resource_id: s for s in run(bill, [shop])
              if s.candidate.rule_id == "R6b_extended_support"}  # fmt: skip
    assert set(scored) == {"shop", "orders"}

    matched = scored["shop"].candidate
    assert matched.resource is shop and engine._row(scored["shop"], 1)["resource_ref"] == shop.id
    assert matched.title == "Upgrade database shop off MySQL 5.7.44 Extended Support"
    assert matched.current_config["supportYears"] == "Yr3"
    assert scored["shop"].risk_score == 35  # non-production: the upgrade alone

    orders = scored["orders"].candidate
    assert orders.resource.resource_type == "rds_cluster"
    assert orders.confidence == "medium" and orders.confidence_notes == ["short_billing_history"]
    assert orders.title == "Upgrade database cluster orders off PostgreSQL 11 Extended Support"
    assert "--engine aurora-postgresql" in orders.fix["steps"][0]
    assert "modify-db-cluster --db-cluster-identifier orders" in orders.fix["steps"][3]
    assert "restore-db-cluster-from-snapshot" in orders.fix["rollback"][0]

    # The ignore tag on the inventory resource hides it; a disabled rule produces nothing.
    ignored = rules.Resource(**{**shop.__dict__, "tags": {"cloudoptics:ignore": "yes"}})
    assert {s.candidate.resource.resource_id for s in run(bill, [ignored])
            if s.candidate.rule_id == "R6b_extended_support"} == {"orders"}  # fmt: skip
    off = RuleSettings(disabled_rules=frozenset({"R6b_extended_support", "R10_idle_nat"}))
    assert run(FIXTURE_BILL, settings=off) == []
    assert {"R6b_extended_support", "R10_idle_nat"} <= set(RULE_IDS)


def test_billing_rules_need_a_bill() -> None:
    assert run([]) == []
    hidden = as_billed(resource_rows(generate(SEPT).table), tags={"cloudoptics:ignore": "1"})
    assert run(hidden) == []
    assert billing.bill_tags({"user_env": "prod", "aws_createdBy": "x"}) == {
        "env": "prod",
        "aws_createdBy": "x",
    }
    assert billing.short_id("nat-1") == "nat-1"
    assert billing.short_id(EKS_ARN) == "payments"


async def test_from_the_bill_to_the_api(
    client: AsyncClient,
    login,  # type: ignore[no-untyped-def]
    owner_sql,
    bucket: Path,  # noqa: F811
) -> None:
    tenant, connection, headers = await connected(client, login, owner_sql)
    # R8 (Savings Plan advisory) is payer-wide and sized from the same bills; it has its
    # own tests (test_commitments.py), so it is off here.
    r = await client.put(
        f"/v1/tenants/{tenant}/rule-settings",
        json={"disabledRules": ["R8_savings_plan"]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    write_export(generate(SEPT), bucket / "cloudoptics")
    assert (await ingest(client, tenant, connection, headers))["status"] == "succeeded"
    # Every resource line is kept; five of them a day are the ones the billing rules read.
    assert owner_sql(
        "SELECT count(*) FROM cost_resource_daily WHERE connection_id = %s AND usage_type ~* %s",
        connection,
        billing.BILLED_USAGE_TYPES,
    ) == [(5 * SEPT.days,)]

    result = await engine.run_analysis(uuid.UUID(tenant), now=NOW)
    assert result["recommendations"] == 3
    base = f"/v1/tenants/{tenant}/recommendations"
    recs = (await client.get(base, headers=headers)).json()
    assert sorted((r["ruleId"], r["resourceId"]) for r in recs) == [
        ("R10_idle_nat", "nat-0idle00000000001"),
        ("R6b_extended_support", "ledger-pg11"),
        ("R6b_extended_support", "payments"),
    ]
    nat = next(r for r in recs if r["ruleId"] == "R10_idle_nat")
    assert nat["accountId"] == "333333333333" and nat["riskLevel"] == "low"  # dev
    detail = (await client.get(f"{base}/{nat['id']}", headers=headers)).json()
    assert detail["fix"]["console"]["url"].endswith("natGatewayId=nat-0idle00000000001")
    assert (await client.get(f"{base}/{nat['id']}/usage", headers=headers)).json() == []
    assert owner_sql(
        "SELECT count(*) FROM recommendation WHERE tenant_id = %s AND resource_ref IS NULL",
        tenant,
    ) == [(3,)]

    # Re-running keeps the same rows; disconnecting expires them and they stay expired.
    await engine.run_analysis(uuid.UUID(tenant), now=NOW)
    assert {r["id"] for r in (await client.get(base, headers=headers)).json()} == {
        r["id"] for r in recs
    }
    r = await client.delete(f"/v1/tenants/{tenant}/connections/{connection}", headers=headers)
    assert r.status_code == 204, r.text
    await engine.run_analysis(uuid.UUID(tenant), now=NOW)
    assert owner_sql("SELECT DISTINCT status FROM recommendation WHERE tenant_id = %s", tenant) == [
        ("expired",)
    ]


def nat(config: dict[str, object], resource_id: str = "nat-0idle00000000001") -> rules.Resource:
    """The idle fixture gateway as the inventory scanned it (account 333333333333)."""
    return rules.Resource(
        id=uuid.uuid4(), account_id="333333333333", region_id="af-south-1",
        resource_type="nat_gateway", resource_id=resource_id, name=None, state="available",
        config={"subnetId": "subnet-pub", "allocationIds": ["eipalloc-1"], **config},
        tags={}, state_since=NOW, first_seen_at=NOW,
    )  # fmt: skip


def r10(resources: list[rules.Resource]) -> engine.Scored:
    [s] = [s for s in run(FIXTURE_BILL, resources) if s.candidate.rule_id == "R10_idle_nat"]
    return s


CHECKED = {"routeTablesStatus": "checked", "routeTablesCheckedAt": (NOW - timedelta(days=1))}


def test_idle_nat_gateway_with_routes_still_pointing_at_it() -> None:
    tables = [
        {"routeTableId": "rtb-private-a", "vpcId": "vpc-1", "subnetIds": ["subnet-a", "subnet-b"],
         "destinations": ["0.0.0.0/0", "::/0"], "main": False},
        {"routeTableId": "rtb-main", "vpcId": "vpc-1", "subnetIds": [],
         "destinations": ["pl-0123"], "main": True},
    ]  # fmt: skip
    gateway = nat({**CHECKED, "routeTables": tables})
    s = r10([gateway])
    c = s.candidate
    assert c.resource is gateway and engine._row(s, 1)["resource_ref"] == gateway.id
    assert c.confidence_notes == ["routes_still_point_here"]
    # Untagged 15 + routes still pointing at it 30: higher than unchecked (15).
    assert {f["factor"] for f in c.risk_factors} == {"environment_unknown", "routes_point_here"}
    assert (s.risk_score, s.risk_level) == (45, "medium")
    assert {"kind": "routeTables", "value": 2} in c.evidence
    routes = c.fix["routes"]
    assert routes["natGatewayId"] == "nat-0idle00000000001"
    first, main = routes["tables"]
    assert first["consoleUrl"] == (
        "https://af-south-1.console.aws.amazon.com/vpc/home?region=af-south-1"
        "#RouteTableDetails:RouteTableId=rtb-private-a"
    )
    assert first["inspect"] == (
        "aws ec2 describe-route-tables --route-table-ids rtb-private-a --region af-south-1"
    )
    assert first["replace"] == [
        "aws ec2 replace-route --route-table-id rtb-private-a --destination-cidr-block "
        "0.0.0.0/0 --nat-gateway-id '<other-nat-gateway-id>' --region af-south-1",
        "aws ec2 replace-route --route-table-id rtb-private-a --destination-ipv6-cidr-block "
        "::/0 --nat-gateway-id '<other-nat-gateway-id>' --region af-south-1",
    ]
    assert main["delete"] == [
        "aws ec2 delete-route --route-table-id rtb-main --destination-prefix-list-id pl-0123 "
        "--region af-south-1"
    ]
    # The CLI route inspects and replaces each route before deleting the gateway; nothing
    # deletes a route for the customer.
    steps = c.fix["steps"]
    assert steps[1] == first["inspect"] and steps[2:4] == first["replace"]
    assert steps[-1].startswith("aws ec2 delete-nat-gateway") and len(steps) == 7
    assert not any("delete-route" in step for step in steps)
    assert "never changes routes" in c.fix["notes"]
    listed = c.console_steps[1]
    assert "rtb-private-a (VPC vpc-1): 0.0.0.0/0, ::/0, for subnets subnet-a, subnet-b" in listed
    assert "rtb-main (VPC vpc-1, main route table): pl-0123, for every subnet" in listed
    # Older route data still lists the routes it found.
    stale = {**CHECKED, "routeTablesCheckedAt": NOW - timedelta(days=30), "routeTables": tables}
    assert r10([nat(stale)]).candidate.confidence_notes == ["routes_still_point_here"]


def test_idle_nat_gateway_with_no_routes_to_it() -> None:
    s = r10([nat({**CHECKED, "routeTables": []})])
    c = s.candidate
    assert c.confidence_notes == ["no_routes_to_it"]
    assert [f["factor"] for f in c.risk_factors] == ["environment_unknown"]
    assert (s.risk_score, s.risk_level) == (15, "low")
    assert {"kind": "routeTables", "value": 0} in c.evidence
    assert "routes" not in c.fix
    assert not any("delete-route" in step for step in c.fix["steps"])
    assert c.fix["rollback"] == [
        "aws ec2 create-nat-gateway --subnet-id '<subnet-id>' --allocation-id "
        "'<allocation-id>' --region af-south-1"
    ]
    assert c.console_steps[1].startswith("No route tables send traffic to it")
    # Route data older than a week does not prove there are none: back to unchecked.
    old = {**CHECKED, "routeTablesCheckedAt": NOW - timedelta(days=8), "routeTables": []}
    stale = r10([nat(old)]).candidate
    assert stale.confidence_notes == ["route_tables_not_checked"]
    assert {"factor": "routes_unchecked", "points": 15} in stale.risk_factors


def test_idle_nat_gateway_when_route_tables_could_not_be_read() -> None:
    denied = r10([nat({"routeTablesStatus": "access_denied", "routeTables": None})]).candidate
    assert denied.confidence_notes == ["route_tables_need_access_update"]
    assert {"factor": "routes_unchecked", "points": 15} in denied.risk_factors
    assert "delete-route" in denied.fix["steps"][2]  # today's steps: find, then remove
    assert "Update CloudOptics access (Connections) to version 2.0.4" in denied.fix["notes"]
    assert "routes" not in denied.fix
    # Another error, or a gateway scanned before 2.0.4 (no route fields): as from the bill.
    for config in ({"routeTablesStatus": "error", "routeTables": None}, {}):
        c = r10([nat(config)]).candidate
        assert c.confidence_notes == ["route_tables_not_checked"]
        assert "2.0.4" not in c.fix["notes"]
