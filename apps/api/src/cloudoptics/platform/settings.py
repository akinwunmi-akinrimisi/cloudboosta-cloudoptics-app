"""Process settings, read from environment variables prefixed ``CLOUDOPTICS_``."""

import re
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_AUTH_SECRET = "dev-only-secret-change-me-0123456789abcdef"  # noqa: S105 - refused outside dev
DEV_DATA_KEY = "Y2xvdWRvcHRpY3MtbG9jYWwtZGV2LWtleS0zMmJ5dGU="  # dev only gitleaks:allow

# Local placeholders for the AWS connection settings. The template host is under the reserved
# ".invalid" TLD, so nobody can register it and serve a template to a customer who copies it.
PLACEHOLDER_PRINCIPAL_ARN = "arn:aws:iam::000000000000:role/CloudopticsBroker"
PLACEHOLDER_TEMPLATE_BASE = "https://cloudoptics-templates.invalid/cfn/v1"
# Templates are served only from the bucket infra/aws/bootstrap.py creates:
# cloudoptics-public-<12-digit account ID>, virtual-hosted-style S3 URL over HTTPS.
_TEMPLATE_HOST = re.compile(r"cloudoptics-public-\d{12}\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com")
_PRINCIPAL_ARN = re.compile(r"arn:aws:iam::(\d{12}):role/[\w+=,.@/-]+")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLOUDOPTICS_", extra="ignore")

    environment: Literal["local", "test", "demo", "staging", "production"] = "local"
    log_level: str = "INFO"
    service_name: str = "cloudoptics-api"
    cors_origins: list[str] = ["http://localhost:5173"]

    # Migrations run as the schema owner; the app connects as a role without BYPASSRLS.
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/cloudoptics"
    app_database_url: str = "postgresql+psycopg://cloudoptics_app:app@localhost:5432/cloudoptics"

    # "oidc" in staging/production (WorkOS AuthKit or Keycloak, ADR-006); "dev" signs local
    # tokens with a shared secret and is refused in production.
    auth_mode: Literal["oidc", "dev"] = "dev"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    # WorkOS AuthKit (ADR-006). With a client ID, the JWKS URL and issuers are derived; the API
    # key lets the API read a user's email when the access token does not carry it.
    workos_client_id: str = ""
    workos_api_key: str = ""
    workos_api_base: str = "https://api.workos.com"
    dev_auth_secret: str = DEV_AUTH_SECRET

    # Base64 32-byte key for the local encryptor (connectors/crypto.py): local, test and demo
    # encrypt with it. With a KMS key it only reads ciphertexts written before KMS, until the
    # re-encryption job has rewritten them all; then staging and production may drop it.
    local_data_key: str = DEV_DATA_KEY
    # AWS KMS envelope encryption (ADR-007): a symmetric key in Cloudoptics' broker account,
    # used through the broker credentials. Set: new secrets are encrypted under KMS data keys.
    kms_key_id: str = ""
    kms_region: str = "eu-west-2"
    # Staging setup turns this on once the re-encryption has run. The API and worker then
    # refuse to start without a KMS key, and the development data key is no longer refused
    # (it is ignored: with KMS required, old ciphertexts need the real local key or none).
    kms_required: bool = False
    data_key_cache_seconds: int = Field(default=300, ge=0, le=3600)
    data_key_cache_entries: int = Field(default=1024, ge=1, le=100_000)
    data_key_max_uses: int = Field(default=1000, ge=1)

    # Where the broker's own AWS credentials come from (ADR-007). "roles_anywhere": IAM Roles
    # Anywhere with the X.509 certificate and key below (short-lived, refreshed before
    # expiry). "default_chain": boto3's default chain, i.e. the IAM user key on App Platform;
    # the explicit fallback. "auto": Roles Anywhere once a certificate is set, else the chain.
    broker_credentials: Literal["auto", "roles_anywhere", "default_chain"] = "auto"
    # PEM (or base64 of PEM): the end-entity certificate first, then any intermediates.
    roles_anywhere_certificate: str = ""
    roles_anywhere_private_key: SecretStr = SecretStr("")
    roles_anywhere_trust_anchor_arn: str = ""
    roles_anywhere_profile_arn: str = ""
    roles_anywhere_role_arn: str = ""
    roles_anywhere_session_seconds: int = Field(default=3600, ge=900, le=3600)

    # Azure (ADR-020): the multi-tenant Entra app customers consent to, and its certificate
    # credential (no client secret). The certificate and key are rotated by Staging setup
    # (infra/azure/entra_certificate.py); PEM or base64 of PEM, like Roles Anywhere's.
    azure_client_id: str = ""
    azure_certificate: str = ""
    azure_private_key: SecretStr = SecretStr("")
    azure_authority_url: str = "https://login.microsoftonline.com"
    azure_arm_url: str = "https://management.azure.com"
    # Blob endpoints are https://<account>.<suffix>; tests point this at the fake.
    azure_blob_suffix: str = "blob.core.windows.net"
    azure_blob_scheme: Literal["https", "http"] = "https"

    # DigitalOcean sign-in (ADR-021, "Connect DigitalOcean"): the OAuth application registered
    # in DigitalOcean's control panel (API -> OAuth Applications). Without both, onboarding
    # offers only the token paste. The scope is space-separated and configurable because
    # DigitalOcean does not document whether OAuth grants granular scopes: the per-scope access
    # checks then say exactly what is missing. ``read`` is the documented fallback.
    do_oauth_client_id: str = ""
    do_oauth_client_secret: SecretStr = SecretStr("")
    do_oauth_scope: str = (
        "account:read billing:read droplet:read block_storage:read snapshot:read "
        "reserved_ip:read load_balancer:read database:read monitoring:read tag:read"
    )
    # Where DigitalOcean sends the browser back; empty: ``app_url`` + the callback page. It must
    # match the application's registered callback URL exactly.
    do_oauth_redirect_url: str = ""
    do_oauth_authorize_url: str = "https://cloud.digitalocean.com/v1/oauth/authorize"
    # Token and revoke endpoints (``/v1/oauth/token``, ``/v1/oauth/revoke``) are under this.
    do_oauth_base_url: str = "https://cloud.digitalocean.com"
    # PKCE (S256): required for public clients, allowed for confidential ones.
    do_oauth_pkce: bool = True
    # Tokens last 30 days; renew this many days before expiry (the hourly job).
    do_oauth_refresh_days: int = Field(default=7, ge=1, le=29)

    # AWS connection (PRD FR-CON-01/13). Templates must be served from S3 for quick-create.
    # The defaults are local placeholders; the API and worker refuse them in staging and
    # production (require_aws_connection_settings).
    cloudoptics_principal_arn: str = PLACEHOLDER_PRINCIPAL_ARN
    cfn_billing_template_url: str = f"{PLACEHOLDER_TEMPLATE_BASE}/billing-role.yaml"
    cfn_resource_template_url: str = f"{PLACEHOLDER_TEMPLATE_BASE}/resource-role.yaml"
    aws_region: str = "eu-west-2"
    # Cloudoptics' broker role (the principal in customers' trust policies). When set, the
    # process credentials assume it first and it assumes the customer role (role chaining).
    aws_broker_role_arn: str = ""
    # Quick-create needs the templates in our S3 bucket. Until that bucket exists, onboarding
    # offers the template as a download for a manual "Upload a template file" in AWS.
    cfn_quick_create_enabled: bool = False
    cfn_template_download_base: str = (
        "https://raw.githubusercontent.com/akinwunmi-akinrimisi/cloudboosta-cloudoptics-app/"
        "main/infra/cloudformation"
    )

    # "inline" runs background jobs inside the request (local, tests, demo); staging and
    # production use the Procrastinate queue and a worker process.
    jobs_mode: Literal["inline", "queue"] = "inline"
    # Jobs one worker process runs at once. Each extra slot can hold one more bill load in
    # memory, so this is sized to the worker's memory: 2 for the 512 MB staging worker. Raise it
    # (CLOUDOPTICS_WORKER_CONCURRENCY) only with a larger worker instance.
    worker_concurrency: int = Field(default=2, ge=1, le=32)

    # Days between a workspace deletion request and the deletion, while admins can cancel it
    # (docs/08 §1.7). PRD US-8.3: the data is gone within 30 days of the request.
    tenant_deletion_grace_days: int = Field(default=7, ge=1, le=21)

    # Billing ingestion. With a folder set, normalised FOCUS Parquet is written there per
    # tenant and version (local development); staging keeps aggregates in PostgreSQL only
    # until object storage is provisioned (docs/11b §2.3).
    normalised_dir: str = ""
    # Limits on what one export may make the shared worker read (the customer writes the
    # bucket). A version past one is recorded as failed with the limit it hit.
    cur_max_manifest_bytes: int = 8 * 1024 * 1024
    cur_max_data_files: int = 500
    cur_max_period_bytes: int = 8 * 1024**3
    cur_max_period_rows: int = 20_000_000
    # Bill-only accounts (in the bills, not seen in the Organization) scanned per connection.
    max_bill_only_accounts: int = 100

    # Cloudoptics staff who may set exchange rates (comma-separated emails). A platform-admin
    # role with MFA replaces this list with the admin console (WBS ADM-10).
    platform_admin_emails: str = ""
    # Reference exchange-rate feed (ADR-010): a JSON URL answering {"base": "USD",
    # "rates": {"NGN": 1530.2, ...}}, as open.er-api.com does. Empty: no feed.
    fx_feed_url: str = ""

    # GetMetricData metric queries each tenant may spend per day (WBS CC-13). AWS bills
    # $0.01 per 1,000 metrics, so 20,000 is about $0.20 a day in the customer's account.
    metrics_daily_budget: int = 20_000

    # Email through Postmark (docs/11a). Without a server token, emails are logged and not
    # sent. The sender's address or domain must be verified in Postmark.
    postmark_server_token: str = ""
    email_from: str = "CloudOptics <alerts@cloudboosta.com>"
    # Public address of the web app, for links in emails.
    app_url: str = "http://localhost:5173"
    # Public address of this API, for the stack's callback. Empty: ``app_url`` + ``/api``
    # (App Platform routes /api to the API).
    api_public_url: str = ""

    # Rate limits (WBS API-8, docs/09 §3.1), per minute, shared by every API instance through
    # PostgreSQL. Users and workspaces on authenticated routes; client addresses on the public
    # routes (the stack callback, the dev token, the sign-in config).
    rate_limit_enabled: bool = True
    rate_limit_user_per_minute: int = 300
    rate_limit_tenant_per_minute: int = 1_000
    rate_limit_public_per_minute: int = 60
    rate_limit_callback_per_minute: int = 20
    # The public site's first-party beacon (SITE-15): page views and clicks per visitor address.
    rate_limit_site_per_minute: int = 120
    # "Send to my cloud admin" emails a workspace may send in 24 hours (ADR-022).
    connect_invites_per_day: int = 10
    # The request header that carries the client's address. App Platform sets
    # "do-connecting-ip" (its X-Forwarded-For names DigitalOcean's ingress, and clients can
    # send their own), so staging and production use that unless set; elsewhere the socket
    # peer is used. No other forwarding header is ever trusted.
    client_ip_header: str | None = None

    # In-process cache of the costliest aggregate responses, keyed by tenant and data version.
    query_cache_entries: int = 512
    query_cache_ttl_seconds: int = 600

    @property
    def trusted_ip_header(self) -> str:
        if self.client_ip_header is not None:
            return self.client_ip_header.strip().lower()
        return "do-connecting-ip" if self.environment in ("staging", "production") else ""

    @property
    def public_api_url(self) -> str:
        return (self.api_public_url or f"{self.app_url}/api").rstrip("/")

    @property
    def do_oauth_enabled(self) -> bool:
        return bool(self.do_oauth_client_id and self.do_oauth_client_secret.get_secret_value())

    @property
    def do_oauth_callback_url(self) -> str:
        return (
            self.do_oauth_redirect_url
            or f"{self.app_url.rstrip('/')}/connect/digitalocean/callback"
        )

    @property
    def cfn_access_template_url(self) -> str:
        """The single stack, hosted next to the billing template (infra/aws/bootstrap.py)."""
        return self.cfn_billing_template_url.rsplit("/", 1)[0] + "/cloudoptics-access.yaml"

    @property
    def oidc_configured(self) -> bool:
        return bool(self.workos_client_id or self.oidc_jwks_url)

    @property
    def platform_admins(self) -> frozenset[str]:
        return frozenset(
            e.strip().lower() for e in self.platform_admin_emails.split(",") if e.strip()
        )

    @field_validator("database_url", "app_database_url")
    @classmethod
    def _psycopg_driver(cls, url: str) -> str:
        """Accept plain ``postgresql://`` URLs (as DigitalOcean provides) for psycopg 3."""
        for prefix in ("postgresql://", "postgres://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url

    @model_validator(mode="after")
    def _guard_production(self) -> Self:
        if self.environment in ("staging", "production"):
            if self.auth_mode == "dev":
                raise ValueError(f"auth_mode=dev is not allowed in {self.environment}")
            if self.jobs_mode != "queue":
                raise ValueError(f"jobs_mode must be 'queue' in {self.environment}")
            if self.local_data_key == DEV_DATA_KEY and not self.kms_required:
                raise ValueError(f"the development data key is not allowed in {self.environment}")
        if self.kms_required and not self.kms_key_id:
            raise ValueError("kms_required is set but CLOUDOPTICS_KMS_KEY_ID is not")
        return self

    @property
    def uses_roles_anywhere(self) -> bool:
        if self.broker_credentials == "auto":
            return bool(self.roles_anywhere_certificate)
        return self.broker_credentials == "roles_anywhere"

    def roles_anywhere_problems(self) -> list[str]:
        if not self.uses_roles_anywhere:
            return []
        missing = [
            f"CLOUDOPTICS_{name.upper()}"
            for name in (
                "roles_anywhere_certificate",
                "roles_anywhere_trust_anchor_arn",
                "roles_anywhere_profile_arn",
                "roles_anywhere_role_arn",
            )
            if not getattr(self, name)
        ]
        if not self.roles_anywhere_private_key.get_secret_value():
            missing.append("CLOUDOPTICS_ROLES_ANYWHERE_PRIVATE_KEY")
        return [f"{name} is not set (Roles Anywhere)" for name in missing]

    def require_aws_connection_settings(self) -> None:
        """In staging/production, refuse placeholder or foreign AWS connection settings.

        Customers deploy the template at these URLs (the resource template as an org-wide
        StackSet) and trust the principal, so they must be explicitly set to Cloudoptics'
        own bucket and role. The API and worker call this at start-up; the migrate job, which
        does not get these settings, does not.
        """
        if self.environment not in ("staging", "production"):
            return
        problems: list[str] = []
        arn = self.cloudoptics_principal_arn
        match = _PRINCIPAL_ARN.fullmatch(arn)
        if "cloudoptics_principal_arn" not in self.model_fields_set:
            problems.append("CLOUDOPTICS_CLOUDOPTICS_PRINCIPAL_ARN is not set")
        elif arn == PLACEHOLDER_PRINCIPAL_ARN or not match or match.group(1) == "0" * 12:
            problems.append("CLOUDOPTICS_CLOUDOPTICS_PRINCIPAL_ARN is not an IAM role ARN")
        for field, name in (
            ("cfn_billing_template_url", "billing-role.yaml"),
            ("cfn_resource_template_url", "resource-role.yaml"),
        ):
            env = f"CLOUDOPTICS_{field.upper()}"
            url = getattr(self, field)
            if field not in self.model_fields_set:
                problems.append(f"{env} is not set")
            elif not _is_template_url(url, name):
                problems.append(
                    f"{env} must be https://cloudoptics-public-<account>.s3.<region>"
                    f".amazonaws.com/.../{name}"
                )
        problems += self.roles_anywhere_problems()
        if problems:
            raise RuntimeError(f"refusing to start in {self.environment}: " + "; ".join(problems))


def _is_template_url(url: str, name: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "https"
        and parts.netloc == parts.hostname  # no user info, port or upper case
        and _TEMPLATE_HOST.fullmatch(parts.hostname or "") is not None
        and parts.path.startswith("/")
        and parts.path.endswith(f"/{name}")
        and not parts.query
        and not parts.fragment
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
