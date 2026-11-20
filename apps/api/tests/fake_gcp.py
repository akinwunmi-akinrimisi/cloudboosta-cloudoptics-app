"""A fake Google Cloud for tests and the E2E server: an ``httpx.MockTransport`` that answers
the calls the GCP plug-in makes (STS, IAM credentials, BigQuery, Resource Manager, Compute,
Cloud SQL Admin, Recommender, Monitoring), plus synthetic FOCUS rows (``focus_rows``).

BigQuery cannot run SQL here: each query the plug-in sends starts with a ``/* co:<name> */``
marker, and the fake computes that query's answer from the FOCUS rows in Python. Results are
paged (``PAGE``) so the client's paging is exercised. Nothing here contacts Google.
"""

from __future__ import annotations

import json
import re
import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

BILLING_ACCOUNT = "01A2B3-C4D5E6-F7A8B9"
EXPORT_PROJECT = "acme-billing"
DATASET = "billing_export"
TABLE = "focus"
PROJECTS = ("acme-shop-prod", "acme-data-dev")
SERVICE_ACCOUNT = "cloudoptics-reader@cloudoptics-test.iam.gserviceaccount.com"
WORKLOAD_PROVIDER = (
    "//iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/"
    "cloudoptics/providers/aws-broker"
)
FED_TOKEN = "fake-federated-token"  # noqa: S105 - fake
SA_TOKEN = "fake-service-account-token"  # noqa: S105 - fake
PAGE = 40
#: The daily query's group keys, in its column order (before the tags).
KEY = ("ChargePeriodStart", "BillingAccountId", "SubAccountId", "ServiceName", "ChargeCategory",
       "BillingCurrency", "RegionId", "SkuId")  # fmt: skip
FOCUS_FIELDS = (
    "BillingAccountId", "BillingPeriodStart", "ChargePeriodStart", "ChargeCategory",
    "BillingCurrency", "BilledCost", "EffectiveCost", "ListCost", "ConsumedQuantity",
    "SubAccountId", "SubAccountName", "ServiceName", "ServiceCategory", "RegionId", "SkuId",
    "Tags", "x_CurrencyConversionRate",
)  # fmt: skip

# service, category, sku, region, daily cost per project index
SERVICES = (
    ("Compute Engine", "Compute", "6E2B-1A5C-9C1F", "europe-west2", (42.0, 9.5)),
    ("Cloud Storage", "Storage", "9DB3-2F6A-1D7E", "europe-west2", (6.4, 1.2)),
    ("BigQuery", "Analytics", "24E6-581D-38E5", "US", (3.1, 7.8)),
    ("Cloud SQL", "Databases", "C9A4-1B2E-5D88", "europe-west2", (11.0, 0.0)),
)


def focus_rows(
    period: date,
    *,
    billing_account: str = BILLING_ACCOUNT,
    projects: tuple[str, ...] = PROJECTS,
    currency: str = "USD",
    rate: float | None = None,
    through: date | None = None,
) -> list[dict[str, Any]]:
    """A month of FOCUS rows: four services a day in each project, a daily Compute credit
    (free-tier style) and one tax line without a project. ``rate`` is units of ``currency``
    per USD (amounts are multiplied by it, as Google bills in the local currency)."""
    nxt = (period.replace(day=28) + timedelta(days=4)).replace(day=1)
    last = min(nxt - timedelta(days=1), through or nxt - timedelta(days=1))
    scale = Decimal(str(rate or 1))
    rows: list[dict[str, Any]] = []
    day = period
    while day <= last:
        for i, project in enumerate(projects):
            env = "prod" if project.endswith("prod") else "dev"
            for name, category, sku, region, costs in SERVICES:
                cost = Decimal(str(costs[i % 2])) * scale
                if cost == 0:
                    continue
                rows.append(
                    _row(period, day, billing_account, currency, rate, "Usage", cost, project,
                         name, category, sku, region, {"env": env, "team": "core"})
                )  # fmt: skip
            credit = -Decimal("1.25") * scale
            rows.append(
                _row(period, day, billing_account, currency, rate, "Credit", credit, project,
                     "Compute Engine", "Compute", "6E2B-1A5C-9C1F", "europe-west2", {})
            )  # fmt: skip
        day += timedelta(days=1)
    rows.append(
        _row(period, last, billing_account, currency, rate, "Tax", Decimal("7.50") * scale, None,
             "Tax", "Other", None, None, {})
    )  # fmt: skip
    return rows


def _row(
    period: date,
    day: date,
    account: str,
    currency: str,
    rate: float | None,
    charge: str,
    cost: Decimal,
    project: str | None,
    service: str,
    category: str,
    sku: str | None,
    region: str | None,
    tags: dict[str, str],
) -> dict[str, Any]:
    return {
        "BillingAccountId": account,
        "BillingPeriodStart": period,
        "ChargePeriodStart": day,
        "ChargeCategory": charge,
        "BillingCurrency": currency,
        "BilledCost": cost,
        "EffectiveCost": cost,
        "ListCost": cost * Decimal("1.1") if charge == "Usage" else None,
        "ConsumedQuantity": Decimal(24) if charge == "Usage" else None,
        "SubAccountId": project,
        "SubAccountName": project.replace("-", " ").title() if project else None,
        "ServiceName": service,
        "ServiceCategory": category,
        "RegionId": region,
        "SkuId": sku,
        "Tags": tags,
        "x_CurrencyConversionRate": rate,
    }


def new_billing_account() -> str:
    return "-".join(secrets.token_hex(3).upper() for _ in range(3))


def _ts(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def default_resources(now: datetime) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Resources of ``acme-shop-prod``: an idle VM, a busy VM with a machine-type
    recommendation, an unattached disk, a reserved address, an old snapshot of a deleted
    disk and an idle Cloud SQL instance."""
    zone, region = "europe-west2-a", "europe-west2"
    base = f"https://compute.googleapis.com/compute/v1/projects/{PROJECTS[0]}"
    old = _ts(now - timedelta(days=200))
    return {
        PROJECTS[0]: {
            "instances": [
                {"id": "1001", "name": "web-idle-1", "status": "RUNNING",
                 "machineType": f"{base}/zones/{zone}/machineTypes/e2-standard-4",
                 "creationTimestamp": old, "labels": {"env": "dev"},
                 "disks": [{"source": f"{base}/zones/{zone}/disks/web-idle-1"}],
                 "networkInterfaces": [{"accessConfigs": [{"natIP": "34.1.1.1"}]}],
                 "selfLink": f"{base}/zones/{zone}/instances/web-idle-1", "zone": zone},
                {"id": "1002", "name": "api-1", "status": "RUNNING",
                 "machineType": f"{base}/zones/{zone}/machineTypes/n2-standard-8",
                 "creationTimestamp": old, "labels": {"env": "prod"},
                 "selfLink": f"{base}/zones/{zone}/instances/api-1", "zone": zone},
            ],
            "disks": [
                {"id": "2001", "name": "web-idle-1", "status": "READY", "sizeGb": "50",
                 "type": f"{base}/zones/{zone}/diskTypes/pd-balanced",
                 "users": [f"{base}/zones/{zone}/instances/web-idle-1"], "creationTimestamp": old},
                {"id": "2002", "name": "orphan-disk", "status": "READY", "sizeGb": "500",
                 "type": f"{base}/zones/{zone}/diskTypes/pd-ssd", "creationTimestamp": old,
                 "lastDetachTimestamp": _ts(now - timedelta(days=60)), "labels": {"env": "dev"}},
            ],
            "addresses": [
                {"name": "old-ip", "address": "34.2.2.2", "status": "RESERVED",
                 "addressType": "EXTERNAL", "region": region, "creationTimestamp": old},
                {"name": "lb-ip", "address": "34.3.3.3", "status": "IN_USE",
                 "addressType": "EXTERNAL", "region": region, "creationTimestamp": old},
            ],
            "snapshots": [
                {"name": "old-snap", "status": "READY", "storageBytes": str(100 * 1024**3),
                 "diskSizeGb": "200", "creationTimestamp": old,
                 "sourceDisk": f"{base}/zones/{zone}/disks/deleted-disk"},
                {"name": "kept-snap", "status": "READY", "storageBytes": str(10 * 1024**3),
                 "creationTimestamp": old, "autoCreated": True,
                 "sourceDisk": f"{base}/zones/{zone}/disks/deleted-disk"},
            ],
            "sql": [
                {"name": "reports-db", "state": "RUNNABLE", "region": region,
                 "databaseVersion": "POSTGRES_15",
                 "settings": {"tier": "db-custom-4-16384", "activationPolicy": "ALWAYS"}},
            ],
        }
    }  # fmt: skip


def default_recommendations() -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    project, zone, region = PROJECTS[0], "europe-west2-a", "europe-west2"
    return {
        (project, zone, "google.compute.instance.MachineTypeRecommender"): [
            {
                "name": f"projects/{project}/locations/{zone}/recommenders/x/recommendations/r1",
                "description": "Save cost by changing machine type from n2-standard-8 to "
                "n2-standard-4.",
                "priority": "P2",
                "primaryImpact": {
                    "costProjection": {
                        "cost": {"currencyCode": "USD", "units": "-120", "nanos": -500000000},
                        "duration": "2628000s",
                    }
                },
                "content": {
                    "overview": {"resourceName": f"//compute.googleapis.com/projects/{project}/"
                                 f"zones/{zone}/instances/api-1"},
                    "operationGroups": [
                        {"operations": [{"action": "replace", "path": "/machineType",
                                         "value": f"zones/{zone}/machineTypes/n2-standard-4"}]}
                    ],
                },
            }
        ],
        (project, region, "google.cloudsql.instance.IdleRecommender"): [
            {
                "name": f"projects/{project}/locations/{region}/recommenders/y/recommendations/r2",
                "description": "Stop idle instance reports-db.",
                "priority": "P3",
                "primaryImpact": {
                    "costProjection": {
                        "cost": {"currencyCode": "USD", "units": "-80"},
                        "duration": "2628000s",
                    }
                },
                "content": {
                    "overview": {"resourceName": f"//sqladmin.googleapis.com/projects/{project}/"
                                 "instances/reports-db"}
                },
            }
        ],
    }  # fmt: skip


@dataclass
class FakeGcp:
    """State of the fake cloud; ``transport`` answers httpx requests from it."""

    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: A fresh billing account per fake: a proven one is reserved for its first workspace.
    billing_account: str = field(default_factory=lambda: new_billing_account())
    labels: dict[tuple[str, str], dict[str, str]] = field(default_factory=dict)
    datasets: set[tuple[str, str]] = field(default_factory=lambda: {(EXPORT_PROJECT, DATASET)})
    tables: dict[tuple[str, str, str], tuple[str, ...]] = field(
        default_factory=lambda: {(EXPORT_PROJECT, DATASET, TABLE): FOCUS_FIELDS}
    )
    #: billing period -> FOCUS rows; filled by ``bill`` (last month and this month).
    rows: dict[date, list[dict[str, Any]]] = field(default_factory=dict)
    #: project -> permissions the service account holds (testIamPermissions).
    permissions: dict[str, set[str]] = field(default_factory=dict)
    resources: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)
    recommendations: dict[tuple[str, str, str], list[dict[str, Any]]] = field(default_factory=dict)
    #: instance or database id -> hourly CPU utilisation (0-1)
    cpu: dict[str, float] = field(default_factory=lambda: {"1001": 0.01, "1002": 0.55})
    #: path fragment -> (HTTP status, reason) to fail matching requests
    failures: dict[str, tuple[int, str]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    bytes_per_query: int = 5 * 1024**2
    dataset_access: bool = False
    sts_ok: bool = True
    _results: dict[str, tuple[list[dict[str, str]], list[list[Any]]]] = field(default_factory=dict)

    def bill(self, currency: str = "USD", rate: float | None = None) -> FakeGcp:
        this = self.now.date().replace(day=1)
        last = (this - timedelta(days=1)).replace(day=1)
        self.rows = {
            last: focus_rows(
                last, billing_account=self.billing_account, currency=currency, rate=rate
            ),
            this: focus_rows(
                this,
                billing_account=self.billing_account,
                currency=currency,
                rate=rate,
                through=self.now.date(),
            ),
        }
        return self

    def grant_all(self) -> FakeGcp:
        everything = {
            "bigquery.jobs.create",
            "compute.instances.list",
            "compute.disks.list",
            "monitoring.timeSeries.list",
            "recommender.computeInstanceIdleResourceRecommendations.list",
        }
        for p in (EXPORT_PROJECT, *PROJECTS):
            self.permissions[p] = set(everything)
        self.dataset_access = True
        self.resources = default_resources(self.now)
        self.recommendations = default_recommendations()
        return self

    def set_label(self, project: str, dataset: str, key: str, value: str) -> None:
        self.labels.setdefault((project, dataset), {})[key] = value

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # --- HTTP -------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        path = unquote(url.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.calls.append(f"{request.method} {url.netloc}{path}")
        for fragment, (status, reason) in self.failures.items():
            if fragment in f"{url.netloc}{path}":
                return _err(status, reason)
        if url.netloc == "sts.googleapis.com":
            return self._sts(request)
        if url.netloc == "iamcredentials.googleapis.com":
            if request.headers.get("authorization") != f"Bearer {FED_TOKEN}":
                return _err(401, "UNAUTHENTICATED")
            return httpx.Response(
                200,
                json={"accessToken": SA_TOKEN, "expireTime": _ts(self.now + timedelta(hours=1))},
            )
        if request.headers.get("authorization") != f"Bearer {SA_TOKEN}":
            return _err(401, "UNAUTHENTICATED")
        body = json.loads(request.content) if request.content else {}
        host = url.netloc.split(".")[0]
        handler = getattr(self, f"_{host}", None)
        if handler is None:
            return _err(404, "notFound")
        result = handler(request.method, path, query, body)
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    def _sts(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if not self.sts_ok or form.get("audience") != WORKLOAD_PROVIDER:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "no"})
        signed = json.loads(unquote(form["subject_token"]))
        headers = {h["key"].lower(): h["value"] for h in signed["headers"]}
        if headers.get("x-goog-cloud-target-resource") != WORKLOAD_PROVIDER or not headers.get(
            "authorization", ""
        ).startswith("AWS4-HMAC-SHA256"):
            return httpx.Response(400, json={"error": "invalid_grant"})
        return httpx.Response(
            200, json={"access_token": FED_TOKEN, "expires_in": 3600, "token_type": "Bearer"}
        )

    def _cloudresourcemanager(self, method: str, path: str, q: dict[str, str], body: Any) -> Any:
        project = path.split("/projects/")[1].split(":")[0]
        held = self.permissions.get(project, set())
        return {"permissions": [p for p in body.get("permissions", []) if p in held]}

    def _bigquery(self, method: str, path: str, q: dict[str, str], body: Any) -> Any:
        m = re.match(
            r"/bigquery/v2/projects/([^/]+)/(datasets|queries)(?:/([^/]+))?(?:/tables/([^/]+))?",
            path,
        )
        if not m:
            return _err(404, "notFound")
        project, kind, name, table = m.groups()
        if kind == "datasets":
            if (project, name) not in self.datasets:
                return _err(404, "notFound")
            if not self.dataset_access:
                return _err(403, "accessDenied")
            if table is not None:
                fields = self.tables.get((project, name, table))
                if fields is None:
                    return _err(404, "notFound")
                return {"schema": {"fields": [{"name": f, "type": "STRING"} for f in fields]}}
            return {"datasetReference": {"datasetId": name}, "location": "US",
                    "labels": self.labels.get((project, name), {})}  # fmt: skip
        if "bigquery.jobs.create" not in self.permissions.get(project, set()):
            return _err(403, "accessDenied")
        if method == "GET":  # getQueryResults: the next page
            schema, rows = self._results[name]
            start = int(q.get("pageToken", "0"))
            return self._page(name, schema, rows, start)
        if body.get("dryRun"):
            return {"jobComplete": True, "totalBytesProcessed": str(self.bytes_per_query)}
        if int(body.get("maximumBytesBilled", "0")) < self.bytes_per_query:
            return _err(400, "bytesBilledLimitExceeded")
        params = {p["name"]: p["parameterValue"]["value"] for p in body.get("queryParameters", [])}
        marker = re.search(r"/\* co:(\w+) \*/", body["query"])
        schema, rows = getattr(self, f"_q_{marker.group(1) if marker else 'none'}")(params)
        job = f"job{len(self.calls)}"
        self._results[job] = (schema, rows)
        return self._page(job, schema, rows, 0)

    def _page(
        self, job: str, schema: list[dict[str, str]], rows: list[list[Any]], start: int
    ) -> Any:
        chunk = rows[start : start + PAGE]
        page: dict[str, Any] = {
            "jobComplete": True,
            "jobReference": {"jobId": job, "location": "US"},
            "schema": {"fields": schema},
            "totalBytesProcessed": str(self.bytes_per_query),
            "totalBytesBilled": str(self.bytes_per_query),
            "rows": [{"f": [{"v": None if v is None else str(v)} for v in r]} for r in chunk],
        }
        if start + PAGE < len(rows):
            page["pageToken"] = str(start + PAGE)
        return page

    def _period_rows(self, value: str | None) -> list[dict[str, Any]]:
        return self.rows.get(date.fromisoformat(str(value)[:10]), []) if value else []

    def _q_daily(self, params: dict[str, Any]) -> tuple[list[dict[str, str]], list[list[Any]]]:
        cols = [
            ("usage_date", "DATE"), ("billing_account_id", "STRING"), ("project_id", "STRING"),
            ("service_name", "STRING"), ("charge_category", "STRING"),
            ("billing_currency", "STRING"), ("region_id", "STRING"), ("sku_id", "STRING"),
            ("tags", "STRING"), ("billed_cost", "NUMERIC"), ("effective_cost", "NUMERIC"),
            ("project_name", "STRING"), ("service_category", "STRING"), ("list_cost", "NUMERIC"),
            ("list_n", "INTEGER"), ("quantity", "NUMERIC"), ("quantity_n", "INTEGER"),
            ("conversion_rate", "FLOAT"), ("line_items", "INTEGER"),
        ]  # fmt: skip
        groups: dict[tuple[Any, ...], list[Any]] = {}
        for r in self._period_rows(params.get("period")):
            tags = json.dumps(r["Tags"], sort_keys=True) if r["Tags"] else None
            key = (*(r[c] for c in KEY), tags)
            rate = r["x_CurrencyConversionRate"]
            g = groups.setdefault(key, [Decimal(0), Decimal(0), None, None, Decimal(0), 0,
                                        Decimal(0), 0, rate, 0])  # fmt: skip
            g[0] += r["BilledCost"]
            g[1] += r["EffectiveCost"]
            g[2] = g[2] or r["SubAccountName"]
            g[3] = g[3] or r["ServiceCategory"]
            if r["ListCost"] is not None:
                g[4] += r["ListCost"]
                g[5] += 1
            if r["ConsumedQuantity"] is not None:
                g[6] += r["ConsumedQuantity"]
                g[7] += 1
            g[9] += 1
        rows = [
            [
                *key,
                g[0],
                g[1],
                g[2],
                g[3],
                g[4] if g[5] else None,
                g[5],
                g[6] if g[7] else None,
                g[7],
                g[8],
                g[9],
            ]
            for key, g in groups.items()
        ]
        return [{"name": n, "type": t} for n, t in cols], rows

    def _q_totals(self, params: dict[str, Any]) -> tuple[list[dict[str, str]], list[list[Any]]]:
        totals: dict[str | None, Decimal] = defaultdict(Decimal)
        for r in self._period_rows(params.get("period")):
            totals[r["SubAccountId"]] += r["BilledCost"]
        schema = [{"name": "project_id", "type": "STRING"}, {"name": "billed", "type": "NUMERIC"}]
        return schema, [[k, v] for k, v in totals.items()]

    def _since(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        since = date.fromisoformat(str(params["since"])[:10])
        return [r for p, rows in self.rows.items() if p >= since for r in rows]

    def _q_projects(self, params: dict[str, Any]) -> tuple[list[dict[str, str]], list[list[Any]]]:
        found = {
            r["SubAccountId"]: r["SubAccountName"] for r in self._since(params) if r["SubAccountId"]
        }
        schema = [{"name": "project_id", "type": "STRING"}, {"name": "name", "type": "STRING"}]
        return schema, [[k, v] for k, v in sorted(found.items())]

    def _q_billing_accounts(
        self, params: dict[str, Any]
    ) -> tuple[list[dict[str, str]], list[list[Any]]]:
        counts: dict[str, int] = defaultdict(int)
        for r in self._since(params):
            counts[r["BillingAccountId"]] += 1
        schema = [
            {"name": "billing_account_id", "type": "STRING"},
            {"name": "n", "type": "INTEGER"},
        ]
        return schema, [[k, v] for k, v in counts.items()]

    def _q_none(self, params: dict[str, Any]) -> tuple[list[dict[str, str]], list[list[Any]]]:
        return [], []

    def _allowed(self, project: str, permission: str) -> httpx.Response | None:
        if permission not in self.permissions.get(project, set()):
            return _err(403, "accessDenied")
        return None

    def _compute(self, method: str, path: str, q: dict[str, str], body: Any) -> Any:
        m = re.match(r"/compute/v1/projects/([^/]+)/(aggregated|global)/(\w+)", path)
        if not m:
            return _err(404, "notFound")
        project, scope, kind = m.groups()
        denied = self._allowed(project, "compute.instances.list")
        if denied:
            return denied
        items = self.resources.get(project, {})
        if scope == "global":
            if kind == "addresses":
                return {"items": []}
            return {"items": items.get(kind, [])}
        scoped: dict[str, Any] = {}
        for item in items.get(kind, []):
            where = f"regions/{item['region']}" if "region" in item else "zones/europe-west2-a"
            scoped.setdefault(where, {kind: []})[kind].append(item)
        return {"items": scoped}

    def _sqladmin(self, method: str, path: str, q: dict[str, str], body: Any) -> Any:
        project = path.split("/projects/")[1].split("/")[0]
        if project not in self.resources:
            return _err(403, "accessNotConfigured")
        return {"items": self.resources[project].get("sql", [])}

    def _recommender(self, method: str, path: str, q: dict[str, str], body: Any) -> Any:
        m = re.match(r"/v1/projects/([^/]+)/locations/([^/]+)/recommenders/([^/]+)/", path)
        if not m:
            return _err(404, "notFound")
        return {"recommendations": self.recommendations.get(m.groups(), [])}

    def _monitoring(self, method: str, path: str, q: dict[str, str], body: Any) -> Any:
        project = path.split("/projects/")[1].split("/")[0]
        denied = self._allowed(project, "monitoring.timeSeries.list")
        if denied:
            return denied
        start = datetime.fromisoformat(q["interval.startTime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(q["interval.endTime"].replace("Z", "+00:00"))
        series = []
        sql = "cloudsql" in q.get("filter", "")
        targets = (
            {f"{project}:{i['name']}": 0.02 for i in self.resources.get(project, {}).get("sql", [])}
            if sql
            else {
                i["id"]: self.cpu.get(i["id"], 0.3)
                for i in self.resources.get(project, {}).get("instances", [])
            }
        )
        for key, value in targets.items():
            points = []
            t = start
            while t < end:
                t += timedelta(hours=1)
                points.append({"interval": {"endTime": _ts(t)}, "value": {"doubleValue": value}})
            label = "database_id" if sql else "instance_id"
            series.append({"resource": {"labels": {label: key}}, "points": points})
        return {"timeSeries": series}


def _err(status: int, reason: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"code": status, "message": reason, "errors": [{"reason": reason}]}},
    )


def install(fake: FakeGcp) -> None:
    """Point the GCP plug-in at the fake (and give it fake AWS broker credentials)."""
    from cloudoptics.providers.gcp import client
    from cloudoptics.providers.gcp.settings import get_gcp_settings

    settings = get_gcp_settings()
    settings.workload_provider = WORKLOAD_PROVIDER
    settings.service_account = SERVICE_ACCOUNT
    client.set_transport(fake.transport)
    client.set_aws_credentials(
        lambda: client.AwsCredentials("AKIDEXAMPLE", "fake-secret-key", "fake-session-token")
    )


def uninstall() -> None:
    from cloudoptics.providers.gcp import client
    from cloudoptics.providers.gcp.settings import get_gcp_settings

    settings = get_gcp_settings()
    settings.workload_provider = ""
    settings.service_account = ""
    client.set_transport(None)
    client.set_aws_credentials(None)
