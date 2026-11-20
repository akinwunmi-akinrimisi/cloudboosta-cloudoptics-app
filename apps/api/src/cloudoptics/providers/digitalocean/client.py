"""A small, read-only client for the DigitalOcean API (ADR-021).

The customer's token is the only credential. It lives in this object for the length of one
job or request and nowhere else: it is not in ``repr``, in exceptions, in log records or in
the URLs we follow. Errors carry the HTTP status and DigitalOcean's error ``id`` only.

Limits (DigitalOcean: 250 requests a minute and 5,000 an hour per token). Each process keeps
to ``REQUESTS_PER_MINUTE`` per token, below DigitalOcean's own limit, and honours the
``ratelimit-remaining`` / ``ratelimit-reset`` headers. A 429 waits for ``Retry-After`` (or the
reset time), 5xx and network errors back off exponentially; both give up after
``MAX_RETRIES``.

``set_transport`` swaps the HTTP transport (tests and the E2E server use the fake API in
``tests/fake_digitalocean.py``); nothing else in the code knows the fake exists.
"""

from __future__ import annotations

import contextlib
import hashlib
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit

import httpx

API_BASE = "https://api.digitalocean.com"
#: Personal access tokens (``dop_v1_``), OAuth tokens (``doo_v1_``) and older unprefixed ones.
TOKEN_PATTERN = re.compile(r"(do[po]_v1_)?[a-f0-9]{64}")
REQUESTS_PER_MINUTE = 200
MAX_RETRIES = 3
MAX_WAIT_SECONDS = 60.0
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
#: An invoice CSV larger than this is refused (a small team's is a few kilobytes).
MAX_TEXT_BYTES = 20 * 1024 * 1024
MAX_PAGES = 100
PER_PAGE = 200

_transport: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    """Send every request through ``transport`` (tests and E2E: the fake API)."""
    global _transport
    _transport = transport


def get_transport() -> httpx.BaseTransport | None:
    """The transport ``set_transport`` installed (the OAuth endpoints use it too)."""
    return _transport


def valid_token(token: str) -> bool:
    return TOKEN_PATTERN.fullmatch(token) is not None


def token_key(token: str) -> str:
    """Names a token in the limiter without revealing it."""
    return hashlib.sha256(b"cloudoptics/do-token:" + token.encode()).hexdigest()[:16]


class DigitalOceanError(Exception):
    """A request DigitalOcean refused or could not answer. Never holds the token."""

    def __init__(self, status: int | None, code: str, path: str = "") -> None:
        super().__init__(f"{code} ({status})" if status else code)
        self.status = status
        self.code = code
        self.path = path

    @property
    def forbidden(self) -> bool:
        return self.status == 403

    @property
    def unauthorized(self) -> bool:
        return self.status == 401


class RateLimiter:
    """At most ``per_minute`` requests a minute per token in this process, and none before a
    reset DigitalOcean announced (``ratelimit-remaining: 0``)."""

    def __init__(
        self,
        per_minute: int = REQUESTS_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._sent: dict[str, deque[float]] = {}
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> None:
        while True:
            with self._lock:
                now = self._clock()
                sent = self._sent.setdefault(key, deque())
                while sent and now - sent[0] >= 60.0:
                    sent.popleft()
                wait = max(0.0, self._blocked_until.get(key, 0.0) - now)
                if len(sent) >= self._per_minute:
                    wait = max(wait, 60.0 - (now - sent[0]))
                if wait <= 0:
                    sent.append(now)
                    return
            self._sleep(min(wait, MAX_WAIT_SECONDS))

    def block(self, key: str, seconds: float) -> None:
        with self._lock:
            until = self._clock() + max(0.0, min(seconds, MAX_WAIT_SECONDS))
            self._blocked_until[key] = max(self._blocked_until.get(key, 0.0), until)


LIMITER = RateLimiter()


def _error_code(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    code = body.get("id") if isinstance(body, dict) else None
    if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,64}", code):
        return code
    return {401: "unauthorized", 403: "forbidden", 404: "not_found", 429: "too_many_requests"}.get(
        response.status_code, "server_error" if response.status_code >= 500 else "client_error"
    )


class Client:
    """Read-only DigitalOcean API calls with one token. Use as a context manager."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_BASE,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.__token = token
        self._key = token_key(token)
        self._base = base_url.rstrip("/")
        self._host = urlsplit(self._base).netloc
        self._limiter = limiter or LIMITER
        self._sleep = sleep
        self._clock = clock
        self.calls = 0
        self._http = httpx.Client(
            base_url=self._base,
            transport=_transport,
            timeout=TIMEOUT,
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "cloudoptics"},
        )

    def __repr__(self) -> str:
        return f"<digitalocean.Client token={self._key}>"

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    # -- sending --------------------------------------------------------------------------

    def _send(self, method: str, path: str, *, retry: bool, **kwargs: Any) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self.__token}"}
        attempt = 0
        while True:
            self._limiter.acquire(self._key)
            self.calls += 1
            try:
                response = self._http.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                if not retry or attempt >= MAX_RETRIES:
                    raise DigitalOceanError(None, f"network:{type(exc).__name__}", path) from None
                self._sleep(min(2.0**attempt, MAX_WAIT_SECONDS))
                attempt += 1
                continue
            self._note_limits(response)
            if response.status_code == 429 and retry and attempt < MAX_RETRIES:
                self._sleep(self._retry_after(response))
                attempt += 1
                continue
            if response.status_code >= 500 and retry and attempt < MAX_RETRIES:
                self._sleep(min(2.0**attempt, MAX_WAIT_SECONDS))
                attempt += 1
                continue
            return response

    def _note_limits(self, response: httpx.Response) -> None:
        remaining = response.headers.get("ratelimit-remaining")
        reset = response.headers.get("ratelimit-reset")
        if remaining is not None and remaining.strip() == "0" and reset:
            with contextlib.suppress(ValueError):
                self._limiter.block(self._key, float(reset) - self._clock())

    def _retry_after(self, response: httpx.Response) -> float:
        for header, relative in (("retry-after", True), ("ratelimit-reset", False)):
            value = response.headers.get(header)
            if value:
                try:
                    seconds = float(value) if relative else float(value) - self._clock()
                except ValueError:
                    continue
                return max(1.0, min(seconds, MAX_WAIT_SECONDS))
        return 5.0

    def _checked(self, response: httpx.Response, path: str) -> httpx.Response:
        if response.status_code >= 400:
            raise DigitalOceanError(response.status_code, _error_code(response), path)
        return response

    # -- reading --------------------------------------------------------------------------

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._checked(self._send("GET", path, retry=True, params=params), path)
        try:
            return response.json()
        except ValueError:
            raise DigitalOceanError(response.status_code, "invalid_json", path) from None

    def get_text(self, path: str) -> str:
        """A text body (the invoice CSV), refused past ``MAX_TEXT_BYTES``."""
        response = self._checked(self._send("GET", path, retry=True), path)
        if len(response.content) > MAX_TEXT_BYTES:
            raise DigitalOceanError(response.status_code, "too_large", path)
        return response.content.decode("utf-8-sig")

    def paged(
        self, path: str, key: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Every item of a list endpoint, following ``links.pages.next`` on the same host."""
        query: dict[str, Any] | None = {"per_page": PER_PAGE, **(params or {})}
        url = path
        for _ in range(MAX_PAGES):
            body = self.get(url, query)
            yield from body.get(key) or []
            nxt = ((body.get("links") or {}).get("pages") or {}).get("next")
            if not nxt:
                return
            parts = urlsplit(nxt)
            if parts.netloc and parts.netloc != self._host:
                raise DigitalOceanError(None, "foreign_next_link", path)
            if not parts.path.startswith("/v2/"):
                raise DigitalOceanError(None, "foreign_next_link", path)
            url = parts.path + (f"?{parts.query}" if parts.query else "")
            query = None
        raise DigitalOceanError(None, "too_many_pages", path)

    def status_of(self, method: str, path: str, body: dict[str, Any] | None = None) -> int:
        """The status DigitalOcean answers, for the access checks (no retry, body unread)."""
        return self._send(method, path, retry=False, json=body).status_code
