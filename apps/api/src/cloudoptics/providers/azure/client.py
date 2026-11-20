"""Azure REST calls over httpx (ADR-020): Resource Manager, Resource Graph, Cost Management,
Advisor, Monitor and Blob Storage. No Azure SDK: every call here is a plain REST request with
a bearer token from ``credentials.AzureTokens``.

One ``httpx.AsyncClient`` serves the process; tests and the E2E stack swap its transport for
the fake Azure (``set_transport``). Answers the customer controls are bounded: pages followed
(``MAX_PAGES``), bytes downloaded (``max_bytes``).
"""

from __future__ import annotations

import asyncio
import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from cloudoptics.platform.settings import get_settings
from cloudoptics.providers.azure.credentials import (
    ARM_RESOURCE,
    STORAGE_RESOURCE,
    AssertionSource,
    AzureAuthError,
    AzureTokens,
    CertificateAssertion,
)

MAX_PAGES = 50
#: API versions, each the latest stable (non-preview) one in Microsoft's REST reference or
#: github.com/Azure/azure-rest-api-specs, read 20 Nov 2026 (ADR-020, Sources).
API = {
    "subscriptions": "2022-12-01",  # Microsoft.Resources subscriptions
    "billing": "2024-04-01",  # Microsoft.Billing billingAccounts
    "cost_management": "2026-06-01",  # Microsoft.CostManagement exports and query
    "storage": "2026-09-01",  # Microsoft.Storage storageAccounts (tags)
    "resource_graph": "2024-04-01",  # Microsoft.ResourceGraph resources
    "advisor": "2026-10-01",  # Microsoft.Advisor recommendations
    "metrics": "2023-10-01",  # Microsoft.Insights metrics
}
BLOB_API_VERSION = "2023-11-03"  # x-ms-version for List Blobs / Get Blob
#: 429 / 503: wait as Azure says (Retry-After, or Cost Management's QPU header), then retry.
RETRIES = 3
MAX_WAIT = 60.0
QPU_RETRY_AFTER = "x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
CHUNK = 1024 * 1024


class AzureError(RuntimeError):
    """An Azure API refused or failed. ``status`` is the HTTP status (0: no answer)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code

    @property
    def denied(self) -> bool:
        return self.status in (401, 403)


class TooLargeError(AzureError):
    def __init__(self, message: str) -> None:
        super().__init__(0, "too_large", message)


_transport: httpx.AsyncBaseTransport | None = None
_tokens: AzureTokens | None = None
_source_override: AssertionSource | None = None


def set_transport(
    transport: httpx.AsyncBaseTransport | None, source: AssertionSource | None = None
) -> None:
    """Route every Azure call through ``transport`` (tests, E2E). ``source`` replaces the
    certificate from settings."""
    global _transport, _tokens, _source_override
    _transport, _tokens, _source_override = transport, None, source


def _assertion_source() -> AssertionSource | None:
    if _source_override is not None:
        return _source_override
    settings = get_settings()
    key = settings.azure_private_key.get_secret_value()
    if not (settings.azure_certificate and key):
        return None
    return CertificateAssertion.from_pem(settings.azure_certificate, key)


def tokens() -> AzureTokens:
    global _tokens
    if _tokens is None:
        settings = get_settings()
        _tokens = AzureTokens(
            client_id=settings.azure_client_id,
            source=_assertion_source(),
            authority=settings.azure_authority_url,
            http=httpx.AsyncClient(transport=_transport, timeout=TIMEOUT),
        )
    return _tokens


def _error(response: httpx.Response) -> AzureError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        code, message = str(err.get("code") or response.status_code), str(err.get("message") or "")
    else:
        code, message = str(response.status_code), ""
        if response.headers.get("x-ms-error-code"):
            code = response.headers["x-ms-error-code"]
    return AzureError(
        response.status_code, code, message.splitlines()[0][:300] if message else code
    )


def retry_after(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait: Azure's own header when present, else 2, 4, 8 s."""
    for header in ("retry-after", QPU_RETRY_AFTER):
        value = response.headers.get(header)
        if value:
            try:
                return min(max(float(value), 0.0), MAX_WAIT)
            except ValueError:
                continue
    return min(2.0 ** (attempt + 1), MAX_WAIT)


@dataclass
class Blob:
    name: str
    size: int


class AzureApi:
    """Calls in one customer directory."""

    def __init__(self, directory_id: str) -> None:
        self.directory_id = directory_id
        self._tokens = tokens()
        self._http = self._tokens.http
        settings = get_settings()
        self._arm = settings.azure_arm_url.rstrip("/")
        self._blob_scheme = settings.azure_blob_scheme
        self._blob_suffix = settings.azure_blob_suffix
        self.calls = 0

    async def sign_in(self) -> None:
        """A Resource Manager token in the directory: proves admin consent."""
        await self._tokens.token(self.directory_id, ARM_RESOURCE)

    async def _headers(self, resource: str) -> dict[str, str]:
        token = await self._tokens.token(self.directory_id, resource)
        return {"Authorization": f"Bearer {token}"}

    async def _send(self, method: str, url: str, resource: str, **kw: Any) -> httpx.Response:
        """One request; a throttled one (429, or 503) is retried after the wait Azure asks
        for, at most ``RETRIES`` times (Resource Manager request limits; Cost Management QPU)."""
        extra = kw.pop("headers", {})
        for attempt in range(RETRIES + 1):
            headers = {**await self._headers(resource), **extra}
            self.calls += 1
            try:
                response = await self._http.request(method, url, headers=headers, **kw)
            except httpx.HTTPError as exc:
                raise AzureError(
                    0, "network", f"Azure did not answer ({type(exc).__name__})."
                ) from exc
            if response.status_code not in (429, 503) or attempt == RETRIES:
                return response
            await asyncio.sleep(retry_after(response, attempt))
        return response  # pragma: no cover - the loop always returns

    def _url(self, path: str) -> str:
        return (
            path
            if path.startswith("https://") or path.startswith("http://")
            else (f"{self._arm}{path}")
        )

    async def arm(
        self,
        method: str,
        path: str,
        *,
        api_version: str | None,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``api_version`` None: ``path`` is a ``nextLink``, which carries its own."""
        query = dict(params or {})
        if api_version is not None:
            query["api-version"] = api_version
        response = await self._send(
            method, self._url(path), ARM_RESOURCE, params=query or None, json=body
        )
        if response.status_code >= 400:
            raise _error(response)
        # 204 No Content (Cost Management query with no data) reads as an empty answer.
        data = response.json() if response.content else {}
        return data if isinstance(data, dict) else {}

    def same_host(self, link: str) -> str:
        if not link.startswith(self._arm):
            raise AzureError(0, "bad_link", "Azure returned a next page on another host.")
        return link

    async def arm_list(
        self, path: str, *, api_version: str, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """Every ``value`` of a paged list (``nextLink``)."""
        items: list[dict[str, Any]] = []
        page = await self.arm("GET", path, api_version=api_version, params=params)
        for _ in range(MAX_PAGES):
            items.extend(v for v in page.get("value", []) if isinstance(v, dict))
            link = page.get("nextLink")
            if not link:
                break
            page = await self.arm("GET", self.same_host(str(link)), api_version=None)
        return items

    async def resource_graph(self, subscriptions: list[str], query: str) -> list[dict[str, Any]]:
        """Azure Resource Graph rows (``$skipToken`` paging)."""
        rows: list[dict[str, Any]] = []
        options: dict[str, Any] = {"resultFormat": "objectArray", "$top": 1000}
        for _ in range(MAX_PAGES):
            page = await self.arm(
                "POST",
                "/providers/Microsoft.ResourceGraph/resources",
                api_version=API["resource_graph"],
                body={"subscriptions": subscriptions, "query": query, "options": options},
            )
            rows.extend(r for r in page.get("data", []) if isinstance(r, dict))
            token = page.get("$skipToken")
            if not token:
                break
            options = {**options, "$skipToken": token}
        return rows

    # --- Blob Storage -----------------------------------------------------------------

    def blob_url(self, account: str, container: str, name: str = "") -> str:
        base = f"{self._blob_scheme}://{account}.{self._blob_suffix}/{container}"
        return f"{base}/{quote(name, safe='/')}" if name else base

    async def list_blobs(self, account: str, container: str, prefix: str) -> list[Blob]:
        blobs: list[Blob] = []
        marker = ""
        for _ in range(MAX_PAGES):
            params = {"restype": "container", "comp": "list", "prefix": prefix}
            if marker:
                params["marker"] = marker
            response = await self._send(
                "GET",
                self.blob_url(account, container),
                STORAGE_RESOURCE,
                params=params,
                headers={"x-ms-version": BLOB_API_VERSION},
            )
            if response.status_code >= 400:
                raise _error(response)
            root = ET.fromstring(response.content)  # noqa: S314 - Azure's answer
            for blob in root.iter("Blob"):
                name = (blob.findtext("Name") or "").strip()
                size = int(blob.findtext("Properties/Content-Length") or 0)
                blobs.append(Blob(name, size))
            marker = root.findtext("NextMarker") or ""
            if not marker:
                break
        return blobs

    async def read_blob(self, account: str, container: str, name: str, max_bytes: int) -> bytes:
        response = await self._send(
            "GET",
            self.blob_url(account, container, name),
            STORAGE_RESOURCE,
            headers={"x-ms-version": BLOB_API_VERSION},
        )
        if response.status_code >= 400:
            raise _error(response)
        if len(response.content) > max_bytes:
            raise TooLargeError(f"{name} is larger than {max_bytes:,} bytes")
        return response.content

    async def download_blob(
        self, account: str, container: str, name: str, dest: Path, max_bytes: int
    ) -> tuple[str, int]:
        """Stream a blob to ``dest``; its SHA-256 and size. ``TooLargeError`` past the limit."""
        headers = {**await self._headers(STORAGE_RESOURCE), "x-ms-version": BLOB_API_VERSION}
        digest, size = hashlib.sha256(), 0
        self.calls += 1
        try:
            async with self._http.stream(
                "GET", self.blob_url(account, container, name), headers=headers
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise _error(response)
                with dest.open("wb") as fh:
                    async for chunk in response.aiter_bytes(CHUNK):
                        size += len(chunk)
                        if size > max_bytes:
                            raise TooLargeError(
                                f"{name} is larger than the {max_bytes:,}-byte limit"
                            )
                        digest.update(chunk)
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise AzureError(0, "network", f"Azure did not answer ({type(exc).__name__}).") from exc
        return digest.hexdigest(), size


AZURE_ERRORS = (AzureError, AzureAuthError)
