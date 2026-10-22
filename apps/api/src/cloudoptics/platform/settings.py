"""Process settings, read from environment variables prefixed ``CLOUDOPTICS_``."""

from functools import lru_cache
from typing import Literal, Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_AUTH_SECRET = "dev-only-secret-change-me-0123456789abcdef"  # noqa: S105 - refused outside dev
DEV_DATA_KEY = "Y2xvdWRvcHRpY3MtbG9jYWwtZGV2LWtleS0zMmJ5dGU="  # dev only gitleaks:allow


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
    dev_auth_secret: str = DEV_AUTH_SECRET

    # Base64 32-byte key for the local envelope encryptor. KMS replaces it in staging/prod.
    local_data_key: str = DEV_DATA_KEY

    # AWS connection (PRD FR-CON-01/13). Templates must be served from S3 for quick-create.
    cloudoptics_principal_arn: str = "arn:aws:iam::000000000000:role/CloudopticsBroker"
    cfn_billing_template_url: str = (
        "https://cloudoptics-public.s3.eu-west-2.amazonaws.com/cfn/v1/billing-role.yaml"
    )
    cfn_resource_template_url: str = (
        "https://cloudoptics-public.s3.eu-west-2.amazonaws.com/cfn/v1/resource-role.yaml"
    )
    aws_region: str = "eu-west-2"

    # "inline" runs background jobs inside the request (local, tests, demo); staging and
    # production use the Procrastinate queue and a worker process.
    jobs_mode: Literal["inline", "queue"] = "inline"

    @model_validator(mode="after")
    def _guard_production(self) -> Self:
        if self.environment in ("staging", "production"):
            if self.auth_mode == "dev":
                raise ValueError(f"auth_mode=dev is not allowed in {self.environment}")
            if self.jobs_mode != "queue":
                raise ValueError(f"jobs_mode must be 'queue' in {self.environment}")
            if self.local_data_key == DEV_DATA_KEY:
                raise ValueError(f"the development data key is not allowed in {self.environment}")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
