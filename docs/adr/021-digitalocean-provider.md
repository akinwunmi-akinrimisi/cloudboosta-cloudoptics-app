# ADR-021: DigitalOcean provider

**Status:** Decided 20 Nov 2026 (multi-cloud plan, Phase 2c, "costs first"; the founder approved Google Cloud, Azure and DigitalOcean on 19 Nov 2026)

## Context

DigitalOcean is the third cloud among sites hosted in Nigeria and a common choice for African startups (market research §6.1). Its read-only Billing API (`billing:read`) covers the balance, billing history, invoices with a CSV per invoice and a daily "insights" endpoint (§6.3). It has no identity federation that would let Cloudoptics sign in without a stored secret, so the plan accepts a customer-made read-only token kept under KMS encryption (plan, credentials table).

ADR-018 made providers plug-ins. This ADR records how the DigitalOcean plug-in (`apps/api/src/cloudoptics/providers/digitalocean`) holds the token, which data it loads and its limits.

## Decision

### The token

- **The customer makes it.** In DigitalOcean, API → Generate New Token → Custom Scopes, ticking `account:read` and `billing:read` (required) and, for recommendations, `droplet:read`, `block_storage:read`, `snapshot:read`, `reserved_ip:read`, `load_balancer:read`, `database:read` and `monitoring:read`. A "Read Only" token (`api:read`) also works but reads more than we need. The customer pastes it once (`POST /v1/tenants/{t}/connections/digitalocean`).
- **Checked before it is stored.** Every scope is tested by using it once (DigitalOcean has no endpoint that lists a token's scopes): `GET /v2/account` (the team), the balance, the invoice list, the insights endpoint, one list call per resource kind and `GET /v2/monitoring/alerts`. Each becomes a `permission_checks` entry (`ok`, `warning`, `error`, `skipped`), the same shape as AWS's.
- **A token that can write is refused, not warned about.** Two create requests that cannot succeed test for write access: `POST /v2/droplets` with an empty body and `POST /v2/tags` with an empty name. A token without those scopes gets 403 before the body is read; a token with them gets 422 (invalid body) and nothing is created. Any answer other than 401 or 403 means write access: the API answers 422 with "This token can change your DigitalOcean resources…" and the token is not kept. We chose refusal because a stored write token is a far larger secret than a read token, and the customer fixes it in a minute. The probes catch Full Access tokens and custom tokens with `droplet:create` or `tag:create`; a custom token with only other write scopes (say `domain:update`) is not detected. That gap is listed below.
- **Stored only encrypted.** The token goes in `cloud_connection.external_id_enc`, the connection secret column, through the existing `Encryptor` (`connectors/crypto.py`, ADR-007): KMS envelope encryption in staging and production, the tenant ID bound as associated data and in the KMS encryption context, so a ciphertext copied to another tenant's row does not decrypt. The hourly re-encryption job and the lazy upgrade in `read_external_id` treat it exactly like an External ID.
- **Never shown again.** No response carries it (the connection model has no token field), the audit log records the provider, team and status only, `sync_run` rows carry DigitalOcean's error IDs, and the HTTP client keeps it in a private attribute, out of `repr`, exceptions and log records (errors carry the HTTP status and DigitalOcean's `id` only). A test greps the responses, the audit log, `sync_run`, the connection row and every captured log line at DEBUG for the token.
- **Replace.** `PUT /v1/tenants/{t}/connections/{c}/token` runs the same checks; the new token must read the same team (409 otherwise). Audit: `connection.token_replaced`.
- **Disconnect wipes it.** The shared `DELETE` route sets `external_id_enc` to NULL for any provider but AWS (whose External ID only works with the customer's own role). Migration 0046 allows NULL there only for a disconnected non-AWS connection (`cloud_connection_secret_check`). Connecting the same team again reuses the row, so its cost history stays.
- **One workspace per team.** The team UUID is `provider_account_id`. A passing check proves control of the team and sets `account_verified_at`, so ADR-017's one-verified-workspace-per-account index applies: a second workspace gets 409.

### Data

| What | Endpoint | Into | When |
|---|---|---|---|
| Closed months | `GET /v2/customers/my/invoices`, then `/{uuid}/csv` and `/{uuid}/summary` | `cost_daily` (provider `digitalocean`, USD), `ingest_batch`, `billing_file` | on connect, then the shared 6-hourly `cur.poll` (one list request when nothing is new) |
| This month (provisional) | `GET /v2/customers/my/balance` (total), `GET /v2/billing/do:team:{uuid}/insights/{start}/{end}` (days and SKUs) | `cost_snapshot_ce` (provider `digitalocean`) | on connect, then the shared daily `ce.poll` |
| Reconciliation | the invoice list's amount | `cost_reconciliation` | after each new month |
| Resources | Droplets, volumes, snapshots, reserved IPs, load balancers, managed databases; Droplet CPU from `/v2/monitoring/metrics/droplet/cpu` | `resource` (types `do_droplet`, `do_volume`, `do_snapshot`, `do_reserved_ip`, `do_load_balancer`, `do_database`) | on connect, then the shared `inventory.poll` (12-hourly) |

- **One invoice is one export version** (its UUID), so the poll loads a month once and is idempotent; the shared `ingestion.service._store` replaces a month's rows in one transaction, as for AWS. A new connection loads the last 12 closed months.
- **FOCUS mapping** (`do-invoice-csv-1`): `product` → ServiceName (the code is its slug), ServiceCategory by product (Droplets, Kubernetes, App Platform → Compute; Volumes, Spaces, Snapshots, Backups → Storage; Load Balancers, Reserved IPs, Bandwidth → Networking; Managed Databases → Databases; GenAI → AI and Machine Learning; else Other). The usage type is the size slug in brackets in the description, else the product. Each line is spread over the days it covers in proportion to hours, to the micro-dollar, with the remainder on the last day, so a month sums exactly to its lines. Taxes and credits from the summary become `Tax` and `Credit` rows on the month's last day. `project_name` becomes the tag `do:project`, so the breakdown by tag splits by project. Descriptions, resource names and IP addresses are not stored in cost rows, and the summary's name, email and address are dropped as soon as it is read.
- **Quality checks**: required columns, unreadable amounts (both block the month; the previous good load stays), rows outside the month (warning) and the invoice total (lines + taxes + credits against the invoice, warning).
- **Month to date**: `month_to_date_usage` is the total; insights give the days and services (SKU parts mapped to products). DigitalOcean's daily data starts on 1 December 2025 and is an estimate; when insights are refused or absent the snapshot shows the balance alone.

### Rules

Five rules in `providers/digitalocean/rules.py`, run by the shared engine over DigitalOcean resources only, each with doctl fix and rollback commands, console steps and risk factors: DO1 unattached volume (snapshot, delete), DO2 unassigned reserved IP (release; irreversible), DO3 snapshot older than 90 days (delete; may be the only copy), DO4 idle Droplet (CPU under the tenant's idle threshold for 14 days, peak under five times it; snapshot and destroy, since a powered-off Droplet is still billed), DO5 load balancer with no Droplets or tag behind it (save its JSON, delete). Costs are list prices (volumes USD 0.10/GiB-month, snapshots 0.06, an unassigned reserved IP 5.00, a load balancer node 12.00) and, for Droplets, the price DigitalOcean returns with the size. Tenants can turn each rule off like the AWS ones.

### Limits and back-off

DigitalOcean allows 250 requests a minute and 5,000 an hour per token. The client keeps to 200 a minute per token per process, stops when `ratelimit-remaining` reaches 0 until `ratelimit-reset`, waits for `Retry-After` on 429, backs off 1, 2, 4 s on 5xx and network errors, and gives up after three retries. A connect is about 14 requests, an invoice poll 1 (2 more per new month), a scan about 7 plus one per running Droplet (at most 100 Droplets' CPU per scan). The HTTP client follows `links.pages.next` only on DigitalOcean's host and under `/v2/`, refuses redirects, and refuses an invoice CSV over 20 MiB.

## Consequences

- The customer holds a revocable read-only secret with us. If our database and KMS were both compromised, an attacker could read the bills and resource lists of connected teams, not change them.
- Costs, budgets, anomalies, forecasts, exports, data deletion and the recommendation workflow carry DigitalOcean data with no change, as ADR-018 intended.
- Still to do: detection of write scopes other than `droplet:create` and `tag:create`; per-resource costs (`cost_resource_daily`) from the invoice JSON items, for realised savings; the connection alert email says "AWS connection"; Scan now and Reload bills on the Connections page are AWS-only in the UI (the shared `/scan` and `/reload` routes work for DigitalOcean); App Platform, Kubernetes, Spaces and Functions inventory; rightsizing Droplets.

## Alternatives

- **Warn about write tokens instead of refusing them.** Rejected: we would keep a secret that can delete the customer's servers.
- **A separate credentials table.** Rejected for now: `external_id_enc` is already the encrypted, tenant-bound connection secret with KMS re-encryption around it; a table would need its own RLS, guard-list and deletion entries for one column.
- **OAuth app instead of a token.** DigitalOcean OAuth tokens expire and need a stored refresh token and a registered app; it is no smaller a secret and adds a sign-in flow. Revisit if customers ask for it.
- **Insights for closed months too.** Rejected: invoices are the record; insights are estimates.
