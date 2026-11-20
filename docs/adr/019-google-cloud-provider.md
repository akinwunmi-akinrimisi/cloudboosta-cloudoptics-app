# ADR-019: Google Cloud provider (credentials, data, costs, limits)

**Status:** Decided 20 Nov 2026 (founder approved Google Cloud, Azure and DigitalOcean; multi-cloud plan, Phase 2a). Built against a fake Google Cloud only: no real Google project has been called yet. Checked line by line against Google's documentation on 20 Nov 2026 (see Sources and the 20 Nov audit below); the fake now follows the documented shapes.

## Context

ADR-018 made the core provider-neutral and left each cloud as a plug-in (`providers/<name>`). Google Cloud is the first new one. The market research (multicloud/market-research.md §4) found Google's bill is in BigQuery, as the detailed export with Google's FOCUS view or the managed FOCUS export (Preview, columns up to FOCUS 1.2), read with BigQuery Data Viewer on the dataset and BigQuery Job User on a project. docs/10 and docs/11b forbid long-lived secrets on our side.

## Decision

### Credentials: no Google key anywhere

Workload Identity Federation with AWS as the identity provider (`providers/gcp/client.py`):

1. The broker's short-lived AWS credentials (Roles Anywhere, ADR-007) sign an `sts:GetCallerIdentity` request with SigV4 (botocore's signer). Only the signed request leaves Cloudoptics.
2. Google STS (`sts.googleapis.com/v1/token`, `subject_token_type=urn:ietf:params:aws:token-type:aws4_request`, audience without `https:`) checks it with AWS and returns a federated token for our pool `cloudoptics`, provider `aws-broker`. The provider accepts only the broker account (`--account-id`). Google's documented mapping gives `attribute.aws_role` = the role name, and the condition `assertion.arn.startsWith('arn:aws:sts::<broker account>:assumed-role/CloudopticsBroker/')` admits only that role's sessions. `roles/iam.workloadIdentityUser` is granted to `principalSet://.../attribute.aws_role/CloudopticsBroker`.
3. The federated token impersonates `cloudoptics-reader@<project>.iam.gserviceaccount.com` for one hour (`iamcredentials ... :generateAccessToken`). The service account has no keys and no roles in our project. Customers grant it read roles.

The token is cached until five minutes before its `expireTime`. Transient errors (429, 500, 502, 503, 504, and BigQuery's `rateLimitExceeded`/`backendError`, which BigQuery sends as 403) are retried three times with truncated exponential backoff and jitter, honouring `Retry-After`. `jobs.query` carries a `requestId`, so a retry does not run a query twice. A 403 that is a quota error is not treated as a missing permission. `CLOUDOPTICS_GCP_FEDERATION_ROLE_ARN` can name a dedicated AWS role the broker assumes first, if the owner wants the federation trust narrower than the broker role.

**REST, not SDKs.** Google's Python libraries (google-auth, google-cloud-bigquery, and the gRPC and protobuf stacks behind them) would add about 30 packages to the lockfile for a dozen small JSON calls. The plug-in uses httpx, which is already a dependency. The lockfile is unchanged.

### Telling workspaces apart: the dataset label

Every customer grants roles to the same service account, so that account alone cannot tell workspaces apart. Anyone could type someone else's dataset into Cloudoptics. Each connection therefore gets a random 128-bit **label value**. It plays the part of AWS's External ID: stored encrypted in `cloud_connection.external_id_enc` and shown only to the workspace's admins. The customer sets it as label `cloudoptics` on the dataset (`bq update --set_label`), which only someone who can change that dataset can do. Until the label matches, the check fails (`OwnershipNotProven`) and no bill is read. Only then is the billing account reserved for the workspace (`account_verified_at`, migration 0017's one-verified-workspace rule, per billing account).

**Views prove their sources (changed 20 Nov 2026).** A label on the dataset that holds a *view* proves nothing about what the view reads. Anyone can save a view over another customer's export in a dataset of their own and label that dataset. BigQuery would then run the view with the shared service account's access to the other customer's data (a confused deputy). So when `tables.get` says the table is not a plain `TABLE`, the check dry-runs a query on it (`jobs.insert` with `dryRun`, not billed) and reads `statistics.query.referencedTables`. Every dataset among them must carry the same label, or the new check `view_sources` fails the connection (`OwnershipNotProven`) and nothing is read. BigQuery Data Viewer includes `bigquery.datasets.get`, and `datasets.get` returns the labels, so Data Viewer is enough to read them. Setting a label needs `bigquery.datasets.update` (BigQuery Data Owner or Admin). BigQuery's sharing docs say a subscriber can update a linked dataset's description and labels, so the label can go on the managed FOCUS export's read-only linked dataset. Projects come from the bill itself (`SubAccountId`), never from typed input, so a workspace scans only projects billed to its own proven billing account.

### Data model (migration 0044)

- `cloud_connection` row: `provider = 'gcp'`, `provider_account_id` = billing account ID (`XXXXXX-XXXXXX-XXXXXX`), `account_type = 'management'` (it covers many projects), `role_arn` = our service account, `stack_name = 'gcp'`, no CUR columns.
- `gcp_connection` (new; FORCE RLS, tenant policy, removed with its connection; in the deletion `TABLES`, `UNVERSIONED`, and seeded for the cross-tenant test): billing account, export project, dataset, table or view, export kind (`focus_export` | `focus_view`), the query project (which pays for queries; default: the export project) and the dataset location.
- Projects are `cloud_account` rows (`account_type = 'member'`). The billing account has one too (`management`), for lines without a project (tax, support).
- Resource IDs are Google's relative names (`instances/web-1`, `disks/web-1`), because an instance and its boot disk often share a name and a zone. Types: `gce_instance`, `gce_disk`, `gce_address`, `gce_snapshot`, `cloudsql_instance`.

### Connect flow and checks

`POST /v1/tenants/{t}/connections/gcp` (admins) returns the setup: the service account, the label value and four copy-paste commands. They label the dataset, `GRANT roles/bigquery.dataViewer ON SCHEMA`, grant `roles/bigquery.jobUser` on the query project, and optionally grant Compute Viewer, Monitoring Viewer, the Compute and Cloud SQL Recommender Viewers and Cloud SQL Viewer per project. Console steps are offered as well. `GET .../gcp/{id}` shows the setup again. Checking, listing, reload, scan and disconnect use the shared routes, which now dispatch by provider. The checks keep the `permission_checks` shape:

| Check | Fails the connection when | Otherwise |
|---|---|---|
| `federation` | Our side cannot get a token, or is not set up (`GcpNotConfigured`) | |
| `dataset` | The dataset is missing or not readable | |
| `ownership` | The label does not match, or the billing account is proven in another workspace (`AccountInUse`) | |
| `export_table` | | error (degraded): no FOCUS columns |
| `view_sources` | The table is a view and a dataset it reads lacks the label (`OwnershipNotProven`) | (only for views) |
| `query_jobs` | | error (degraded): no `bigquery.jobs.create` (testIamPermissions) |
| `billing_account` | The export belongs to another billing account | warning: no rows yet |
| `projects` | | warning: the projects without the optional roles; skipped before the first bill |

### Costs

`billing.ingest` reads each billing period with one BigQuery query. The query filters on `BillingPeriodStart`, groups to the `cost_daily` grain (day, project, service, region, SKU, charge category, tags) and is capped by `maximumBytesBilled` (20 GiB by default) and a row limit (500,000 groups). Columns a FOCUS table lacks are read as NULL. The version is the SHA-256 of the result. This month and last month are read at every 6-hourly poll; the first load also reads two earlier months. A new version replaces the period in one transaction, the same idempotent period replace as AWS's CUR loads, recorded in `ingest_batch` (`mapping_version gcp-focus-1`, `manifest_key bq://project.dataset.table`). `provider_account_id` is the project ID, or the billing account for project-less lines. Google's FOCUS export has no `Credit` or `Purchase` rows (its `ChargeCategory` is `Usage`, `Tax` or `Adjustment`). Credits are inside `BilledCost` and `EffectiveCost` (cost + credits) and itemised in `x_Credits`. Net cost = effective cost. The export has no FOCUS `Tags` or `ServiceCategory`. Tags are read from `Tags` when a table has it (a FOCUS view) and otherwise from `x_Labels` (resource labels, `[{Key, Value}]`); service category is NULL (`Other`). `BillingPeriodStart` is a TIMESTAMP made from `invoice.month`. Google does not document its hour, and Google's invoice months follow US Pacific time, so a period matches `BillingPeriodStart` within a day of midnight UTC rather than by equality.

**Currency.** GCP bills may be in any currency (`billing_currencies = None`). Rows keep `billing_currency` and native amounts. USD is converted day by day: first the FX service (a staff-set rate, ADR-010); when Cloudoptics has no rate for that day, the rate Google used (`x_CurrencyConversionRate`, local currency per USD, so cost / rate = USD; flagged as a warning); a day with neither blocks the period (`fx` error) and the previous load stays.

**Latency and retention.** Google gives no delivery or latency guarantee. A new export starts within a few hours. A multi-region (US or EU) dataset is backfilled from the start of the previous month, which can take up to five days; a single-region dataset is not backfilled. The FOCUS table keeps two years. The export is in Preview and its schema can change, which is why columns a table lacks are read as NULL.

**Who pays for queries.** The customer's query project pays, at BigQuery on-demand prices ($6.25 a TiB; the first TiB a month is free). A month of grouped FOCUS rows usually scans a few MB. Bytes billed and the estimate are recorded on each `sync_run`.

### Reconciliation and month to date

Google has no invoice API: invoice totals are in the console only. `billing.reconcile` therefore compares each loaded period, per project, with BigQuery's own `SUM(BilledCost)` from the source table, in USD at the period's stored rate. This checks the load from end to end (rows lost, doubled, or restated since the load), not the invoice. The month-to-date snapshot (`ce.snapshot`) returns `skipped`: the export itself is near real time and this month is loaded at every poll.

### Inventory, metrics, Recommender and rules

Per project, at most `CLOUDOPTICS_GCP_MAX_PROJECTS` (100), the scan reads the Compute API (aggregated instances, disks and addresses; global addresses and snapshots) and the Cloud SQL Admin API. Cloud Asset Inventory would list everything in one call but needs an organization-level grant, which is more than the costs-first customer gives. Recommender (`google.compute.instance.{IdleResource,MachineType}Recommender`, the disk and address idle recommenders, and the Cloud SQL idle and over-provisioned recommenders) is read for the zones and regions that hold resources. Locations follow Google's guides: VMs and machine types by zone; disks by zone or region; addresses by region, plus `global` for global addresses; Cloud SQL by region. A recommendation is matched to a resource by its overview and then its operations, with `add` operations last (the idle-disk recommendation adds a snapshot before it removes the disk). Savings are `-costProjection.cost` (units + nanos, in its `currencyCode`) over `costProjection.duration` (a `"2592000s"` string). Its findings are kept on each resource (`config.recommendations`) with their savings converted to USD at today's rate. Cloud Monitoring (`timeSeries.list` with `view=FULL`, filter `metric.type = ... AND resource.type = ...`, `ALIGN_MEAN` over 3600 s) gives the hourly mean CPU of running instances (`compute.googleapis.com/instance/cpu/utilization` on `gce_instance`) and Cloud SQL instances (`cloudsql.googleapis.com/database/cpu/utilization` on `cloudsql_database`). Both are fractions (0.0 to 1.0), stored as percent (`resource_metric`, metric `cpu`). The roles asked for in each project are Compute Viewer, Monitoring Viewer, Compute Recommender Viewer (`roles/recommender.computeViewer`), Cloud SQL Recommender Viewer (`roles/recommender.cloudsqlViewer`) and Cloud SQL Viewer. The two Recommender roles replace Recommender Viewer, which reads every recommender's findings.

The rules (`providers/gcp/rules.py`, `RuleSpec` provider `gcp`) are GCP1 idle VM, GCP2 unattached persistent disk, GCP3 reserved external IP not in use, GCP4 snapshot 90+ days old of a deleted disk, GCP5 Recommender machine type, GCP6 Recommender idle Cloud SQL, and GCP7 Recommender over-provisioned Cloud SQL. Each fix is `gcloud` commands with a rollback, a console link with steps, and risk factors. GCP1-GCP4 price with Google's public us-central1 list prices (`list_price` basis, an estimate). GCP5-GCP7 take Recommender's savings, which use the customer's own prices (`effective_rate`). Fingerprints are `gcp:<rule>:<project>:<zone or region>:<kind>/<name>`.

## Consequences and limits

- **Not yet proven against Google.** Everything is tested against `tests/fake_gcp.py`, which now follows the documented shapes. The docs could not settle the points under "Needs a real account" below.
- `cost_resource_daily` is not filled for Google Cloud yet. Realised savings use the recommendation's estimate until it is.
- Commitments (CUDs), price catalog and the R8-style commitment sizing are not built (`commitments`/`prices` capabilities absent).
- Per-rule settings (`analysis/settings.py RULE_IDS`) do not list the GCP rules yet, so a workspace cannot switch them off one by one.
- Recommender savings are converted at today's rate, not the bill's; native amounts stay in the evidence.
- The 6-hourly ingestion poll now includes every non-AWS connection (`cur_bucket` is AWS's export). Azure and DigitalOcean have the same need; the merger keeps one condition.
- Shared routes changed to dispatch by provider: validation in inline mode, scan, ingest-now, reload, and reconciliation after an inline reload. AWS behaviour is unchanged.

## Owner steps

1. Choose a Google Cloud project of Cloudoptics' own per environment, then run `PROJECT_ID=... AWS_ACCOUNT_ID=<broker account> ./infra/gcp/setup.sh` in Cloud Shell. It creates the pool, the AWS provider and the keyless service account, and prints the two settings.
2. Set `CLOUDOPTICS_GCP_WORKLOAD_PROVIDER` and `CLOUDOPTICS_GCP_SERVICE_ACCOUNT` on the API and worker (not secrets). Until both are set, the wizard shows "not switched on" and checks fail with `GcpNotConfigured`.
3. Optional: set the organization policy `iam.disableServiceAccountKeyCreation`.
4. For a first real test: a Google Cloud **billing account** with at least one project that has spend, the **BigQuery billing export** turned on into a dataset (the managed FOCUS export, or the detailed export plus Google's FOCUS view saved as a view), and a day of data. Connect it from a staging workspace and run the four commands. Watch `sync_run` (`cur.ingest` bytes billed) and compare the loaded month with the console's billing report.

## 20 Nov 2026 audit against Google's documentation

| Area | Finding | Change |
|---|---|---|
| Workload Identity Federation | Token format, STS parameters and generateAccessToken matched. The mapping and principal set used the full assumed-role ARN; Google documents the role name. | setup.sh uses the documented mapping and a `startsWith` condition on the broker role, and updates an existing provider. Token expiry comes from `expireTime`. |
| Billing export | The managed FOCUS export exists (Preview, 8 Jun 2026): dataset `gcp_billing_immutable_<ID>_<location>`, table `gcp_billing_export_focus_<ID>`. It has no Credit rows, no Tags or ServiceCategory, and labels in `x_Labels`. `BillingPeriodStart = TIMESTAMP(@period)` could match no rows. | Period window, `x_Labels` for tags, credits text fixed, and the wizard fills the table name in. |
| BigQuery jobs | `maximumBytesBilled`, `location`, `dryRun`, `labels` and paging matched. `totalBytesBilled` is in QueryResponse, not GetQueryResultsResponse. | Bytes billed are read from jobs.query; `requestId` makes retries safe. |
| Dataset labels | `datasets.get` (in Data Viewer) returns labels, and linked datasets accept label updates. The label did not cover views. | `view_sources` check (above). |
| Recommender | IDs matched. Global addresses and regional disks were never queried. The idle-disk recommendation was matched to its snapshot, not the disk. One error stopped every location. Recommender Viewer is broader than needed. | Locations per type, operation-aware matching, errors per location, and the two narrower roles. |
| Inventory and Monitoring | Fields matched. `view` was not sent, and the filter did not name the resource type. | Both added. |
| Resource Manager | testIamPermissions needs no permission and takes no wildcards. Matched. | None. |
| Errors and retries | The format matched, but BigQuery's 403 `rateLimitExceeded` counted as access denied, and nothing was retried. | Retries, and a quota-aware `denied`. |

### Needs a real account

1. That `bq update --set_label` and the `GRANT ... ON SCHEMA` statement both work on the managed export's linked dataset. The docs say labels can be updated and table-level grants cannot, but they do not show a dataset-level grant on a linked dataset.
2. The `type` that `tables.get` reports for the managed export's table. If it is a `VIEW` over Google's own dataset, `view_sources` would fail for every customer, and the check would need an exception for the managed dataset.
3. That a dry run fills `referencedTables`, and that it lists a view's base tables.
4. The hour stored in `BillingPeriodStart`, and how the managed table is partitioned (bytes scanned per period).
5. The columns of Google's FOCUS view. Its guide is behind a sign-up page, so `Tags` and the x_ columns are unverified there.
6. Recommender's operation `path` and `value` for machine-type and Cloud SQL tier changes (the docs have no example), and whether `cost` is in USD with `costInLocalCurrency` in the bill's currency.
7. Whether Google accepts the regional AWS STS endpoint (`sts.<region>.amazonaws.com`, which google-auth uses) as well as the global one in its example.

## Sources (read 20 Nov 2026)

- https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds
- https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-tables
- https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-focus-setup
- https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-tables/focus-export
- https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-tables/detailed-usage
- https://docs.cloud.google.com/billing/docs/release-notes
- https://cloud.google.com/blog/topics/cost-management/cloud-costs-come-into-view-with-focus-v1-0-ga
- https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/jobs/query
- https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/Job
- https://docs.cloud.google.com/bigquery/docs/access-control
- https://docs.cloud.google.com/bigquery/docs/viewing-labels
- https://docs.cloud.google.com/bigquery/docs/analytics-hub-view-subscribe-listings
- https://docs.cloud.google.com/bigquery/docs/troubleshoot-quotas
- https://docs.cloud.google.com/recommender/docs/recommenders
- https://docs.cloud.google.com/compute/docs/viewing-and-applying-idle-resources-recommendations
- https://docs.cloud.google.com/compute/docs/instances/apply-machine-type-recommendations-for-instances
- https://docs.cloud.google.com/recommender/docs/reference/rest/v1/projects.locations.recommenders.recommendations
- https://recommender.googleapis.com/$discovery/rest?version=v1
- https://docs.cloud.google.com/iam/docs/roles-permissions/recommender
- https://docs.cloud.google.com/compute/docs/reference/rest/v1/disks/aggregatedList
- https://docs.cloud.google.com/monitoring/api/ref_v3/rest/v3/projects.timeSeries/list
- https://docs.cloud.google.com/monitoring/api/metrics_gcp_c
- https://docs.cloud.google.com/resource-manager/reference/rest/v1/projects/testIamPermissions
- https://google.aip.dev/193
- https://google.aip.dev/194

## Alternatives

- **A service account key per environment.** Rejected: a long-lived secret (docs/10 T2).
- **One service account per workspace** (tenant isolation by identity). Rejected for now: it needs IAM writes in our project at sign-up, and a quota of about 100 service accounts per project. The label gives the same proof without them.
- **Direct federated access** (customers grant roles to a workspace-specific `principal://` subject, no service account). Simpler isolation, but not every API used here supports federated principals, and customers' IAM screens make it harder to read. Worth revisiting.
- **Google's SDKs.** Rejected for size; see above.
- **Running queries in our project** (we pay). Possible later via `query_project_id`. Today the customer's project pays, which is visible in their bill and costs us nothing.
