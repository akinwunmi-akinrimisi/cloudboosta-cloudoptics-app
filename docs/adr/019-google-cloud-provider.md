# ADR-019: Google Cloud provider (credentials, data, costs, limits)

**Status:** Decided 20 Nov 2026 (founder approved Google Cloud, Azure and DigitalOcean; multi-cloud plan, Phase 2a). Built against a fake Google Cloud only: no real Google project has been called yet. Checked line by line against Google's documentation on 20 Nov 2026 (see Sources and the 20 Nov audit below); the fake now follows the documented shapes. **Changed 20 Nov 2026 (founder approved): one Cloudoptics service account per workspace connection and a connect flow with no commands** (see "One service account per connection"); the dataset label and its view check are gone.

## Context

ADR-018 made the core provider-neutral and left each cloud as a plug-in (`providers/<name>`). Google Cloud is the first new one. The market research (multicloud/market-research.md §4) found Google's bill is in BigQuery, as the detailed export with Google's FOCUS view or the managed FOCUS export (Preview, columns up to FOCUS 1.2), read with BigQuery Data Viewer on the dataset and BigQuery Job User on a project. docs/10 and docs/11b forbid long-lived secrets on our side.

## Decision

### Credentials: no Google key anywhere

Workload Identity Federation with AWS as the identity provider (`providers/gcp/client.py`):

1. The broker's short-lived AWS credentials (Roles Anywhere, ADR-007) sign an `sts:GetCallerIdentity` request with SigV4 (botocore's signer). Only the signed request leaves Cloudoptics.
2. Google STS (`sts.googleapis.com/v1/token`, `subject_token_type=urn:ietf:params:aws:token-type:aws4_request`, audience without `https:`) checks it with AWS and returns a federated token for our pool `cloudoptics`, provider `aws-broker`. The provider accepts only the broker account (`--account-id`). Google's documented mapping gives `attribute.aws_role` = the role name, and the condition `assertion.arn.startsWith('arn:aws:sts::<broker account>:assumed-role/CloudopticsBroker/')` admits only that role's sessions. `roles/iam.workloadIdentityUser` is granted to `principalSet://.../attribute.aws_role/CloudopticsBroker`.
3. To read a customer's data, the federated token impersonates **that connection's own** service account (`co-<12 hex>@<project>.iam.gserviceaccount.com`) for one hour (`iamcredentials ... :generateAccessToken`). The accounts have no keys. The federated token itself (no service account) creates and deletes them through the IAM API, which has no known limitations for federated principals.

The federated token and each account's token are cached until five minutes before they expire. Transient errors (429, 500, 502, 503, 504, and BigQuery's `rateLimitExceeded`/`backendError`, which BigQuery sends as 403) are retried three times with truncated exponential backoff and jitter, honouring `Retry-After`. `jobs.query` carries a `requestId`, so a retry does not run a query twice. A 403 that is a quota error is not treated as a missing permission. `CLOUDOPTICS_GCP_FEDERATION_ROLE_ARN` can name a dedicated AWS role the broker assumes first, if the owner wants the federation trust narrower than the broker role.

**REST, not SDKs.** Google's Python libraries (google-auth, google-cloud-bigquery, and the gRPC and protobuf stacks behind them) would add about 30 packages to the lockfile for a dozen small JSON calls. The plug-in uses httpx, which is already a dependency. The lockfile is unchanged.

### One service account per connection (20 Nov 2026; replaces the dataset label)

**Decision.** Each Google Cloud connection gets its own service account in Cloudoptics' project (`CLOUDOPTICS_GCP_SERVICE_ACCOUNT_PROJECT`), made when the connection is created (`providers/gcp/identity.py`) and recorded on it (`gcp_connection.service_account_email`, also `cloud_connection.role_arn`). Every read for the connection is made as that account. The customer shares the export with that account only, so data shared with one workspace's account can only be read by that workspace: workspace B typing workspace A's project and dataset gets a 403, because B's account was never given A's data. A view is no way round it: BigQuery runs a view with the querying account's own access to the tables it reads, unless their owner authorised the view, so a view of B's over A's export reads nothing. The dataset label, its encrypted value and the `view_sources` check are removed. What stays: the export's `BillingAccountId` must be the connection's billing account (`billing_account`), the billing account is reserved for the first workspace that proves it (migration 0017, `AccountInUse`), and projects come from the bill, never from typed input.

**Names.** `co-` + 12 random hex characters: 15 characters, inside Google's 6-30 and its pattern `[a-z]([-a-z0-9]*[a-z0-9])`. The display name is "Cloudoptics connection"; the description names the connection ID only (no workspace or person's name leaves Cloudoptics).

**Rights in our project (infra/gcp/setup.sh).** The broker's principal set holds, on the project: Create Service Accounts (`iam.serviceAccounts.create`), Delete Service Accounts, and Workload Identity User (`iam.serviceAccounts.getAccessToken`). Google's WIF guide grants Workload Identity User on each service account; the role has no "lowest-level resource" restriction, so it can also be granted on the project, which covers every account in it. **Least privilege:** per-account is narrower *as granted*, but it would mean Cloudoptics writing an IAM policy on each new account, which needs `iam.serviceAccounts.setIamPolicy` (Service Account Admin). With that right the broker could grant itself impersonation of any account anyway, so per-account binding adds an admin right without reducing what the broker can reach. Project-level Workload Identity User plus Create/Delete is the smaller set of permissions; the project must therefore hold nothing but these accounts.

**Lifecycle.** Disconnect deletes the account (`identity.retire`, from `connectors.service.mark_disconnected`): everything the customer granted it stops working at once and shows as `deleted:serviceAccount:...` until Google purges it (within 60 days); a new account never inherits it. If Google cannot be reached, the account is marked `delete_pending` (audit `gcp.service_account_delete_failed`) and deleted at the connection's data deletion or the workspace's deletion (`retire_all`, before the data goes). Reconnecting gives the connection a new account (the customer shares again). Delete was preferred to disable: a disabled account still counts toward the quota and needs Service Account Admin to disable. Created, deleted and failed deletes are audit entries.

**Quota.** Google: the number of service accounts "depends on your project" (Quotas page, "Service Account Count"; older localised pages and community answers say 100); deleted accounts do not count; the console's quota page can raise it, or Google support if it cannot. A full quota makes connecting fail with HTTP 503 "Cloudoptics has reached its Google Cloud limit for connection accounts", nothing is written (the request rolls back), and it is logged. The owner raises it (runbook RB-21). One account per connection, so the limit is the number of live Google Cloud connections across all workspaces. Google also limits create requests per minute and says a new account can take 60 seconds or more to be usable: a check right after connecting may say "still being set up; try again in a minute".

**Queries run in our project, at our cost (no BigQuery Job User for the customer).** Job User can only be granted on projects, folders or organisations, and setup.sh grants it once on our project to `principalSet://cloudresourcemanager.googleapis.com/projects/<number>/type/ServiceAccount` (all service accounts of the project; supported in allow policies), so no policy is written per connection. BigQuery bills the project that runs the query ("You are charged for queries run against shared data. The data owner is not charged"); jobs run in the dataset's location whatever the project. Cost: a month of grouped FOCUS rows usually scans a few MB, billed at least 10 MB per table and per query; four polls a day of two months is about 240 queries a month per connection, so small customers stay in the free first TiB and a large one (1 GB a scan) is about $1.50 a month. The risk is a hostile or huge export: each query is capped by `maximumBytesBilled` (20 GiB, about $0.12), and the owner sets BigQuery's custom quotas on our project, "Query usage per day" (all of Cloudoptics) and "Query usage per day per user", which applies to each service account and so caps each connection; over a cap, queries fail with `quotaExceeded` until the next day. A connection may still name its own query project (More options); then it needs Job User there and pays (`customer_pays`, the check says "finish step 3"). `est_customer_api_cost_usd` is 0 when we pay.

**Migration (0049).** Label-era connections (none from real customers) are disconnected. Connecting the same billing account again reuses the row, keeping its cost history, and gives it its own account. Their removal steps still list the old shared account's bindings and the label.

### Data model (migration 0044)
### Data model (migration 0044)

- `cloud_connection` row: `provider = 'gcp'`, `provider_account_id` = billing account ID (`XXXXXX-XXXXXX-XXXXXX`), `account_type = 'management'` (it covers many projects), `role_arn` = the connection's own service account (migration 0049: also `gcp_connection.service_account_email` and `service_account_state`; `external_id_enc` is NULL), `stack_name = 'gcp'`, no CUR columns.
- `gcp_connection` (new; FORCE RLS, tenant policy, removed with its connection; in the deletion `TABLES`, `UNVERSIONED`, and seeded for the cross-tenant test): billing account, export project, dataset, table or view, export kind (`focus_export` | `focus_view`), the query project (NULL since 0049: Cloudoptics' project, which pays; or the customer's own) and the dataset location.
- Projects are `cloud_account` rows (`account_type = 'member'`). The billing account has one too (`management`), for lines without a project (tax, support).
- Resource IDs are Google's relative names (`instances/web-1`, `disks/web-1`), because an instance and its boot disk often share a name and a zone. Types: `gce_instance`, `gce_disk`, `gce_address`, `gce_snapshot`, `cloudsql_instance`.

### Connect flow and checks

`POST /v1/tenants/{t}/connections/gcp` (admins) creates the connection and its service account, and returns the setup: the account's email, console deep links, the roles for step 3 and the same steps as commands. The wizard (apps/web `GcpConnect.tsx`) needs no commands:

1. **Turn on the billing export** ("Enable FOCUS export, then Save"), linked to `https://console.cloud.google.com/billing/export`, the page Google's export guides link to. Google has no API for it. Then the form: billing account, project, dataset, table.
2. **Share the dataset with your account**: the account with a Copy button, and a link to the dataset's page, `https://console.cloud.google.com/bigquery?project=<p>&p=<p>&d=<d>&page=dataset` (the form Google's public-dataset docs use), where Google's steps are Sharing, Permissions, Add principal, BigQuery Data Viewer. For a FOCUS view, each dataset it reads too.
3. **Grant the viewer roles once** (optional: resources and recommendations): Compute Viewer, Monitoring Viewer, Compute Recommender Viewer, Cloud SQL Recommender Viewer and Cloud SQL Viewer, on the organisation or one project, linked to `https://console.cloud.google.com/iam-admin/iam?project=<p>` and to the IAM page's resource picker (`/projectselector/iam-admin/iam?supportedpurview=project,folder,organizationId`). All five can be granted on an organisation or folder (Monitoring and Cloud SQL Viewer's lowest level is the project; the Recommender viewers have no restriction), and projects inherit them. BigQuery Job User is added only for a connection with its own query project.

"Prefer commands?" (collapsed) has the `GRANT ... ON SCHEMA`, the Job User command when needed, and the project-roles loop. **Check access** runs the checks; a failed check stays on the page with its words, which name the step: "We can't see dataset p.d yet: finish step 2. Share it with co-...@... as BigQuery Data Viewer." `GET .../gcp/{id}` shows the setup again (and makes the account if the connection was created while Google Cloud was switched off). Checking, listing, reload, scan and disconnect use the shared routes. The checks keep the `permission_checks` shape:

| Check | Fails the connection when | Otherwise |
|---|---|---|
| `federation` | Our side cannot get a token, is not set up (`GcpNotConfigured`), or the account is not usable yet / could not be made | |
| `dataset` | The dataset is missing or not shared with this account ("finish step 2") | |
| `export_table` | | error (degraded): not found, or no FOCUS columns |
| `query_jobs` | | error (degraded): no `bigquery.jobs.create` in the query project (ours: our side; theirs: "finish step 3") |
| `billing_account` | The export belongs to another billing account ("check step 1"), or the account is proven in another workspace (`AccountInUse`) | error (degraded): the query was refused (a view's sources not shared); warning: no rows yet |
| `projects` | | warning: the projects without the optional roles ("finish step 3"); skipped before the first bill |

### Costs

`billing.ingest` reads each billing period with one BigQuery query. The query filters on `BillingPeriodStart`, groups to the `cost_daily` grain (day, project, service, region, SKU, charge category, tags) and is capped by `maximumBytesBilled` (20 GiB by default) and a row limit (500,000 groups). Columns a FOCUS table lacks are read as NULL. The version is the SHA-256 of the result. This month and last month are read at every 6-hourly poll; the first load also reads two earlier months. A new version replaces the period in one transaction, the same idempotent period replace as AWS's CUR loads, recorded in `ingest_batch` (`mapping_version gcp-focus-1`, `manifest_key bq://project.dataset.table`). `provider_account_id` is the project ID, or the billing account for project-less lines. Google's FOCUS export has no `Credit` or `Purchase` rows (its `ChargeCategory` is `Usage`, `Tax` or `Adjustment`). Credits are inside `BilledCost` and `EffectiveCost` (cost + credits) and itemised in `x_Credits`. Net cost = effective cost. The export has no FOCUS `Tags` or `ServiceCategory`. Tags are read from `Tags` when a table has it (a FOCUS view) and otherwise from `x_Labels` (resource labels, `[{Key, Value}]`); service category is NULL (`Other`). `BillingPeriodStart` is a TIMESTAMP made from `invoice.month`. Google does not document its hour, and Google's invoice months follow US Pacific time, so a period matches `BillingPeriodStart` within a day of midnight UTC rather than by equality.

**Currency.** GCP bills may be in any currency (`billing_currencies = None`). Rows keep `billing_currency` and native amounts. USD is converted day by day: first the FX service (a staff-set rate, ADR-010); when Cloudoptics has no rate for that day, the rate Google used (`x_CurrencyConversionRate`, local currency per USD, so cost / rate = USD; flagged as a warning); a day with neither blocks the period (`fx` error) and the previous load stays.

**Latency and retention.** Google gives no delivery or latency guarantee. A new export starts within a few hours. A multi-region (US or EU) dataset is backfilled from the start of the previous month, which can take up to five days; a single-region dataset is not backfilled. The FOCUS table keeps two years. The export is in Preview and its schema can change, which is why columns a table lacks are read as NULL.

**Who pays for queries.** Cloudoptics' query project, by default (see "One service account per connection"), at BigQuery on-demand prices ($6.25 a TiB; the first TiB a month is free). A month of grouped FOCUS rows usually scans a few MB. Bytes billed are recorded on each `sync_run`; the customer's estimate is 0 unless the connection uses its own query project.

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

1. Choose a Google Cloud project of Cloudoptics' own per environment **that holds nothing else**, then run `PROJECT_ID=... AWS_ACCOUNT_ID=<broker account> ./infra/gcp/setup.sh` in Cloud Shell. It creates the pool and the AWS provider, grants the broker Create/Delete Service Accounts and Workload Identity User on the project, grants BigQuery Job User to all of the project's service accounts, and prints the settings. Re-run it on an environment set up before 20 Nov 2026: the shared `cloudoptics-reader` account is no longer used (delete it once no label-era connection is left).
2. Set `CLOUDOPTICS_GCP_WORKLOAD_PROVIDER` and `CLOUDOPTICS_GCP_SERVICE_ACCOUNT_PROJECT` on the API and worker (not secrets; `CLOUDOPTICS_GCP_QUERY_PROJECT` optional, default the same project) and remove `CLOUDOPTICS_GCP_SERVICE_ACCOUNT`. Until they are set, the wizard shows "not switched on" and checks fail with `GcpNotConfigured`.
3. In the project's IAM & Admin, Quotas: note "Service Account Count" (one per live Google Cloud connection) and request more before it is reached (RB-21 has the steps); set the BigQuery custom quotas "Query usage per day" and "Query usage per day per user" (suggested: 1 TiB and 50 GiB) so a hostile export cannot run up our bill.
4. Optional: set the organization policy `iam.disableServiceAccountKeyCreation`.
5. For a first real test: a Google Cloud **billing account** with at least one project that has spend, the **BigQuery billing export** turned on (the managed FOCUS export, or the detailed export plus Google's FOCUS view saved as a view), and a day of data. Connect it from a staging workspace and follow the three steps. Watch `sync_run` (`cur.ingest` bytes billed) and compare the loaded month with the console's billing report.

## 20 Nov 2026 audit against Google's documentation

| Area | Finding | Change |
|---|---|---|
| Workload Identity Federation | Token format, STS parameters and generateAccessToken matched. The mapping and principal set used the full assumed-role ARN; Google documents the role name. | setup.sh uses the documented mapping and a `startsWith` condition on the broker role, and updates an existing provider. Token expiry comes from `expireTime`. |
| Billing export | The managed FOCUS export exists (Preview, 8 Jun 2026): dataset `gcp_billing_immutable_<ID>_<location>`, table `gcp_billing_export_focus_<ID>`. It has no Credit rows, no Tags or ServiceCategory, and labels in `x_Labels`. `BillingPeriodStart = TIMESTAMP(@period)` could match no rows. | Period window, `x_Labels` for tags, credits text fixed, and the wizard fills the table name in. |
| BigQuery jobs | `maximumBytesBilled`, `location`, `dryRun`, `labels` and paging matched. `totalBytesBilled` is in QueryResponse, not GetQueryResultsResponse. | Bytes billed are read from jobs.query; `requestId` makes retries safe. |
| Dataset labels | `datasets.get` (in Data Viewer) returns labels, and linked datasets accept label updates. The label did not cover views. | `view_sources` check; both replaced later that day by per-connection accounts. |
| Recommender | IDs matched. Global addresses and regional disks were never queried. The idle-disk recommendation was matched to its snapshot, not the disk. One error stopped every location. Recommender Viewer is broader than needed. | Locations per type, operation-aware matching, errors per location, and the two narrower roles. |
| Inventory and Monitoring | Fields matched. `view` was not sent, and the filter did not name the resource type. | Both added. |
| Resource Manager | testIamPermissions needs no permission and takes no wildcards. Matched. | None. |
| Errors and retries | The format matched, but BigQuery's 403 `rateLimitExceeded` counted as access denied, and nothing was retried. | Retries, and a quota-aware `denied`. |

### Needs a real account

1. That a dataset-level `GRANT ... ON SCHEMA` (and the console's Share) works on the managed export's read-only linked dataset. The docs say labels can be updated and table-level grants cannot, but they do not show a dataset-level grant on a linked dataset.
2. The hour stored in `BillingPeriodStart`, and how the managed table is partitioned (bytes scanned per period).
3. The columns of Google's FOCUS view. Its guide is behind a sign-up page, so `Tags` and the x_ columns are unverified there.
4. Recommender's operation `path` and `value` for machine-type and Cloud SQL tier changes (the docs have no example), and whether `cost` is in USD with `costInLocalCurrency` in the bill's currency.
5. Whether Google accepts the regional AWS STS endpoint (`sts.<region>.amazonaws.com`, which google-auth uses) as well as the global one in its example.
6. The exact error `serviceAccounts.create` returns when the project's account quota is full (the docs do not show it). The plug-in treats 429, `RESOURCE_EXHAUSTED`, `quotaExceeded` and messages naming a quota or "maximum number" as the quota; the fake answers 429 `RESOURCE_EXHAUSTED`.
7. Whether BigQuery answers a dataset not shared with the caller with 403 or 404 (the check says "finish step 2" for both), and how long a new account takes before generateAccessToken and the console's Add principal accept it.
8. That the console's dataset deep link (`bigquery?project=&p=&d=&page=dataset`) opens the dataset for a project other than the one last used.

## Sources (read 20 Nov 2026)

One service account per connection, and the no-commands flow (read 20 Nov 2026, later):

- https://docs.cloud.google.com/iam/docs/reference/rest/v1/projects.serviceAccounts/create (accountId 6-30, `[a-z]([-a-z0-9]*[a-z0-9])`, `iam.serviceAccounts.create`)
- https://docs.cloud.google.com/iam/docs/service-accounts-create (Create Service Accounts role, quota "depends on your project", wait 60 s or more before use)
- https://docs.cloud.google.com/iam/quotas (Service Account Count varies by project, quota adjustment in the console or support, create-request rate per project)
- https://docs.cloud.google.com/iam/docs/service-accounts-delete-undelete (deleted: prefix, purged within 60 days, no inheritance, 30-day undelete, deleted accounts do not count, Delete Service Accounts role)
- https://docs.cloud.google.com/iam/docs/roles-permissions/iam (Workload Identity User: getAccessToken, no lowest-level restriction; Token Creator: lowest level Service Account; Creator and Deleter permissions)
- https://docs.cloud.google.com/iam/docs/manage-access-service-accounts (roles on a project cover all its service accounts)
- https://docs.cloud.google.com/iam/docs/service-account-impersonation
- https://docs.cloud.google.com/iam/docs/federated-identity-supported-services (IAM: no known limitations for federated principals)
- https://docs.cloud.google.com/iam/docs/principal-identifiers (`principalSet://cloudresourcemanager.googleapis.com/projects/<number>/type/ServiceAccount`, allow policies)
- https://docs.cloud.google.com/iam/docs/roles-permissions/bigquery (Job User only on projects, folders, organisations; Data Viewer on a dataset)
- https://docs.cloud.google.com/bigquery/docs/access-control
- https://docs.cloud.google.com/bigquery/docs/control-access-to-resources-iam (Sharing, Permissions, Add principal)
- https://cloud.google.com/bigquery/pricing (on-demand $6.25/TiB, first TiB free, 10 MB minimum per table and query, the querying project pays for shared data)
- https://docs.cloud.google.com/bigquery/docs/custom-quotas (QueryUsagePerDay, QueryUsagePerUserPerDay per service account, `quotaExceeded`)
- https://docs.cloud.google.com/bigquery/public-data (console links `bigquery?p=&d=&page=dataset`)
- https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-setup and https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-focus-setup (`console.cloud.google.com/billing/export`, "Enable FOCUS export", Save)
- https://docs.cloud.google.com/iam/docs/granting-changing-revoking-access (IAM page picker `projectselector/iam-admin/iam?supportedpurview=project,folder,organizationId`)
- https://docs.cloud.google.com/iam/docs/roles-permissions/recommender, /compute, /monitoring, /cloudsql (the viewer roles; grantable on an organisation)

The first audit:


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
- **One shared service account plus a dataset label** (the design until 20 Nov 2026). Replaced: it needed four commands, a label only a Data Owner can set, and a separate check that a view's sources carry the label (a confused deputy otherwise). Per-connection accounts make the isolation a property of Google's IAM, not of our checks.
- **Disable on disconnect, delete later.** Rejected: a disabled account still counts toward the quota and disabling needs Service Account Admin; reconnecting is rare and sharing again is one step.
- **Workload Identity User granted per account.** Rejected: it needs Service Account Admin (setIamPolicy) in our project, which is a broader right than the project-level grant it would replace (see above).
- **Direct federated access** (customers grant roles to a workspace-specific `principal://` subject, no service account). Simpler isolation, but not every API used here supports federated principals, and customers' IAM screens make it harder to read. Worth revisiting.
- **Google's SDKs.** Rejected for size; see above.
- **Queries in the customer's project** (they pay, as before 20 Nov 2026). Kept as an option (More options, query project). Not the default: it costs the customer a role (BigQuery Job User, project level only) and a step, to save us cents a month per connection, which the custom quotas cap.
- **"Set it up for me" with a Google sign-in** (steps 2 and 3 done for the customer). Later, if customers get stuck: it needs Google's app verification and, for a moment, a token that can change anything the person can reach.
