# ADR-019: Google Cloud provider (credentials, data, costs, limits)

**Status:** Decided 20 Nov 2026 (founder approved Google Cloud, Azure and DigitalOcean; multi-cloud plan, Phase 2a). Built against a fake Google Cloud only: no real Google project has been called yet.

## Context

ADR-018 made the core provider-neutral and left each cloud as a plug-in (`providers/<name>`). Google Cloud is the first new one. The market research (multicloud/market-research.md §4) found Google's bill is in BigQuery, as the detailed export with Google's FOCUS view or the managed FOCUS export (Preview, columns up to FOCUS 1.2), read with BigQuery Data Viewer on the dataset and BigQuery Job User on a project. docs/10 and docs/11b forbid long-lived secrets on our side.

## Decision

### Credentials: no Google key anywhere

Workload Identity Federation with AWS as the identity provider (`providers/gcp/client.py`):

1. The broker's short-lived AWS credentials (Roles Anywhere, ADR-007) sign an `sts:GetCallerIdentity` request with SigV4 (botocore's signer). Only the signed request leaves Cloudoptics.
2. Google STS (`sts.googleapis.com/v1/token`) checks it with AWS and returns a federated token for our pool `cloudoptics`, provider `aws-broker`. Its attribute condition admits only `arn:aws:sts::<broker account>:assumed-role/CloudopticsBroker`.
3. The federated token impersonates `cloudoptics-reader@<project>.iam.gserviceaccount.com` for one hour (`iamcredentials ... :generateAccessToken`). The service account has no keys and no roles in our project. Customers grant it read roles.

The token is cached until five minutes before it expires. `CLOUDOPTICS_GCP_FEDERATION_ROLE_ARN` can name a dedicated AWS role the broker assumes first, if the owner wants the federation trust narrower than the broker role.

**REST, not SDKs.** Google's Python libraries (google-auth, google-cloud-bigquery, and the gRPC and protobuf stacks behind them) would add about 30 packages to the lockfile for a dozen small JSON calls. The plug-in uses httpx, which is already a dependency. The lockfile is unchanged.

### Telling workspaces apart: the dataset label

Every customer grants roles to the same service account, so that account alone cannot tell workspaces apart. Anyone could type someone else's dataset into Cloudoptics. Each connection therefore gets a random 128-bit **label value**. It plays the part of AWS's External ID: stored encrypted in `cloud_connection.external_id_enc` and shown only to the workspace's admins. The customer sets it as label `cloudoptics` on the dataset (`bq update --set_label`), which only someone who can change that dataset can do. Until the label matches, the check fails (`OwnershipNotProven`) and no bill is read. Only then is the billing account reserved for the workspace (`account_verified_at`, migration 0017's one-verified-workspace rule, per billing account). Projects come from the bill itself (`SubAccountId`), never from typed input, so a workspace scans only projects billed to its own proven billing account.

### Data model (migration 0044)

- `cloud_connection` row: `provider = 'gcp'`, `provider_account_id` = billing account ID (`XXXXXX-XXXXXX-XXXXXX`), `account_type = 'management'` (it covers many projects), `role_arn` = our service account, `stack_name = 'gcp'`, no CUR columns.
- `gcp_connection` (new; FORCE RLS, tenant policy, removed with its connection; in the deletion `TABLES`, `UNVERSIONED`, and seeded for the cross-tenant test): billing account, export project, dataset, table or view, export kind (`focus_export` | `focus_view`), the query project (which pays for queries; default: the export project) and the dataset location.
- Projects are `cloud_account` rows (`account_type = 'member'`). The billing account has one too (`management`), for lines without a project (tax, support).
- Resource IDs are Google's relative names (`instances/web-1`, `disks/web-1`), because an instance and its boot disk often share a name and a zone. Types: `gce_instance`, `gce_disk`, `gce_address`, `gce_snapshot`, `cloudsql_instance`.

### Connect flow and checks

`POST /v1/tenants/{t}/connections/gcp` (admins) returns the setup: the service account, the label value and four copy-paste commands. They label the dataset, `GRANT roles/bigquery.dataViewer ON SCHEMA`, grant `roles/bigquery.jobUser` on the query project, and optionally grant Compute, Monitoring, Recommender and Cloud SQL Viewer per project. Console steps are offered as well. `GET .../gcp/{id}` shows the setup again. Checking, listing, reload, scan and disconnect use the shared routes, which now dispatch by provider. The checks keep the `permission_checks` shape:

| Check | Fails the connection when | Otherwise |
|---|---|---|
| `federation` | Our side cannot get a token, or is not set up (`GcpNotConfigured`) | |
| `dataset` | The dataset is missing or not readable | |
| `ownership` | The label does not match, or the billing account is proven in another workspace (`AccountInUse`) | |
| `export_table` | | error (degraded): no FOCUS columns |
| `query_jobs` | | error (degraded): no `bigquery.jobs.create` (testIamPermissions) |
| `billing_account` | The export belongs to another billing account | warning: no rows yet |
| `projects` | | warning: the projects without the optional roles; skipped before the first bill |

### Costs

`billing.ingest` reads each billing period with one BigQuery query. The query filters on `BillingPeriodStart`, groups to the `cost_daily` grain (day, project, service, region, SKU, charge category, tags) and is capped by `maximumBytesBilled` (20 GiB by default) and a row limit (500,000 groups). Columns a FOCUS table lacks are read as NULL. The version is the SHA-256 of the result. This month and last month are read at every 6-hourly poll; the first load also reads two earlier months. A new version replaces the period in one transaction, the same idempotent period replace as AWS's CUR loads, recorded in `ingest_batch` (`mapping_version gcp-focus-1`, `manifest_key bq://project.dataset.table`). `provider_account_id` is the project ID, or the billing account for project-less lines. Credits are their own FOCUS rows (`ChargeCategory = 'Credit'`). Net cost = effective cost.

**Currency.** GCP bills may be in any currency (`billing_currencies = None`). Rows keep `billing_currency` and native amounts. USD is converted day by day: first the FX service (a staff-set rate, ADR-010); when Cloudoptics has no rate for that day, the rate Google used (`x_CurrencyConversionRate`, flagged as a warning); a day with neither blocks the period (`fx` error) and the previous load stays.

**Who pays for queries.** The customer's query project pays, at BigQuery on-demand prices ($6.25 a TiB; the first TiB a month is free). A month of grouped FOCUS rows usually scans a few MB. Bytes billed and the estimate are recorded on each `sync_run`.

### Reconciliation and month to date

Google has no invoice API: invoice totals are in the console only. `billing.reconcile` therefore compares each loaded period, per project, with BigQuery's own `SUM(BilledCost)` from the source table, in USD at the period's stored rate. This checks the load from end to end (rows lost, doubled, or restated since the load), not the invoice. The month-to-date snapshot (`ce.snapshot`) returns `skipped`: the export itself is near real time and this month is loaded at every poll.

### Inventory, metrics, Recommender and rules

Per project, at most `CLOUDOPTICS_GCP_MAX_PROJECTS` (100), the scan reads the Compute API (aggregated instances, disks and addresses; global addresses and snapshots) and the Cloud SQL Admin API. Cloud Asset Inventory would list everything in one call but needs an organization-level grant, which is more than the costs-first customer gives. Recommender (`google.compute.instance.{IdleResource,MachineType}Recommender`, the disk and address idle recommenders, and the Cloud SQL idle and over-provisioned recommenders) is read for the zones and regions that hold resources. Its findings are kept on each resource (`config.recommendations`) with their savings converted to USD at today's rate. Cloud Monitoring gives the hourly mean CPU of running instances and Cloud SQL instances (`resource_metric`, metric `cpu`).

The rules (`providers/gcp/rules.py`, `RuleSpec` provider `gcp`) are GCP1 idle VM, GCP2 unattached persistent disk, GCP3 reserved external IP not in use, GCP4 snapshot 90+ days old of a deleted disk, GCP5 Recommender machine type, GCP6 Recommender idle Cloud SQL, and GCP7 Recommender over-provisioned Cloud SQL. Each fix is `gcloud` commands with a rollback, a console link with steps, and risk factors. GCP1-GCP4 price with Google's public us-central1 list prices (`list_price` basis, an estimate). GCP5-GCP7 take Recommender's savings, which use the customer's own prices (`effective_rate`). Fingerprints are `gcp:<rule>:<project>:<zone or region>:<kind>/<name>`.

## Consequences and limits

- **Not yet proven against Google.** Everything is tested against `tests/fake_gcp.py`. The first real connection needs a test project (below); the label on a managed FOCUS export dataset in particular must be checked, since Google may not allow labels there. If it does not, the fallback is a FOCUS view in a customer-owned dataset, and the label goes on that dataset.
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

## Alternatives

- **A service account key per environment.** Rejected: a long-lived secret (docs/10 T2).
- **One service account per workspace** (tenant isolation by identity). Rejected for now: it needs IAM writes in our project at sign-up, and a quota of about 100 service accounts per project. The label gives the same proof without them.
- **Direct federated access** (customers grant roles to a workspace-specific `principal://` subject, no service account). Simpler isolation, but not every API used here supports federated principals, and customers' IAM screens make it harder to read. Worth revisiting.
- **Google's SDKs.** Rejected for size; see above.
- **Running queries in our project** (we pay). Possible later via `query_project_id`. Today the customer's project pays, which is visible in their bill and costs us nothing.
