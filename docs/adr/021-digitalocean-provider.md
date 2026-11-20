# ADR-021: DigitalOcean provider

**Status:** Decided 20 Nov 2026 (multi-cloud plan, Phase 2c, "costs first"; the founder approved Google Cloud, Azure and DigitalOcean on 19 Nov 2026)

## Context

DigitalOcean is the third cloud among sites hosted in Nigeria and a common choice for African startups (market research §6.1). Its read-only Billing API (`billing:read`) covers the balance, billing history, invoices with a CSV per invoice and a daily "insights" endpoint (§6.3). It has no identity federation that would let Cloudoptics sign in without a stored secret, so the plan accepts a customer-made read-only token kept under KMS encryption (plan, credentials table).

ADR-018 made providers plug-ins. This ADR records how the DigitalOcean plug-in (`apps/api/src/cloudoptics/providers/digitalocean`) holds the token, which data it loads and its limits.

## Decision

### The token

- **The customer makes it.** In DigitalOcean's control panel, in the team that gets the bill: Account → API (the Tokens tab of Applications & API) → Generate New Token → a name, an expiry and **Custom Scopes**, ticking `account:read` and `billing:read` (required) and, for recommendations, `droplet:read`, `block_storage:read`, `snapshot:read`, `reserved_ip:read`, `load_balancer:read`, `database:read`, `monitoring:read` and `tag:read` (tags on Droplets, volumes, snapshots and databases are only returned with `tag:read`). A **Read Only** token (`api:read`, every read scope) also works but reads more than we need; **Full Access** (`api:write`) is refused. Scopes cannot be changed after a token is made, so a missing scope means a new token. The customer pastes it once (`POST /v1/tenants/{t}/connections/digitalocean`).
- **Checked before it is stored.** Every scope is tested by using it once (the API reference documents no endpoint that lists a token's scopes): `GET /v2/account` (the team), the balance, the invoice list, the insights endpoint, one list call per resource kind, `GET /v2/monitoring/alerts` and `GET /v2/tags`. Each becomes a `permission_checks` entry (`ok`, `warning`, `error`, `skipped`), the same shape as AWS's.
- **A token that can write is refused, not warned about.** Two create requests that cannot succeed test for write access: `POST /v2/droplets` with an empty body and `POST /v2/tags` with an empty name. We expect a token without those scopes to get 403 before the body is read, and a token with them 422 (invalid body), with nothing created. The API reference documents the scopes (`droplet:create`, `tag:create`) but not that the scope check comes before body validation: this is an assumption, which the owner's test step 3 confirms with a Read Only and a Full Access token before customers connect. If DigitalOcean validated the body first, every token would be refused: it fails safe and shows at once. Any answer other than 401 or 403 means write access: the API answers 422 with "This token can change your DigitalOcean resources…" and the token is not kept. We chose refusal because a stored write token is a far larger secret than a read token, and the customer fixes it in a minute. The probes catch Full Access tokens and custom tokens with `droplet:create` or `tag:create`; a custom token with only other write scopes (say `domain:update`) is not detected. That gap is listed below.
- **Stored only encrypted.** The token goes in `cloud_connection.external_id_enc`, the connection secret column, through the existing `Encryptor` (`connectors/crypto.py`, ADR-007): KMS envelope encryption in staging and production, the tenant ID bound as associated data and in the KMS encryption context, so a ciphertext copied to another tenant's row does not decrypt. The hourly re-encryption job and the lazy upgrade in `read_external_id` treat it exactly like an External ID.
- **Never shown again.** No response carries it (the connection model has no token field), the audit log records the provider, team and status only, `sync_run` rows carry DigitalOcean's error IDs, and the HTTP client keeps it in a private attribute, out of `repr`, exceptions and log records (errors carry the HTTP status and DigitalOcean's `id` only). A test greps the responses, the audit log, `sync_run`, the connection row and every captured log line at DEBUG for the token.
- **Replace.** `PUT /v1/tenants/{t}/connections/{c}/token` runs the same checks; the new token must read the same team (409 otherwise). Audit: `connection.token_replaced`.
- **Disconnect wipes it.** The shared `DELETE` route sets `external_id_enc` to NULL for any provider but AWS (whose External ID only works with the customer's own role). Migration 0046 allows NULL there only for a disconnected non-AWS connection (`cloud_connection_secret_check`). Connecting the same team again reuses the row, so its cost history stays.
- **One workspace per team.** The team's ID (`account.team.uuid` from `GET /v2/account`) is `provider_account_id`. The reference types it as a string and its example is 40 hex digits, not a UUID, so any ID of up to 64 letters, digits and hyphens is accepted. `team` is optional ("when authorized in a team context"); without it the user's `account.uuid` is used. A passing check proves control of the team and sets `account_verified_at`, so ADR-017's one-verified-workspace-per-account index applies: a second workspace gets 409.

### Data

| What | Endpoint | Into | When |
|---|---|---|---|
| Closed months | `GET /v2/customers/my/invoices`, then `/{uuid}/csv` and `/{uuid}/summary` | `cost_daily` (provider `digitalocean`, USD), `ingest_batch`, `billing_file` | on connect, then the shared 6-hourly `cur.poll` (one list request when nothing is new) |
| This month (provisional) | `GET /v2/customers/my/balance` (total), `GET /v2/billing/do:team:{uuid}/insights/{start}/{end}` (days and SKUs) | `cost_snapshot_ce` (provider `digitalocean`) | on connect, then the shared daily `ce.poll` |
| Reconciliation | the invoice list's amount | `cost_reconciliation` | after each new month |
| Resources | Droplets, volumes, snapshots, reserved IPs, load balancers, managed databases; Droplet CPU from `/v2/monitoring/metrics/droplet/cpu` | `resource` (types `do_droplet`, `do_volume`, `do_snapshot`, `do_reserved_ip`, `do_load_balancer`, `do_database`) | on connect, then the shared `inventory.poll` (12-hourly) |

- **One invoice is one export version** (its UUID), so the poll loads a month once and is idempotent; the shared `ingestion.service._store` replaces a month's rows in one transaction, as for AWS. A new connection loads the last 12 closed months.
- **The CSV** has the documented header `product,group_description,description,hours,start,end,USD,project_name,category`, with amounts as `$3.11`. The summary has `product_charges` (with `items`), `overages`, `taxes` and `credits_and_adjustments`, each with an `amount`. Whether the CSV lists overages as lines is not documented, so an `Overages` row is added only when the summary's overages are exactly what the lines, taxes and credits miss.
- **FOCUS mapping** (`do-invoice-csv-1`): `product` → ServiceName (the code is its slug), ServiceCategory by product (Droplets, Kubernetes, App Platform → Compute; Volumes, Spaces, Snapshots, Backups → Storage; Load Balancers, Reserved IPs, Bandwidth → Networking; Managed Databases → Databases; GenAI → AI and Machine Learning; else Other). The usage type is the size slug in brackets in the description, else the product. Each line is spread over the days it covers in proportion to hours, to the micro-dollar, with the remainder on the last day, so a month sums exactly to its lines. Taxes and credits from the summary become `Tax` and `Credit` rows on the month's last day. `project_name` becomes the tag `do:project`, so the breakdown by tag splits by project. Descriptions, resource names and IP addresses are not stored in cost rows, and the summary's name, email and address are dropped as soon as it is read.
- **Quality checks**: required columns, unreadable amounts (both block the month; the previous good load stays), rows outside the month (warning) and the invoice total (lines + taxes + credits against the invoice, warning).
- **Month to date**: `month_to_date_usage` is the total; insights give the days and services (SKU parts mapped to products; the SKU format is not documented, so unknown SKUs count as Other). The insights endpoint is the only one whose path names the team (`do:team:<id>`); the `/v2/customers/my/...` endpoints act on the token's customer account. DigitalOcean's daily data starts on 1 December 2025 and is an estimate (it "may not include all invoicing factors"); when insights are refused or absent the snapshot shows the balance alone.
- **What the scan keeps.** Only the fields the rules read. The database list also returns `connection` (with a password and a URI) and `users[].password`; neither is stored, and a test checks it.

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

## Checked against DigitalOcean's documentation (20 Nov 2026)

Every call was checked against the API reference and the OpenAPI spec before the PR. Where the docs disagreed with the first build:

| First build | The docs say | Changed to |
|---|---|---|
| Team ID had to be a UUID (account ID pattern, insights path check) | `team.uuid` is a string, the example is 40 hex digits, and `team` is optional | Any ID of 1-64 letters, digits and hyphens; the user's `uuid` when there is no team |
| Resource scopes without `tag:read` | `tags` on Droplets, volumes, snapshots and databases needs `tag:read` | `tag:read` in the wizard and a "Read tags" check (a warning if missing) |
| The wizard linked to `/account/api/tokens/new` and said "API → Generate New Token" | The documented path is Account → API (Tokens tab) → Generate New Token; the form has a name, an expiration and Full Access / Read Only / Custom Scopes | A link to `/account/api/tokens`, the documented labels, "pick an expiry" |
| The summary was read for taxes and credits only | It also has `overages` | Overages are counted when the lines miss them |
| Fake database objects had no credentials | The list returns `connection` with a password | The fake returns them; a test checks they are never stored |
| Fake account and summary had a few fields | The documented examples have more (`droplet_limit`, `email_verified`, `user_billing_address` with seven fields, `product_charges.items`) | The fake returns the documented fields |

Not documented, so still assumptions: that the scope check comes before body validation (the write probes, above); the insights SKU format; whether invoice CSV lines include overages; the unit of the CPU series (the example's `idle` values rise steadily; read here as cumulative CPU seconds per mode). Documented but not built: Kubernetes (`kubernetes:read`), Spaces (`spaces:read`), App Platform (`app:read`), `/v2/customers/my/billing_history`, the invoice items JSON (`GET /v2/customers/my/invoices/{uuid}`, with `resource_uuid`, for per-resource costs) and Droplet bandwidth (`/v2/monitoring/metrics/droplet/bandwidth`, in Mbps).

## Sources

All read on 20 November 2026.

- API reference, Billing (balance, billing history, invoices, invoice CSV, PDF and summary, billing insights; rate limits and the `ratelimit-*` headers; `per_page` 1-200): https://docs.digitalocean.com/reference/api/reference/billing/
- API reference, Account (`GET /v2/account`, `account:read`, the optional `team` object): https://docs.digitalocean.com/reference/api/reference/account/
- API reference, Droplets (`droplet:read`, `droplet:create`, fields, `status` values, `links.pages`, the extra scopes for `tags` and `volume_ids`): https://docs.digitalocean.com/reference/api/reference/droplets/
- API reference, Block Storage (`GET /v2/volumes`, fields, `tag:read`): https://docs.digitalocean.com/reference/api/reference/block-storage/
- API reference, Snapshots (`GET /v2/snapshots`, `snapshot:read`, fields): https://docs.digitalocean.com/reference/api/reference/snapshots/
- API reference, Reserved IPs (`GET /v2/reserved_ips`, `reserved_ip:read`, `droplet` null when unassigned): https://docs.digitalocean.com/reference/api/reference/reserved-ips/
- API reference, Load Balancers (`GET /v2/load_balancers`, `size_unit`, `status` values, the `region` object): https://docs.digitalocean.com/reference/api/reference/load-balancers/
- API reference, Databases (`GET /v2/databases`, fields, `connection` and `users[].password` in the list): https://docs.digitalocean.com/reference/api/reference/databases/
- API reference, Monitoring (`/v2/monitoring/metrics/droplet/cpu` and `/bandwidth`, `host_id`, Unix `start` and `end`, the matrix result; `/v2/monitoring/alerts` with `monitoring:read`): https://docs.digitalocean.com/reference/api/reference/monitoring/
- Token scopes (custom scopes, `api:read`, `api:write`): https://docs.digitalocean.com/reference/api/scopes/
- Creating a personal access token (control-panel steps and labels): https://docs.digitalocean.com/reference/api/create-personal-access-token/
- pydo, billing insights (path, `per_page` and `page`, example data points): https://docs.digitalocean.com/reference/pydo/reference/billing_insights/list/
- OpenAPI spec, billing operations and their `billing:read` security: https://github.com/digitalocean/openapi/tree/main/specification/resources/billing, https://raw.githubusercontent.com/digitalocean/openapi/main/specification/resources/billing/invoices_get_csvByUUID.yml, https://raw.githubusercontent.com/digitalocean/openapi/main/specification/resources/billing/invoices_get_summaryByUUID.yml, https://raw.githubusercontent.com/digitalocean/openapi/main/specification/resources/billing/billingInsights_list.yml
