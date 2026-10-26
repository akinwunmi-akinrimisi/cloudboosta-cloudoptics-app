"""Inventory scanners (WBS CC-10, docs/11b §1.4 "Inventory").

For every account of a connection, assume ``CloudopticsResourceReadOnly`` (the optional
stage-2 stack, same External ID as the billing role) and list, in each enabled region, the
resources the recommendation rules need: EC2 instances, EBS volumes and snapshots, Elastic
IPs, load balancers (ALB/NLB/GWLB and Classic) and RDS DB instances. An account whose role
cannot be assumed is marked without resource access and skipped. Regions are scanned four at a
time (docs/11b §1.4). Only a region that scanned completely can mark resources deleted.

A scan is recorded as ``running`` in ``sync_run`` while it works (Scan now records it as soon
as it is queued), with the regions scanned so far, so the Connections page can show its
progress to anyone who opens it until it finishes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
# How often a running scan writes its progress, and when a scan that never finished (the
# worker restarted, or the queued scan was never picked up) stops counting as running.
PROGRESS_SECONDS = 2.0
STALE_AFTER = timedelta(minutes=30)
REGIONS_PER_ACCOUNT = 17  # enabled by default in a new account

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


class ScanProgress:
    """Regions scanned so far and the expected total, shared with the scanning threads.

    Accounts are scanned one after another and each one's region list is known only when
    its scan starts, so accounts not reached yet are counted at the average seen so far.
    """

    def __init__(self, accounts: int, regions_per_account: float) -> None:
        self._lock = threading.Lock()
        self._accounts = accounts
        self._per_account = regions_per_account
        self._known: list[int] = []
        self._done = 0

    def account_regions(self, count: int) -> None:
        with self._lock:
            self._known.append(count)

    def region_done(self) -> None:
        with self._lock:
            self._done += 1

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            scanned = [n for n in self._known if n]
            per = sum(scanned) / len(scanned) if scanned else self._per_account
            ahead = max(0, self._accounts - len(self._known))
            total = sum(self._known) + round(per * ahead)
            return {"regionsDone": self._done, "regionsTotal": max(total, self._done)}


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


def scan_account(aws: Any, account_id: str, progress: ScanProgress | None = None) -> AccountScan:
    result = AccountScan(account_id, access=True)
    regions = [
        r["RegionName"]
        for r in aws.client("ec2", STACK_REGION).describe_regions().get("Regions", [])
    ]
    if progress:
        progress.account_regions(len(regions))

    def _one(region: str) -> tuple[str, list[ResourceRecord] | None, str | None]:
        try:
            return region, scan_region(aws, region), None
        except (ClientError, BotoCoreError, KeyError) as exc:
            return region, None, f"{region}: {_code(exc)}"
        finally:
            if progress:
                progress.region_done()

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
    started = datetime.now(UTC)
    progress = ScanProgress(len(accounts), REGIONS_PER_ACCOUNT)
    run_id = await _start_run(tenant_id, connection_id, len(accounts), started)

    def _scan(account_id: str) -> AccountScan:
        try:
            aws = factory.assume(
                resource_role_arn(account_id, conn.stack_name),
                external_id,
                session_name(tenant_id, "inventory"),
            )
        except ClientError as exc:
            progress.account_regions(0)
            scan = AccountScan(account_id, access=False)
            if _code(exc) not in NO_ACCESS:
                scan.errors.append(f"assume: {_code(exc)}")
            return scan
        try:
            return scan_account(aws, account_id, progress)
        except (ClientError, BotoCoreError, KeyError) as exc:
            progress.account_regions(0)
            return AccountScan(account_id, access=True, errors=[f"regions: {_code(exc)}"])

    reporter = asyncio.create_task(_report_progress(tenant_id, run_id, progress))
    try:
        scans = [await run_in_threadpool(_scan, account) for account in accounts]
    except Exception as exc:
        # Recorded so the Connections page can say the scan failed, not just stay silent.
        await _record_failure(tenant_id, connection_id, run_id, started, exc)
        raise
    finally:
        reporter.cancel()
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
                "UPDATE sync_run SET scope = CAST(:scope AS jsonb), status = :status, "
                "finished_at = now(), records = :records, error_code = :code, "
                "error_detail = :detail WHERE id = :id"
            ),
            {
                "id": run_id,
                "scope": json.dumps(
                    {
                        "accounts": len(scans),
                        "with_access": sum(s.access for s in scans),
                        "regions": sum(len(s.regions_ok) for s in scans),
                    }
                ),
                # A scan with no account access is not a failure: the stack is optional.
                "status": "succeeded" if status == "skipped" else status,
                "records": records,
                "code": "scan_errors" if errors else None,
                "detail": "; ".join(errors)[:2000] or None,
            },
        )
    return status


async def queue_run(
    session: AsyncSession, tenant_id: uuid.UUID, connection_id: uuid.UUID, accounts: int
) -> None:
    """Show a queued scan as running at once (inside the request's tenant transaction); the
    worker takes this row over when it starts. Nothing to add while one is running."""
    running = (
        await session.execute(
            text(
                "SELECT 1 FROM sync_run WHERE connection_id = :c AND job_type = 'inventory.scan' "
                "AND status = 'running' AND started_at > :fresh"
            ),
            {"c": connection_id, "fresh": datetime.now(UTC) - STALE_AFTER},
        )
    ).first()
    if running is None:
        await session.execute(
            text(
                "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, "
                "started_at) VALUES (:id, :t, :c, "
                "'inventory.scan', CAST(:scope AS jsonb), 'running', now())"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "scope": json.dumps({"queued": True, "accounts": accounts}),
            },
        )


async def _start_run(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, accounts: int, started: datetime
) -> uuid.UUID:
    """Record the scan as running: take over the row Scan now queued, else add one. Any other
    scan of the connection still marked running did not finish (the worker restarted)."""
    scope_json = json.dumps({"accounts": accounts})
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        run_id = (
            await session.execute(
                text(
                    "UPDATE sync_run SET started_at = :started, scope = CAST(:scope AS jsonb) "
                    "WHERE id = (SELECT id FROM sync_run WHERE connection_id = :c AND "
                    "job_type = 'inventory.scan' AND status = 'running' AND "
                    "scope->>'queued' = 'true' ORDER BY started_at DESC LIMIT 1) RETURNING id"
                ),
                {"c": connection_id, "started": started, "scope": scope_json},
            )
        ).scalar_one_or_none()
        if run_id is None:
            run_id = uuid7()
            await session.execute(
                text(
                    "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, "
                    "status, started_at) VALUES (:id, :t, :c, 'inventory.scan', "
                    "CAST(:scope AS jsonb), 'running', :started)"
                ),
                {
                    "id": run_id,
                    "t": tenant_id,
                    "c": connection_id,
                    "scope": scope_json,
                    "started": started,
                },
            )
        await session.execute(
            text(
                "UPDATE sync_run SET status = 'failed', finished_at = now(), records = 0, "
                "error_code = 'scan_failed', error_detail = 'interrupted' WHERE "
                "connection_id = :c AND job_type = 'inventory.scan' AND status = 'running' "
                "AND id <> :id"
            ),
            {"c": connection_id, "id": run_id},
        )
    return uuid.UUID(str(run_id))


async def _report_progress(tenant_id: uuid.UUID, run_id: uuid.UUID, progress: ScanProgress) -> None:
    """Write the running scan's progress every few seconds until it is cancelled."""
    while True:
        await asyncio.sleep(PROGRESS_SECONDS)
        try:
            async with get_sessionmaker()() as session, session.begin():
                await scope(session, tenant_id=tenant_id)
                await session.execute(
                    text(
                        "UPDATE sync_run SET scope = jsonb_set(coalesce(scope, '{}'), "
                        "'{progress}', CAST(:p AS jsonb)) WHERE id = :id AND status = 'running'"
                    ),
                    {"id": run_id, "p": json.dumps(progress.snapshot())},
                )
        except Exception:  # progress is a nicety; the scan carries on without it
            log.warning("could not record scan progress", exc_info=True)


async def _record_failure(
    tenant_id: uuid.UUID,
    connection_id: uuid.UUID,
    run_id: uuid.UUID,
    started: datetime,
    exc: Exception,
) -> None:
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await session.execute(
            text(
                "UPDATE sync_run SET status = 'failed', finished_at = now(), records = 0, "
                "error_code = 'scan_failed', error_detail = :detail WHERE id = :id"
            ),
            {"id": run_id, "detail": _code(exc)[:2000]},
        )


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
class LastScan:
    connection_id: uuid.UUID
    # running (still scanning, or queued), succeeded, partial or failed.
    status: str
    started_at: datetime
    finished_at: datetime | None
    resources: int
    accounts: int
    accounts_with_access: int
    regions: int
    # "<account> <region>: <AWS error code>" for each part that could not be read.
    errors: list[str]
    # While running: {"regionsDone", "regionsTotal"}; None until the worker has started.
    progress: dict[str, int] | None = None


async def last_scans(session: AsyncSession) -> list[LastScan]:
    """The latest inventory scan of each connection, with what could not be read, or how
    far a running one has got."""
    rows = await session.execute(
        text(
            "SELECT DISTINCT ON (connection_id) connection_id, status, started_at, finished_at, "
            "records, scope, error_detail FROM sync_run WHERE job_type = 'inventory.scan' "
            "ORDER BY connection_id, started_at DESC"
        )
    )
    stale = datetime.now(UTC) - STALE_AFTER
    scans = []
    for r in rows:
        scope_ = r.scope or {}
        status, errors = r.status, [e for e in (r.error_detail or "").split("; ") if e]
        running = status == "running" and r.started_at > stale
        if status == "running" and not running:
            status, errors = "failed", ["interrupted"]
        scans.append(
            LastScan(
                connection_id=r.connection_id,
                status=status,
                started_at=r.started_at,
                finished_at=r.finished_at,
                resources=r.records or 0,
                accounts=scope_.get("accounts", 0),
                accounts_with_access=scope_.get("with_access", 0),
                regions=scope_.get("regions", 0),
                errors=errors,
                progress=scope_.get("progress") if running else None,
            )
        )
    return scans


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
