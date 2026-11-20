"""Google Cloud access without stored secrets (ADR-019): Workload Identity Federation from
Cloudoptics' AWS broker identity, then the connection's own service account, then plain REST.

1. The broker's short-lived AWS credentials (Roles Anywhere, ADR-007) sign an
   ``sts:GetCallerIdentity`` request. The signed request, not the credentials, goes to Google.
2. Google STS checks it with AWS and returns a federated token for our workload identity pool
   (``CLOUDOPTICS_GCP_WORKLOAD_PROVIDER``), whose attribute condition admits only our broker
   role. With it alone, Cloudoptics creates and deletes the per-connection service accounts in
   its own project (IAM API, ``serviceAccounts.create`` / ``delete``).
3. To read a customer's data, the federated token impersonates **that connection's** service
   account for one hour (``iamcredentials.generateAccessToken``). Customers share their data
   with that account only, so one workspace's token cannot read another's data.

Tokens are cached (the federated one, and one per service account) until five minutes
before they expire. Transient errors (429, 5xx, and BigQuery's ``rateLimitExceeded`` /
``backendError``, which BigQuery returns as 403) are retried with truncated exponential
backoff; jobs.query carries a ``requestId`` so a retried query is not run twice. REST over
httpx keeps Google's SDKs (google-auth, google-cloud-bigquery and their gRPC stacks) out of
the image; the few calls Cloudoptics makes are small JSON requests. Tests and the E2E server
swap the transport (``set_transport``) and the AWS credentials (``set_aws_credentials``).
"""

from __future__ import annotations

import asyncio
import json
import random
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from cloudoptics.providers.gcp.settings import GcpSettings, get_gcp_settings

SCOPE = "https://www.googleapis.com/auth/cloud-platform"
REFRESH_BEFORE_SECONDS = 300
PAGE_ROWS = 10_000
#: HTTP statuses worth retrying (AIP-194: UNAVAILABLE; 429 RESOURCE_EXHAUSTED for rate
#: limits; 500/502/504 from front ends), and BigQuery's transient reasons. BigQuery returns
#: rate and quota errors as 403, so a 403 is "denied" only when its reason is not one of these.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_REASONS = frozenset(
    {"rateLimitExceeded", "backendError", "internalError", "RATE_LIMIT_EXCEEDED"}
)
QUOTA_REASONS = frozenset({"rateLimitExceeded", "quotaExceeded", "RATE_LIMIT_EXCEEDED"})


class GcpError(Exception):
    """A Google API answered with an error, or could not be reached."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message

    @property
    def denied(self) -> bool:
        if self.code in QUOTA_REASONS or self.api_disabled:
            return False
        return self.status == 403 or self.code in {"PERMISSION_DENIED", "accessDenied"}

    @property
    def retryable(self) -> bool:
        return self.status in RETRY_STATUSES or self.code in RETRY_REASONS

    @property
    def not_found(self) -> bool:
        return self.status == 404 or self.code in {"NOT_FOUND", "notFound"}

    @property
    def api_disabled(self) -> bool:
        return self.code in {"accessNotConfigured", "SERVICE_DISABLED"}

    @property
    def quota(self) -> bool:
        """Out of a quota: a count (service accounts per project) or a rate."""
        text = self.message.lower()
        return (
            self.status == 429
            or self.code in QUOTA_REASONS
            or self.code in {"RESOURCE_EXHAUSTED", "quotaExceeded"}
            or "quota" in text
            or "maximum number" in text
        )


class NotConfiguredError(GcpError):
    def __init__(self) -> None:
        super().__init__(0, "GcpNotConfigured", "Google Cloud access is not set up")


class NoServiceAccountError(GcpError):
    """A connection made before per-connection accounts (label era): connect it again."""

    def __init__(self) -> None:
        super().__init__(0, "ReconnectNeeded", "This connection has no service account")


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
#: The broker's federated token, and one access token per service account (email -> token).
_federated: tuple[str, float] | None = None
_tokens: dict[str, tuple[str, float]] = {}


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    """Tests and the E2E server: answer Google's APIs with a fake."""
    global _transport
    _transport = transport
    reset_token()


def set_aws_credentials(source: AwsCredentialSource | None) -> None:
    global _aws_credentials
    _aws_credentials = source or _broker_credentials
    reset_token()


def reset_token() -> None:
    global _federated
    _federated = None
    _tokens.clear()


def _fresh(cached: tuple[str, float] | None) -> str | None:
    if cached is not None and cached[1] - time.time() > REFRESH_BEFORE_SECONDS:
        return cached[0]
    return None


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
    # google.rpc.Status over HTTP (AIP-193): ``status`` is the canonical code and
    # ``details[]`` may hold an ErrorInfo with a machine ``reason``. BigQuery and Compute v1
    # also send the older ``errors[].reason`` (accessDenied, rateLimitExceeded, ...).
    reasons = [e.get("reason") for e in err.get("errors") or [] if e.get("reason")]
    details = [d.get("reason") for d in err.get("details") or [] if d.get("reason")]
    code = (details or reasons or [err.get("status") or f"HTTP{response.status_code}"])[0]
    return GcpError(response.status_code, str(code), str(err.get("message") or code)[:500])


def _retry_after(response: httpx.Response | None) -> float | None:
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return None if value is None else max(0.0, float(value))
    except ValueError:
        return None


def _expiry(expire_time: str | None) -> float:
    """``expireTime`` of generateAccessToken (RFC 3339) as epoch seconds; an hour if absent."""
    if expire_time:
        try:
            return datetime.fromisoformat(expire_time.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time() + 3600


class GcpApi:
    """The Google APIs Cloudoptics calls: as ``service_account`` (a connection's own account)
    for customer data, or as the broker's federated identity for the IAM calls in our own
    project (``service_account=None``)."""

    def __init__(
        self,
        settings: GcpSettings | None = None,
        *,
        service_account: str | None = None,
        customer_pays: bool = False,
    ) -> None:
        self.settings = settings or get_gcp_settings()
        self.service_account = service_account
        #: Whether the queries run in (and are billed to) the customer's project.
        self.customer_pays = customer_pays
        self.bytes_billed = 0

    @property
    def customer_cost_usd(self) -> float:
        """What the customer pays for this run's queries: nothing when they run in
        Cloudoptics' project."""
        return bq_cost_usd(self.bytes_billed) if self.customer_pays else 0.0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=_transport, timeout=self.settings.http_timeout_seconds)

    async def _send(
        self, http: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        """One call, retried on transient errors with truncated exponential backoff and
        jitter (honouring ``Retry-After``). The last response is returned, error or not."""
        attempts = max(0, self.settings.retries) + 1
        for attempt in range(attempts):
            last = attempt + 1 == attempts
            response: httpx.Response | None = None
            try:
                response = await http.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if last:
                    raise GcpError(0, "Unreachable", type(exc).__name__) from exc
            else:
                if response.status_code < 400 or last or not _error(response).retryable:
                    return response
            base = self.settings.retry_base_seconds
            delay = _retry_after(response)
            if delay is None:
                delay = min(32.0, base * 2**attempt) * (0.5 + random.random() / 2)  # noqa: S311
            await asyncio.sleep(min(delay, 60.0) if base > 0 else 0)
        raise AssertionError("unreachable")  # pragma: no cover

    async def federated_token(self) -> str:
        """The broker's federated token (STS token exchange), cached."""
        global _federated
        s = self.settings
        if not s.enabled:
            raise NotConfiguredError()
        cached = _fresh(_federated)
        if cached:
            return cached
        from starlette.concurrency import run_in_threadpool

        try:
            creds = await run_in_threadpool(_aws_credentials)
        except Exception as exc:  # broker credentials: botocore, Roles Anywhere
            raise GcpError(0, "AwsCredentials", type(exc).__name__) from exc
        subject = signed_caller_identity(creds, s.workload_provider, s.aws_region)
        async with self._client() as http:
            try:
                sts = await self._send(
                    http,
                    "POST",
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
            except httpx.HTTPError as exc:
                raise GcpError(0, "Unreachable", type(exc).__name__) from exc
        if sts.status_code != 200:
            raise _error(sts)
        granted = sts.json()
        expires = time.time() + float(granted.get("expires_in") or 3600)
        _federated = (str(granted["access_token"]), expires)
        return _federated[0]

    async def token(self) -> str:
        """The token calls are made with: the connection's service account, or the broker's
        federated identity when there is none (IAM calls in our own project)."""
        if not self.settings.enabled:
            raise NotConfiguredError()
        if self.service_account is None:
            return await self.federated_token()
        sa = self.service_account
        cached = _fresh(_tokens.get(sa))
        if cached:
            return cached
        federated = await self.federated_token()
        url = (
            f"{self.settings.iamcredentials_url}/projects/-/serviceAccounts/"
            f"{quote(sa, safe='@.')}:generateAccessToken"
        )
        async with self._client() as http:
            try:
                impersonated = await self._send(
                    http,
                    "POST",
                    url,
                    headers={"Authorization": f"Bearer {federated}"},
                    json={"scope": [SCOPE], "lifetime": "3600s"},
                )
            except httpx.HTTPError as exc:
                raise GcpError(0, "Unreachable", type(exc).__name__) from exc
        if impersonated.status_code != 200:
            raise _error(impersonated)
        granted = impersonated.json()
        _tokens[sa] = (granted["accessToken"], _expiry(granted.get("expireTime")))
        return _tokens[sa][0]

    async def request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None, body: Any = None
    ) -> dict[str, Any]:
        token = await self.token()
        async with self._client() as http:
            try:
                response = await self._send(
                    http,
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

    # --- IAM: the per-connection service accounts, in Cloudoptics' own project ---------

    async def create_service_account(
        self, project: str, account_id: str, display_name: str, description: str
    ) -> dict[str, Any]:
        """``projects.serviceAccounts.create`` (``iam.serviceAccounts.create``)."""
        return await self.request(
            "POST",
            f"{self.settings.iam_url}/projects/{quote(project)}/serviceAccounts",
            body={
                "accountId": account_id,
                "serviceAccount": {"displayName": display_name, "description": description},
            },
        )

    async def delete_service_account(self, email: str) -> None:
        """``projects.serviceAccounts.delete``: what customers granted it stops working at
        once (shown as ``deleted:serviceAccount:...`` until Google purges it)."""
        await self.request(
            "DELETE",
            f"{self.settings.iam_url}/projects/-/serviceAccounts/{quote(email, safe='@.')}",
        )

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
            # Idempotency: a retried request with the same id is not run twice.
            "requestId": str(uuid.uuid4()),
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
        billed: int | None = None
        for _ in range(10_000):
            if billed is None and page.get("totalBytesBilled") is not None:
                billed = int(page["totalBytesBilled"])  # in jobs.query's answer only
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
        if billed is None:
            billed = int(page.get("totalBytesProcessed") or processed)
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
