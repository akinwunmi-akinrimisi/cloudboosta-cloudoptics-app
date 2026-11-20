"""Google Cloud resources, usage and Recommender (ADR-019).

The inventory reads the Compute Engine and Cloud SQL Admin APIs project by project:
instances, persistent disks, static addresses, snapshots and Cloud SQL instances. Projects
are the connection's accounts, which come from its bill (``billing.discover``), at most
``CLOUDOPTICS_GCP_MAX_PROJECTS``. Cloud Asset Inventory would list everything in one call
but needs an organization-level grant; per-project reads need only Compute Viewer and Cloud
SQL Viewer where the customer wants recommendations.

Recommender's own findings (idle VMs, machine types, idle disks and addresses, idle and
over-provisioned Cloud SQL) are fetched for the zones and regions that hold resources and
kept on each resource (``config.recommendations``), where the rules read them. Usage comes
from Cloud Monitoring: hourly mean CPU of running instances and Cloud SQL instances.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from cloudoptics.connectors.service import get_connection
from cloudoptics.fx.service import quote
from cloudoptics.metrics.puller import KEEP_DAYS, daily_rows
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.providers.gcp.client import GcpApi, GcpError
from cloudoptics.providers.gcp.connect import load_settings
from cloudoptics.providers.gcp.settings import get_gcp_settings

USABLE = ("healthy", "degraded")
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
RESOURCE_TYPES = frozenset(
    {"gce_instance", "gce_disk", "gce_address", "gce_snapshot", "cloudsql_instance"}
)
#: recommender -> (location kind, resource type it is about)
RECOMMENDERS: dict[str, tuple[str, str]] = {
    "google.compute.instance.IdleResourceRecommender": ("zone", "gce_instance"),
    "google.compute.instance.MachineTypeRecommender": ("zone", "gce_instance"),
    "google.compute.disk.IdleResourceRecommender": ("zone", "gce_disk"),
    "google.compute.address.IdleResourceRecommender": ("region", "gce_address"),
    "google.cloudsql.instance.IdleRecommender": ("region", "cloudsql_instance"),
    "google.cloudsql.instance.OverprovisionedRecommender": ("region", "cloudsql_instance"),
}
METRIC_DAYS = 15
CPU_METRICS = {
    "gce_instance": "compute.googleapis.com/instance/cpu/utilization",
    "cloudsql_instance": "cloudsql.googleapis.com/database/cpu/utilization",
}
DAYS_PER_MONTH = Decimal("30.4167")

PROJECTS_SQL = text(
    "SELECT provider_account_id FROM cloud_account WHERE connection_id = :c AND provider = 'gcp' "
    "AND account_type = 'member' AND coalesce(org_status, 'ACTIVE') = 'ACTIVE' "
    "ORDER BY provider_account_id LIMIT :n"
)
UPSERT_RESOURCE = text(
    "INSERT INTO resource (id, tenant_id, connection_id, provider, provider_account_id, "
    "region_id, resource_type, resource_id, arn, name, state, config, tags) VALUES (:id, :t, "
    ":c, 'gcp', :a, :r, :type, :rid, :arn, :name, :state, CAST(:config AS jsonb), "
    "CAST(:tags AS jsonb)) ON CONFLICT (tenant_id, provider, provider_account_id, region_id, "
    "resource_id) DO UPDATE SET resource_type = EXCLUDED.resource_type, arn = EXCLUDED.arn, "
    "name = EXCLUDED.name, state = EXCLUDED.state, config = EXCLUDED.config, "
    "tags = EXCLUDED.tags, last_seen_at = now(), state_since = CASE WHEN resource.state IS "
    "DISTINCT FROM EXCLUDED.state OR resource.deleted_at IS NOT NULL THEN now() ELSE "
    "resource.state_since END, deleted_at = NULL, connection_id = EXCLUDED.connection_id"
)
MARK_GONE = text(
    "UPDATE resource SET deleted_at = now() WHERE tenant_id = :t AND provider = 'gcp' "
    "AND provider_account_id = :a AND deleted_at IS NULL AND last_seen_at < :started"
)
SET_ACCESS = text(
    "UPDATE cloud_account SET resource_access = :access WHERE tenant_id = :t "
    "AND provider = 'gcp' AND provider_account_id = :a"
)
RECORD_RUN = text(
    "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, started_at, "
    "finished_at, records, error_code, error_detail) VALUES (:id, :t, :c, :job, "
    "CAST(:scope AS jsonb), :status, :started, now(), :records, :code, :detail)"
)
RUNNING = text(
    "SELECT id, provider_account_id, resource_type, resource_id, config FROM resource "
    "WHERE connection_id = :c AND provider = 'gcp' AND deleted_at IS NULL AND "
    "((resource_type = 'gce_instance' AND state = 'RUNNING') OR "
    "(resource_type = 'cloudsql_instance' AND state = 'RUNNABLE'))"
)
UPSERT_METRIC = text(
    "INSERT INTO resource_metric (tenant_id, resource_id, metric, day, hourly) VALUES (:t, :r, "
    ":m, :d, :h) ON CONFLICT (resource_id, metric, day) DO UPDATE SET hourly = EXCLUDED.hourly, "
    "fetched_at = now()"
)


def short(url: str | None) -> str | None:
    """The last part of a Compute URL (``.../machineTypes/e2-medium`` -> ``e2-medium``)."""
    return None if not url else str(url).rstrip("/").rsplit("/", 1)[-1]


def location_of(scope: str) -> str:
    """``zones/europe-west2-a`` -> ``europe-west2-a``; ``global`` stays."""
    return scope.split("/", 1)[1] if "/" in scope else scope


def region_of(zone: str) -> str:
    return zone.rsplit("-", 1)[0] if re.fullmatch(r".+-[a-z]", zone) else zone


@dataclass
class Record:
    """One resource. ``resource_id`` is Google's relative name (``instances/web-1``,
    ``disks/web-1``): an instance and its boot disk often share a name and a zone."""

    resource_type: str
    location: str
    resource_id: str
    name: str | None
    state: str | None
    config: dict[str, Any]
    tags: dict[str, str]
    arn: str | None


@dataclass
class ProjectScan:
    project: str
    access: bool = True
    records: list[Record] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def instance_record(scope: str, i: dict[str, Any]) -> Record:
    nics = i.get("networkInterfaces") or []
    return Record(
        "gce_instance",
        location_of(scope),
        f"instances/{i['name']}",
        str(i["name"]),
        i.get("status"),
        {
            "id": str(i.get("id", "")),
            "machineType": short(i.get("machineType")),
            "zone": location_of(scope),
            "creationTimestamp": i.get("creationTimestamp"),
            "lastStartTimestamp": i.get("lastStartTimestamp"),
            "preemptible": bool((i.get("scheduling") or {}).get("preemptible")),
            "disks": [short(d.get("source")) for d in i.get("disks") or []],
            "externalIp": any(n.get("accessConfigs") for n in nics),
        },
        dict(i.get("labels") or {}),
        i.get("selfLink"),
    )


def disk_record(scope: str, d: dict[str, Any]) -> Record:
    users = [short(u) for u in d.get("users") or []]
    return Record(
        "gce_disk",
        location_of(scope),
        f"disks/{d['name']}",
        str(d["name"]),
        "in_use" if users else ("unattached" if d.get("status") == "READY" else d.get("status")),
        {
            "id": str(d.get("id", "")),
            "type": short(d.get("type")),
            "sizeGb": int(d.get("sizeGb") or 0),
            "users": users,
            "regional": scope.startswith("regions/"),
            "creationTimestamp": d.get("creationTimestamp"),
            "lastAttachTimestamp": d.get("lastAttachTimestamp"),
            "lastDetachTimestamp": d.get("lastDetachTimestamp"),
        },
        dict(d.get("labels") or {}),
        d.get("selfLink"),
    )


def address_record(scope: str, a: dict[str, Any]) -> Record:
    return Record(
        "gce_address",
        location_of(scope),
        f"addresses/{a['name']}",
        str(a["name"]),
        a.get("status"),
        {
            "address": a.get("address"),
            "addressType": a.get("addressType", "EXTERNAL"),
            "networkTier": a.get("networkTier"),
            "users": [short(u) for u in a.get("users") or []],
            "creationTimestamp": a.get("creationTimestamp"),
        },
        dict(a.get("labels") or {}),
        a.get("selfLink"),
    )


def snapshot_record(s: dict[str, Any]) -> Record:
    return Record(
        "gce_snapshot",
        "global",
        f"snapshots/{s['name']}",
        str(s["name"]),
        s.get("status"),
        {
            "sourceDisk": short(s.get("sourceDisk")),
            "sourceDiskUrl": s.get("sourceDisk"),
            "storageBytes": int(s.get("storageBytes") or 0),
            "diskSizeGb": int(s.get("diskSizeGb") or 0),
            "autoCreated": bool(s.get("autoCreated")),
            "creationTimestamp": s.get("creationTimestamp"),
            "snapshotType": s.get("snapshotType", "STANDARD"),
        },
        dict(s.get("labels") or {}),
        s.get("selfLink"),
    )


def sql_record(i: dict[str, Any]) -> Record:
    settings = i.get("settings") or {}
    return Record(
        "cloudsql_instance",
        str(i.get("region") or "global"),
        f"sqlInstances/{i['name']}",
        str(i["name"]),
        i.get("state"),
        {
            "databaseVersion": i.get("databaseVersion"),
            "tier": settings.get("tier"),
            "activationPolicy": settings.get("activationPolicy"),
            "availabilityType": settings.get("availabilityType"),
            "dataDiskSizeGb": int(settings.get("dataDiskSizeGb") or 0),
            "createTime": i.get("createTime"),
        },
        dict(settings.get("userLabels") or {}),
        i.get("selfLink"),
    )


def money(m: dict[str, Any] | None) -> Decimal:
    if not m:
        return Decimal(0)
    return Decimal(str(m.get("units") or 0)) + Decimal(int(m.get("nanos") or 0)) / Decimal(10**9)


def _seconds(duration: str | None) -> Decimal:
    try:
        return Decimal(str(duration or "2592000s").rstrip("s"))
    except ArithmeticError:
        return Decimal(2592000)


def recommendation_summary(rec: dict[str, Any], recommender: str) -> dict[str, Any]:
    """What a rule needs from one Recommender recommendation: monthly savings in the bill's
    currency, and the machine type or tier it suggests."""
    cost = ((rec.get("primaryImpact") or {}).get("costProjection")) or {}
    seconds = _seconds(cost.get("duration"))
    amount = money(cost.get("cost"))
    monthly = -amount * DAYS_PER_MONTH * 86400 / seconds if seconds else Decimal(0)
    suggested = None
    for group in (rec.get("content") or {}).get("operationGroups") or []:
        for op in group.get("operations") or []:
            if op.get("path") in ("/machineType", "/settings/tier") and op.get("value"):
                suggested = short(str(op["value"]))
    return {
        "recommender": recommender,
        "name": rec.get("name"),
        "description": rec.get("description"),
        "subtype": rec.get("recommenderSubtype"),
        "priority": rec.get("priority"),
        "savingsMonthly": float(round(monthly, 2)),
        "currency": ((cost.get("cost") or {}).get("currencyCode")) or "USD",
        "suggested": suggested,
        "refreshed": (rec.get("lastRefreshTime")),
    }


def target_name(rec: dict[str, Any]) -> str | None:
    """The resource a recommendation is about (its overview, else its first operation)."""
    content = rec.get("content") or {}
    name = (content.get("overview") or {}).get("resourceName")
    for group in content.get("operationGroups") or []:
        for op in group.get("operations") or []:
            name = name or op.get("resource")
    return short(str(name)) if name else None


async def fetch_recommendations(api: GcpApi, project: str, records: list[Record]) -> list[str]:
    """Attach Recommender's active recommendations to the matching records; errors per
    recommender and location (a disabled API or a missing role is not fatal)."""
    by_key: dict[tuple[str, str | None], Record] = {(r.resource_type, r.name): r for r in records}
    locations: dict[str, set[str]] = {"zone": set(), "region": set()}
    for r in records:
        if r.location == "global":
            continue
        zonal = re.fullmatch(r".+-[a-z]", r.location) is not None
        locations["zone" if zonal else "region"].add(r.location)
        if zonal:
            locations["region"].add(region_of(r.location))
    errors: list[str] = []
    s = api.settings
    for recommender, (kind, rtype) in RECOMMENDERS.items():
        if not any(r.resource_type == rtype for r in records):
            continue
        for location in sorted(locations[kind]):
            url = (
                f"{s.recommender_url}/projects/{project}/locations/{location}/recommenders/"
                f"{recommender}/recommendations"
            )
            try:
                async for rec in api.pages(
                    url, "recommendations", {"filter": "stateInfo.state=ACTIVE"}
                ):
                    name = target_name(rec)
                    target = by_key.get((rtype, name)) if name else None
                    if target is not None:
                        target.config.setdefault("recommendations", []).append(
                            recommendation_summary(rec, recommender)
                        )
            except GcpError as exc:
                if not (exc.api_disabled or exc.denied):
                    errors.append(
                        f"{project} {location} {recommender.rsplit('.', 2)[-2]}: {exc.code}"
                    )
                break  # the same answer for every location of this recommender
    return errors


async def scan_project(api: GcpApi, project: str) -> ProjectScan:
    s = api.settings
    out = ProjectScan(project)
    base = f"{s.compute_url}/projects/{project}"
    try:
        out.records += [
            instance_record(sc, i)
            async for sc, i in api.aggregated(f"{base}/aggregated/instances", "instances")
        ]
        out.records += [
            disk_record(sc, d)
            async for sc, d in api.aggregated(f"{base}/aggregated/disks", "disks")
        ]
        out.records += [
            address_record(sc, a)
            async for sc, a in api.aggregated(f"{base}/aggregated/addresses", "addresses")
        ]
        out.records += [
            address_record("global", a)
            async for a in api.pages(f"{base}/global/addresses", "items")
        ]
        out.records += [
            snapshot_record(x) async for x in api.pages(f"{base}/global/snapshots", "items")
        ]
    except GcpError as exc:
        out.access = False
        out.errors.append(f"{project}: {exc.code}")
        return out
    try:
        out.records += [
            sql_record(i)
            async for i in api.pages(f"{s.sqladmin_url}/projects/{project}/instances", "items")
        ]
    except GcpError as exc:
        if not (exc.api_disabled or exc.denied):
            out.errors.append(f"{project} cloudsql: {exc.code}")
    out.errors += await fetch_recommendations(api, project, out.records)
    return out


async def _usd_rates(session: AsyncSession, scans: list[ProjectScan]) -> None:
    """Recommender's savings are in the bill's currency: add USD (today's rate)."""
    today = datetime.now(UTC).date()
    rates: dict[str, float | None] = {"USD": 1.0}
    for scan in scans:
        for rec in scan.records:
            for summary in rec.config.get("recommendations", []):
                ccy = summary["currency"]
                if ccy not in rates:
                    q = await quote(session, ccy, today)
                    rates[ccy] = q.rate if q else None
                rate = rates[ccy]
                summary["savingsUsdMonth"] = (
                    None if rate is None else round(summary["savingsMonthly"] / rate, 2)
                )


async def _projects(session: AsyncSession, connection_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        PROJECTS_SQL, {"c": connection_id, "n": get_gcp_settings().max_projects}
    )
    return [r.provider_account_id for r in rows if PROJECT_ID.fullmatch(r.provider_account_id)]


async def scan(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    """Scan every project of the connection and store what was found."""
    started = datetime.now(UTC)
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        gs = await load_settings(session, connection_id)
        projects = await _projects(session, connection_id)
    if conn.status not in USABLE or gs is None:
        return "skipped"
    api = GcpApi()
    scans = [await scan_project(api, p) for p in projects]
    errors = [e for s in scans for e in s.errors]
    records = sum(len(s.records) for s in scans)
    with_access = sum(s.access for s in scans)
    if not projects:
        status = "skipped"
    elif with_access == 0:
        status = "failed"
    else:
        status = "partial" if errors else "succeeded"
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        await _usd_rates(session, scans)
        for s in scans:
            await session.execute(SET_ACCESS, {"access": s.access, "t": tenant_id, "a": s.project})
            if not s.access:
                continue
            if s.records:
                await session.execute(
                    UPSERT_RESOURCE,
                    [
                        {
                            "id": uuid7(),
                            "t": tenant_id,
                            "c": connection_id,
                            "a": s.project,
                            "r": r.location,
                            "type": r.resource_type,
                            "rid": r.resource_id,
                            "arn": r.arn,
                            "name": r.name,
                            "state": r.state,
                            "config": json.dumps(r.config, default=str),
                            "tags": json.dumps(r.tags),
                        }
                        for r in s.records
                    ],
                )
            await session.execute(MARK_GONE, {"t": tenant_id, "a": s.project, "started": started})
        await session.execute(
            RECORD_RUN,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "job": "inventory.scan",
                "scope": json.dumps(
                    {
                        "accounts": len(projects),
                        "with_access": with_access,
                        "regions": len({r.location for s in scans for r in s.records}),
                    }
                ),
                "status": "failed"
                if status == "skipped" and projects
                else ("succeeded" if status == "skipped" else status),
                "started": started,
                "records": records,
                "code": "scan_failed" if status == "failed" else None,
                "detail": "; ".join(errors)[:2000] or None,
            },
        )
    return status


def _points(series: dict[str, Any]) -> dict[datetime, float]:
    values: dict[datetime, float] = {}
    for p in series.get("points") or []:
        end = (p.get("interval") or {}).get("endTime")
        value = (p.get("value") or {}).get("doubleValue")
        if end is None or value is None:
            continue
        ts = datetime.fromisoformat(str(end).replace("Z", "+00:00")) - timedelta(seconds=1)
        values[ts.replace(minute=0, second=0, microsecond=0)] = float(value) * 100
    return values


async def pull(tenant_id: uuid.UUID, connection_id: uuid.UUID, now: datetime | None = None) -> str:
    """Hourly mean CPU (percent) of running instances and Cloud SQL instances, the last
    ``METRIC_DAYS`` days, into ``resource_metric`` as ``cpu``."""
    now = now or datetime.now(UTC)
    started = datetime.now(UTC)
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        running = list(await session.execute(RUNNING, {"c": connection_id}))
    if conn.status not in USABLE:
        return "skipped"
    by_project: dict[str, list[Any]] = defaultdict(list)
    for r in running:
        by_project[r.provider_account_id].append(r)
    api = GcpApi()
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=METRIC_DAYS)
    params: list[dict[str, Any]] = []
    errors: list[str] = []
    for project, resources in sorted(by_project.items()):
        ids = {
            ("gce_instance", str(r.config.get("id"))): r.id
            for r in resources
            if r.resource_type == "gce_instance"
        } | {
            ("cloudsql_instance", f"{project}:{r.resource_id.rsplit('/', 1)[-1]}"): r.id
            for r in resources
            if r.resource_type == "cloudsql_instance"
        }
        for rtype, metric in CPU_METRICS.items():
            if not any(k[0] == rtype for k in ids):
                continue
            query = {
                "filter": f'metric.type="{metric}"',
                "interval.startTime": start.isoformat().replace("+00:00", "Z"),
                "interval.endTime": end.isoformat().replace("+00:00", "Z"),
                "aggregation.alignmentPeriod": "3600s",
                "aggregation.perSeriesAligner": "ALIGN_MEAN",
            }
            try:
                async for series in api.pages(
                    f"{api.settings.monitoring_url}/projects/{project}/timeSeries",
                    "timeSeries",
                    query,
                ):
                    labels = (series.get("resource") or {}).get("labels") or {}
                    key = (
                        labels.get("instance_id")
                        if rtype == "gce_instance"
                        else labels.get("database_id")
                    )
                    rid = ids.get((rtype, str(key)))
                    if rid is None:
                        continue
                    for day, hourly in daily_rows(_points(series)).items():
                        params.append({"t": tenant_id, "r": rid, "m": "cpu", "d": day, "h": hourly})
            except GcpError as exc:
                errors.append(f"{project} {rtype}: {exc.code}")
    status = "failed" if errors and not params else ("partial" if errors else "succeeded")
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        if params:
            await session.execute(UPSERT_METRIC, params)
        await session.execute(
            text("DELETE FROM resource_metric WHERE tenant_id = :t AND day < :d"),
            {"t": tenant_id, "d": now.date() - timedelta(days=KEEP_DAYS)},
        )
        await session.execute(
            RECORD_RUN,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "job": "metrics.pull",
                "scope": json.dumps({"projects": len(by_project)}),
                "status": status,
                "started": started,
                "records": len(params),
                "code": "pull_failed" if status == "failed" else None,
                "detail": "; ".join(errors)[:2000] or None,
            },
        )
    return status
