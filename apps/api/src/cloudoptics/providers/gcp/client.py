"""Google Cloud access without stored secrets (ADR-019): Workload Identity Federation from
Cloudoptics' AWS broker identity, then the Cloudoptics service account, then plain REST.

1. The broker's short-lived AWS credentials (Roles Anywhere, ADR-007) sign an
   ``sts:GetCallerIdentity`` request. The signed request, not the credentials, goes to Google.
2. Google STS checks it with AWS and returns a federated token for our workload identity pool
   (``CLOUDOPTICS_GCP_WORKLOAD_PROVIDER``), whose attribute condition admits only our broker
   role.
3. The federated token impersonates ``CLOUDOPTICS_GCP_SERVICE_ACCOUNT`` for one hour
   (``iamcredentials.generateAccessToken``). Customers grant that service account read roles.

The token is cached until five minutes before it expires. REST over httpx keeps Google's
SDKs (google-auth, google-cloud-bigquery and their gRPC stacks) out of the image; the few
calls Cloudoptics makes are small JSON requests. Tests and the E2E server swap the transport
(``set_transport``) and the AWS credentials (``set_aws_credentials``) for fakes.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from cloudoptics.providers.gcp.settings import GcpSettings, get_gcp_settings

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
REFRESH_BEFORE_SECONDS = 300
PAGE_ROWS = 10_000


class GcpError(Exception):
    """A Google API answered with an error, or could not be reached."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def denied(self) -> bool:
        return self.status == 403 or self.code in {"PERMISSION_DENIED", "accessDenied"}

    @property
    def not_found(self) -> bool:
        return self.status == 404 or self.code in {"NOT_FOUND", "notFound"}

    @property
    def api_disabled(self) -> bool:
        return self.code in {"accessNotConfigured", "SERVICE_DISABLED"}


class NotConfiguredError(GcpError):
    def __init__(self) -> None:
        super().__init__(0, "GcpNotConfigured", "Google Cloud access is not set up")


@dataclass(frozen=True)
class AwsCredentials:
    access_key: str
    secret_key: str
    token: str | None


AwsCredentialSource = Callable[[], AwsCredentials]


def _broker_credentials() -> AwsCredentials:
    """The broker's own AWS credentials, through the connector's session factory."""
    from cloudoptics.connectors.service import get_session_factory

    settings = get_gcp_settings()
    session = get_session_factory().broker_session()
    if settings.federation_role_arn:
        creds = session.client("sts", region_name=settings.aws_region).assume_role(
            RoleArn=settings.federation_role_arn,
            RoleSessionName="cloudoptics-gcp",
            DurationSeconds=3600,
        )["Credentials"]
        return AwsCredentials(creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"])
    frozen = session.get_credentials().get_frozen_credentials()
    return AwsCredentials(frozen.access_key, frozen.secret_key, frozen.token)


_transport: httpx.AsyncBaseTransport | None = None
_aws_credentials: AwsCredentialSource = _broker_credentials
_token: tuple[str, float] | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Tests and the E2E server: answer Google's APIs with a fake."""
    global _transport, _token
    _transport, _token = transport, None


def set_aws_credentials(source: AwsCredentialSource | None) -> None:
    global _aws_credentials, _token
    _aws_credentials, _token = source or _broker_credentials, None


def reset_token() -> None:
    global _token
    _token = None


def signed_caller_identity(creds: AwsCredentials, audience: str, region: str) -> str:
    """The subject token Google STS accepts from AWS: a SigV4-signed GetCallerIdentity
    request, as URL-encoded JSON (Google's AWS workload identity format)."""
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials

    host = f"sts.{region}.amazonaws.com"
    url = f"https://{host}?Action=GetCallerIdentity&Version=2011-06-15"
    request = AWSRequest(
        method="POST", url=url, headers={"host": host, "x-goog-cloud-target-resource": audience}
    )
    SigV4Auth(Credentials(creds.access_key, creds.secret_key, creds.token), "sts", region).add_auth(
        request
    )
    headers = [{"key": k, "value": v} for k, v in sorted(request.headers.items())]
    return quote(json.dumps({"url": url, "method": "POST", "headers": headers}), safe="")


def _error(response: httpx.Response) -> GcpError:
    try:
        body = response.json()
    except ValueError:
        return GcpError(response.status_code, f"HTTP{response.status_code}", response.text[:200])
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, str):  # OAuth-style errors (STS)
        return GcpError(response.status_code, err, str(body.get("error_description") or err))
    err = err or {}
    reasons = [e.get("reason") for e in err.get("errors") or [] if e.get("reason")]
    details = [d.get("reason") for d in err.get("details") or [] if d.get("reason")]
    code = (details or reasons or [err.get("status") or f"HTTP{response.status_code}"])[0]
    return GcpError(response.status_code, str(code), str(err.get("message") or code)[:500])


class GcpApi:
    """The Google APIs Cloudoptics calls, as the Cloudoptics service account."""

    def __init__(self, settings: GcpSettings | None = None) -> None:
        self.settings = settings or get_gcp_settings()
        self.bytes_billed = 0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=_transport, timeout=self.settings.http_timeout_seconds)

    async def token(self) -> str:
        global _token
        s = self.settings
        if not s.enabled:
            raise NotConfiguredError()
        if _token is not None and _token[1] - time.time() > REFRESH_BEFORE_SECONDS:
            return _token[0]
        from starlette.concurrency import run_in_threadpool

        try:
            creds = await run_in_threadpool(_aws_credentials)
        except Exception as exc:  # broker credentials: botocore, Roles Anywhere
            raise GcpError(0, "AwsCredentials", type(exc).__name__) from exc
        subject = signed_caller_identity(creds, s.workload_provider, s.aws_region)
        async with self._client() as http:
            try:
                sts = await http.post(
                    s.sts_url,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                        "audience": s.workload_provider,
                        "scope": SCOPE,
                        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                        "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
                        "subject_token": subject,
                    },
                )
                if sts.status_code != 200:
                    raise _error(sts)
                federated = sts.json()["access_token"]
                sa = quote(s.service_account, safe="@.")
                impersonated = await http.post(
                    f"{s.iamcredentials_url}/projects/-/serviceAccounts/{sa}:generateAccessToken",
                    headers={"Authorization": f"Bearer {federated}"},
                    json={"scope": [SCOPE], "lifetime": "3600s"},
                )
            except httpx.HTTPError as exc:
                raise GcpError(0, "Unreachable", type(exc).__name__) from exc
            if impersonated.status_code != 200:
                raise _error(impersonated)
            _token = (impersonated.json()["accessToken"], time.time() + 3600)
        return _token[0]

    async def request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None, body: Any = None
    ) -> dict[str, Any]:
        token = await self.token()
        async with self._client() as http:
            try:
                response = await http.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise GcpError(0, "Unreachable", type(exc).__name__) from exc
        if response.status_code >= 400:
            raise _error(response)
        data: dict[str, Any] = response.json() if response.content else {}
        return data

    async def pages(
        self, url: str, key: str, params: dict[str, Any] | None = None, *, limit: int = 50
    ) -> AsyncIterator[dict[str, Any]]:
        """Items under ``key`` across pages (``nextPageToken``), at most ``limit`` pages."""
        params = dict(params or {})
        for _ in range(limit):
            page = await self.request("GET", url, params=params)
            for item in page.get(key) or []:
                yield item
            token = page.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    async def aggregated(
        self, url: str, kind: str, *, limit: int = 50
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Compute ``aggregatedList``: ``(scope, item)`` for every zone or region, where
        scope is ``zones/<zone>`` or ``regions/<region>``."""
        params: dict[str, Any] = {"returnPartialSuccess": "true"}
        for _ in range(limit):
            page = await self.request("GET", url, params=params)
            for scope, scoped in (page.get("items") or {}).items():
                for item in scoped.get(kind) or []:
                    yield scope, item
            token = page.get("nextPageToken")
            if not token:
                return
            params["pageToken"] = token

    # --- Resource Manager ---------------------------------------------------------------

    async def test_permissions(self, project: str, permissions: list[str]) -> set[str]:
        """The subset of ``permissions`` Cloudoptics holds on the project (no role needed)."""
        data = await self.request(
            "POST",
            f"{self.settings.resourcemanager_url}/projects/{quote(project)}:testIamPermissions",
            body={"permissions": permissions},
        )
        return set(data.get("permissions") or [])

    # --- BigQuery -----------------------------------------------------------------------

    async def dataset(self, project: str, dataset: str) -> dict[str, Any]:
        return await self.request(
            "GET",
            f"{self.settings.bigquery_url}/projects/{quote(project)}/datasets/{quote(dataset)}",
        )

    async def table(self, project: str, dataset: str, table: str) -> dict[str, Any]:
        return await self.request(
            "GET",
            f"{self.settings.bigquery_url}/projects/{quote(project)}/datasets/{quote(dataset)}"
            f"/tables/{quote(table)}",
        )

    async def query(
        self,
        project: str,
        sql: str,
        params: list[dict[str, Any]] | None = None,
        *,
        location: str | None = None,
        dry_run: bool = False,
        max_rows: int | None = None,
    ) -> QueryResult:
        """Run a standard-SQL query in ``project`` (which pays for it), capped at
        ``max_bytes_billed``; every page of rows, decoded by the result schema."""
        body: dict[str, Any] = {
            "query": sql,
            "useLegacySql": False,
            "parameterMode": "NAMED",
            "queryParameters": params or [],
            "maximumBytesBilled": str(self.settings.max_bytes_billed),
            "maxResults": PAGE_ROWS,
            "timeoutMs": 30_000,
            "dryRun": dry_run,
            "labels": {"cloudoptics": "billing"},
        }
        if location:
            body["location"] = location
        url = f"{self.settings.bigquery_url}/projects/{quote(project)}/queries"
        page = await self.request("POST", url, body=body)
        processed = int(page.get("totalBytesProcessed") or 0)
        if dry_run:
            return QueryResult([], processed, 0)
        job = page.get("jobReference") or {}
        rows: list[dict[str, Any]] = []
        schema: list[dict[str, Any]] = []
        for _ in range(10_000):
            if page.get("jobComplete", True):
                schema = (page.get("schema") or {}).get("fields") or schema
                rows.extend(decode_rows(schema, page.get("rows") or []))
                if max_rows is not None and len(rows) > max_rows:
                    raise GcpError(0, "TooManyRows", f"more than {max_rows} rows")
                if not page.get("pageToken"):
                    break
            params_: dict[str, Any] = {"maxResults": PAGE_ROWS, "timeoutMs": 30_000}
            if job.get("location"):
                params_["location"] = job["location"]
            if page.get("pageToken"):
                params_["pageToken"] = page["pageToken"]
            page = await self.request(
                "GET", f"{url}/{quote(str(job.get('jobId', '')))}", params=params_
            )
        billed = int(page.get("totalBytesBilled") or processed)
        self.bytes_billed += billed
        return QueryResult(rows, processed, billed)


@dataclass(frozen=True)
class QueryResult:
    rows: list[dict[str, Any]]
    bytes_processed: int
    bytes_billed: int


def param(name: str, kind: str, value: Any) -> dict[str, Any]:
    return {
        "name": name,
        "parameterType": {"type": kind},
        "parameterValue": {"value": None if value is None else str(value)},
    }


def _decode(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind in ("INTEGER", "INT64"):
        return int(value)
    if kind in ("FLOAT", "FLOAT64", "NUMERIC", "BIGNUMERIC"):
        return Decimal(str(value))
    if kind in ("BOOLEAN", "BOOL"):
        return str(value).lower() == "true"
    if kind == "DATE":
        return date.fromisoformat(str(value))
    return value


def decode_rows(schema: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """BigQuery's ``{"f": [{"v": ...}]}`` rows as dicts of Python values (scalar columns)."""
    names = [(f["name"], str(f.get("type", "STRING")).upper()) for f in schema]
    return [
        {
            name: _decode(kind, cell.get("v"))
            for (name, kind), cell in zip(names, r["f"], strict=True)
        }
        for r in rows
    ]


def bq_cost_usd(bytes_billed: int) -> float:
    from cloudoptics.providers.gcp.settings import BQ_USD_PER_TIB

    return round(bytes_billed / 1024**4 * BQ_USD_PER_TIB, 4)
