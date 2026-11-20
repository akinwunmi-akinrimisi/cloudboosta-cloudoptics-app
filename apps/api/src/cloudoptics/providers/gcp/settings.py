"""Google Cloud settings, read from ``CLOUDOPTICS_GCP_*`` environment variables (ADR-019).

Kept beside the plug-in rather than in ``platform/settings.py`` so each provider owns its
configuration. Nothing here is a secret: Cloudoptics reaches Google Cloud with Workload
Identity Federation from its AWS broker identity, so there is no Google key to store.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

#: BigQuery on-demand price per TiB scanned (us and eu multi-regions, Nov 2026); the
#: customer's query project pays it. Used for the cost estimate recorded with each run.
BQ_USD_PER_TIB = 6.25


class GcpSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLOUDOPTICS_GCP_", extra="ignore")

    #: The workload identity pool provider that trusts our AWS broker account:
    #: ``//iam.googleapis.com/projects/<number>/locations/global/workloadIdentityPools/<pool>/
    #: providers/<provider>`` (infra/gcp/setup.sh prints it).
    workload_provider: str = ""
    #: Cloudoptics' own service account that customers grant read roles to.
    service_account: str = ""
    #: Optional AWS role the broker assumes before signing for Google (a dedicated role keeps
    #: the federation trust narrower than the broker role). Empty: the broker itself signs.
    federation_role_arn: str = ""
    aws_region: str = "us-east-1"

    #: Every BigQuery job is capped: a query that would scan more fails instead of costing more.
    max_bytes_billed: int = 20 * 1024**3
    #: Grouped rows read for one billing period at most (the bill is aggregated in BigQuery).
    max_rows_per_period: int = 500_000
    #: Months before the current one read on the first load.
    backfill_months: int = 2
    #: Projects scanned for resources and recommendations per connection at most.
    max_projects: int = 100
    http_timeout_seconds: float = 60.0
    #: Transient Google errors (429, 5xx, BigQuery's ``rateLimitExceeded`` and
    #: ``backendError``) are retried this many times, with truncated exponential backoff
    #: from ``retry_base_seconds`` (AIP-194; BigQuery's quota troubleshooting page).
    retries: int = 3
    retry_base_seconds: float = 1.0

    sts_url: str = "https://sts.googleapis.com/v1/token"
    iamcredentials_url: str = "https://iamcredentials.googleapis.com/v1"
    bigquery_url: str = "https://bigquery.googleapis.com/bigquery/v2"
    resourcemanager_url: str = "https://cloudresourcemanager.googleapis.com/v1"
    compute_url: str = "https://compute.googleapis.com/compute/v1"
    monitoring_url: str = "https://monitoring.googleapis.com/v3"
    recommender_url: str = "https://recommender.googleapis.com/v1"
    sqladmin_url: str = "https://sqladmin.googleapis.com/v1"

    @property
    def enabled(self) -> bool:
        return bool(self.workload_provider and self.service_account)


@lru_cache
def get_gcp_settings() -> GcpSettings:
    return GcpSettings()
