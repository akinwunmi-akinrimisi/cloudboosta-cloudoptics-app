"""Inventory scanners (WBS CC-10, docs/11b §1.4 "Inventory").

For every account of a connection, assume ``CloudopticsResourceReadOnly`` (the optional
stage-2 stack, same External ID as the billing role) and list, in each enabled region, the
resources the recommendation rules need: EC2 instances, EBS volumes and snapshots, Elastic
IPs, load balancers (ALB/NLB/GWLB and Classic) and RDS DB instances. An account whose role
cannot be assumed is marked without resource access and skipped. Regions are scanned four at a
time (docs/11b §1.4). Only a region that scanned completely can mark resources deleted.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from cloudoptics.connectors.aws import STACK_REGION, resource_role_arn, session_name
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.service import get_connection, get_session_factory
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7

log = logging.getLogger(__name__)

USABLE = ("healthy", "degraded")
REGION_CONCURRENCY = 4
NO_ACCESS = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}

ScanStatus = Literal["skipped", "succeeded", "partial", "failed"]


@dataclass(frozen=True)
class ResourceRecord:
    resource_type: str
    resource_id: str
    state: str | None
    config: dict[str, Any]
    tags: dict[str, str] = field(default_factory=dict)
    arn: str | None = None

    @property
    def name(self) -> str | None:
        return self.tags.get("Name")


@dataclass
class AccountScan:
    account_id: str
    access: bool
    regions_ok: list[str] = field(default_factory=list)
    resources: dict[str, list[ResourceRecord]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "ClientError"))
    return type(exc).__name__


def check_access(
    factory: Any,
    account_id: str,
    external_id: str,
    tenant_id: uuid.UUID,
    stack_name: str | None = None,
) -> str | None:
    """Assume the resource role once: None when it works, else the AWS error code."""
    try:
        factory.assume(
            resource_role_arn(account_id, stack_name),
            external_id,
            session_name(tenant_id, "inventory"),
        )
    except (ClientError, BotoCoreError) as exc:
        return _code(exc)
    return None


def _paged(
    call: Callable[..., dict[str, Any]], key: str, token_in: str, token_out: str, **kwargs: Any
) -> Iterator[dict[str, Any]]:
    token = None
    while True:
        page = call(**kwargs, **({token_in: token} if token else {}))
        yield from page.get(key, [])
        token = page.get(token_out)
        if not token:
            return


def _tags(items: list[dict[str, str]] | None) -> dict[str, str]:
    return {t["Key"]: t.get("Value", "") for t in items or [] if "Key" in t}


def _instances(ec2: Any) -> Iterator[ResourceRecord]:
    for reservation in _paged(ec2.describe_instances, "Reservations", "NextToken", "NextToken"):
        for i in reservation.get("Instances", []):
            yield ResourceRecord(
                "ec2_instance",
                i["InstanceId"],
                (i.get("State") or {}).get("Name"),
                {
                    "instanceType": i.get("InstanceType"),
                    "launchTime": i.get("LaunchTime"),
                    "platform": i.get("PlatformDetails") or i.get("Platform"),
                    "lifecycle": i.get("InstanceLifecycle", "on-demand"),
                    "stateTransitionReason": i.get("StateTransitionReason"),
                    "volumeIds": [
                        m["Ebs"]["VolumeId"] for m in i.get("BlockDeviceMappings", []) if "Ebs" in m
                    ],
                },
                _tags(i.get("Tags")),
            )


def _volumes(ec2: Any) -> Iterator[ResourceRecord]:
    for v in _paged(ec2.describe_volumes, "Volumes", "NextToken", "NextToken"):
        yield ResourceRecord(
            "ebs_volume",
            v["VolumeId"],
            v.get("State"),
            {
                "volumeType": v.get("VolumeType"),
                "sizeGiB": v.get("Size"),
                "iops": v.get("Iops"),
                "throughput": v.get("Throughput"),
                "createTime": v.get("CreateTime"),
                "availabilityZone": v.get("AvailabilityZone"),
                "encrypted": v.get("Encrypted"),
                "attachments": [
                    {"instanceId": a.get("InstanceId"), "state": a.get("State")}
                    for a in v.get("Attachments", [])
                ],
            },
            _tags(v.get("Tags")),
        )


def _ami_snapshots(ec2: Any) -> set[str]:
    """Snapshot IDs that back the account's own AMIs (never recommended for deletion)."""
    used = set()
    for image in ec2.describe_images(Owners=["self"]).get("Images", []):
        for mapping in image.get("BlockDeviceMappings", []):
            snapshot = (mapping.get("Ebs") or {}).get("SnapshotId")
            if snapshot:
                used.add(snapshot)
    return used


def _snapshots(ec2: Any) -> Iterator[ResourceRecord]:
    used_by_ami = _ami_snapshots(ec2)
    for s in _paged(
        ec2.describe_snapshots, "Snapshots", "NextToken", "NextToken", OwnerIds=["self"]
    ):
        yield ResourceRecord(
            "ebs_snapshot",
            s["SnapshotId"],
            s.get("State"),
            {
                "volumeId": s.get("VolumeId"),
                "volumeSizeGiB": s.get("VolumeSize"),
                "startTime": s.get("StartTime"),
                "storageTier": s.get("StorageTier", "standard"),
                "description": (s.get("Description") or "")[:200],
                "usedByAmi": s["SnapshotId"] in used_by_ami,
            },
            _tags(s.get("Tags")),
        )


def _addresses(ec2: Any) -> Iterator[ResourceRecord]:
    for a in ec2.describe_addresses().get("Addresses", []):
        associated = bool(a.get("AssociationId") or a.get("InstanceId"))
        yield ResourceRecord(
            "eip",
            a.get("AllocationId") or a["PublicIp"],
            "associated" if associated else "unassociated",
            {
                "publicIp": a.get("PublicIp"),
                "domain": a.get("Domain"),
                "instanceId": a.get("InstanceId"),
                "networkInterfaceId": a.get("NetworkInterfaceId"),
            },
            _tags(a.get("Tags")),
        )


def _load_balancers(elbv2: Any, elb: Any) -> Iterator[ResourceRecord]:
    for lb in _paged(elbv2.describe_load_balancers, "LoadBalancers", "Marker", "NextMarker"):
        arn = lb["LoadBalancerArn"]
        groups = elbv2.describe_target_groups(LoadBalancerArn=arn).get("TargetGroups", [])
        targets = healthy = 0
        for group in groups:
            health = elbv2.describe_target_health(TargetGroupArn=group["TargetGroupArn"])
            for t in health.get("TargetHealthDescriptions", []):
                targets += 1
                healthy += (t.get("TargetHealth") or {}).get("State") == "healthy"
        state = (lb.get("State") or {}).get("Code")
        yield ResourceRecord(
            "elb",
            lb.get("LoadBalancerName") or arn,
            # "idle" (no healthy targets) gets its own state so state_since dates it.
            "idle" if state == "active" and healthy == 0 else state,
            {
                "type": lb.get("Type"),
                "scheme": lb.get("Scheme"),
                "createdTime": lb.get("CreatedTime"),
                "targetGroups": len(groups),
                "targets": targets,
                "healthyTargets": healthy,
            },
            arn=arn,
        )
    for lb in _paged(
        elb.describe_load_balancers, "LoadBalancerDescriptions", "Marker", "NextMarker"
    ):
        name = lb["LoadBalancerName"]
        instances = lb.get("Instances", [])
        healthy = 0
        if instances:
            states = elb.describe_instance_health(LoadBalancerName=name).get("InstanceStates", [])
            healthy = sum(s.get("State") == "InService" for s in states)
        yield ResourceRecord(
            "elb",
            name,
            "active" if healthy else "idle",
            {
                "type": "classic",
                "scheme": lb.get("Scheme"),
                "createdTime": lb.get("CreatedTime"),
                "targetGroups": 0,
                "targets": len(instances),
                "healthyTargets": healthy,
            },
        )


def _db_instances(rds: Any) -> Iterator[ResourceRecord]:
    for db in _paged(rds.describe_db_instances, "DBInstances", "Marker", "Marker"):
        yield ResourceRecord(
            "rds_instance",
            db["DBInstanceIdentifier"],
            db.get("DBInstanceStatus"),
            {
                "instanceClass": db.get("DBInstanceClass"),
                "engine": db.get("Engine"),
                "engineVersion": db.get("EngineVersion"),
                "multiAz": db.get("MultiAZ", False),
                "storageType": db.get("StorageType"),
                "allocatedStorageGiB": db.get("AllocatedStorage"),
                "createTime": db.get("InstanceCreateTime"),
                "clusterId": db.get("DBClusterIdentifier"),
                "readReplicaOf": db.get("ReadReplicaSourceDBInstanceIdentifier"),
            },
            _tags(db.get("TagList")),
            arn=db.get("DBInstanceArn"),
        )


def compute_optimizer_findings(optimizer: Any) -> dict[str, dict[str, Any]]:
    """AWS Compute Optimizer's EC2 findings by instance ID (WBS CC-12, FR-ANA-05).

    Empty when the account has not turned Compute Optimizer on: CloudOptics is read-only and
    does not opt accounts in, so rightsizing then relies on its own check.
    """
    findings: dict[str, dict[str, Any]] = {}
    try:
        pages = _paged(
            optimizer.get_ec2_instance_recommendations,
            "instanceRecommendations",
            "nextToken",
            "nextToken",
        )
        for rec in pages:
            instance_id = str(rec.get("instanceArn", "")).rsplit("/", 1)[-1]
            options = sorted(rec.get("recommendationOptions", []), key=lambda o: o.get("rank", 99))
            findings[instance_id] = {
                "finding": rec.get("finding"),
                "options": [
                    {
                        "instanceType": o.get("instanceType"),
                        "performanceRisk": o.get("performanceRisk"),
                        "rank": o.get("rank"),
                    }
                    for o in options[:3]
                ],
            }
    except (ClientError, BotoCoreError):
        return {}
    return findings


def scan_region(aws: Any, region: str) -> list[ResourceRecord]:
    ec2 = aws.client("ec2", region)
    records: list[ResourceRecord] = []
    findings = compute_optimizer_findings(aws.client("compute-optimizer", region))
    for instance in _instances(ec2):
        if instance.resource_id in findings:
            instance.config["computeOptimizer"] = findings[instance.resource_id]
        records.append(instance)
    for lister in (_volumes, _snapshots, _addresses):
        records.extend(lister(ec2))
    records.extend(_load_balancers(aws.client("elbv2", region), aws.client("elb", region)))
    records.extend(_db_instances(aws.client("rds", region)))
    return records


def scan_account(aws: Any, account_id: str) -> AccountScan:
    result = AccountScan(account_id, access=True)
    regions = [
        r["RegionName"]
        for r in aws.client("ec2", STACK_REGION).describe_regions().get("Regions", [])
    ]

    def _one(region: str) -> tuple[str, list[ResourceRecord] | None, str | None]:
        try:
            return region, scan_region(aws, region), None
        except (ClientError, BotoCoreError, KeyError) as exc:
            return region, None, f"{region}: {_code(exc)}"

    with ThreadPoolExecutor(max_workers=REGION_CONCURRENCY) as pool:
        for region, records, error in pool.map(_one, sorted(regions)):
            if records is None:
                result.errors.append(error or region)
            else:
                result.regions_ok.append(region)
                result.resources[region] = records
    return result


async def scan_connection(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> ScanStatus:
    """Scan every current account of one connection and store what was found."""
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        accounts = [
            r.aws_account_id
            for r in await session.execute(
                text(
                    "SELECT aws_account_id FROM cloud_account WHERE connection_id = :c AND "
                    "coalesce(org_status, 'ACTIVE') = 'ACTIVE' ORDER BY aws_account_id"
                ),
                {"c": connection_id},
            )
        ]
    if conn.status not in USABLE:
        return "skipped"
    external_id = get_encryptor().decrypt(bytes(conn.external_id_enc), tenant_id)
    factory = get_session_factory()

    def _scan(account_id: str) -> AccountScan:
        try:
            aws = factory.assume(
                resource_role_arn(account_id, conn.stack_name),
                external_id,
                session_name(tenant_id, "inventory"),
            )
        except ClientError as exc:
            scan = AccountScan(account_id, access=False)
            if _code(exc) not in NO_ACCESS:
                scan.errors.append(f"assume: {_code(exc)}")
            return scan
        try:
            return scan_account(aws, account_id)
        except (ClientError, BotoCoreError, KeyError) as exc:
            return AccountScan(account_id, access=True, errors=[f"regions: {_code(exc)}"])

    started = datetime.now(UTC)
    scans = [await run_in_threadpool(_scan, account) for account in accounts]
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        for scan in scans:
            await _store(session, tenant_id, connection_id, scan, started)
        records = sum(len(r) for s in scans for r in s.resources.values())
        errors = [f"{s.account_id} {e}" for s in scans for e in s.errors]
        status: ScanStatus = (
            "skipped"
            if not any(s.access for s in scans)
            else ("partial" if errors else "succeeded")
        )
        await session.execute(
            text(
                "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, "
                "started_at, finished_at, records, error_code, error_detail) VALUES (:id, :t, "
                ":c, 'inventory.scan', CAST(:scope AS jsonb), :status, :started, now(), "
                ":records, :code, :detail)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "scope": json.dumps(
                    {
                        "accounts": len(scans),
                        "with_access": sum(s.access for s in scans),
                        "regions": sum(len(s.regions_ok) for s in scans),
                    }
                ),
                # A scan with no account access is not a failure: the stack is optional.
                "status": "succeeded" if status == "skipped" else status,
                "started": started,
                "records": records,
                "code": "scan_errors" if errors else None,
                "detail": "; ".join(errors)[:2000] or None,
            },
        )
    return status


async def _store(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    scan: AccountScan,
    started: datetime,
) -> None:
    await session.execute(
        text(
            "UPDATE cloud_account SET resource_access = :access WHERE tenant_id = :t "
            "AND aws_account_id = :a"
        ),
        {"access": scan.access, "t": tenant_id, "a": scan.account_id},
    )
    for region, records in scan.resources.items():
        if records:
            await session.execute(
                text(
                    "INSERT INTO resource (id, tenant_id, connection_id, account_id, region_id, "
                    "resource_type, resource_id, arn, name, state, config, tags) VALUES (:id, "
                    ":t, :c, :a, :r, :type, :rid, :arn, :name, :state, CAST(:config AS jsonb), "
                    "CAST(:tags AS jsonb)) ON CONFLICT (tenant_id, account_id, region_id, "
                    "resource_id) DO UPDATE SET resource_type = EXCLUDED.resource_type, "
                    "arn = EXCLUDED.arn, name = EXCLUDED.name, state = EXCLUDED.state, "
                    "config = EXCLUDED.config, tags = EXCLUDED.tags, last_seen_at = now(), "
                    "state_since = CASE WHEN resource.state IS DISTINCT FROM EXCLUDED.state "
                    "OR resource.deleted_at IS NOT NULL THEN now() ELSE resource.state_since END, "
                    "deleted_at = NULL, connection_id = EXCLUDED.connection_id"
                ),
                [
                    {
                        "id": uuid7(),
                        "t": tenant_id,
                        "c": connection_id,
                        "a": scan.account_id,
                        "r": region,
                        "type": rec.resource_type,
                        "rid": rec.resource_id,
                        "arn": rec.arn,
                        "name": rec.name,
                        "state": rec.state,
                        "config": json.dumps(rec.config, default=str),
                        "tags": json.dumps(rec.tags),
                    }
                    for rec in records
                ],
            )
        await session.execute(
            text(
                "UPDATE resource SET deleted_at = now() WHERE tenant_id = :t AND account_id = :a "
                "AND region_id = :r AND deleted_at IS NULL AND last_seen_at < :started"
            ),
            {"t": tenant_id, "a": scan.account_id, "r": region, "started": started},
        )


@dataclass(frozen=True)
class InventoryCount:
    resource_type: str
    count: int


async def summary(session: AsyncSession) -> tuple[list[InventoryCount], datetime | None]:
    rows = await session.execute(
        text(
            "SELECT resource_type, count(*) AS n FROM resource WHERE deleted_at IS NULL "
            "GROUP BY resource_type ORDER BY resource_type"
        )
    )
    last = (
        await session.execute(
            text(
                "SELECT max(finished_at) FROM sync_run WHERE job_type = 'inventory.scan' "
                "AND status <> 'failed'"
            )
        )
    ).scalar_one()
    return [InventoryCount(r.resource_type, r.n) for r in rows], last
