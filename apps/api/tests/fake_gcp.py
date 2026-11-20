"""A fake Google Cloud for tests and the E2E server: an ``httpx.MockTransport`` that answers
the calls the GCP plug-in makes (STS, IAM, IAM credentials, BigQuery, Resource Manager,
Compute, Cloud SQL Admin, Recommender, Monitoring), plus synthetic FOCUS rows (``focus_rows``).

**Identity is enforced** (ADR-019, one service account per connection): the broker's federated
token creates and deletes service accounts in Cloudoptics' project (``SA_PROJECT``, with a
count quota) and impersonates any of them; every other call is made as one service account,
and it sees only what the customer shared with that account (``share``, ``grant``). A dataset
shared with workspace A's account is a 403 for workspace B's, whatever names B types, and a
view reads its base tables with the caller's own access. Queries run in ``QUERY_PROJECT``,
where every service account of ``SA_PROJECT`` may run jobs (setup.sh's principal set).

BigQuery cannot run SQL here: each query the plug-in sends starts with a ``/* co:<name> */``
marker, and the fake computes that query's answer from the FOCUS rows in Python. Results are
paged (``PAGE``) so the client's paging is exercised. Nothing here contacts Google.

The shapes follow Google's documentation (checked 20 Nov 2026, ADR-019 Sources): the FOCUS
export's columns and types (no ``Credit`` rows, credits inside ``BilledCost`` and listed in
``x_Credits``, labels in ``x_Labels``, no ``Tags``); BigQuery's ``Dataset``, ``Table``,
``QueryResponse``, ``GetQueryResultsResponse`` resources; IAM's ``ServiceAccount``; errors as
``google.rpc.Status`` over HTTP (AIP-193) with BigQuery's ``errors[].reason``, quota errors
as 403 ``rateLimitExceeded``; Compute ``aggregatedList`` scopes with ``warning`` entries;
Recommender's idle-disk example (an ``add`` snapshot operation before the ``remove``); and
Monitoring ``timeSeries`` with points newest first.
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
#: Cloudoptics' own project: the per-connection service accounts, and the queries.
SA_PROJECT = "cloudoptics-test"
QUERY_PROJECT = SA_PROJECT
ACCOUNT_ID = re.compile(r"[a-z]([-a-z0-9]*[a-z0-9])")
WORKLOAD_PROVIDER = (
    "//iam.googleapis.com/projects/123456789/locations/global/workloadIdentityPools/"
    "cloudoptics/providers/aws-broker"
)
FED_TOKEN = "fake-federated-token"  # noqa: S105 - fake
SA_TOKEN = "fake-sa-token/"  # noqa: S105 - fake; followed by the account's email
PAGE = 40
#: The daily query's group keys, in its column order (before the tags).
KEY = ("ChargePeriodStart", "BillingAccountId", "SubAccountId", "ServiceName", "ChargeCategory",
       "BillingCurrency", "RegionId", "SkuId")  # fmt: skip
#: Columns of Google's managed FOCUS export the plug-in may read, with their BigQuery types
#: (Structure of FOCUS data export). There is no ServiceCategory and no Tags column.
FOCUS_TYPES: dict[str, tuple[str, str]] = {
    "BillingAccountId": ("STRING", "NULLABLE"), "BillingPeriodStart": ("TIMESTAMP", "NULLABLE"),
    "BillingPeriodEnd": ("TIMESTAMP", "NULLABLE"), "ChargePeriodStart": ("TIMESTAMP", "NULLABLE"),
    "ChargePeriodEnd": ("TIMESTAMP", "NULLABLE"), "ChargeCategory": ("STRING", "NULLABLE"),
    "BillingCurrency": ("STRING", "NULLABLE"), "BilledCost": ("NUMERIC", "NULLABLE"),
    "EffectiveCost": ("NUMERIC", "NULLABLE"), "ListCost": ("NUMERIC", "NULLABLE"),
    "ConsumedQuantity": ("NUMERIC", "NULLABLE"), "SubAccountId": ("STRING", "NULLABLE"),
    "SubAccountName": ("STRING", "NULLABLE"), "ServiceName": ("STRING", "NULLABLE"),
    "RegionId": ("STRING", "NULLABLE"), "SkuId": ("STRING", "NULLABLE"),
    "x_Credits": ("RECORD", "REPEATED"), "x_CurrencyConversionRate": ("FLOAT", "NULLABLE"),
    "x_ExportTime": ("TIMESTAMP", "NULLABLE"), "x_Labels": ("RECORD", "REPEATED"),
    "x_Tags": ("RECORD", "REPEATED"),
}  # fmt: skip
FOCUS_FIELDS = tuple(FOCUS_TYPES)

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
    """A month of FOCUS rows: four services a day in each project, a free-tier credit on
    each day's Compute line (inside ``BilledCost``, as Google's export has no Credit rows)
    and one tax line without a project. ``rate`` is units of ``currency`` per USD (amounts
    are multiplied by it, as Google bills in the local currency)."""
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
                credit = -Decimal("1.25") * scale if name == "Compute Engine" else Decimal(0)
                rows.append(
                    _row(period, day, billing_account, currency, rate, "Usage", cost, project,
                         name, category, sku, region, {"env": env, "team": "core"}, credit)
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
    labels: dict[str, str],
    credit: Decimal = Decimal(0),
) -> dict[str, Any]:
    """One FOCUS row. ``BilledCost`` and ``EffectiveCost`` are cost + credits; ``ListCost``
    is ``cost_at_list``. ``BillingPeriodStart`` is the invoice month at midnight US Pacific
    time here: Google does not document the hour, and the plug-in must not depend on it."""
    del category  # the FOCUS export has no ServiceCategory
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return {
        "BillingAccountId": account,
        "BillingPeriodStart": datetime(period.year, period.month, 1, 8, tzinfo=UTC),
        "ChargePeriodStart": start,
        "ChargePeriodEnd": start + timedelta(hours=1),
        "ChargeCategory": charge,
        "BillingCurrency": currency,
        "BilledCost": cost + credit,
        "EffectiveCost": cost + credit,
        "ListCost": cost * Decimal("1.1") if charge == "Usage" else None,
        "ConsumedQuantity": Decimal(24) if charge == "Usage" else None,
        "SubAccountId": project,
        "SubAccountName": project.replace("-", " ").title() if project else None,
        "ServiceName": service,
        "RegionId": region,
        "SkuId": sku,
        "x_Labels": [{"Key": k, "Value": v} for k, v in sorted(labels.items())],
        "x_Credits": [
            {
                "Id": "FreeTier",
                "FullName": "Free tier",
                "Type": "FREE_TIER",
                "Name": "Free tier",
                "Amount": credit,
            }
        ]
        if credit
        else [],
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
    compute = f"//compute.googleapis.com/projects/{project}"
    return {
        # Google's idle-disk example: a snapshot "add" first, then the disk "remove".
        (project, zone, "google.compute.disk.IdleResourceRecommender"): [
            {
                "name": f"projects/{project}/locations/{zone}/recommenders/"
                "google.compute.disk.IdleResourceRecommender/recommendations/r3",
                "description": "Save cost by snapshotting and then deleting idle persistent "
                "disk 'orphan-disk'.",
                "recommenderSubtype": "SNAPSHOT_AND_DELETE_DISK",
                "priority": "P4",
                "stateInfo": {"state": "ACTIVE"},
                "lastRefreshTime": "2026-11-18T07:00:00Z",
                "primaryImpact": {
                    "category": "COST",
                    "costProjection": {
                        "cost": {"currencyCode": "USD", "units": "-85"},
                        "duration": "2592000s",
                    },
                },
                "content": {
                    "operationGroups": [
                        {"operations": [
                            {"action": "add", "resourceType": "compute.googleapis.com/Snapshot",
                             "resource": f"{compute}/global/snapshots/orphan-disk-snap",
                             "path": "/", "value": {
                                 "name": "orphan-disk-snap",
                                 "sourceDisk": f"projects/{project}/zones/{zone}/disks/orphan-disk",
                                 "storageLocations": [region]}},
                            {"action": "remove", "resourceType": "compute.googleapis.com/Disk",
                             "resource": f"{compute}/zones/{zone}/disks/orphan-disk", "path": "/"},
                        ]}
                    ]
                },
            }
        ],
        (project, zone, "google.compute.instance.MachineTypeRecommender"): [
            {
                "name": f"projects/{project}/locations/{zone}/recommenders/x/recommendations/r1",
                "description": "Save cost by changing machine type from n2-standard-8 to "
                "n2-standard-4.",
                "priority": "P2",
                "stateInfo": {"state": "ACTIVE"},
                "primaryImpact": {
                    "category": "COST",
                    "costProjection": {
                        "cost": {"currencyCode": "USD", "units": "-120", "nanos": -500000000},
                        "duration": "2628000s",
                    },
                },
                "content": {
                    "overview": {"resourceName": f"//compute.googleapis.com/projects/{project}/"
                                 f"zones/{zone}/instances/api-1"},
                    "operationGroups": [
                        {"operations": [{"action": "replace", "path": "/machineType",
                                         "resourceType": "compute.googleapis.com/Instance",
                                         "resource": f"{compute}/zones/{zone}/instances/api-1",
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
                "stateInfo": {"state": "ACTIVE"},
                "primaryImpact": {
                    "category": "COST",
                    "costProjection": {
                        "cost": {"currencyCode": "USD", "units": "-80"},
                        "duration": "2628000s",
                    },
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
    datasets: set[tuple[str, str]] = field(default_factory=lambda: {(EXPORT_PROJECT, DATASET)})
    tables: dict[tuple[str, str, str], tuple[str, ...]] = field(
        default_factory=lambda: {(EXPORT_PROJECT, DATASET, TABLE): FOCUS_FIELDS}
    )
    #: (project, dataset, view) -> the base tables it reads (``referencedTables``)
    views: dict[tuple[str, str, str], list[tuple[str, str, str]]] = field(default_factory=dict)
    #: path fragment -> errors answered once each, in order, before normal answers (retries)
    flaky: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    #: jobs.query answers ``jobComplete: false`` first, as for a query still running
    slow_jobs: bool = False
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
    sts_ok: bool = True
    #: email -> ServiceAccount resource, in SA_PROJECT; at most ``sa_quota`` of them.
    service_accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    sa_quota: int = 100
    #: (project, dataset) -> service accounts it is shared with (BigQuery Data Viewer)
    readers: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    #: project -> service accounts granted the viewer roles (and Job User) there
    members: dict[str, set[str]] = field(default_factory=dict)
    #: (method, path) -> the service account each call was made as
    callers: list[tuple[str, str | None]] = field(default_factory=list)
    _caller: str | None = None
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
        self.resources = default_resources(self.now)
        self.recommendations = default_recommendations()
        return self

    def share(self, email: str, project: str = EXPORT_PROJECT, dataset: str = DATASET) -> None:
        """Step 2: the customer shares a dataset with one service account."""
        self.readers.setdefault((project, dataset), set()).add(email)

    def unshare(self, email: str, project: str = EXPORT_PROJECT, dataset: str = DATASET) -> None:
        self.readers.get((project, dataset), set()).discard(email)

    def grant(self, email: str, *projects: str) -> None:
        """Step 3: the viewer roles (and Job User) for one service account on projects."""
        for p in projects or (EXPORT_PROJECT, *PROJECTS):
            self.members.setdefault(p, set()).add(email)

    def add_account(self, account_id: str = "co-unit00000001") -> str:
        """A service account made without the API (unit tests of the client)."""
        email = f"{account_id}@{SA_PROJECT}.iam.gserviceaccount.com"
        self.service_accounts[email] = {"email": email, "projectId": SA_PROJECT}
        return email

    def customer_setup(self, email: str) -> None:
        """Steps 2 and 3, as a customer following the wizard does them."""
        self.share(email)
        self.grant(email)

    def _member(self, project: str) -> bool:
        return self._caller is not None and self._caller in self.members.get(project, set())

    def _reads(self, project: str, dataset: str) -> bool:
        return self._caller is not None and self._caller in self.readers.get(
            (project, dataset), set()
        )

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
        for fragment, queue in self.flaky.items():
            if fragment in f"{url.netloc}{path}" and queue:
                status, reason = queue.pop(0)
                return _err(status, reason)
        if url.netloc == "sts.googleapis.com":
            return self._sts(request)
        broker = request.headers.get("authorization") == f"Bearer {FED_TOKEN}"
        body = json.loads(request.content) if request.content else {}
        if url.netloc == "iamcredentials.googleapis.com":
            if not broker:
                return _err(401, "UNAUTHENTICATED")
            email = path.split("/serviceAccounts/")[1].split(":")[0]
            if email not in self.service_accounts:
                return _err(404, "NOT_FOUND")
            return httpx.Response(
                200,
                json={
                    "accessToken": SA_TOKEN + email,
                    "expireTime": _ts(datetime.now(UTC) + timedelta(hours=1)),
                },
            )
        if url.netloc == "iam.googleapis.com":
            return self._iam(request.method, path, body) if broker else _err(401, "UNAUTHENTICATED")
        auth = request.headers.get("authorization", "")
        caller = (
            auth.removeprefix(f"Bearer {SA_TOKEN}")
            if auth.startswith(f"Bearer {SA_TOKEN}")
            else None
        )
        if caller not in self.service_accounts:
            return _err(401, "UNAUTHENTICATED")  # no token, or the account was deleted
        self._caller = caller
        self.callers.append((f"{request.method} {url.netloc}{path}", caller))
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
            200,
            json={
                "access_token": FED_TOKEN,
                "issued_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    def _iam(self, method: str, path: str, body: Any) -> httpx.Response:
        """projects.serviceAccounts.create and .delete, in SA_PROJECT only."""
        m = re.match(r"/v1/projects/([^/]+)/serviceAccounts(?:/([^/]+))?$", path)
        if not m:
            return _err(404, "NOT_FOUND")
        project, email = m.groups()
        if method == "DELETE" and email:
            if self.service_accounts.pop(email, None) is None:
                return _err(404, "NOT_FOUND")
            for readers in (*self.readers.values(), *self.members.values()):
                readers.discard(email)  # the grants stop working (deleted:serviceAccount:)
            return httpx.Response(200, json={})
        if method != "POST" or email or project != SA_PROJECT:
            return _err(403, "PERMISSION_DENIED")
        account_id = str(body.get("accountId", ""))
        if not (6 <= len(account_id) <= 30 and ACCOUNT_ID.fullmatch(account_id)):
            return _err(400, "INVALID_ARGUMENT")
        email = f"{account_id}@{project}.iam.gserviceaccount.com"
        if email in self.service_accounts:
            return _err(409, "ALREADY_EXISTS")
        if len(self.service_accounts) >= self.sa_quota:
            # The documented quota is "Service Account Count"; this error's exact shape is
            # not documented (ADR-019, needs a real account).
            return _err(429, "RESOURCE_EXHAUSTED")
        resource = {
            "name": f"projects/{project}/serviceAccounts/{email}",
            "projectId": project,
            "uniqueId": str(100000000000000000000 + len(self.service_accounts)),
            "email": email,
            "displayName": (body.get("serviceAccount") or {}).get("displayName", ""),
            "description": (body.get("serviceAccount") or {}).get("description", ""),
            "oauth2ClientId": str(200000000000000000000 + len(self.service_accounts)),
        }
        self.service_accounts[email] = resource
        return httpx.Response(200, json=resource)

    def _cloudresourcemanager(self, method: str, path: str, q: dict[str, str], body: Any) -> Any:
        project = path.split("/projects/")[1].split(":")[0]
        if project == QUERY_PROJECT:  # every service account of SA_PROJECT runs jobs here
            held = {"bigquery.jobs.create"}
        else:
            held = self.permissions.get(project, set()) if self._member(project) else set()
        return {"permissions": [p for p in body.get("permissions", []) if p in held]}

    def _bigquery(self, method: str, path: str, q: dict[str, str], body: Any) -> Any:
        m = re.match(
            r"/bigquery/v2/projects/([^/]+)/(datasets|queries|jobs)(?:/([^/]+))?"
            r"(?:/tables/([^/]+))?",
            path,
        )
        if not m:
            return _err(404, "notFound")
        project, kind, name, table = m.groups()
        if kind == "datasets":
            if (project, name) not in self.datasets:
                return _err(404, "notFound")
            if not self._reads(project, name):
                return _err(403, "accessDenied")
            if table is not None:
                return self._table(project, name, table)
            dataset: dict[str, Any] = {
                "kind": "bigquery#dataset",
                "id": f"{project}:{name}",
                "datasetReference": {"projectId": project, "datasetId": name},
                "location": "US",
                "type": "DEFAULT",
            }
            return dataset
        if project != QUERY_PROJECT and not (
            self._member(project) and "bigquery.jobs.create" in self.permissions.get(project, set())
        ):
            return _err(403, "accessDenied")
        if kind == "jobs":
            return _err(400, "invalid")  # the plug-in runs jobs.query only
        if method == "GET":  # getQueryResults: the next page (or the finished first one)
            schema, rows = self._results[name]
            start = int(q.get("pageToken", "0"))
            page = self._page(name, schema, rows, start)
            page["kind"] = "bigquery#getQueryResultsResponse"
            page.pop("totalBytesBilled")  # GetQueryResultsResponse has no totalBytesBilled
            return page
        if body.get("dryRun"):
            return {
                "kind": "bigquery#queryResponse",
                "jobComplete": True,
                "totalBytesProcessed": str(self.bytes_per_query),
                "schema": {"fields": []},
            }
        if int(body.get("maximumBytesBilled", "0")) < self.bytes_per_query:
            return _err(400, "bytesBilledLimitExceeded")
        denied = self._table_access(body["query"])
        if denied:
            return denied
        params = {p["name"]: p["parameterValue"]["value"] for p in body.get("queryParameters", [])}
        marker = re.search(r"/\* co:(\w+) \*/", body["query"])
        schema, rows = getattr(self, f"_q_{marker.group(1) if marker else 'none'}")(params)
        job = f"job{len(self.calls)}"
        self._results[job] = (schema, rows)
        if self.slow_jobs:
            return {
                "kind": "bigquery#queryResponse",
                "jobComplete": False,
                "jobReference": {"projectId": project, "jobId": job, "location": "US"},
                "totalBytesBilled": str(self.bytes_per_query),
            }
        page = self._page(job, schema, rows, 0, project)
        page["kind"] = "bigquery#queryResponse"
        return page

    def _table(self, project: str, dataset: str, table: str) -> Any:
        ref = {"projectId": project, "datasetId": dataset, "tableId": table}
        if (project, dataset, table) in self.views:
            return {"kind": "bigquery#table", "id": f"{project}:{dataset}.{table}",
                    "tableReference": ref, "type": "VIEW",
                    "view": {"query": "SELECT ...", "useLegacySql": False},
                    "schema": {"fields": _schema(FOCUS_FIELDS)}}  # fmt: skip
        fields = self.tables.get((project, dataset, table))
        if fields is None:
            return _err(404, "notFound")
        return {
            "kind": "bigquery#table",
            "id": f"{project}:{dataset}.{table}",
            "tableReference": ref,
            "type": "TABLE",
            "schema": {"fields": _schema(fields)},
        }

    def _table_access(self, sql: str) -> httpx.Response | None:
        """The caller must read the table, or each base table of a view (as BigQuery runs a
        view with the querying account's own access)."""
        m = re.search(r"FROM `([^.`]+)\.([^.`]+)\.([^.`]+)`", sql)
        if not m:
            return None
        ref = (m.group(1), m.group(2), m.group(3))
        for project, dataset, _ in self.views.get(ref, [ref]):
            if not self._reads(project, dataset):
                return _err(403, "accessDenied")
        if not self._reads(ref[0], ref[1]):
            return _err(403, "accessDenied")
        return None

    def _page(
        self,
        job: str,
        schema: list[dict[str, str]],
        rows: list[list[Any]],
        start: int,
        project: str = EXPORT_PROJECT,
    ) -> Any:
        chunk = rows[start : start + PAGE]
        page: dict[str, Any] = {
            "jobComplete": True,
            "jobReference": {"projectId": project, "jobId": job, "location": "US"},
            "schema": {"fields": schema},
            "totalRows": str(len(rows)),
            "totalBytesProcessed": str(self.bytes_per_query),
            "totalBytesBilled": str(self.bytes_per_query),
            "rows": [{"f": [{"v": None if v is None else str(v)} for v in r]} for r in chunk],
        }
        if start + PAGE < len(rows):
            page["pageToken"] = str(start + PAGE)
        return page

    def _period_rows(self, value: str | None) -> list[dict[str, Any]]:
        """Rows whose BillingPeriodStart is within a day of the period's midnight UTC (the
        plug-in's filter), whatever hour the fake stores."""
        if not value:
            return []
        at = datetime.fromisoformat(str(value)[:10]).replace(tzinfo=UTC)
        return [
            r
            for rows in self.rows.values()
            for r in rows
            if abs(r["BillingPeriodStart"] - at) < timedelta(days=1)
        ]

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
            tags = json.dumps(r["x_Labels"]) if r["x_Labels"] else "[]"  # TO_JSON_STRING
            key = (r["ChargePeriodStart"].date(), *(r[c] for c in KEY[1:]), tags)
            rate = r["x_CurrencyConversionRate"]
            g = groups.setdefault(key, [Decimal(0), Decimal(0), None, None, Decimal(0), 0,
                                        Decimal(0), 0, rate, 0])  # fmt: skip
            g[0] += r["BilledCost"]
            g[1] += r["EffectiveCost"]
            g[2] = g[2] or r["SubAccountName"]
            g[3] = None  # no ServiceCategory in the FOCUS export: the query reads NULL
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
        since = datetime.fromisoformat(str(params["since"])[:10]).replace(tzinfo=UTC)
        return [
            r
            for rows in self.rows.values()
            for r in rows
            if r["BillingPeriodStart"] >= since - timedelta(days=1)
        ]

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
        if not self._member(project) or permission not in self.permissions.get(project, set()):
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
                return {"kind": "compute#addressList", "items": []}
            return {"kind": f"compute#{kind[:-1]}List", "items": items.get(kind, [])}
        # Every scope is listed; one without resources carries a warning instead.
        scoped: dict[str, Any] = {
            "zones/us-central1-a": {
                "warning": {
                    "code": "NO_RESULTS_ON_PAGE",
                    "message": "There are no results for scope 'zones/us-central1-a' on this page.",
                    "data": [{"key": "scope", "value": "zones/us-central1-a"}],
                }
            }
        }
        for item in items.get(kind, []):
            where = f"regions/{item['region']}" if "region" in item else "zones/europe-west2-a"
            scoped.setdefault(where, {kind: []})[kind].append(item)
        return {"kind": f"compute#{kind[:-1]}AggregatedList", "items": scoped}

    def _sqladmin(self, method: str, path: str, q: dict[str, str], body: Any) -> Any:
        project = path.split("/projects/")[1].split("/")[0]
        if project not in self.resources:
            return _err(403, "accessNotConfigured", info="SERVICE_DISABLED")
        if not self._member(project):
            return _err(403, "PERMISSION_DENIED")
        return {"kind": "sql#instancesList", "items": self.resources[project].get("sql", [])}

    def _recommender(self, method: str, path: str, q: dict[str, str], body: Any) -> Any:
        m = re.match(r"/v1/projects/([^/]+)/locations/([^/]+)/recommenders/([^/]+)/", path)
        if not m:
            return _err(404, "notFound")
        if not self._member(m.group(1)):
            return _err(403, "PERMISSION_DENIED")
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
                points.append(
                    {
                        "interval": {"startTime": _ts(t), "endTime": _ts(t + timedelta(hours=1))},
                        "value": {"doubleValue": value},
                    }
                )
                t += timedelta(hours=1)
            points.reverse()  # newest first, as Monitoring returns them
            if sql:
                resource = {
                    "type": "cloudsql_database",
                    "labels": {"project_id": project, "database_id": key, "region": "europe-west2"},
                }
                metric = "cloudsql.googleapis.com/database/cpu/utilization"
            else:
                resource = {
                    "type": "gce_instance",
                    "labels": {"project_id": project, "instance_id": key, "zone": "europe-west2-a"},
                }
                metric = "compute.googleapis.com/instance/cpu/utilization"
            series.append(
                {
                    "metric": {"type": metric, "labels": {}},
                    "resource": resource,
                    "metricKind": "GAUGE",
                    "valueType": "DOUBLE",
                    "points": points,
                }
            )
        return {"timeSeries": series, "unit": "10^2.%"}


#: HTTP status -> google.rpc.Code name (AIP-193's mapping)
CANONICAL = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED",
             404: "NOT_FOUND", 409: "ALREADY_EXISTS", 429: "RESOURCE_EXHAUSTED",
             500: "INTERNAL", 503: "UNAVAILABLE", 504: "DEADLINE_EXCEEDED"}  # fmt: skip


def _schema(fields: tuple[str, ...]) -> list[dict[str, str]]:
    return [
        {"name": f, "type": FOCUS_TYPES.get(f, ("STRING", ""))[0],
         "mode": FOCUS_TYPES.get(f, ("", "NULLABLE"))[1]}
        for f in fields
    ]  # fmt: skip


def _err(status: int, reason: str, info: str | None = None) -> httpx.Response:
    """A ``google.rpc.Status`` error as Google's REST APIs send it: ``code``, ``message`` and
    ``status``, BigQuery's and Compute's ``errors[].reason``, and an ErrorInfo for ``info``."""
    error: dict[str, Any] = {
        "code": status,
        "message": reason,
        "errors": [{"message": reason, "domain": "global", "reason": reason}],
        "status": CANONICAL.get(status, "UNKNOWN"),
    }
    if info:
        error["details"] = [
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": info,
             "domain": "googleapis.com"}
        ]  # fmt: skip
    return httpx.Response(status, json={"error": error})


def install(fake: FakeGcp) -> None:
    """Point the GCP plug-in at the fake (and give it fake AWS broker credentials)."""
    from cloudoptics.providers.gcp import client
    from cloudoptics.providers.gcp.settings import get_gcp_settings

    settings = get_gcp_settings()
    settings.workload_provider = WORKLOAD_PROVIDER
    settings.service_account_project = SA_PROJECT
    settings.query_project = QUERY_PROJECT
    settings.retry_base_seconds = 0  # retries without waiting
    client.set_transport(fake.transport)
    client.set_aws_credentials(
        lambda: client.AwsCredentials("AKIDEXAMPLE", "fake-secret-key", "fake-session-token")
    )


def uninstall() -> None:
    from cloudoptics.providers.gcp import client
    from cloudoptics.providers.gcp.settings import get_gcp_settings

    settings = get_gcp_settings()
    settings.workload_provider = ""
    settings.service_account_project = ""
    settings.query_project = ""
    settings.retry_base_seconds = 1.0
    client.set_transport(None)
    client.set_aws_credentials(None)
