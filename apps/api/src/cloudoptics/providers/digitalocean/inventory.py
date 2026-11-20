"""The DigitalOcean resource scan (ADR-021): Droplets, volumes, snapshots, reserved IPs, load
balancers and managed databases, read with the customer's token, into ``resource``.

Each kind needs its own read scope; a kind the token cannot read is skipped (and its stored
rows are left alone), the others still load. Droplet CPU comes from the monitoring API
(``monitoring:read``): the scan stores each running Droplet's average and peak CPU over the
last ``CPU_DAYS`` days in its ``config``, which the idle-Droplet rule reads. Without the scope
the rule has nothing to go on and stays quiet.
"""

from __future__ import annotations

import itertools
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from cloudoptics.connectors.service import get_connection
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.providers.digitalocean.client import Client, DigitalOceanError
from cloudoptics.providers.digitalocean.connect import usable_token

USABLE = ("healthy", "degraded")
CPU_DAYS = 14
CPU_STEP_SECONDS = 3600
#: Droplets whose CPU is read per scan (each is one request; the API allows 250 a minute).
MAX_CPU_DROPLETS = 100

RESOURCE_TYPES = frozenset(
    {"do_droplet", "do_volume", "do_snapshot", "do_reserved_ip", "do_load_balancer", "do_database"}
)


@dataclass(frozen=True)
class Record:
    resource_type: str
    resource_id: str
    region: str
    name: str | None
    state: str | None
    config: dict[str, Any]
    tags: dict[str, str] = field(default_factory=dict)


@dataclass
class Scan:
    records: list[Record] = field(default_factory=list)
    #: Resource types read in full this time (their missing rows are marked deleted).
    complete: set[str] = field(default_factory=set)
    #: Kinds the token may not read (``droplet:read``...).
    no_access: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cpu: str = "not_read"


def tags_of(values: Any) -> dict[str, str]:
    """DigitalOcean tags are plain strings; ``env:prod`` becomes ``{"env": "prod"}``."""
    out: dict[str, str] = {}
    for tag in values or []:
        key, _, value = str(tag).partition(":")
        out[key] = value
    return out


def _region(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("slug") or "global")
    return str(value or "global")


def _droplets(client: Client) -> list[Record]:
    out = []
    for d in client.paged("/v2/droplets", "droplets"):
        size = d.get("size") or {}
        out.append(
            Record(
                "do_droplet",
                str(d["id"]),
                _region(d.get("region")),
                d.get("name"),
                d.get("status"),
                {
                    "sizeSlug": d.get("size_slug") or size.get("slug"),
                    "vcpus": d.get("vcpus"),
                    "memoryMiB": d.get("memory"),
                    "diskGiB": d.get("disk"),
                    "priceMonthly": size.get("price_monthly"),
                    "createdAt": d.get("created_at"),
                    "volumeIds": d.get("volume_ids") or [],
                },
                tags_of(d.get("tags")),
            )
        )
    return out


def _volumes(client: Client) -> list[Record]:
    return [
        Record(
            "do_volume",
            str(v["id"]),
            _region(v.get("region")),
            v.get("name"),
            "attached" if v.get("droplet_ids") else "available",
            {
                "sizeGiB": v.get("size_gigabytes"),
                "dropletIds": v.get("droplet_ids") or [],
                "createdAt": v.get("created_at"),
                "filesystemType": v.get("filesystem_type"),
            },
            tags_of(v.get("tags")),
        )
        for v in client.paged("/v2/volumes", "volumes")
    ]


def _snapshots(client: Client) -> list[Record]:
    return [
        Record(
            "do_snapshot",
            str(s["id"]),
            _region((s.get("regions") or [None])[0]),
            s.get("name"),
            "available",
            {
                "sizeGiB": s.get("size_gigabytes"),
                "minDiskSizeGiB": s.get("min_disk_size"),
                "resourceType": s.get("resource_type"),
                "resourceId": s.get("resource_id"),
                "createdAt": s.get("created_at"),
                "regions": s.get("regions") or [],
            },
            tags_of(s.get("tags")),
        )
        for s in client.paged("/v2/snapshots", "snapshots")
    ]


def _reserved_ips(client: Client) -> list[Record]:
    return [
        Record(
            "do_reserved_ip",
            str(ip["ip"]),
            _region(ip.get("region")),
            None,
            "assigned" if ip.get("droplet") else "unassigned",
            {
                "dropletId": (ip.get("droplet") or {}).get("id"),
                "projectId": ip.get("project_id"),
                "locked": bool(ip.get("locked")),
            },
        )
        for ip in client.paged("/v2/reserved_ips", "reserved_ips")
    ]


def _load_balancers(client: Client) -> list[Record]:
    return [
        Record(
            "do_load_balancer",
            str(lb["id"]),
            _region(lb.get("region")),
            lb.get("name"),
            lb.get("status"),
            {
                "sizeUnit": lb.get("size_unit") or 1,
                "size": lb.get("size"),
                "dropletIds": lb.get("droplet_ids") or [],
                "dropletTag": lb.get("tag") or None,
                "createdAt": lb.get("created_at"),
                "forwardingRules": len(lb.get("forwarding_rules") or []),
            },
        )
        for lb in client.paged("/v2/load_balancers", "load_balancers")
    ]


def _databases(client: Client) -> list[Record]:
    return [
        Record(
            "do_database",
            str(db["id"]),
            _region(db.get("region")),
            db.get("name"),
            db.get("status"),
            {
                "engine": db.get("engine"),
                "version": db.get("version"),
                "numNodes": db.get("num_nodes"),
                "size": db.get("size"),
                "createdAt": db.get("created_at"),
            },
            tags_of(db.get("tags")),
        )
        for db in client.paged("/v2/databases", "databases")
    ]


KINDS = (
    ("do_droplet", "droplet:read", _droplets),
    ("do_volume", "block_storage:read", _volumes),
    ("do_snapshot", "snapshot:read", _snapshots),
    ("do_reserved_ip", "reserved_ip:read", _reserved_ips),
    ("do_load_balancer", "load_balancer:read", _load_balancers),
    ("do_database", "database:read", _databases),
)


def cpu_usage(body: dict[str, Any]) -> tuple[float, float, float] | None:
    """(average %, peak %, days covered) from the per-mode CPU second counters, or None."""
    series = ((body.get("data") or {}).get("result")) or []
    totals: dict[float, float] = {}
    idle: dict[float, float] = {}
    for s in series:
        mode = (s.get("metric") or {}).get("mode")
        for ts, value in s.get("values") or []:
            try:
                t, v = float(ts), float(value)
            except (TypeError, ValueError):
                continue
            totals[t] = totals.get(t, 0.0) + v
            if mode == "idle":
                idle[t] = idle.get(t, 0.0) + v
    times = sorted(t for t in totals if t in idle)
    utils: list[float] = []
    for a, b in itertools.pairwise(times):
        total = totals[b] - totals[a]
        if total > 0:
            utils.append(max(0.0, min(100.0, (1 - (idle[b] - idle[a]) / total) * 100)))
    if not utils:
        return None
    first, last = times[0], times[-1]
    span = (last - first) / 86400
    return round(sum(utils) / len(utils), 3), round(max(utils), 3), round(span, 2)


def _with_cpu(client: Client, records: list[Record], now: datetime, scan: Scan) -> list[Record]:
    end = int(now.timestamp())
    start = int((now - timedelta(days=CPU_DAYS)).timestamp())
    out: list[Record] = []
    read = 0
    for r in records:
        if r.resource_type != "do_droplet" or r.state != "active" or read >= MAX_CPU_DROPLETS:
            out.append(r)
            continue
        if scan.cpu == "no_access":
            out.append(r)
            continue
        try:
            body = client.get(
                "/v2/monitoring/metrics/droplet/cpu",
                {"host_id": r.resource_id, "start": start, "end": end},
            )
        except DigitalOceanError as exc:
            if exc.forbidden:
                scan.cpu = "no_access"
            else:
                scan.errors.append(f"cpu {r.resource_id}: {exc.code}")
            out.append(r)
            continue
        read += 1
        scan.cpu = "read"
        usage = cpu_usage(body or {})
        if usage is None:
            out.append(r)
            continue
        avg, peak, days = usage
        out.append(
            Record(
                r.resource_type,
                r.resource_id,
                r.region,
                r.name,
                r.state,
                {**r.config, "cpuAvgPct": avg, "cpuMaxPct": peak, "cpuDays": days},
                r.tags,
            )
        )
    return out


def scan_team(token: str, now: datetime) -> Scan:
    scan = Scan()
    with Client(token) as client:
        for resource_type, scope_name, read in KINDS:
            try:
                records = read(client)
            except DigitalOceanError as exc:
                if exc.forbidden:
                    scan.no_access.append(scope_name)
                else:
                    scan.errors.append(f"{resource_type}: {exc.code}")
                continue
            except (KeyError, TypeError):
                scan.errors.append(f"{resource_type}: unexpected_answer")
                continue
            scan.complete.add(resource_type)
            scan.records.extend(records)
        scan.records = _with_cpu(client, scan.records, now, scan)
    return scan


UPSERT_RESOURCE = text(
    "INSERT INTO resource (id, tenant_id, connection_id, provider, provider_account_id, "
    "region_id, resource_type, resource_id, name, state, config, tags) VALUES (:id, :t, :c, "
    "'digitalocean', :a, :r, :type, :rid, :name, :state, CAST(:config AS jsonb), "
    "CAST(:tags AS jsonb)) ON CONFLICT (tenant_id, provider, provider_account_id, region_id, "
    "resource_id) DO UPDATE SET resource_type = EXCLUDED.resource_type, name = EXCLUDED.name, "
    "state = EXCLUDED.state, config = EXCLUDED.config, tags = EXCLUDED.tags, "
    "last_seen_at = now(), state_since = CASE WHEN resource.state IS DISTINCT FROM "
    "EXCLUDED.state OR resource.deleted_at IS NOT NULL THEN now() ELSE resource.state_since END, "
    "deleted_at = NULL, connection_id = EXCLUDED.connection_id"
)
MARK_GONE = text(
    "UPDATE resource SET deleted_at = now() WHERE tenant_id = :t AND provider = 'digitalocean' "
    "AND provider_account_id = :a AND resource_type = ANY(:types) AND deleted_at IS NULL "
    "AND last_seen_at < :started"
)
SET_ACCESS = text(
    "UPDATE cloud_account SET resource_access = :access WHERE tenant_id = :t "
    "AND provider = 'digitalocean' AND provider_account_id = :a"
)
RECORD_SCAN = text(
    "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, started_at, "
    "finished_at, records, error_code, error_detail) VALUES (:id, :t, :c, 'inventory.scan', "
    "CAST(:scope AS jsonb), :status, :started, now(), :records, :code, :detail)"
)


async def scan_connection(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, now: datetime | None = None
) -> str:
    """Scan the team and store what was found (the ``inventory`` capability)."""
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        token = await usable_token(session, tenant_id, conn)
    if conn.status not in USABLE or token is None:
        return "skipped"
    started = datetime.now(UTC)
    try:
        scan = await run_in_threadpool(scan_team, token, now or started)
    except DigitalOceanError as exc:
        scan = Scan(errors=[exc.code])
    del token
    team = conn.aws_account_id
    status = "partial" if scan.errors else "succeeded"
    if not scan.complete and not scan.errors:
        status = "skipped"
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        if scan.records:
            await session.execute(
                UPSERT_RESOURCE,
                [
                    {
                        "id": uuid7(),
                        "t": tenant_id,
                        "c": connection_id,
                        "a": team,
                        "r": r.region,
                        "type": r.resource_type,
                        "rid": r.resource_id,
                        "name": r.name,
                        "state": r.state,
                        "config": json.dumps(r.config, default=str),
                        "tags": json.dumps(r.tags),
                    }
                    for r in scan.records
                ],
            )
        if scan.complete:
            await session.execute(
                MARK_GONE,
                {"t": tenant_id, "a": team, "types": sorted(scan.complete), "started": started},
            )
        await session.execute(
            SET_ACCESS, {"access": bool(scan.complete), "t": tenant_id, "a": team}
        )
        await session.execute(
            RECORD_SCAN,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "scope": json.dumps(
                    {
                        "accounts": 1,
                        "with_access": 1 if scan.complete else 0,
                        "regions": len({r.region for r in scan.records}),
                        "noAccess": scan.no_access,
                        "cpu": scan.cpu,
                    }
                ),
                "status": "succeeded" if status == "skipped" else status,
                "started": started,
                "records": len(scan.records),
                "code": "scan_errors" if scan.errors else None,
                "detail": "; ".join(scan.errors)[:2000] or None,
            },
        )
    return status
