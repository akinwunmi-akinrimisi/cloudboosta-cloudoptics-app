"""CloudWatch metrics puller (WBS CC-11) with a per-tenant API budget (WBS CC-13).

Only candidates are pulled: EC2 instances that are running and RDS DB instances that are
available, in accounts with resource access. For each one, GetMetricData returns hourly
values for the last 14 days on the first pull, then only from the last stored day (which
was partial) onwards. CloudWatch keeps hourly data for 15 months, so the first pull already
has two weeks of history and nobody waits for data to build up.

GetMetricData is billed per metric requested, so each tenant has a daily budget of metric
queries (``metrics_daily_budget``). Resources with no data yet go first; the rest wait for
the next run when the budget is spent.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from cloudoptics.connectors.aws import resource_role_arn, session_name
from cloudoptics.connectors.crypto import get_encryptor
from cloudoptics.connectors.service import get_connection, get_session_factory
from cloudoptics.inventory.scanner import USABLE
from cloudoptics.platform.db import get_sessionmaker, scope
from cloudoptics.platform.ids import uuid7
from cloudoptics.platform.settings import get_settings

log = logging.getLogger(__name__)

WINDOW_DAYS = 14
KEEP_DAYS = 35
PERIOD = 3600
MAX_QUERIES = 500  # GetMetricData limit per call
BUDGET_API = "cloudwatch.GetMetricData"

# resource type -> (namespace, dimension, [(metric key, CloudWatch metric, statistic)])
METRICS: dict[str, tuple[str, str, list[tuple[str, str, str]]]] = {
    "ec2_instance": (
        "AWS/EC2",
        "InstanceId",
        [
            ("cpu", "CPUUtilization", "Average"),
            ("cpu_max", "CPUUtilization", "Maximum"),
            ("network_in", "NetworkIn", "Sum"),
            ("network_out", "NetworkOut", "Sum"),
        ],
    ),
    "rds_instance": (
        "AWS/RDS",
        "DBInstanceIdentifier",
        [
            ("cpu", "CPUUtilization", "Average"),
            ("cpu_max", "CPUUtilization", "Maximum"),
            ("connections", "DatabaseConnections", "Maximum"),
        ],
    ),
}
ACTIVE_STATE = {"ec2_instance": "running", "rds_instance": "available"}

PullStatus = Literal["skipped", "succeeded", "partial", "failed"]


@dataclass(frozen=True)
class Candidate:
    id: uuid.UUID
    account_id: str
    region: str
    resource_type: str
    resource_id: str
    since: date

    @property
    def queries(self) -> int:
        return len(METRICS[self.resource_type][2])


@dataclass
class AccountPull:
    # (resource uuid, metric key) -> {hour start: value}
    values: dict[tuple[uuid.UUID, str], dict[datetime, float]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    errors: list[str] = field(default_factory=list)


def _code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", "ClientError"))
    return type(exc).__name__


def _queries(
    batch: list[Candidate],
) -> tuple[list[dict[str, Any]], dict[str, tuple[uuid.UUID, str]]]:
    queries: list[dict[str, Any]] = []
    ids: dict[str, tuple[uuid.UUID, str]] = {}
    for c in batch:
        namespace, dimension, metrics = METRICS[c.resource_type]
        for key, name, stat in metrics:
            qid = f"m{len(queries)}"
            ids[qid] = (c.id, key)
            queries.append(
                {
                    "Id": qid,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": namespace,
                            "MetricName": name,
                            "Dimensions": [{"Name": dimension, "Value": c.resource_id}],
                        },
                        "Period": PERIOD,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                }
            )
    return queries, ids


def _batches(candidates: list[Candidate]) -> list[list[Candidate]]:
    """Group by start day (one time window per call), at most 500 queries per call."""
    by_since: dict[date, list[Candidate]] = defaultdict(list)
    for c in candidates:
        by_since[c.since].append(c)
    batches: list[list[Candidate]] = []
    for since in sorted(by_since):
        batch: list[Candidate] = []
        for c in by_since[since]:
            if sum(b.queries for b in batch) + c.queries > MAX_QUERIES:
                batches.append(batch)
                batch = []
            batch.append(c)
        if batch:
            batches.append(batch)
    return batches


def pull_region(
    cloudwatch: Any, candidates: list[Candidate], end: datetime, out: AccountPull
) -> None:
    for batch in _batches(candidates):
        queries, ids = _queries(batch)
        start = datetime.combine(batch[0].since, time(), UTC)
        token = None
        while True:
            page = cloudwatch.get_metric_data(
                MetricDataQueries=queries,
                StartTime=start,
                EndTime=end,
                ScanBy="TimestampAscending",
                **({"NextToken": token} if token else {}),
            )
            for result in page.get("MetricDataResults", []):
                key = ids.get(result.get("Id", ""))
                if key is None:
                    continue
                for ts, value in zip(
                    result.get("Timestamps", []), result.get("Values", []), strict=False
                ):
                    out.values[key][ts.astimezone(UTC)] = float(value)
            token = page.get("NextToken")
            if not token:
                break


def daily_rows(values: dict[datetime, float]) -> dict[date, list[float | None]]:
    """Hourly values per UTC day: 24 slots, None where CloudWatch had no data."""
    days: dict[date, list[float | None]] = {}
    for ts, value in values.items():
        days.setdefault(ts.date(), [None] * 24)[ts.hour] = value
    return days


async def pull_connection(
    tenant_id: uuid.UUID, connection_id: uuid.UUID, now: datetime | None = None
) -> PullStatus:
    """Pull usage metrics for the candidates of one connection and store them."""
    now = now or datetime.now(UTC)
    today = now.date()
    first_day = today - timedelta(days=WINDOW_DAYS)
    budget = get_settings().metrics_daily_budget
    maker = get_sessionmaker()
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        conn = await get_connection(session, connection_id)
        if conn.status not in USABLE:
            return "skipped"
        rows = (
            await session.execute(
                text(
                    "SELECT r.id, r.account_id, r.region_id, r.resource_type, r.resource_id, "
                    "max(m.day) AS last_day FROM resource r JOIN cloud_account a ON "
                    "a.tenant_id = r.tenant_id AND a.aws_account_id = r.account_id AND "
                    "a.resource_access LEFT JOIN resource_metric m ON m.resource_id = r.id "
                    "WHERE r.connection_id = :c AND r.deleted_at IS NULL AND "
                    "((r.resource_type = 'ec2_instance' AND r.state = 'running') OR "
                    "(r.resource_type = 'rds_instance' AND r.state = 'available')) "
                    "GROUP BY r.id ORDER BY max(m.day) NULLS FIRST, r.id"
                ),
                {"c": connection_id},
            )
        ).all()
        used = (
            await session.execute(
                text(
                    "SELECT units FROM aws_api_usage WHERE tenant_id = :t AND day = :d "
                    "AND api = :api"
                ),
                {"t": tenant_id, "d": today, "api": BUDGET_API},
            )
        ).scalar_one_or_none() or 0
        remaining = max(budget - used, 0)
        candidates: list[Candidate] = []
        over_budget = 0
        for r in rows:
            c = Candidate(
                r.id,
                r.account_id.strip(),
                r.region_id,
                r.resource_type,
                r.resource_id,
                max(first_day, r.last_day) if r.last_day else first_day,
            )
            if c.queries > remaining:
                over_budget += 1
                continue
            remaining -= c.queries
            candidates.append(c)
        planned = sum(c.queries for c in candidates)
        if planned:
            # Reserve before calling AWS, so parallel pulls cannot both spend the budget.
            await session.execute(
                text(
                    "INSERT INTO aws_api_usage (tenant_id, day, api, units) VALUES "
                    "(:t, :d, :api, :n) ON CONFLICT (tenant_id, day, api) DO UPDATE SET "
                    "units = aws_api_usage.units + EXCLUDED.units"
                ),
                {"t": tenant_id, "d": today, "api": BUDGET_API, "n": planned},
            )
    if not rows:
        return "skipped"

    external_id = get_encryptor().decrypt(bytes(conn.external_id_enc), tenant_id)
    factory = get_session_factory()
    end = now.replace(minute=0, second=0, microsecond=0)

    def _pull(account_id: str, items: list[Candidate]) -> AccountPull:
        out = AccountPull()
        try:
            aws = factory.assume(
                resource_role_arn(account_id, conn.stack_name),
                external_id,
                session_name(tenant_id, "metrics"),
            )
        except (ClientError, BotoCoreError) as exc:
            out.errors.append(f"{account_id} assume: {_code(exc)}")
            return out
        by_region: dict[str, list[Candidate]] = defaultdict(list)
        for c in items:
            by_region[c.region].append(c)
        for region, region_items in sorted(by_region.items()):
            try:
                pull_region(aws.client("cloudwatch", region), region_items, end, out)
            except (ClientError, BotoCoreError, KeyError) as exc:
                out.errors.append(f"{account_id} {region}: {_code(exc)}")
        return out

    by_account: dict[str, list[Candidate]] = defaultdict(list)
    for c in candidates:
        by_account[c.account_id].append(c)
    started = datetime.now(UTC)
    pulls = [await run_in_threadpool(_pull, a, items) for a, items in sorted(by_account.items())]

    errors = [e for p in pulls for e in p.errors]
    if over_budget:
        errors.append(f"budget: {over_budget} resources wait for tomorrow's budget")
    stored = 0
    async with maker() as session, session.begin():
        await scope(session, tenant_id=tenant_id)
        params = [
            {"t": tenant_id, "r": rid, "m": metric, "d": day, "h": hourly}
            for p in pulls
            for (rid, metric), values in p.values.items()
            for day, hourly in daily_rows(values).items()
        ]
        if params:
            await session.execute(
                text(
                    "INSERT INTO resource_metric (tenant_id, resource_id, metric, day, hourly) "
                    "VALUES (:t, :r, :m, :d, :h) ON CONFLICT (resource_id, metric, day) DO "
                    "UPDATE SET hourly = EXCLUDED.hourly, fetched_at = now()"
                ),
                params,
            )
        stored = len(params)
        await session.execute(
            text("DELETE FROM resource_metric WHERE tenant_id = :t AND day < :d"),
            {"t": tenant_id, "d": today - timedelta(days=KEEP_DAYS)},
        )
        # Failed: every account errored and nothing came back.
        failed = bool(pulls) and not params and all(p.errors for p in pulls)
        status: PullStatus = "failed" if failed else ("partial" if errors else "succeeded")
        await session.execute(
            text(
                "INSERT INTO sync_run (id, tenant_id, connection_id, job_type, scope, status, "
                "started_at, finished_at, records, error_code, error_detail) VALUES (:id, :t, "
                ":c, 'metrics.pull', CAST(:scope AS jsonb), :status, :started, now(), "
                ":records, :code, :detail)"
            ),
            {
                "id": uuid7(),
                "t": tenant_id,
                "c": connection_id,
                "scope": json.dumps(
                    {
                        "resources": len(candidates),
                        "queries": planned,
                        "over_budget": over_budget,
                    }
                ),
                "status": status,
                "started": started,
                "records": stored,
                "code": "pull_errors" if errors else None,
                "detail": "; ".join(errors)[:2000] or None,
            },
        )
    return status


@dataclass(frozen=True)
class UsageSeries:
    """One metric of one resource: daily (day, average, maximum) over the stored window."""

    metric: str
    days: list[tuple[date, float | None, float | None]]


async def usage(session: Any, resource_uuid: uuid.UUID) -> list[UsageSeries]:
    """Daily summaries for one resource, for rules and evidence charts."""
    rows = (
        await session.execute(
            text(
                "SELECT metric, day, hourly FROM resource_metric WHERE resource_id = :r "
                "ORDER BY metric, day"
            ),
            {"r": resource_uuid},
        )
    ).all()
    series: dict[str, list[tuple[date, float | None, float | None]]] = defaultdict(list)
    for metric, day, hourly in rows:
        present = [v for v in hourly if v is not None]
        series[metric].append(
            (day, sum(present) / len(present) if present else None, max(present, default=None))
        )
    return [UsageSeries(m, d) for m, d in series.items()]
