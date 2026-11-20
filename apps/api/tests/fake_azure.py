"""A stand-in for every Azure API the Azure plug-in calls (ADR-020), for tests and the E2E
stack, as an ``httpx.MockTransport`` handler. Nothing here contacts Azure.

It plays Entra ID (the token endpoint, which checks the client assertion's signature,
audience, issuer, expiry and certificate thumbprint like the real one), Resource Manager
(subscriptions, billing accounts, storage accounts, Cost Management exports and queries,
Resource Graph, Advisor, Monitor) and Blob Storage. Every permission is a set the test can
change (``roles``), so each validation check can be made to fail on its own. A call it does
not know answers 403 ``AuthorizationFailed``, as Azure would without the role.

``focus_export`` writes a synthetic Cost Management FOCUS export (manifest and Parquet or
gzipped CSV) for one subscription and month: a running VM with Advisor resize advice, a VM
stopped but not deallocated, an unattached Premium disk, an unused public IP, an old
snapshot of a deleted disk, storage and bandwidth, and a credit.
"""

from __future__ import annotations

import base64
import gzip
import io
import json
import re
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from xml.sax.saxutils import escape

import httpx
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

DIRECTORY = "8f1c2d3e-4b5a-4c6d-8e7f-901a2b3c4d5e"
SUBSCRIPTION = "1b2c3d4e-5f60-4a7b-8c9d-0e1f2a3b4c5d"
CLIENT_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
STORAGE = "costexports"
CONTAINER = "focus"
FOLDER = "cloudoptics"
EXPORT = "cloudoptics-focus"
GROUP = "rg-app"
REGION = "westeurope"
NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


def rid(kind: str, name: str, sub: str = SUBSCRIPTION) -> str:
    return f"/subscriptions/{sub}/resourceGroups/{GROUP}/providers/{kind}/{name}"


_IDS = {
    "vm_web": ("Microsoft.Compute/virtualMachines", "vm-web"),
    "vm_stopped": ("Microsoft.Compute/virtualMachines", "vm-stopped"),
    "disk_loose": ("Microsoft.Compute/disks", "disk-loose"),
    "disk_os": ("Microsoft.Compute/disks", "disk-web-os"),
    "pip_idle": ("Microsoft.Network/publicIPAddresses", "pip-idle"),
    "snap_old": ("Microsoft.Compute/snapshots", "snap-old"),
    "storage": ("Microsoft.Storage/storageAccounts", STORAGE),
}


@dataclass(frozen=True)
class Ids:
    """The fixture's resource IDs in one subscription (``Ids(sub).vm_web``)."""

    sub: str

    def __getattr__(self, name: str) -> str:
        if name not in _IDS:
            raise AttributeError(name)
        kind, res_name = _IDS[name]
        return rid(kind, res_name, self.sub)


VM_WEB = Ids(SUBSCRIPTION).vm_web


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_certificate(days: int = 30) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cloudoptics-test-azure")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    return key, cert


# --- the synthetic FOCUS export -----------------------------------------------------------


def focus_rows(
    period: date,
    through: date,
    *,
    sub: str = SUBSCRIPTION,
    currency: str = "EUR",
    usd_per_unit: float = 1.08,
) -> list[dict[str, Any]]:
    """One row per resource and day from the 1st to ``through``, plus one credit."""
    daily = [
        ("vm_web", "Virtual Machines", "Compute", "D4s v5", 4.80, '{"env": "prod", "app": "web"}'),
        ("vm_stopped", "Virtual Machines", "Compute", "D2s v5", 4.00, '"env": "dev"'),
        ("disk_loose", "Storage", "Storage", "P15 LRS Disk", 1.25, ""),
        ("disk_os", "Storage", "Storage", "P10 LRS Disk", 0.65, '{"env": "prod"}'),
        ("pip_idle", "Virtual Network", "Networking", "Standard IPv4 Static Public IP", 0.12, ""),
        ("snap_old", "Storage", "Storage", "Snapshots LRS", 0.40, ""),
        ("", "Bandwidth", "Networking", "Standard Data Transfer Out", 0.30, ""),
    ]
    ids = Ids(sub)
    daily = [(getattr(ids, r) if r else "", *rest) for r, *rest in daily]
    rows: list[dict[str, Any]] = []
    day = period
    while day <= through:
        start = datetime(day.year, day.month, day.day, tzinfo=UTC)
        for resource, meter, category, meter_name, cost, tags in daily:
            rows.append(
                _row(sub, start, resource.lower(), meter, category, meter_name, cost, tags,
                     currency, usd_per_unit, "Usage")
            )  # fmt: skip
        day += timedelta(days=1)
    rows.append(
        _row(sub, datetime(period.year, period.month, 2, tzinfo=UTC), "", "Azure Credit",
             "Other", "Credit", -5.0, "", currency, usd_per_unit, "Credit")
    )  # fmt: skip
    return rows


def _row(
    sub: str,
    start: datetime,
    resource: str,
    meter: str,
    category: str,
    meter_name: str,
    cost: float,
    tags: str,
    currency: str,
    usd_per_unit: float,
    charge: str,
) -> dict[str, Any]:
    end = start + timedelta(days=1)
    first = start.replace(day=1)
    return {
        "BillingAccountId": "/providers/Microsoft.Billing/billingAccounts/12345678",
        "BillingCurrency": currency,
        "BilledCost": round(cost, 6),
        "EffectiveCost": round(cost, 6),
        "ListCost": round(abs(cost) * 1.1, 6) if charge == "Usage" else 0.0,
        "BillingPeriodStart": first.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ChargeCategory": charge,
        "ChargeClass": "",
        "ChargeDescription": meter_name,
        "ChargePeriodStart": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ChargePeriodEnd": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ConsumedQuantity": 24.0 if charge == "Usage" else 0.0,
        "PricingUnit": "1 Hour",
        "ProviderName": "Microsoft",
        "RegionId": REGION if resource else "",
        "ResourceId": resource,
        "ServiceCategory": category,
        "ServiceName": meter if meter != "Virtual Network" else "Virtual Network",
        "SubAccountId": f"/subscriptions/{sub}",
        "SubAccountName": "Production",
        "Tags": tags,
        "x_BilledCostInUsd": round(cost * usd_per_unit, 6),
        "x_EffectiveCostInUsd": round(cost * usd_per_unit, 6),
        "x_SkuMeterCategory": meter,
        "x_SkuMeterName": meter_name,
    }


def rows_table(rows: list[dict[str, Any]]) -> pa.Table:
    return pa.Table.from_pylist(rows)


def encode(rows: list[dict[str, Any]], fmt: str) -> bytes:
    table = rows_table(rows)
    buffer = io.BytesIO()
    if fmt == "parquet":
        pq.write_table(table, buffer)
        return buffer.getvalue()
    pacsv.write_csv(table, buffer)
    return gzip.compress(buffer.getvalue()) if fmt == "csv.gz" else buffer.getvalue()


# --- the fake -----------------------------------------------------------------------------


@dataclass
class Storage:
    resource_id: str
    tags: dict[str, str] = field(default_factory=dict)
    blobs: dict[str, dict[str, bytes]] = field(default_factory=lambda: defaultdict(dict))


@dataclass
class FakeAzure:
    """State plus ``handler`` for ``httpx.MockTransport``."""

    certificate: x509.Certificate
    client_id: str = CLIENT_ID
    authority: str = "https://login.microsoftonline.com"
    directories: dict[str, bool] = field(default_factory=lambda: {DIRECTORY: True})
    # (scope, role) the app holds. Scopes are ARM IDs, lower case.
    roles: set[tuple[str, str]] = field(default_factory=set)
    storage: dict[str, Storage] = field(default_factory=dict)
    exports: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    resources: list[dict[str, Any]] = field(default_factory=list)
    advisor: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    cpu: dict[str, tuple[float, float]] = field(default_factory=dict)
    # month ("YYYY-MM") -> subscription -> (native, usd): what Cost Management's query answers
    totals: dict[str, dict[str, tuple[float, float]]] = field(default_factory=dict)
    currency: str = "EUR"
    # Random per fake: workspaces in one test database must not share a verified subscription.
    subscription: str = field(default_factory=lambda: str(uuid.uuid4()))
    calls: list[str] = field(default_factory=list)
    # Answer the next N Cost Management queries with 429 (QPU throttling).
    throttle_queries: int = 0
    tokens_issued: int = 0

    # --- set-up helpers -----------------------------------------------------------------

    @property
    def ids(self) -> Ids:
        return Ids(self.subscription)

    def grant_all(self, sub: str | None = None) -> None:
        sub = sub or self.subscription
        storage_id = self.ids.storage
        for role in ("Reader", "Cost Management Reader"):
            self.roles.add((f"/subscriptions/{sub}", role))
        self.roles.add((storage_id.lower(), "Reader"))
        self.roles.add((f"{storage_id}/blobServices/default/containers/{CONTAINER}".lower(),
                        "Storage Blob Data Reader"))  # fmt: skip

    def add_storage(self, code: str | None) -> Storage:
        store = Storage(self.ids.storage, {"cloudoptics-verify": code} if code else {})
        self.storage[STORAGE] = store
        return store

    def add_export(self, sub: str | None = None, *, folder: str = FOLDER) -> None:
        sub = sub or self.subscription
        # The shape of Exports - List (Cost Management 2026-06-01), FOCUS 1.0r2 dataset.
        scope = f"/subscriptions/{sub}"
        self.exports[scope.lower()].append(
            {
                "name": EXPORT,
                "type": "Microsoft.CostManagement/exports",
                "id": f"{scope}/providers/Microsoft.CostManagement/exports/{EXPORT}",
                "identity": {"type": "SystemAssigned", "principalId": str(uuid.uuid4()),
                             "tenantId": DIRECTORY},
                "location": "centralus",
                "properties": {
                    "format": "Parquet",
                    "compressionMode": "snappy",
                    "dataOverwriteBehavior": "OverwritePreviousReport",
                    "definition": {
                        "type": "FocusCost",
                        "dataSet": {"configuration": {"dataVersion": "1.0r2"},
                                    "granularity": "Daily"},
                        "timeframe": "MonthToDate",
                    },
                    "deliveryInfo": {
                        "destination": {
                            "type": "AzureBlob",
                            "container": CONTAINER,
                            "resourceId": self.ids.storage,
                            "rootFolderPath": folder,
                        }
                    },
                    "partitionData": True,
                },
            }
        )  # fmt: skip

    def add_resources(self, now: datetime = NOW) -> None:
        old = (now - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent = (now - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%SZ")

        def res(id_: str, kind: str, props: dict[str, Any], **extra: Any) -> dict[str, Any]:
            return {
                "id": id_,
                "name": id_.rsplit("/", 1)[1],
                "type": kind,
                "location": REGION,
                "subscriptionId": self.subscription,
                "resourceGroup": GROUP,
                "tags": extra.pop("tags", {}),
                "sku": extra.pop("sku", None),
                "managedBy": extra.pop("managedBy", None),
                "properties": props,
            }

        def vm(state: str, size: str) -> dict[str, Any]:
            return {
                "hardwareProfile": {"vmSize": size},
                "timeCreated": recent,
                "extended": {"instanceView": {"powerState": {"code": f"PowerState/{state}"}}},
            }

        ids = self.ids
        gone = rid("Microsoft.Compute/disks", "gone", self.subscription)
        self.resources = [
            res(ids.vm_web, "Microsoft.Compute/virtualMachines", vm("running", "Standard_D4s_v5"),
                tags={"env": "prod"}),
            res(ids.vm_stopped, "Microsoft.Compute/virtualMachines",
                vm("stopped", "Standard_D2s_v5"), tags={"env": "dev"}),
            res(ids.disk_loose, "Microsoft.Compute/disks",
                {"diskState": "Unattached", "diskSizeGB": 256, "timeCreated": recent},
                sku={"name": "Premium_LRS"}),
            res(ids.disk_os, "Microsoft.Compute/disks",
                {"diskState": "Attached", "diskSizeGB": 128, "timeCreated": recent},
                sku={"name": "Premium_LRS"}, managedBy=ids.vm_web),
            res(ids.pip_idle, "Microsoft.Network/publicIPAddresses",
                {"ipAddress": "20.1.2.3", "publicIPAllocationMethod": "Static"},
                sku={"name": "Standard"}),
            res(ids.snap_old, "Microsoft.Compute/snapshots",
                {"diskSizeGB": 64, "timeCreated": old, "provisioningState": "Succeeded",
                 "creationData": {"sourceResourceId": gone}}),
        ]  # fmt: skip
        self.advisor[self.subscription] = [
            {
                # Recommendations - List (Advisor 2026-10-01). extendedProperties is an open
                # object in the API; these keys are what Advisor returns for this type.
                "id": f"{ids.vm_web}/providers/Microsoft.Advisor/recommendations/adv-1",
                "name": "adv-1",
                "type": "Microsoft.Advisor/recommendations",
                "properties": {
                    "category": "Cost",
                    "impact": "High",
                    "impactedField": "Microsoft.Compute/virtualMachines",
                    "impactedValue": "vm-web",
                    "recommendationStatus": "New",
                    "lastUpdated": "2026-11-19T06:00:00Z",
                    "recommendationTypeId": "e10b1381-5f0a-47ff-8c7b-37bd13d7c974",
                    "shortDescription": {
                        "problem": "Right-size or shutdown underutilized virtual machines",
                        "solution": "Right-size or shutdown underutilized virtual machines",
                    },
                    "resourceMetadata": {"resourceId": ids.vm_web},
                    "extendedProperties": {
                        "currentSku": "Standard_D4s_v5",
                        "targetSku": "Standard_D2s_v5",
                        "savingsAmount": "70",
                        "savingsCurrency": "USD",
                    },
                },
            }
        ]
        self.cpu[ids.vm_web.lower()] = (3.0, 6.0)

    def deliver(
        self,
        period: date,
        through: date,
        *,
        fmt: str = "parquet",
        run_id: str | None = None,
        submitted: datetime | None = None,
        rows: list[dict[str, Any]] | None = None,
        manifest_rows: int | None = None,
    ) -> str:
        """One export run into the container; returns its run ID."""
        rows = (
            rows
            if rows is not None
            else focus_rows(period, through, sub=self.subscription, currency=self.currency)
        )
        run_id = run_id or str(uuid.uuid4())
        month_end = (period + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        folder = f"{FOLDER}/{EXPORT}/{period:%Y%m%d}-{month_end:%Y%m%d}/{run_id}"
        name = f"{folder}/part_0_0001.{fmt}"
        blobs = self.storage[STORAGE].blobs[CONTAINER]
        blobs[name] = encode(rows, fmt)
        # The documented manifest.json (improved exports), field for field.
        manifest = {
            "manifestVersion": "2024-04-01",
            "byteCount": len(blobs[name]),
            "blobCount": 1,
            "dataRowCount": len(rows) if manifest_rows is None else manifest_rows,
            "exportConfig": {
                "exportName": EXPORT,
                "resourceId": f"/subscriptions/{self.subscription}/providers/"
                f"Microsoft.CostManagement/exports/{EXPORT}",
                "dataVersion": "1.0r2",
                "apiVersion": "2026-06-01",
                "type": "FocusCost",
                "timeFrame": "MonthToDate",
                "granularity": "Daily",
            },
            "deliveryConfig": {
                "partitionData": True,
                "dataOverwriteBehavior": "OverwritePreviousReport",
                "fileFormat": "Parquet" if fmt == "parquet" else "Csv",
                "compressionMode": "snappy"
                if fmt == "parquet"
                else ("gzip" if fmt == "csv.gz" else "None"),
                "containerUri": self.ids.storage,
                "rootFolderPath": FOLDER,
            },
            "runInfo": {
                "executionType": "Scheduled",
                "runId": run_id,
                "submittedTime": (submitted or datetime.now(UTC)).isoformat(),
                "startDate": f"{period:%Y-%m-%d}T00:00:00",
                "endDate": f"{month_end:%Y-%m-%d}T00:00:00",
            },
            "blobs": [{"blobName": name, "byteCount": len(blobs[name]), "dataRowCount": len(rows)}],
        }
        blobs[f"{folder}/manifest.json"] = json.dumps(manifest).encode()
        totals = self.totals.setdefault(f"{period:%Y-%m}", {})
        native = sum(r["BilledCost"] for r in rows)
        usd = sum(r.get("x_BilledCostInUsd", r["BilledCost"]) for r in rows)
        sub = rows[0]["SubAccountId"].rsplit("/", 1)[1] if rows else self.subscription
        totals[sub] = (native, usd)
        return run_id

    # --- the transport ------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        self.calls.append(f"{request.method} {url.host}{url.path}")
        if url.host == urlsplit(self.authority).hostname:
            return self._token(request)
        if url.host.endswith(".blob.core.windows.net"):
            return self._blob(request)
        return self._arm(request)

    def _deny(self, message: str = "The client does not have authorization") -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"code": "AuthorizationFailed", "message": message}}
        )

    def _has(self, scope: str, role: str) -> bool:
        scope = scope.lower()
        return any(r == role and (scope == s or scope.startswith(s + "/")) for s, r in self.roles)

    def _caller(self, request: httpx.Request, resource: str) -> str | None:
        auth = request.headers.get("authorization", "")
        match = re.fullmatch(r"Bearer tok\|([0-9a-f-]+)\|(.+)", auth)
        if not match or match.group(2) != resource:
            return None
        return match.group(1)

    def _token(self, request: httpx.Request) -> httpx.Response:
        directory = request.url.path.strip("/").split("/")[0]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}

        def refuse(code: str, text: str) -> httpx.Response:
            # The documented error body: error, error_description, error_codes, trace IDs.
            return httpx.Response(
                400,
                json={
                    "error": "invalid_request",
                    "error_description": f"AADSTS{code}: {text}\r\nTrace ID: x\r\n"
                    "Correlation ID: y\r\nTimestamp: 2026-11-20 10:00:00Z",
                    "error_codes": [int(code)],
                    "timestamp": "2026-11-20 10:00:00Z",
                    "trace_id": "x",
                    "correlation_id": "y",
                },
            )

        if directory not in self.directories:
            return refuse("90002", f"Tenant '{directory}' not found.")
        if form.get("client_id") != self.client_id:
            return refuse("700016", "Application not found.")
        if (
            form.get("client_assertion_type")
            != "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
        ):
            return refuse("7000215", "Invalid client secret provided.")
        problem = self._check_assertion(form.get("client_assertion", ""), str(request.url))
        if problem:
            return refuse("700027", problem)
        if not self.directories[directory]:
            return refuse("700016", f"Application with identifier '{self.client_id}' was not "
                          f"found in the directory '{directory}'.")  # fmt: skip
        resource = form.get("scope", "").removesuffix("/.default")
        self.tokens_issued += 1
        return httpx.Response(
            200,
            json={"access_token": f"tok|{directory}|{resource}", "expires_in": 3599,
                  "token_type": "Bearer"},
        )  # fmt: skip

    def _check_assertion(self, assertion: str, url: str) -> str | None:
        try:
            head, body, sig = assertion.split(".")
            header = json.loads(b64url_decode(head))
            claims = json.loads(b64url_decode(body))
        except ValueError:
            return "Client assertion is not a JWT."
        if header.get("alg") != "PS256":
            return "Unsupported alg."
        if header.get("x5t#S256") != b64url(self.certificate.fingerprint(hashes.SHA256())):
            return "Certificate thumbprint not registered on the app."
        public = self.certificate.public_key()
        assert isinstance(public, rsa.RSAPublicKey)
        try:
            public.verify(
                b64url_decode(sig),
                f"{head}.{body}".encode(),
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                hashes.SHA256(),
            )
        except InvalidSignature:
            return "Signature verification failed."
        now = time.time()
        if claims.get("aud") != url.split("?")[0]:
            return "Wrong audience."
        if claims.get("iss") != self.client_id or claims.get("sub") != self.client_id:
            return "Wrong issuer."
        if not (claims.get("nbf", 0) <= now < claims.get("exp", 0)) or claims["exp"] - now > 3600:
            return "Assertion expired or too long."
        if not claims.get("jti"):
            return "No jti."
        return None

    def _arm(self, request: httpx.Request) -> httpx.Response:
        directory = self._caller(request, "https://management.azure.com")
        if directory is None:
            return httpx.Response(
                401, json={"error": {"code": "InvalidAuthenticationToken", "message": "x"}}
            )
        path = request.url.path.rstrip("/")
        lowered = path.lower()
        body = json.loads(request.content) if request.content else {}
        if m := re.fullmatch(r"/subscriptions/([0-9a-f-]+)", lowered):
            if not self._has(path, "Reader"):
                return self._deny()
            return httpx.Response(
                200,
                json={
                    "id": f"/subscriptions/{m.group(1)}",
                    "subscriptionId": m.group(1),
                    "tenantId": DIRECTORY,
                    "displayName": "Production",
                    "state": "Enabled",
                    "authorizationSource": "RoleBased",
                },
            )
        if (
            lowered.startswith("/providers/microsoft.billing/billingaccounts/")
            and lowered.count("/") == 4
        ):
            return httpx.Response(200, json={"name": path.rsplit("/", 1)[1]}) if self._has(
                path, "Billing account reader") else self._deny()  # fmt: skip
        if lowered.endswith("/providers/microsoft.costmanagement/exports"):
            scope = path[: -len("/providers/Microsoft.CostManagement/exports")]
            if not self._has(scope, "Cost Management Reader"):
                return self._deny()
            return httpx.Response(200, json={"value": self.exports.get(scope.lower(), [])})
        if lowered.endswith("/providers/microsoft.costmanagement/query"):
            scope = path[: -len("/providers/Microsoft.CostManagement/query")]
            if not self._has(scope, "Cost Management Reader"):
                return self._deny()
            if self.throttle_queries:
                # Cost Management's QPU limit: 429 and how long to back off.
                self.throttle_queries -= 1
                return httpx.Response(
                    429,
                    headers={"x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after": "0"},
                    json={"error": {"code": "429", "message": "Too many requests."}},
                )
            return httpx.Response(
                200,
                json={
                    "id": f"{scope}/providers/Microsoft.CostManagement/query/{uuid.uuid4()}",
                    "name": str(uuid.uuid4()),
                    "type": "Microsoft.CostManagement/query",
                    "properties": self._query(body),
                },
            )
        if lowered == "/providers/microsoft.resourcegraph/resources":
            subs = [s.lower() for s in body.get("subscriptions", [])]
            if not all(self._has(f"/subscriptions/{s}", "Reader") for s in subs):
                return self._deny()
            data = [r for r in self.resources if r["subscriptionId"].lower() in subs]
            return httpx.Response(
                200,
                json={
                    "totalRecords": len(data),
                    "count": len(data),
                    "resultTruncated": "false",
                    "facets": [],
                    "data": data,
                },
            )
        if m := re.fullmatch(
            r"/subscriptions/([0-9a-f-]+)/providers/microsoft\.advisor/recommendations", lowered
        ):
            if not self._has(f"/subscriptions/{m.group(1)}", "Reader"):
                return self._deny()
            return httpx.Response(200, json={"value": self.advisor.get(m.group(1), [])})
        if lowered.endswith("/providers/microsoft.insights/metrics"):
            vm = path[: -len("/providers/Microsoft.Insights/metrics")]
            if not self._has(vm, "Reader"):
                return self._deny()
            return httpx.Response(200, json=self._metrics(vm.lower(), request))
        for store in self.storage.values():
            if lowered == store.resource_id.lower():
                if not self._has(path, "Reader"):
                    return self._deny()
                return httpx.Response(
                    200,
                    json={
                        "id": store.resource_id,
                        "name": STORAGE,
                        "type": "Microsoft.Storage/storageAccounts",
                        "location": REGION,
                        "kind": "StorageV2",
                        "sku": {"name": "Standard_LRS", "tier": "Standard"},
                        "tags": store.tags,
                        "properties": {"provisioningState": "Succeeded"},
                    },
                )
        return self._deny(f"Unknown call {request.method} {path}")

    def _query(self, body: dict[str, Any]) -> dict[str, Any]:
        dataset = body.get("dataset") or {}
        if body.get("timeframe") == "MonthToDate":
            month = f"{datetime.now(UTC):%Y-%m}"
            native, usd = (0.0, 0.0)
            for n, u in self.totals.get(month, {}).values():
                native, usd = native + n, usd + u
            day = int(f"{datetime.now(UTC):%Y%m%d}")
            return {
                "columns": [{"name": "Cost"}, {"name": "CostUSD"}, {"name": "UsageDate"},
                            {"name": "ServiceName"}, {"name": "Currency"}],
                "rows": [[native, usd, day, "Virtual Machines", self.currency]] if native else [],
            }  # fmt: skip
        month = str(body["timePeriod"]["from"])[:7]
        assert dataset.get("granularity") == "None"
        return {
            "columns": [{"name": "Cost"}, {"name": "SubscriptionId"}, {"name": "Currency"}],
            "rows": [[n, s, self.currency] for s, (n, _) in self.totals.get(month, {}).items()],
        }

    def _metrics(self, vm: str, request: httpx.Request) -> dict[str, Any]:
        avg, peak = self.cpu.get(vm, (40.0, 90.0))
        span = request.url.params.get("timespan", "")
        start = datetime.fromisoformat(span.split("/")[0].replace("Z", "+00:00"))
        points = [
            {"timeStamp": (start + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "average": avg, "maximum": peak}
            for h in range(14 * 24)
        ]  # fmt: skip
        # Metrics - List (Monitor 2023-10-01).
        return {
            "cost": 0,
            "timespan": span,
            "interval": "PT1H",
            "value": [
                {
                    "id": f"{vm}/providers/Microsoft.Insights/metrics/Percentage CPU",
                    "type": "Microsoft.Insights/metrics",
                    "name": {"value": "Percentage CPU", "localizedValue": "Percentage CPU"},
                    "unit": "Percent",
                    "timeseries": [{"metadatavalues": [], "data": points}],
                    "errorCode": "Success",
                }
            ],
            "namespace": "Microsoft.Compute/virtualMachines",
            "resourceregion": REGION,
        }

    def _blob(self, request: httpx.Request) -> httpx.Response:
        if self._caller(request, "https://storage.azure.com") is None:
            return httpx.Response(401, headers={"x-ms-error-code": "InvalidAuthenticationInfo"})
        account = request.url.host.split(".")[0]
        store = self.storage.get(account)
        parts = request.url.path.lstrip("/").split("/", 1)
        container = parts[0]
        if store is None:
            return httpx.Response(404, headers={"x-ms-error-code": "ResourceNotFound"})
        scope = f"{store.resource_id}/blobServices/default/containers/{container}"
        if not self._has(scope, "Storage Blob Data Reader"):
            return httpx.Response(
                403, headers={"x-ms-error-code": "AuthorizationPermissionMismatch"}
            )
        blobs = store.blobs.get(container, {})
        if len(parts) == 1 or not parts[1]:
            prefix = request.url.params.get("prefix", "")
            items = "".join(
                f"<Blob><Name>{escape(n)}</Name><Properties><Last-Modified>Wed, 20 Nov 2026 "
                f"06:00:00 GMT</Last-Modified><Content-Length>{len(b)}</Content-Length>"
                "<BlobType>BlockBlob</BlobType></Properties></Blob>"
                for n, b in sorted(blobs.items())
                if n.startswith(prefix)
            )
            xml = (
                '<?xml version="1.0" encoding="utf-8"?><EnumerationResults '
                f'ServiceEndpoint="https://{account}.blob.core.windows.net/" '
                f'ContainerName="{container}"><Prefix>{escape(prefix)}</Prefix>'
                f"<Blobs>{items}</Blobs><NextMarker /></EnumerationResults>"
            )
            return httpx.Response(
                200, content=xml.encode(), headers={"content-type": "application/xml"}
            )
        name = unquote(parts[1])
        if name not in blobs:
            return httpx.Response(404, headers={"x-ms-error-code": "BlobNotFound"})
        return httpx.Response(200, content=blobs[name])


def install() -> tuple[FakeAzure, Any]:
    """A fake with its certificate, wired into the plug-in. Returns (fake, undo)."""
    from cloudoptics.platform.settings import get_settings
    from cloudoptics.providers.azure import client
    from cloudoptics.providers.azure.credentials import CertificateAssertion

    key, cert = new_certificate()
    fake = FakeAzure(certificate=cert)
    settings = get_settings()
    before = settings.azure_client_id
    settings.azure_client_id = CLIENT_ID
    client.set_transport(fake.transport(), CertificateAssertion(key, cert))

    def undo() -> None:
        settings.azure_client_id = before
        client.set_transport(None)

    return fake, undo
