"""Billing-only rules: R6b Extended Support charges and R10 idle NAT gateways.

Both read the per-resource CUR lines in ``cost_resource_daily`` rather than the inventory
(PRD FR-ANA-06b, FR-ANA-14):

- R6b: an RDS (MySQL, PostgreSQL, Aurora) database or EKS cluster billed for Extended
  Support. Upgrading to a version in standard support removes the charge; the saving is
  that charge over the last 30 days of bills. Medium risk: an engine or Kubernetes upgrade
  needs testing.
- R10: a NAT gateway billed ``NatGateway-Hours`` and no ``NatGateway-Bytes`` over the last
  30 days. Deleting it saves the hourly charge. From template 2.0.4 the inventory records the
  route tables that send traffic to each gateway: routes still pointing at it raise the risk
  and the fix lists them (changing them is the customer's decision); none clears the
  "routes unchecked" factor. Without route data (an older template, or not scanned) the fix
  starts by finding the routes.

The window is the 30 days up to the newest day on the bill (CUR arrives a day or two late),
not up to today. Savings are at the tenant's own rate (``effective_rate``).
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal
from urllib.parse import quote

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.analysis.pricing import HOURS_PER_MONTH
from cloudoptics.analysis.registry import RuleSpec
from cloudoptics.analysis.rules import (
    BilledUsage,
    Candidate,
    Confidence,
    Context,
    Resource,
    _cli,
    _region_args,
    env_factor,
    parse_time,
)

WINDOW_DAYS = 30
# Usage types these rules read (EKS writes "extendedSupport").
BILLED_USAGE_TYPES = r"NatGateway-(Hours|Bytes)$|ExtendedSupport"
# A charge that stopped this many days before the bill's last day is gone (upgraded or
# deleted): no recommendation.
STILL_BILLED_DAYS = 2
HIGH_CONFIDENCE_DAYS = 28
MEDIUM_CONFIDENCE_DAYS = 14
_NAMESPACE = uuid.UUID("6f1c3c2e-8f0a-4b7e-9d55-0c0c0c0c0c10")

# "AFS1-ExtendedSupport:Yr1-Yr2:PostgreSQL11", "USE1-Aurora:ExtendedSupport:Yr1-Yr2:MySQL5.7"
_RDS_USAGE = re.compile(
    r"ExtendedSupport:(?P<years>[^:]+):(?P<engine>[A-Za-z]+)(?P<version>[\d.]+)"
)
# Usage-type engine -> (RDS CLI engine, Aurora CLI engine, display name)
_RDS_ENGINES = {
    "postgresql": ("postgres", "aurora-postgresql", "PostgreSQL"),
    "mysql": ("mysql", "aurora-mysql", "MySQL"),
}


async def load_billing(session: AsyncSession) -> list[BilledUsage]:
    """The last 30 days of per-resource lines, inside a tenant-scoped transaction.

    Lines from disconnected connections are left out: their recommendations expired.
    """
    rows = await session.execute(
        text(
            "WITH last AS (SELECT max(usage_date) AS d FROM cost_resource_daily "
            "WHERE provider = 'aws') "
            "SELECT c.usage_date, c.provider_account_id, c.region_id, c.service_code, "
            "c.usage_type, "
            "c.resource_id, c.effective_cost_usd, c.usage_quantity, t.tags "
            "FROM cost_resource_daily c CROSS JOIN last "
            "JOIN cloud_connection k ON k.id = c.connection_id "
            "LEFT JOIN tag_set t ON t.hash = c.tag_set_hash "
            "WHERE c.provider = 'aws' AND c.usage_date > last.d - :days "
            "AND k.status <> 'disconnected' "
            "AND c.usage_type ~* :usage_types"
        ),
        {"days": WINDOW_DAYS, "usage_types": BILLED_USAGE_TYPES},
    )
    return [
        BilledUsage(
            day=r.usage_date,
            account_id=r.provider_account_id.strip(),
            region_id=r.region_id or "global",
            service_code=r.service_code,
            usage_type=r.usage_type,
            resource_id=r.resource_id,
            cost=float(r.effective_cost_usd),
            quantity=float(r.usage_quantity),
            tags=bill_tags(r.tags),
        )
        for r in rows
    ]


def bill_tags(tags: dict[str, str] | None) -> dict[str, str]:
    """CUR 2.0 prefixes user tags with ``user_``; the rules read them without it."""
    return {k.removeprefix("user_"): v for k, v in (tags or {}).items()}


@dataclass
class _Billed:
    account_id: str
    region_id: str
    resource_id: str
    usage_types: set[str] = field(default_factory=set)
    tags: dict[str, str] = field(default_factory=dict)
    cost: float = 0.0
    quantity: float = 0.0
    days: set[date] = field(default_factory=set)

    def add(self, line: BilledUsage) -> None:
        self.usage_types.add(line.usage_type)
        self.tags = self.tags or line.tags
        self.cost += line.cost
        self.quantity += line.quantity
        if line.cost > 0 or line.quantity > 0:
            self.days.add(line.day)


def _window(ctx: Context) -> tuple[date, list[BilledUsage]] | None:
    if not ctx.billing:
        return None
    end = max(line.day for line in ctx.billing)
    start = end - timedelta(days=WINDOW_DAYS - 1)
    return end, [line for line in ctx.billing if line.day >= start]


def _confidence(days: int, notes: list[str]) -> Confidence:
    if days >= HIGH_CONFIDENCE_DAYS:
        return "high"
    notes.append("short_billing_history")
    return "medium" if days >= MEDIUM_CONFIDENCE_DAYS else "low"


def short_id(resource_id: str) -> str:
    """``arn:aws:ec2:...:natgateway/nat-1`` -> ``nat-1``; ``...:db:ledger`` -> ``ledger``."""
    if not resource_id.startswith("arn:"):
        return resource_id
    return re.split(r"[:/]", resource_id)[-1]


def _billed_resource(
    ctx: Context, b: _Billed, resource_type: str, resource_id: str, arn: str | None
) -> Resource:
    """The inventory resource when it was scanned, otherwise one known only from the bill."""
    for key in (resource_id, arn):
        found = ctx.inventory.get((b.account_id, b.region_id, resource_type, key or ""))
        if found is not None:
            return found
    first = datetime.combine(min(b.days), time(), tzinfo=UTC)
    return Resource(
        id=uuid.uuid5(_NAMESPACE, f"{b.account_id}:{b.region_id}:{resource_type}:{resource_id}"),
        account_id=b.account_id,
        region_id=b.region_id,
        resource_type=resource_type,
        resource_id=resource_id,
        name=None,
        state=None,
        config={},
        tags=b.tags,
        state_since=first,
        first_seen_at=first,
        arn=arn,
        in_inventory=False,
    )


def _evidence(b: _Billed, end: date) -> list[dict[str, Any]]:
    return [
        {"kind": "billedDays", "value": len(b.days), "days": WINDOW_DAYS},
        {"kind": "billedThrough", "value": end.isoformat()},
        {"kind": "usageType", "value": ", ".join(sorted(b.usage_types))},
    ]


# --- R6b: Extended Support charges (FR-ANA-06b) -------------------------------------------


@dataclass(frozen=True)
class _Support:
    service: Literal["rds", "eks"]
    cluster: bool  # an Aurora or other RDS cluster rather than one DB instance
    engine: str | None  # the RDS CLI engine name, e.g. "postgres" or "aurora-mysql"
    engine_name: str | None  # "PostgreSQL" or "MySQL"
    version: str | None  # from the usage type, e.g. "11" or "5.7"
    years: str | None  # e.g. "Yr1-Yr2" (the charge goes up from year 3)


def _support(b: _Billed, service_code: str) -> _Support | None:
    if service_code == "AmazonEKS":
        return _Support("eks", True, None, None, None, None)
    if service_code != "AmazonRDS":
        return None
    cluster = ":cluster:" in b.resource_id
    for usage_type in sorted(b.usage_types):
        m = _RDS_USAGE.search(usage_type)
        if m is None:
            continue
        engines = _RDS_ENGINES.get(m["engine"].lower())
        if engines is None:
            return None  # only MySQL and PostgreSQL have Extended Support (PRD FR-ANA-06b)
        aurora = "aurora" in usage_type.lower() or cluster
        engine = engines[1] if aurora else engines[0]
        return _Support("rds", cluster, engine, engines[2], m["version"], m["years"])
    return _Support("rds", cluster, None, None, None, None)


def _rds_fix(r: Resource, s: _Support) -> tuple[dict[str, Any], list[str]]:
    region = _region_args(r)
    db = r.resource_id
    snap = f"{db}-before-upgrade"[:63]
    engine = s.engine or "<engine>"
    current = str(r.config.get("engineVersion") or s.version or "<current-version>")
    targets = _cli(
        "aws", "rds", "describe-db-engine-versions", "--engine", engine, "--engine-version",
        current, "--query", "DBEngineVersions[].ValidUpgradeTarget[].EngineVersion", *region,
    )  # fmt: skip
    if s.cluster:
        steps = [
            targets,
            _cli("aws", "rds", "create-db-cluster-snapshot", "--db-cluster-identifier", db,
                 "--db-cluster-snapshot-identifier", snap, *region),
            _cli("aws", "rds", "wait", "db-cluster-snapshot-available",
                 "--db-cluster-snapshot-identifier", snap, *region),
            _cli("aws", "rds", "modify-db-cluster", "--db-cluster-identifier", db,
                 "--engine-version", "<target-version>", "--allow-major-version-upgrade",
                 "--apply-immediately", *region),
        ]  # fmt: skip
        rollback = [
            _cli("aws", "rds", "restore-db-cluster-from-snapshot", "--db-cluster-identifier",
                 f"{db}-restored"[:63], "--snapshot-identifier", snap, "--engine", engine,
                 *region),
        ]  # fmt: skip
    else:
        steps = [
            targets,
            _cli("aws", "rds", "create-db-snapshot", "--db-instance-identifier", db,
                 "--db-snapshot-identifier", snap, *region),
            _cli("aws", "rds", "wait", "db-snapshot-available", "--db-snapshot-identifier",
                 snap, *region),
            _cli("aws", "rds", "modify-db-instance", "--db-instance-identifier", db,
                 "--engine-version", "<target-version>", "--allow-major-version-upgrade",
                 "--apply-immediately", *region),
        ]  # fmt: skip
        rollback = [
            _cli("aws", "rds", "restore-db-instance-from-db-snapshot",
                 "--db-instance-identifier", f"{db}-restored"[:63], "--db-snapshot-identifier",
                 snap, *region),
        ]  # fmt: skip
    console = [
        "Open the database in the AWS console.",
        "Test the upgrade first: restore a recent snapshot to a new database, upgrade that "
        "copy and run your application's tests against it.",
        "Choose Actions, then Take snapshot, and wait until the snapshot shows Available.",
        "Choose Modify, set the engine version to one in standard support, choose Continue, "
        "then pick when to apply it and choose Modify.",
    ]
    fix = {
        "steps": steps,
        "rollback": rollback,
        "notes": "Pick a target from the first command's list that is in standard support. "
        "Major upgrades take the database offline for a few minutes and cannot be undone in "
        "place: to go back, restore the snapshot to a new database and point the application "
        "at it. Upgrade any read replicas and check the parameter group for the new version.",
    }
    return fix, console


def _eks_fix(r: Resource) -> tuple[dict[str, Any], list[str]]:
    region = _region_args(r)
    name = r.resource_id
    steps = [
        _cli("aws", "eks", "describe-cluster", "--name", name, "--query", "cluster.version",
             *region),
        _cli("aws", "eks", "list-insights", "--cluster-name", name, *region),
        _cli("aws", "eks", "update-cluster-version", "--name", name, "--kubernetes-version",
             "<next-version>", *region),
        _cli("aws", "eks", "wait", "cluster-active", "--name", name, *region),
        _cli("aws", "eks", "update-nodegroup-version", "--cluster-name", name,
             "--nodegroup-name", "<nodegroup>", *region),
    ]  # fmt: skip
    console = [
        "Open the cluster in the AWS console.",
        "On the Upgrade insights tab, fix every check that is not passing.",
        "Back up the cluster's workloads (for example with Velero) before you start.",
        "Choose Upgrade now, pick the next Kubernetes version and choose Update.",
        "When the cluster is Active again, update each node group and add-on to match. "
        "Repeat one version at a time until the cluster is in standard support.",
    ]
    fix = {
        "steps": steps,
        "rollback": [],
        "notes": "EKS upgrades one minor version at a time and a control plane cannot be "
        "downgraded. To go back, create a cluster on the old version and restore the "
        "workloads from the backup.",
    }
    return fix, console


def extended_support(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    found = _window(ctx)
    if found is None:
        return
    end, lines = found
    billed: dict[tuple[str, str, str, str], _Billed] = {}
    for line in lines:
        if "extendedsupport" not in line.usage_type.lower():
            continue
        key = (line.account_id, line.region_id, line.service_code, line.resource_id)
        billed.setdefault(key, _Billed(*key[:2], line.resource_id)).add(line)
    for (_, _, service_code, _), b in sorted(billed.items()):
        if b.cost <= 0.01 or not b.days or max(b.days) < end - timedelta(STILL_BILLED_DAYS):
            continue
        support = _support(b, service_code)
        if support is None:
            continue
        arn = b.resource_id if b.resource_id.startswith("arn:") else None
        if support.service == "eks":
            resource_type, label = "eks_cluster", "EKS cluster"
        else:
            resource_type = "rds_cluster" if support.cluster else "rds_instance"
            label = "database cluster" if support.cluster else "database"
        r = _billed_resource(ctx, b, resource_type, short_id(b.resource_id), arn)
        if ctx.settings.ignores(r.tags):
            continue
        notes: list[str] = []
        confidence = _confidence(len(b.days), notes)
        version = str(r.config.get("engineVersion") or support.version or "")
        if support.service == "eks":
            fix, console = _eks_fix(r)
            what = "Kubernetes"
            target = {"action": "upgrade", "kubernetesVersion": "standard_support"}
            factors = [{"factor": "version_upgrade", "points": 35},
                       {"factor": "no_downgrade", "points": 10}]  # fmt: skip
            reversibility: Literal["with_snapshot", "irreversible"] = "irreversible"
        else:
            fix, console = _rds_fix(r, support)
            what = support.engine_name or "engine"
            target = {"action": "upgrade", "engineVersion": "standard_support"}
            factors = [{"factor": "version_upgrade", "points": 35}]
            reversibility = "with_snapshot"
        title_version = f" {version}" if version else ""
        yield Candidate(
            rule_id="R6b_extended_support",
            rule_version=1,
            type="upgrade_extended_support",
            resource=r,
            title=f"Upgrade {label} {r.resource_id} off {what}{title_version} Extended Support",
            current_config={
                "service": support.service,
                "engine": support.engine,
                "version": version or None,
                "supportYears": support.years,
            },
            recommended_config=target,
            cost_now=b.cost,
            cost_after=0.0,
            basis="effective_rate",
            effort="M",
            reversibility=reversibility,
            confidence=confidence,
            confidence_notes=notes,
            risk_factors=[*env_factor(r.tags), *factors],
            evidence=[
                {"kind": "extendedSupportUsd", "value": round(b.cost, 2), "days": WINDOW_DAYS},
                *_evidence(b, end),
            ],
            rule_config={"windowDays": WINDOW_DAYS, "stillBilledDays": STILL_BILLED_DAYS},
            restart_required=True,
            console_steps=console,
            fix=fix,
        )


# --- R10: idle NAT gateways (FR-ANA-14) ---------------------------------------------------


# Route data older than this (scans stopped) is not trusted to say that no route points at it.
ROUTES_FRESH_DAYS = 7


@dataclass(frozen=True)
class _Routes:
    """What the inventory knows about the routes to one NAT gateway."""

    # checked: ``tables`` is the full list (maybe empty); access_needed: the role cannot read
    # route tables (a template before 2.0.4); unchecked: not scanned, stale or the call failed.
    status: Literal["checked", "access_needed", "unchecked"]
    tables: list[dict[str, Any]] = field(default_factory=list)


def _routes(ctx: Context, r: Resource) -> _Routes:
    if not r.in_inventory:
        return _Routes("unchecked")
    status = r.config.get("routeTablesStatus")
    if status == "access_denied":
        return _Routes("access_needed")
    tables = r.config.get("routeTables")
    checked = parse_time(r.config.get("routeTablesCheckedAt"))
    if status != "checked" or not isinstance(tables, list) or checked is None:
        return _Routes("unchecked")
    if tables:
        return _Routes("checked", tables)  # routes found are worth showing even if older
    if ctx.now - checked > timedelta(days=ROUTES_FRESH_DAYS):
        return _Routes("unchecked")
    return _Routes("checked")


def route_table_url(region: str, route_table_id: str) -> str:
    """The route table's page in the AWS console."""
    q = quote(region, safe="")
    return (
        f"https://{q}.console.aws.amazon.com/vpc/home?region={q}"
        f"#RouteTableDetails:RouteTableId={quote(route_table_id, safe='')}"
    )


def _destination_args(destination: str) -> list[str]:
    if destination.startswith("pl-"):
        return ["--destination-prefix-list-id", destination]
    if ":" in destination:
        return ["--destination-ipv6-cidr-block", destination]
    return ["--destination-cidr-block", destination]


def _route_fix(r: Resource, gateway: str, tables: list[dict[str, Any]]) -> dict[str, Any]:
    """The routes that still send traffic to the gateway: where to look and how to move each
    one. Changing a route is the customer's decision; Cloudoptics only advises it."""
    region = _region_args(r)
    out = []
    for t in tables:
        table_id = str(t.get("routeTableId"))
        destinations = [str(d) for d in t.get("destinations") or []]
        out.append(
            {
                "routeTableId": table_id,
                "vpcId": t.get("vpcId"),
                "destinations": destinations,
                "subnetIds": [str(s) for s in t.get("subnetIds") or []],
                "main": bool(t.get("main")),
                "consoleUrl": route_table_url(r.region_id, table_id),
                "inspect": _cli("aws", "ec2", "describe-route-tables", "--route-table-ids",
                                table_id, *region),
                "replace": [
                    _cli("aws", "ec2", "replace-route", "--route-table-id", table_id,
                         *_destination_args(d), "--nat-gateway-id", "<other-nat-gateway-id>",
                         *region)
                    for d in destinations
                ],
                "delete": [
                    _cli("aws", "ec2", "delete-route", "--route-table-id", table_id,
                         *_destination_args(d), *region)
                    for d in destinations
                ],
            }
        )  # fmt: skip
    return {"natGatewayId": gateway, "tables": out}


def _describe_table(t: dict[str, Any]) -> str:
    destinations = ", ".join(str(d) for d in t.get("destinations") or [])
    subnets = [str(s) for s in t.get("subnetIds") or []]
    if subnets:
        used_by = f"subnets {', '.join(subnets)}"
    elif t.get("main"):
        used_by = "every subnet without its own route table"
    else:
        used_by = "no subnet"
    main = ", main route table" if t.get("main") else ""
    return f"{t.get('routeTableId')} (VPC {t.get('vpcId')}{main}): {destinations}, for {used_by}"


def idle_nat_gateways(ctx: Context, resources: list[Resource]) -> Iterator[Candidate]:
    found = _window(ctx)
    if found is None:
        return
    end, lines = found
    hours: dict[tuple[str, str, str], _Billed] = {}
    traffic: dict[tuple[str, str, str], float] = defaultdict(float)
    for line in lines:
        key = (line.account_id, line.region_id, short_id(line.resource_id))
        if line.usage_type.endswith("NatGateway-Hours"):
            hours.setdefault(key, _Billed(*key)).add(line)
        elif line.usage_type.endswith("NatGateway-Bytes"):
            traffic[key] += line.quantity
    for key, b in sorted(hours.items()):
        if traffic.get(key, 0.0) > 0 or b.quantity <= 0 or b.cost <= 0 or not b.days:
            continue
        if max(b.days) < end - timedelta(STILL_BILLED_DAYS):
            continue  # deleted already
        gateway = key[2]
        r = _billed_resource(ctx, b, "nat_gateway", gateway, None)
        if ctx.settings.ignores(r.tags):
            continue
        routes = _routes(ctx, r)
        if routes.status == "checked" and routes.tables:
            notes = ["routes_still_point_here"]
            route_factor = [{"factor": "routes_point_here", "points": 30}]
        elif routes.status == "checked":
            notes = ["no_routes_to_it"]
            route_factor = []
        else:
            notes = [
                "route_tables_need_access_update"
                if routes.status == "access_needed"
                else "route_tables_not_checked"
            ]
            # The inventory has no route tables for it: a route may still point at it.
            route_factor = [{"factor": "routes_unchecked", "points": 15}]
        confidence = _confidence(len(b.days), notes)
        hourly = b.cost / b.quantity
        region = _region_args(r)
        export = f"{gateway}.json"
        save = _cli("aws", "ec2", "describe-nat-gateways", "--nat-gateway-ids", gateway,
                    *region) + f" > {export}"  # fmt: skip
        find_routes = _cli(
            "aws", "ec2", "describe-route-tables", "--filters",
            f"Name=route.nat-gateway-id,Values={gateway}", "--query",
            f"RouteTables[].[RouteTableId,Routes[?NatGatewayId==`{gateway}`].DestinationCidrBlock]",
            *region,
        )  # fmt: skip
        delete = _cli("aws", "ec2", "delete-nat-gateway", "--nat-gateway-id", gateway, *region)
        recreate = _cli("aws", "ec2", "create-nat-gateway", "--subnet-id", "<subnet-id>",
                        "--allocation-id", "<allocation-id>", *region)  # fmt: skip
        evidence: list[dict[str, Any]] = [
            {"kind": "natBytes", "value": 0, "days": WINDOW_DAYS},
            {"kind": "natHours", "value": round(b.quantity, 1), "days": WINDOW_DAYS},
            *_evidence(b, end),
        ]
        first_step = (
            "Open the NAT gateway in the AWS console and note its subnet, Elastic IP "
            "allocation ID and tags."
        )
        wait_step = "Wait a few days and check that nothing broke (no failed outbound calls)."
        last_step = (
            "Back on the NAT gateway, choose Actions, then Delete NAT gateway, and confirm. "
            "Keep its Elastic IP, or release it if you will not recreate the gateway."
        )
        fix_notes = (
            f"The first command saves the gateway's configuration to {export}; take the subnet "
            "and Elastic IP allocation from it to recreate the gateway."
        )
        extra: dict[str, Any] = {}
        if routes.status == "checked" and routes.tables:
            route_fix = _route_fix(r, gateway, routes.tables)
            extra["routes"] = route_fix
            evidence.append({"kind": "routeTables", "value": len(routes.tables)})
            steps = [save]
            for t in route_fix["tables"]:
                steps += [t["inspect"], *t["replace"]]
            steps.append(delete)
            rollback = [recreate, *(t["inspect"] for t in route_fix["tables"])]
            console_steps = [
                first_step,
                "These route tables still send traffic to it: "
                + "; ".join(_describe_table(t) for t in routes.tables)
                + ". Open each one, check what its subnets need, then choose Edit routes and "
                "point each route at another NAT gateway, or remove it if the subnets need no "
                "internet access.",
                wait_step,
                last_step,
            ]
            fix_notes += (
                " Changing a route is your decision: Cloudoptics never changes routes. "
                "replace-route sends the traffic to another NAT gateway (use one in the same "
                "Availability Zone); delete-route removes it. Wait a few days after changing "
                "the routes before you delete the gateway. Keep the Elastic IP if allow-lists "
                "use its address."
            )
        elif routes.status == "checked":
            evidence.append({"kind": "routeTables", "value": 0})
            steps = [save, find_routes, delete]
            rollback = [recreate]
            console_steps = [
                first_step,
                "No route tables send traffic to it (from the last resource scan). To be sure "
                "nothing was added since, open Route tables and search for the gateway's ID.",
                last_step,
            ]
            fix_notes += " Keep the Elastic IP if allow-lists use its address."
        else:
            steps = [
                save,
                find_routes,
                _cli("aws", "ec2", "delete-route", "--route-table-id", "<route-table-id>",
                     "--destination-cidr-block", "0.0.0.0/0", *region),
                delete,
            ]  # fmt: skip
            rollback = [
                recreate,
                _cli("aws", "ec2", "create-route", "--route-table-id", "<route-table-id>",
                     "--destination-cidr-block", "0.0.0.0/0", "--nat-gateway-id",
                     "<new-nat-gateway-id>", *region),
            ]  # fmt: skip
            console_steps = [
                first_step,
                "Open Route tables, search for the gateway's ID and delete each route that "
                "points at it.",
                wait_step,
                last_step,
            ]
            fix_notes += (
                " Wait a few days between removing the routes and deleting the gateway. Keep "
                "the Elastic IP if allow-lists use its address."
            )
            if routes.status == "access_needed":
                fix_notes += (
                    " Update CloudOptics access (Connections) to version 2.0.4 so the next "
                    "scan checks the route tables for you."
                )
        yield Candidate(
            rule_id="R10_idle_nat",
            rule_version=2,
            type="delete_idle_nat_gateway",
            resource=r,
            title=f"Delete idle NAT gateway {gateway} (no traffic in {WINDOW_DAYS} days)",
            current_config={"hourlyUsd": round(hourly, 4), "bytesProcessed": 0},
            recommended_config={"action": "delete"},
            cost_now=hourly * HOURS_PER_MONTH,
            cost_after=0.0,
            basis="effective_rate",
            effort="S",
            reversibility="reversible",
            confidence=confidence,
            confidence_notes=notes,
            risk_factors=[*env_factor(r.tags), *route_factor],
            evidence=evidence,
            rule_config={
                "windowDays": WINDOW_DAYS,
                "stillBilledDays": STILL_BILLED_DAYS,
                "routesFreshDays": ROUTES_FRESH_DAYS,
            },
            console_steps=console_steps,
            fix={"steps": steps, "rollback": rollback, "notes": fix_notes, **extra},
        )


# The AWS rules of this module, in evaluation order (analysis/registry.py; ADR-018).
RULE_SPECS: tuple[RuleSpec, ...] = (
    RuleSpec(
        id="R6b",
        provider="aws",
        rule_ids=("R6b_extended_support",),
        resource_types=frozenset({"eks_cluster", "rds_cluster", "rds_instance"}),
        version=1,
        evaluate=extended_support,
    ),
    RuleSpec(
        id="R10",
        provider="aws",
        rule_ids=("R10_idle_nat",),
        resource_types=frozenset({"nat_gateway"}),
        version=2,
        evaluate=idle_nat_gateways,
    ),
)
