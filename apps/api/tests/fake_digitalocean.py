"""A fake DigitalOcean API (an ``httpx`` transport) for the tests and the E2E server.

Install it with ``client.set_transport(FakeDigitalOcean().transport())``; nothing reaches
DigitalOcean. Tokens decide what the caller may do:

- tokens added with ``add_token`` have the scopes given there;
- with ``accept_any`` (the E2E server), any well-formed token works with every read scope,
  and its team is derived from the token (each test gets its own team). Hex starting with
  ``dead`` is refused (401), ``ffff`` can also write, ``b111`` lacks ``billing:read``.

OAuth (https://docs.digitalocean.com/reference/api/oauth/): ``authorize`` stands in for the
consent page (sign in, pick the team, approve) and returns the redirect with ``code`` and
``state``; ``POST https://cloud.digitalocean.com/v1/oauth/token`` exchanges a code (single
use, 10 minutes, the registered client and redirect URI, the PKCE verifier) or a refresh
token (single use; the access token it came with stops working) and answers in the
documented shape; ``POST /v1/oauth/revoke`` revokes an access token. Errors are
``{"error": "invalid_grant" | "invalid_client" | "invalid_request" | ...}``.

Every team has the same synthetic bill and resources (``Team``): two closed monthly
invoices with a CSV and summary, this month's balance and daily insights, and Droplets
(one idle), an unattached volume, an old snapshot, an unassigned reserved IP, a load
balancer with nothing behind it and a database.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

HOST = "api.digitalocean.com"
OAUTH_HOST = "cloud.digitalocean.com"
CLIENT_ID = "fake-do-client-id"
CLIENT_SECRET = "fake-do-client-secret"  # noqa: S105 - the fake's own test value
READ_SCOPES = frozenset(
    {
        "account:read",
        "billing:read",
        "droplet:read",
        "block_storage:read",
        "snapshot:read",
        "reserved_ip:read",
        "load_balancer:read",
        "database:read",
        "monitoring:read",
        "tag:read",
    }
)
WRITE_SCOPES = frozenset({"droplet:create", "tag:create"})
NAMESPACE = uuid.UUID("6f0a4c1e-5d3b-4c35-9a59-7d5a3c7e0d01")
CSV_HEADER = [
    "product",
    "group_description",
    "description",
    "hours",
    "start",
    "end",
    "USD",
    "project_name",
    "category",
]

# (product, description, USD, project, start day or None for the 1st, category)
LINES: tuple[tuple[str, str, str, str, int | None, str], ...] = (
    ("Droplets", "web-1 (s-2vcpu-4gb)", "24.00", "Production", None, "iaas"),
    ("Droplets", "idle-box (s-1vcpu-2gb)", "12.00", "Staging", None, "iaas"),
    ("Volumes", "loose-data (100 GiB)", "10.00", "Staging", None, "iaas"),
    ("Load Balancers", "lb-orphan", "12.00", "Staging", None, "iaas"),
    ("Reserved IPs", "Unused Reserved IP - 203.0.113.7", "2.50", "", 16, "iaas"),
    ("Snapshots", "old-snap", "1.20", "", None, "iaas"),
    ("Managed Databases", "pg-main (db-s-1vcpu-1gb)", "15.00", "Production", None, "paas"),
    ("Bandwidth", "Outbound transfer", "0.30", "", None, "iaas"),
)
TAXES = Decimal("3.00")
CREDITS = Decimal("-2.00")
IDLE_DROPLET = 900002
BUSY_DROPLET = 900001


def secret_token(prefix: str = "") -> str:
    """A well-formed personal access token; ``prefix`` (hex) picks its behaviour."""
    return "dop_v1_" + (prefix + secrets.token_hex(32))[:64]


def month_start(day: date) -> date:
    return day.replace(day=1)


def prev_month(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def next_month(day: date) -> date:
    return (day.replace(day=1) + timedelta(days=32)).replace(day=1)


def _stamp(day: date) -> str:
    return f"{day:%Y-%m-%d} 00:00:00 +0000"


@dataclass
class Team:
    uuid: str
    name: str
    today: date

    def periods(self) -> list[date]:
        last = prev_month(self.today)
        return [last, prev_month(last)]

    def invoice_uuid(self, period: date) -> str:
        return str(uuid.uuid5(NAMESPACE, f"invoice:{self.uuid}:{period:%Y-%m}"))

    def products_total(self) -> Decimal:
        return sum((Decimal(line[2]) for line in LINES), Decimal(0))

    def invoice_amount(self) -> Decimal:
        return self.products_total() + TAXES + CREDITS

    def csv(self, period: date) -> str:
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(CSV_HEADER)
        end = next_month(period)
        for product, description, usd, project, start_day, category in LINES:
            start = period.replace(day=start_day) if start_day else period
            hours = int((end - start).days * 24)
            writer.writerow(
                [
                    product,
                    "",
                    description,
                    hours,
                    _stamp(start),
                    _stamp(end),
                    f"${usd}",
                    project,
                    category,
                ]
            )
        return out.getvalue()

    def summary(self, period: date) -> dict[str, Any]:
        return {
            "invoice_uuid": self.invoice_uuid(period),
            "billing_period": f"{period:%Y-%m}",
            "amount": f"{self.invoice_amount():.2f}",
            "user_name": "Ada Customer",
            "user_email": "billing@customer.example",
            "user_company": "Customer Ltd",
            "user_billing_address": {
                "address_line1": "1 Example Street",
                "city": "Lagos",
                "country_iso2_code": "NG",
                "postal_code": "100001",
                "region": "Lagos",
                "created_at": "2019-09-03T16:34:46.000+00:00",
                "updated_at": "2019-09-03T16:34:46.000+00:00",
            },
            "product_charges": {
                "name": "Product usage charges",
                "amount": f"{self.products_total():.2f}",
                "items": [
                    {"amount": f"{Decimal(line[2]):.2f}", "name": line[0], "count": "1"}
                    for line in LINES
                ],
            },
            "overages": {"name": "Overages", "amount": "0"},
            "taxes": {"name": "Taxes", "amount": f"{TAXES:.2f}"},
            "credits_and_adjustments": {
                "name": "Credits & adjustments",
                "amount": f"{CREDITS:.2f}",
            },
        }

    def mtd_points(self) -> list[dict[str, Any]]:
        points = []
        day = month_start(self.today)
        while day < self.today:
            points.append(
                {
                    "usage_team_urn": f"do:team:{self.uuid}",
                    "start_date": day.isoformat(),
                    "total_amount": "1.20",
                    "region": "lon1",
                    "sku": "1-DO-DROP-0109",
                    "description": "web-1 (s-2vcpu-4gb)",
                    "group_description": "",
                }
            )
            points.append(
                {
                    "usage_team_urn": f"do:team:{self.uuid}",
                    "start_date": day.isoformat(),
                    "total_amount": "0.33",
                    "region": "lon1",
                    "sku": "1-DO-VOL-0001",
                    "description": "loose-data",
                    "group_description": "",
                }
            )
            day += timedelta(days=1)
        return points

    def mtd_total(self) -> Decimal:
        return sum((Decimal(p["total_amount"]) for p in self.mtd_points()), Decimal(0)) or Decimal(
            "0.75"
        )

    # resources ---------------------------------------------------------------------------

    def droplets(self) -> list[dict[str, Any]]:
        def one(did: int, name: str, slug: str, vcpus: int, price: float) -> dict[str, Any]:
            return {
                "id": did,
                "name": name,
                "memory": 2048 * vcpus,
                "vcpus": vcpus,
                "disk": 50,
                "status": "active",
                "created_at": "2025-01-10T09:00:00Z",
                "size_slug": slug,
                "size": {"slug": slug, "price_monthly": price},
                "region": {"slug": "lon1"},
                "tags": ["env:prod"] if did == BUSY_DROPLET else ["env:staging"],
                "volume_ids": [],
            }

        return [
            one(BUSY_DROPLET, "web-1", "s-2vcpu-4gb", 2, 24.0),
            one(IDLE_DROPLET, "idle-box", "s-1vcpu-2gb", 1, 12.0),
        ]

    def volumes(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "506f78a4-e098-11e5-ad9f-000f53306ae1",
                "name": "loose-data",
                "size_gigabytes": 100,
                "region": {"slug": "lon1"},
                "droplet_ids": [],
                "created_at": "2025-06-01T10:00:00Z",
                "filesystem_type": "ext4",
                "tags": ["env:staging"],
            }
        ]

    def snapshots(self) -> list[dict[str, Any]]:
        old = datetime.combine(self.today - timedelta(days=200), datetime.min.time(), tzinfo=UTC)
        return [
            {
                "id": "6372321",
                "name": "old-snap",
                "regions": ["lon1"],
                "created_at": old.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "resource_id": "200",
                "resource_type": "droplet",
                "min_disk_size": 20,
                "size_gigabytes": 20,
                "tags": [],
            }
        ]

    def reserved_ips(self) -> list[dict[str, Any]]:
        return [
            {
                "ip": "203.0.113.7",
                "region": {"slug": "lon1"},
                "droplet": None,
                "locked": False,
                "project_id": None,
            },
            {
                "ip": "203.0.113.8",
                "region": {"slug": "lon1"},
                "droplet": {"id": BUSY_DROPLET},
                "locked": False,
                "project_id": None,
            },
        ]

    def load_balancers(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "4de7ac8b-495b-4884-9a69-1050c6793cd6",
                "name": "lb-orphan",
                "ip": "203.0.113.9",
                "size_unit": 1,
                "size": "lb-small",
                "status": "active",
                "created_at": "2025-03-01T10:00:00Z",
                "region": {"slug": "lon1"},
                "droplet_ids": [],
                "tag": "",
                "forwarding_rules": [{"entry_port": 80}],
            }
        ]

    def databases(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "9cc10173-e9ea-4176-9dbc-a4cee4c4ff30",
                "name": "pg-main",
                "engine": "pg",
                "version": "16",
                "num_nodes": 1,
                "size": "db-s-1vcpu-1gb",
                "region": "lon1",
                "status": "online",
                "created_at": "2025-01-01T00:00:00Z",
                "tags": [],
                # The list carries connection details with a password (API reference,
                # Databases); the scan must never store them.
                "connection": {
                    "uri": "postgresql://doadmin:fake-db-password@db.example:25060/defaultdb",
                    "user": "doadmin",
                    "password": "fake-db-password",
                },
                "users": [{"name": "doadmin", "role": "primary", "password": "fake-db-password"}],
            }
        ]

    def cpu(self, host_id: str, start: int, end: int) -> dict[str, Any]:
        busy = 0.01 if host_id == str(IDLE_DROPLET) else 0.45
        idle_values, user_values = [], []
        idle = user = 0.0
        for ts in range(start - start % 3600, end, 3600):
            idle += 3600 * (1 - busy)
            user += 3600 * busy
            idle_values.append([ts, f"{idle:.1f}"])
            user_values.append([ts, f"{user:.1f}"])
        return {
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [
                    {"metric": {"host_id": host_id, "mode": "idle"}, "values": idle_values},
                    {"metric": {"host_id": host_id, "mode": "user"}, "values": user_values},
                ],
            },
        }


@dataclass
class Profile:
    team: Team
    scopes: frozenset[str]


@dataclass
class FakeDigitalOcean:
    today: date = field(default_factory=lambda: datetime.now(UTC).date())
    accept_any: bool = False
    tokens: dict[str, Profile] = field(default_factory=dict)
    #: (method, path, had a bearer token) for every request.
    requests: list[tuple[str, str, bool]] = field(default_factory=list)
    #: Answer the next N requests with 429 (``Retry-After: 1``).
    throttle: int = 0
    #: Answer the next N requests with 503.
    unavailable: int = 0
    #: Remove these paths' answers (403) for every token, e.g. the insights endpoint.
    forbidden_paths: set[str] = field(default_factory=set)
    # -- OAuth ------------------------------------------------------------------------------
    client_id: str = CLIENT_ID
    client_secret: str = CLIENT_SECRET
    #: The registered callback URL; None accepts any.
    redirect_uri: str | None = None
    #: The team the customer picks on the consent page; None: a new team per sign-in.
    oauth_team: str | None = None
    oauth_team_name: str = "Ocean Team"
    #: What the customer approves on "Manage permissions", replacing the requested scope.
    granted: frozenset[str] | None = None
    #: Refuse every refresh (``invalid_grant``), as after the customer removed the app.
    refuse_refresh: bool = False
    #: Seconds a refresh takes (to make racing refreshes overlap).
    refresh_delay: float = 0.0
    codes: dict[str, dict[str, Any]] = field(default_factory=dict)
    refresh_tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: (grant_type, sorted form field names) for every token request: values are not kept.
    token_requests: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    revoked: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def add_token(
        self,
        token: str | None = None,
        *,
        scopes: frozenset[str] = READ_SCOPES,
        team: str | None = None,
        name: str = "Acme Engineering",
    ) -> str:
        token = token or secret_token()
        team_uuid = team or str(uuid.uuid5(NAMESPACE, token))
        self.tokens[token] = Profile(Team(team_uuid, name, self.today), frozenset(scopes))
        return token

    def team_for(self, token: str) -> Team:
        profile = self._profile(token)
        assert profile is not None
        return profile.team

    def _profile(self, token: str) -> Profile | None:
        if token in self.tokens:
            return self.tokens[token]
        if not self.accept_any or not token.startswith("dop_v1_") or len(token) != 71:
            return None
        hexpart = token[7:]
        if hexpart.startswith("dead"):
            return None
        scopes = set(READ_SCOPES)
        if hexpart.startswith("ffff"):
            scopes |= WRITE_SCOPES
        if hexpart.startswith("b111"):
            scopes.discard("billing:read")
        return self.tokens.setdefault(
            token,
            Profile(
                Team(str(uuid.uuid5(NAMESPACE, token)), "E2E Team", self.today), frozenset(scopes)
            ),
        )

    # -- the handler ------------------------------------------------------------------------

    # -- OAuth ------------------------------------------------------------------------------

    def authorize(self, query: dict[str, str], *, approve: bool = True) -> str:
        """The consent page: the customer signs in, picks the team and approves. Returns the
        URL DigitalOcean redirects the browser to."""
        redirect = query.get("redirect_uri", "")
        if query.get("client_id") != self.client_id:
            raise ValueError("unknown client_id")
        if self.redirect_uri is not None and redirect != self.redirect_uri:
            raise ValueError("The redirect uri included is not valid.")
        if query.get("response_type") != "code":
            raise ValueError("unsupported response_type")
        challenge = query.get("code_challenge")
        if challenge and query.get("code_challenge_method") != "S256":
            raise ValueError("only S256 is supported")
        back: dict[str, str] = {}
        if "state" in query:
            back["state"] = query["state"]
        if not approve:
            return f"{redirect}?{urlencode({'error': 'access_denied', **back})}"
        requested = query.get("scope")
        scopes = self.granted if self.granted is not None else _scopes(requested)
        if scopes is None:
            raise ValueError("The requested scope is invalid, unknown, or malformed.")
        code = secrets.token_hex(10)
        team = self.oauth_team or str(uuid.uuid4())
        with self._lock:
            self.codes[code] = {
                "redirect_uri": redirect,
                "challenge": challenge,
                "scopes": frozenset(scopes),
                "scope": requested or "read",
                "team": team,
                "at": time.monotonic(),
            }
        return f"{redirect}?{urlencode({'code': code, **back})}"

    def _issue(self, team: str, scopes: frozenset[str], scope: str) -> dict[str, Any]:
        access = "doo_v1_" + secrets.token_hex(32)
        refresh = "dor_v1_" + secrets.token_hex(32)
        self.tokens[access] = Profile(Team(team, self.oauth_team_name, self.today), scopes)
        self.refresh_tokens[refresh] = {
            "access": access,
            "team": team,
            "scopes": scopes,
            "scope": scope,
        }
        return {
            "access_token": access,
            "token_type": "bearer",
            "expires_in": 2592000,
            "refresh_token": refresh,
            "scope": scope,
            "info": {
                "name": "Sammy the Shark",
                "email": "sammy@example.com",
                "uuid": str(uuid.uuid5(NAMESPACE, "user:" + team)),
                "team_uuid": team,
                "team_name": self.oauth_team_name,
            },
        }

    def _oauth(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        form |= {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        if request.method != "POST":
            return _oauth_error(405, "invalid_request")
        if path == "/v1/oauth/revoke":
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            if not token or form.get("token") != token:
                return _oauth_error(401, "invalid_token")
            with self._lock:
                self.tokens.pop(token, None)
                self.revoked.append(token)
            return httpx.Response(200, json={})
        if path != "/v1/oauth/token":
            return _oauth_error(404, "not_found")
        grant = form.get("grant_type", "")
        self.token_requests.append((grant, tuple(sorted(form))))
        if "client_secret" in form and (
            form.get("client_id") != self.client_id or form["client_secret"] != self.client_secret
        ):
            return _oauth_error(401, "invalid_client")
        if grant == "authorization_code":
            return self._exchange(form)
        if grant == "refresh_token":
            if self.refresh_delay:
                time.sleep(self.refresh_delay)
            with self._lock:
                live = self.refresh_tokens.pop(form.get("refresh_token", ""), None)
                if live is None or self.refuse_refresh:
                    return _oauth_error(400, "invalid_grant")
                # The access token it was issued with stops working.
                self.tokens.pop(live["access"], None)
                return httpx.Response(
                    200, json=self._issue(live["team"], live["scopes"], live["scope"])
                )
        return _oauth_error(400, "unsupported_grant_type")

    def _exchange(self, form: dict[str, str]) -> httpx.Response:
        if "client_secret" not in form and "code_verifier" not in form:
            return _oauth_error(400, "invalid_request")
        with self._lock:
            grant = self.codes.pop(form.get("code", ""), None)
            if grant is None or time.monotonic() - grant["at"] > 600:
                return _oauth_error(400, "invalid_grant")
            if form.get("redirect_uri") != grant["redirect_uri"]:
                return _oauth_error(400, "invalid_grant")
            if grant["challenge"]:
                verifier = form.get("code_verifier", "")
                digest = hashlib.sha256(verifier.encode()).digest()
                expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                if expected != grant["challenge"]:
                    return _oauth_error(400, "invalid_grant")
            return httpx.Response(
                200, json=self._issue(grant["team"], grant["scopes"], grant["scope"])
            )

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        path = url.path
        auth = request.headers.get("authorization", "")
        self.requests.append((request.method, path, auth.startswith("Bearer ")))
        if url.host == OAUTH_HOST:
            return self._oauth(request)
        if url.host != HOST:
            return _error(404, "not_found")
        if self.throttle > 0:
            self.throttle -= 1
            return httpx.Response(
                429,
                headers={"retry-after": "1"},
                json={"id": "too_many_requests", "message": "slow down"},
            )
        if self.unavailable > 0:
            self.unavailable -= 1
            return _error(503, "service_unavailable")
        profile = self._profile(auth.removeprefix("Bearer "))
        if profile is None:
            return _error(401, "unauthorized")
        query = {k: v[0] for k, v in parse_qs(url.query.decode()).items()}
        if request.method == "POST":
            scope = {"/v2/droplets": "droplet:create", "/v2/tags": "tag:create"}.get(path)
            if scope is None or scope not in profile.scopes:
                return _error(403, "forbidden")
            return _error(422, "unprocessable_entity")
        if request.method != "GET":
            return _error(405, "method_not_allowed")
        if path in self.forbidden_paths or any(path.startswith(p) for p in self.forbidden_paths):
            return _error(403, "forbidden")
        return self._get(profile, path, query)

    def _get(self, profile: Profile, path: str, query: dict[str, str]) -> httpx.Response:
        team, scopes = profile.team, profile.scopes

        def need(scope: str) -> httpx.Response | None:
            return None if scope in scopes else _error(403, "forbidden")

        if path == "/v2/account":
            return need("account:read") or httpx.Response(
                200,
                json={
                    "account": {
                        "droplet_limit": 25,
                        "floating_ip_limit": 5,
                        "email": "a@b.example",
                        "name": "Ada",
                        "uuid": str(uuid.uuid5(NAMESPACE, "user:" + team.uuid)),
                        "email_verified": True,
                        "status": "active",
                        "status_message": " ",
                        "team": {"uuid": team.uuid, "name": team.name},
                    }
                },
            )
        if path.startswith("/v2/customers/my/") or path.startswith("/v2/billing/"):
            denied = need("billing:read")
            if denied:
                return denied
            return self._billing(team, path, query)
        lists = {
            "/v2/droplets": ("droplet:read", "droplets", team.droplets),
            "/v2/volumes": ("block_storage:read", "volumes", team.volumes),
            "/v2/snapshots": ("snapshot:read", "snapshots", team.snapshots),
            "/v2/reserved_ips": ("reserved_ip:read", "reserved_ips", team.reserved_ips),
            "/v2/load_balancers": ("load_balancer:read", "load_balancers", team.load_balancers),
            "/v2/databases": ("database:read", "databases", team.databases),
            "/v2/monitoring/alerts": ("monitoring:read", "policies", lambda: []),
            "/v2/tags": ("tag:read", "tags", lambda: [{"name": "env:prod"}]),
        }
        if path in lists:
            scope, key, items = lists[path]
            listed = items()
            if "tag:read" not in scopes:  # fields gated by tag:read are left out
                listed = [{k: v for k, v in i.items() if k != "tags"} for i in listed]
            return need(scope) or _paged(path, key, listed, query)
        if path == "/v2/monitoring/metrics/droplet/cpu":
            return need("monitoring:read") or httpx.Response(
                200, json=team.cpu(query.get("host_id", ""), int(query["start"]), int(query["end"]))
            )
        return _error(404, "not_found")

    def _billing(self, team: Team, path: str, query: dict[str, str]) -> httpx.Response:
        if path == "/v2/customers/my/balance":
            return httpx.Response(
                200,
                json={
                    "month_to_date_balance": f"{team.mtd_total():.2f}",
                    "account_balance": "0.00",
                    "month_to_date_usage": f"{team.mtd_total():.2f}",
                    "generated_at": datetime.now(UTC).isoformat(),
                },
            )
        if path == "/v2/customers/my/invoices":
            invoices = [
                {
                    "invoice_uuid": team.invoice_uuid(p),
                    "invoice_id": str(i + 1),
                    "amount": f"{team.invoice_amount():.2f}",
                    "invoice_period": f"{p:%Y-%m}",
                    "updated_at": f"{next_month(p)}T00:00:00Z",
                }
                for i, p in enumerate(team.periods())
            ]
            response = _paged(path, "invoices", invoices, query)
            body = json.loads(response.content)
            body["invoice_preview"] = {
                "invoice_uuid": str(uuid.uuid4()),
                "amount": f"{team.mtd_total():.2f}",
                "invoice_period": f"{team.today:%Y-%m}",
            }
            return httpx.Response(200, json=body)
        for period in team.periods():
            base = f"/v2/customers/my/invoices/{team.invoice_uuid(period)}"
            if path == f"{base}/csv":
                return httpx.Response(
                    200, text=team.csv(period), headers={"content-type": "text/csv"}
                )
            if path == f"{base}/summary":
                return httpx.Response(200, json=team.summary(period))
        if path.startswith(f"/v2/billing/do:team:{team.uuid}/insights/"):
            points = team.mtd_points()
            per_page = int(query.get("per_page", 20))
            page = int(query.get("page", 1))
            pages = max(1, -(-len(points) // per_page))
            return httpx.Response(
                200,
                json={
                    "data_points": points[(page - 1) * per_page : page * per_page],
                    "total_items": len(points),
                    "total_pages": pages,
                    "current_page": page,
                },
            )
        return _error(404, "not_found")


def _paged(path: str, key: str, items: list[Any], query: dict[str, str]) -> httpx.Response:
    per_page = int(query.get("per_page", 20))
    page = int(query.get("page", 1))
    body: dict[str, Any] = {
        key: items[(page - 1) * per_page : page * per_page],
        "links": {},
        "meta": {"total": len(items)},
    }
    if page * per_page < len(items):
        nxt = urlencode({"page": page + 1, "per_page": per_page})
        body["links"] = {"pages": {"next": f"https://{HOST}{path}?{nxt}"}}
    return httpx.Response(200, json=body)


def _scopes(requested: str | None) -> frozenset[str] | None:
    """What a scope string grants. Without one, the customer ticks scopes on "Manage
    permissions": here, every read scope. ``read`` and ``api:read`` are every read scope."""
    if not requested:
        return READ_SCOPES
    granted: set[str] = set()
    for scope in requested.split():
        if scope in ("read", "api:read"):
            granted |= READ_SCOPES
        elif scope in ("write", "api:write"):
            granted |= READ_SCOPES | WRITE_SCOPES
        elif scope in READ_SCOPES or scope in WRITE_SCOPES:
            granted.add(scope)
        else:
            return None
    return frozenset(granted)


def _oauth_error(code: int, error: str) -> httpx.Response:
    return httpx.Response(code, json={"error": error, "error_description": error.replace("_", " ")})


def _error(code: int, ident: str) -> httpx.Response:
    return httpx.Response(code, json={"id": ident, "message": ident.replace("_", " ")})


def parse(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
