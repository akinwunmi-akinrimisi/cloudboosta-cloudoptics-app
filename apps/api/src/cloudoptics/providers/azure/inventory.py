"""Azure inventory (Resource Graph), Advisor cost recommendations and Monitor metrics for the
starter rules (ADR-020).

- **Scan**: one Resource Graph query per connection over its verified subscriptions, for the
  types the rules read: virtual machines (with power state), managed disks, public IP
  addresses and snapshots. Each becomes a ``resource`` row (``provider = 'azure'``, the
  subscription as account, the location as region, the ARM ID, lower-cased, as resource ID).
  The last 30 days of billed cost per resource (``cost_resource_daily``) is kept in its config
  (``monthlyCostUsd``), so the rules price what the customer actually pays.
- **Advisor** (native recommendations): the Cost category per subscription; each one is
  attached to its resource's config (``advisor``) and read by AZ5.
- **Metrics**: ``Percentage CPU`` (hourly average and maximum, 14 days) for running VMs, into
  ``resource_metric`` as ``cpu`` / ``cpu_max``, the keys the AWS rules use.

Reader on the subscription covers all three. Azure does not charge for these calls.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text

from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.providers.azure import connection as azconn
from cloudoptics.providers.azure.client import API, AZURE_ERRORS, AzureApi

ADVISOR_API = API["advisor"]
METRICS_API = API["metrics"]
METRIC_DAYS = 14
MAX_METRIC_VMS = 200

TYPES = {
    "microsoft.compute/virtualmachines": "azure_vm",
    "microsoft.compute/disks": "azure_disk",
    "microsoft.network/publicipaddresses": "azure_public_ip",
    "microsoft.compute/snapshots": "azure_snapshot",
}
RESOURCE_TYPES = frozenset(TYPES.values())

QUERY = (
    "Resources | where type in~ ('microsoft.compute/virtualmachines', "
    "'microsoft.compute/disks', 'microsoft.network/publicipaddresses', "
    "'microsoft.compute/snapshots') "
    "| project id, name, type, location, subscriptionId, resourceGroup, tags, sku, "
    "managedBy, properties"
)

COSTS = text(
    "SELECT lower(resource_id) AS rid, sum(effective_cost_usd) AS cost FROM cost_resource_daily "
    "WHERE connection_id = :c AND usage_date > :since GROUP BY lower(resource_id)"
)
UPSERT_RESOURCE = text(
    "INSERT INTO resource (id, tenant_id, connection_id, provider, provider_account_id, "
    "region_id, resource_type, resource_id, arn, name, state, config, tags) VALUES (:id, :t, "
    ":c, 'azure', :a, :r, :type, :rid, NULL, :name, :state, CAST(:config AS jsonb), "
    "CAST(:tags AS jsonb)) ON CONFLICT (tenant_id, provider, provider_account_id, region_id, "
    "resource_id) DO UPDATE SET resource_type = EXCLUDED.resource_type, name = EXCLUDED.name, "
    "state = EXCLUDED.state, config = EXCLUDED.config, tags = EXCLUDED.tags, "
    "last_seen_at = now(), state_since = CASE WHEN resource.state IS DISTINCT FROM "
    "EXCLUDED.state OR resource.deleted_at IS NOT NULL THEN now() ELSE resource.state_since END, "
    "deleted_at = NULL, connection_id = EXCLUDED.connection_id"
)
MARK_GONE = text(
    "UPDATE resource SET deleted_at = now() WHERE connection_id = :c AND provider = 'azure' "
    "AND deleted_at IS NULL AND last_seen_at < :started"
)
SYNC_RUN = text(
    "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, started_at, "
    "finished_at, records, error_code) VALUES (:id, :t, :c, :job, CAST(:scope AS jsonb), "
    ":status, :started, now(), :records, :code)"
)


def scan_subscriptions(s: azconn.AzureSettings, billed: list[str]) -> list[str]:
    """Subscriptions to scan: the connected one, or a billing account's listed ones that its
    bill names (so a workspace scans only subscriptions its own bill proves)."""
    if s.scope_type == "subscription":
        return [s.account_id.lower()]
    known = {b.lower() for b in billed}
    return [sub.lower() for sub in s.subscription_ids if sub.lower() in known]


def state_of(kind: str, props: dict[str, Any], row: dict[str, Any]) -> str | None:
    if kind == "azure_vm":
        code = ((props.get("extended") or {}).get("instanceView") or {}).get("powerState") or {}
        return str(code.get("code") or "").removeprefix("PowerState/") or None
    if kind == "azure_disk":
        return str(props.get("diskState") or "") or None
    if kind == "azure_public_ip":
        used = props.get("ipConfiguration") or props.get("natGateway")
        return "associated" if used else "unassociated"
    return str(props.get("provisioningState") or "") or None


def to_record(row: dict[str, Any], costs: dict[str, float]) -> dict[str, Any] | None:
    kind = TYPES.get(str(row.get("type") or "").lower())
    if kind is None:
        return None
    rid = str(row.get("id") or "").lower()
    props = row.get("properties") or {}
    sku = row.get("sku") or {}
    config: dict[str, Any] = {
        "armId": row.get("id"),
        "resourceGroup": row.get("resourceGroup"),
        "sku": sku.get("name") if isinstance(sku, dict) else sku,
    }
    if kind == "azure_vm":
        config["vmSize"] = (props.get("hardwareProfile") or {}).get("vmSize")
        config["timeCreated"] = props.get("timeCreated")
    elif kind == "azure_disk":
        config.update(
            sizeGiB=props.get("diskSizeGB"),
            timeCreated=props.get("timeCreated"),
            managedBy=row.get("managedBy"),
        )
    elif kind == "azure_public_ip":
        config.update(
            ipAddress=props.get("ipAddress"),
            allocation=props.get("publicIPAllocationMethod"),
        )
    elif kind == "azure_snapshot":
        config.update(
            sizeGiB=props.get("diskSizeGB"),
            timeCreated=props.get("timeCreated"),
            sourceResourceId=str(
                (props.get("creationData") or {}).get("sourceResourceId") or ""
            ).lower()
            or None,
            incremental=props.get("incremental"),
        )
    if rid in costs:
        config["monthlyCostUsd"] = round(costs[rid], 6)
    tags = row.get("tags") or {}
    return {
        "account": str(row.get("subscriptionId") or "").lower(),
        "region": str(row.get("location") or "global").lower(),
        "type": kind,
        "rid": rid,
        "name": row.get("name"),
        "state": state_of(kind, props, row),
        "config": config,
        "tags": {str(k): str(v) for k, v in tags.items()} if isinstance(tags, dict) else {},
    }


def advisor_by_resource(recs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in recs:
        props = rec.get("properties") or {}
        meta = props.get("resourceMetadata") or {}
        rid = str(meta.get("resourceId") or "").lower()
        if not rid:
            continue
        ext = props.get("extendedProperties") or {}
        short = props.get("shortDescription") or {}
        monthly = None
        try:
            if ext.get("savingsAmount") is not None:
                monthly = float(ext["savingsAmount"])
            elif ext.get("annualSavingsAmount") is not None:
                monthly = float(ext["annualSavingsAmount"]) / 12
        except (TypeError, ValueError):
            monthly = None
        out[rid].append(
            {
                "id": rec.get("name"),
                "typeId": props.get("recommendationTypeId"),
                "problem": short.get("problem"),
                "solution": short.get("solution"),
                "impact": props.get("impact"),
                "savingsMonthly": monthly,
                "currency": ext.get("savingsCurrency"),
                "targetSku": ext.get("targetSku") or ext.get("recommendedSkuName"),
                "currentSku": ext.get("currentSku"),
            }
        )
    return dict(out)


async def _settings(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> tuple[Any, list[str]]:
    async with get_sessionmaker()() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        row = await azconn.load(session, connection_id)
        if row is None:
            return None, []
        billed = [
            r[0]
            for r in await session.execute(
                text(
                    "SELECT DISTINCT provider_account_id FROM cost_daily WHERE connection_id = :c"
                ),
                {"c": connection_id},
            )
        ]
        return row, billed


async def scan_connection(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    row, billed = await _settings(tenant_id, connection_id)
    if row is None or row.status not in ("healthy", "degraded") or row.account_verified_at is None:
        return "skipped"
    s = azconn.from_row(row)
    subs = scan_subscriptions(s, billed)
    if not subs:
        return "skipped"
    api = AzureApi(s.directory_id)
    started = datetime.now(UTC)
    status, error = "succeeded", None
    rows: list[dict[str, Any]] = []
    advisor: dict[str, list[dict[str, Any]]] = {}
    try:
        rows = await api.resource_graph(subs, QUERY)
    except AZURE_ERRORS as exc:
        status, error = "failed", getattr(exc, "code", type(exc).__name__)
    if status == "succeeded":
        try:
            recs: list[dict[str, Any]] = []
            for sub in subs:
                recs += await api.arm_list(
                    f"/subscriptions/{sub}/providers/Microsoft.Advisor/recommendations",
                    api_version=ADVISOR_API,
                    params={"$filter": "Category eq 'Cost'"},
                )
            advisor = advisor_by_resource(recs)
        except AZURE_ERRORS as exc:
            status, error = "partial", f"advisor:{getattr(exc, 'code', type(exc).__name__)}"
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        count = 0
        if status != "failed":
            since = started.date() - timedelta(days=30)
            costs = {
                r.rid: float(r.cost)
                for r in await session.execute(COSTS, {"c": connection_id, "since": since})
            }
            records = [r for r in (to_record(x, costs) for x in rows) if r and r["account"] in subs]
            for rec in records:
                if rec["rid"] in advisor:
                    rec["config"]["advisor"] = advisor[rec["rid"]]
            if records:
                await session.execute(
                    UPSERT_RESOURCE,
                    [
                        {
                            "id": uuid7(),
                            "t": tenant_id,
                            "c": connection_id,
                            "a": rec["account"],
                            "r": rec["region"],
                            "type": rec["type"],
                            "rid": rec["rid"],
                            "name": rec["name"],
                            "state": rec["state"],
                            "config": json.dumps(rec["config"], default=str),
                            "tags": json.dumps(rec["tags"]),
                        }
                        for rec in records
                    ],
                )
            await session.execute(MARK_GONE, {"c": connection_id, "started": started})
            count = len(records)
        await session.execute(
            SYNC_RUN,
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "job": "inventory.scan",
                "scope": json.dumps(
                    {"subscriptions": subs, "advisor": sum(map(len, advisor.values()))}
                ),
                "status": status,
                "started": started,
                "records": count,
                "code": error,
            },
        )
    return status


async def fetch_advisor(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    """Advisor is read with each scan (its recommendations live on the resources)."""
    return await scan_connection(tenant_id, connection_id)


RUNNING_VMS = text(
    "SELECT id, resource_id, config->>'armId' AS arm_id FROM resource WHERE connection_id = :c "
    "AND provider = 'azure' AND resource_type = 'azure_vm' AND state = 'running' "
    "AND deleted_at IS NULL ORDER BY resource_id LIMIT :n"
)
UPSERT_METRIC = text(
    "INSERT INTO resource_metric (tenant_id, resource_id, metric, day, hourly) VALUES (:t, :r, "
    ":m, :d, :h) ON CONFLICT (resource_id, metric, day) DO UPDATE SET hourly = EXCLUDED.hourly, "
    "fetched_at = now()"
)


def hourly_values(series: list[dict[str, Any]], field: str) -> dict[date, list[float | None]]:
    days: dict[date, list[float | None]] = {}
    for point in series:
        stamp = str(point.get("timeStamp") or "")
        value = point.get(field)
        try:
            at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        at = at.astimezone(UTC)
        hours = days.setdefault(at.date(), [None] * 24)
        hours[at.hour] = None if value is None else float(value)
    return days


async def pull_connection(tenant_id: uuid.UUID, connection_id: uuid.UUID) -> str:
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        row = await azconn.load(session, connection_id)
        if (
            row is None
            or row.status not in ("healthy", "degraded")
            or row.account_verified_at is None
        ):
            return "skipped"
        vms = (await session.execute(RUNNING_VMS, {"c": connection_id, "n": MAX_METRIC_VMS})).all()
    if not vms:
        return "skipped"
    api = AzureApi(str(row.directory_id))
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=METRIC_DAYS)
    values: list[dict[str, Any]] = []
    failed = 0
    for vm in vms:
        arm_id = vm.arm_id or vm.resource_id
        try:
            answer = await api.arm(
                "GET",
                f"{arm_id}/providers/Microsoft.Insights/metrics",
                api_version=METRICS_API,
                params={
                    "metricnames": "Percentage CPU",
                    "timespan": f"{start:%Y-%m-%dT%H:%M:%SZ}/{end:%Y-%m-%dT%H:%M:%SZ}",
                    "interval": "PT1H",
                    "aggregation": "average,maximum",
                },
            )
        except AZURE_ERRORS:
            failed += 1
            continue
        series: list[dict[str, Any]] = []
        for metric in answer.get("value") or []:
            # Each metric reports its own result ("Success" or an error code).
            if str(metric.get("errorCode") or "Success") != "Success":
                continue
            for ts in metric.get("timeseries") or []:
                series += ts.get("data") or []
        for key, field in (("cpu", "average"), ("cpu_max", "maximum")):
            for day, hourly in hourly_values(series, field).items():
                values.append({"t": tenant_id, "r": vm.id, "m": key, "d": day, "h": hourly})
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        if values:
            await session.execute(UPSERT_METRIC, values)
    if failed == len(vms):
        return "failed"
    return "partial" if failed else "succeeded"
