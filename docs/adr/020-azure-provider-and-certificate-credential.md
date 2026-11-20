# ADR-020: Azure provider: a multi-tenant Entra app with a rotated certificate credential

**Status:** Decided 20 Nov 2026 (multi-cloud plan Phase 2b; founder approved Azure 19 Nov 2026).
Nothing has been created in Azure yet: the owner steps below do that.

## Context

Azure is the second plug-in after AWS (ADR-018). Customers keep their bills in Cost Management;
the improved exports write a FOCUS file set (Parquet or CSV) to a Blob Storage container they
own every day (multi-cloud market research §4). Read-only access is an Entra app (service
principal) with Cost Management Reader and Reader on the scope and Storage Blob Data Reader on
the export container.

The rule for credentials is no long-lived secret on our side (docs/10 threat T2, docs/11b §1.2).
An Entra app proves who it is with a client secret, a certificate, or a federated identity
credential (an OIDC token from an issuer the app trusts). A client secret is ruled out. The
codebase assessment (§B) noted that a federated credential needs an OIDC issuer we control,
since DigitalOcean App Platform has none.

## Decision

### Credential: certificate on the Entra app (option a), rotated by Staging setup

| | (a) Certificate credential (chosen) | (b) Federated credential, our own OIDC issuer |
|---|---|---|
| Signing key | RSA key in App Platform's encrypted settings, at most 30 days old | AWS KMS asymmetric key, never leaves KMS |
| What we host | Nothing | `/.well-known/openid-configuration` and JWKS on the public site, always up |
| Depends on | Staging setup, as Roles Anywhere does | The AWS broker's credentials (Roles Anywhere, not live yet) for every Azure call, plus the site |
| If someone controls the static site | Nothing | They can publish their own key and get tokens in every customer's directory |
| Revoke | Remove the certificate from the app (Graph or portal): immediate | Remove the federated credential: immediate |
| Buildable now | Yes: standard Entra client assertion, `cryptography` already in the lockfile | Needs a KMS key, Roles Anywhere live, issuer pages and key-rollover handling first |

(a) is safe and buildable now. Its only secret is the certificate's private key, which is a
credential for at most `certificate_days` (default 30, as for Roles Anywhere), lives where the
Roles Anywhere key lives (an App Platform secret, masked in the workflow log), and yields only
1-hour tokens with read-only roles. (b) removes that key but makes the public site and the AWS
broker part of the Azure trust chain; tampering with the site's JWKS would give access to every
consenting customer. (b) stays the target for production (DOKS, ADR-008), where an issuer can run
inside the cluster; the code already has the seam for it.

How (a) works:

- **One multi-tenant app**, "Cloudoptics", in our own directory. Customers admin-consent to it
  (it asks for no Microsoft Graph application permission; only the default delegated
  `User.Read`, never used) and assign roles to its service principal.
- **Token per customer directory.** `providers/azure/credentials.py` signs a client assertion
  (RFC 7523 JWT, `PS256`, header `x5t#S256` = the certificate's SHA-256 thumbprint, `aud` = the
  directory's token endpoint, `exp` in 10 minutes) and asks
  `login.microsoftonline.com/<customer directory>/oauth2/v2.0/token` for a client-credentials
  token for Resource Manager or Storage. Tokens are cached until five minutes before expiry.
  Signing sits behind `AssertionSource`; option (b) would add a KMS signer there.
- **Rotation** (`infra/azure/entra_certificate.py`, Actions -> Staging setup with
  `azure_certificate=true`): the workflow's GitHub OIDC token signs a separate *deployer* app in
  to Graph (federated credential on this repository's environment: no Azure secret in GitHub);
  the deployer owns the Cloudoptics app and holds `Application.ReadWrite.OwnedBy`. Each run makes
  a new RSA key and self-signed certificate, keeps on the app only the new certificate and the one
  the running deployment uses (read from App Platform), and hands the new pair to
  `provision.py`. No CA is involved (Entra trusts the certificate itself), so nothing but the leaf
  key exists, as with the self-managed CA of the ADR-007 amendment.

### Proving a workspace owns the subscription

Every customer trusts the same app, so the app reaching a subscription does not prove that the
workspace asking is that customer's (AWS solves this with a per-connection External ID). Each
Azure connection gets an ownership code (`co-<24 hex>`, stored encrypted in
`cloud_connection.external_id_enc`; disconnecting clears it, as for every non-AWS secret, migration 0046, and connecting again issues a new one). The customer's script tags the export's storage account
`cloudoptics-verify=<code>`. Validation reads the tag on the storage account that the scope's own
FOCUS export writes to (found through Cost Management, not taken from the form). Until it
matches, the connection is `failed` and nothing is ingested, scanned or queried. Once it matches,
`account_verified_at` is set and the one-verified-workspace-per-account index (ADR-017) reserves
the subscription or billing account for that workspace.

### Connection model

- `cloud_connection` row: `provider = 'azure'`, `provider_account_id` = the subscription GUID or
  the billing account ID, `account_type` `standalone` (subscription) or `management` (billing
  account), `cur_bucket` = `<storage account>/<container>` and `cur_prefix` = the export folder
  (so the 6-hourly poll picks it up), `role_arn` = `''`, `stack_name` = `azure`.
- `azure_connection` (migration 0047, RLS FORCE): directory ID, scope type, billing account,
  subscriptions, storage account, container, folder, and what validation found (the storage
  account's ARM ID, the export's name). Deleted with the connection's data (`privacy/deletion.py`).
- A subscription connection covers one subscription (each has its own FOCUS export). A billing
  account covers what its bill names; it scans for resources only the listed subscriptions that
  its own bill names.
- API: `POST/GET /v1/tenants/{t}/providers/azure/connections[/{id}]` (admins; the set-up steps
  with the consent link, the Cloud Shell script and portal steps, the export steps) and
  `POST .../{id}/validate`. The generic list, detail, Check again and disconnect cover Azure rows.
  `GET /v1/azure/consent` is Entra's redirect after consent: a static "close this tab" page.

### Checks (`permission_checks`)

`consent`, `reader`, `cost_reader`, `export`, `ownership`, `blob_reader`, `export_data`, each with
an actionable message and hint. `failed` without consent or ownership, `degraded` when another
check errors, `healthy` otherwise (`export_data` is a warning until the first run).

### Bills (`azure-focus-1`)

- Each export run (`<folder>/<export>/<YYYYMMDD-YYYYMMDD>/<run id>/manifest.json` and its data
  files) is a version; the newest run of each month not yet loaded replaces the month in one
  transaction, through the same `ingest_batch` / `billing_file` bookkeeping as AWS. Re-runs never
  double costs; a run that fails a check is recorded and the previous one stays live.
- Columns are mapped onto the shared FOCUS table (`ingestion/focus.FOCUS_SCHEMA`) and the shared
  daily aggregator: `SubAccountId` (`/subscriptions/<guid>`) becomes the account, `ResourceId`
  and `RegionId` lower-cased, `x_SkuMeterCategory` the service code, `ChargeCategory` as is
  (`Credit` rows are credits; an unknown category counts as `Adjustment` with a warning).
- **Currency.** Amounts stay in the bill's currency (`billing_currency` and the native columns,
  migration 0043). USD is converted per day with Cloudoptics' own rate (`fx.service`, ADR-010).
  A day without one uses Microsoft's own rate from the export (`x_BilledCostInUsd`) with an `fx`
  warning; without either the month is blocked (`fx` error) until staff set a rate. One export
  with two currencies is blocked.
- Limits: the AWS period limits (bytes, rows), at most 2,000 data files and 2 MiB per manifest,
  data files only under the export folder.
- Ingestion is plain REST over httpx (Blob list and get, Bearer token). No Azure SDK was added:
  the lockfile is unchanged.

### Reconciliation and snapshot

After a run loads, the month's billed total per subscription, in the bill's currency, is
compared with Cost Management's `ActualCost` query for the same days (the shared 1% / 1-unit
rule; stored in `cost_reconciliation` in USD at the loaded rows' rate). Until this month's run
loads, one daily `MonthToDate` query by service fills `cost_snapshot_ce` (native and Microsoft's
USD). Both queries are free.

### Inventory, metrics, Advisor and rules

- Resource Graph: VMs (with power state), managed disks, public IPs, snapshots, as `resource`
  rows (`azure_vm`, `azure_disk`, `azure_public_ip`, `azure_snapshot`), each with its last 30 days
  of billed cost from the export (`monthlyCostUsd`).
- Advisor (Cost category) recommendations are attached to their resources; Azure Monitor
  `Percentage CPU` (hourly average and maximum, 14 days) for running VMs goes to
  `resource_metric` as `cpu` / `cpu_max`.
- Rules (`providers/azure/rules.py`, each with az CLI fix, rollback and risk notes): AZ1
  unattached disks, AZ2 unassociated public IPs, AZ3 VMs stopped but not deallocated, AZ4
  snapshots 90+ days old of deleted disks, AZ5 underused VMs (Advisor's resize, or CPU under the
  workspace's idle threshold). Priced from the bill (`effective_rate`) or from small list prices
  when the bill has no line (`list_price`, low confidence).

## Consequences

- The jobs, cost tables, budgets, anomalies, forecasts, recommendation workflow and data deletion
  carry Azure data unchanged. Fingerprints are `azure:<rule>:<subscription>:<region>:<ARM ID>`.
- The shared routes find the plug-in by the connection's cloud (added with Google Cloud, ADR-019):
  Check again, Scan now, Load now and Reload bills work for Azure in both job modes. Azure's
  reload answers with the shared `ReloadResult` (reloaded, failed or not in the export, per
  month).
- Still AWS-only: the Connections page's Set up again (it opens the AWS stack), the connection alert email's wording, commitments and prices.
- Staging needs the owner steps below before an Azure customer can connect.

## Owner steps (nothing here has been applied)

In our own Entra directory (the one Cloudoptics' Microsoft account belongs to):

1. **Register the app.** Entra ID -> App registrations -> New: name `Cloudoptics`, *Accounts in
   any organizational directory (multitenant)*, redirect URI (Web)
   `https://cloudoptics.cloudboosta.com/api/v1/azure/consent`. Keep only the default delegated
   Microsoft Graph `User.Read`. Note the Application (client) ID and the Object ID. Set the
   publisher domain and logo (customers see them on the consent page).
2. **Register the deployer.** A second app, `cloudoptics-github-deployer` (single tenant). Under
   Certificates & secrets -> Federated credentials: GitHub Actions, organization/repository of
   this repo, entity *Environment* `github-pages`. API permissions: Microsoft Graph application
   permission `Application.ReadWrite.OwnedBy`, then Grant admin consent. Make its service principal
   an **owner** of the `Cloudoptics` app (Cloudoptics app -> Owners -> Add).
3. **GitHub variables** (environment `github-pages`, variables not secrets: IDs only):
   `AZURE_DIRECTORY_ID` (our directory), `AZURE_DEPLOYER_CLIENT_ID`, `AZURE_APP_CLIENT_ID`,
   `AZURE_APP_OBJECT_ID`.
4. **Issue the first certificate.** Actions -> Staging setup, `azure_certificate=true` (other
   inputs unchanged). It sets `CLOUDOPTICS_AZURE_CLIENT_ID`, `_CERTIFICATE` and `_PRIVATE_KEY`
   on the API and worker and deploys.
5. **Keep rotating** with the Roles Anywhere certificate: re-run before `certificate_days` runs
   out (every 3-4 weeks with the default 30). Revoking: remove the certificate under the app's
   Certificates & secrets.
6. **Test account.** An Azure subscription with real spend under an **Enterprise Agreement or a
   Microsoft Customer Agreement** (an MCA bought on the Azure website is enough). Pay-as-you-go
   (MOSP) subscriptions cannot create FOCUS exports (Microsoft: "Azure MOSP billing scopes and
   subscriptions don't support FOCUS datasets"), so they cannot be connected yet. Put it in a
   *separate*, customer-like directory, ideally billed in a non-USD currency to exercise the FX
   path, with a storage account, a container and a daily Cost and usage (FOCUS) export of
   month-to-date costs. Connect it on staging through the wizard (consent, the Cloud Shell script,
   the export, Check access) and compare the Overview with Cost analysis.

## Alternatives

- **Client secret.** Rejected: a long-lived shared secret.
- **Option (b), federated credential with our own issuer.** Deferred to production (above).
- **Azure SDK (`azure-identity`, `azure-storage-blob`).** Not needed: four REST calls and a JWT;
  adding them would pull a large dependency tree into the lockfile for no gain.
- **Ownership by the storage account named in the form.** Rejected: anyone could name another
  customer's account; the tag is checked on the account the scope's own export writes to.
- **One connection over several subscriptions without a billing account.** Each subscription's
  FOCUS export is separate; one connection per subscription keeps "one export, one connection".

## Checked against Microsoft's documentation (20 Nov 2026)

Every call was checked against the pages under Sources before the PR; where they disagreed with
what had been built from memory, the code now follows the docs:

| Built first | Documentation | Changed to |
|---|---|---|
| Exports list `2023-07-01-preview`, Query `2023-11-01` | Latest stable Cost Management version is `2026-06-01` (REST reference, specs repo) | `2026-06-01` for both |
| Resource Graph `2022-10-01`, Advisor `2023-01-01`, Storage `2023-05-01` | Latest stable: `2024-04-01`, `2026-10-01`, `2026-09-01` | Pinned to those (`client.API`) |
| No retry on throttling | ARM answers 429 with `Retry-After`; the Query API also sends `x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after` (QPU quotas per tenant) | `_send` waits as told and retries 3 times (429 and 503) |
| Query `nextLink` posted with `api-version` added again | `nextLink` is a full URL with its own `api-version` and `$skiptoken`; a query can answer 204 | `nextLink` sent as given; 204 reads as no rows |
| Rows without a subscription blocked the month (`nulls` error) | Improved exports leave the subscription null for unused reservations, savings plans and rounding | Counted against the connection's own account; only a missing charge period blocks |
| `x_SkuMeterName` only | FOCUS 1.2 renames it `SkuMeter` (`x_SkuMeterName` deprecated) | Either column |
| Dates assumed with seconds | FOCUS 1.0 writes `2024-01-01T00:00Z`, 1.0r2 adds `:00` | Both parsed (test) |
| AADSTS code parsed from `error_description` only | Token errors carry `error_codes` | `error_codes[0]` first |
| Billing-account role "Billing account reader" | EA: an app gets **EnrollmentReader** only through the billing role assignments API; MCA: billing account **Reader** | Portal steps and script comments say so |
| `az storage account list --query` with a backtick literal | (JMESPath raw strings) | A quoted raw string |
| Monitor aggregation `Average,Maximum` | Examples use `average,maximum`; each metric has its own `errorCode` | Lower case; failed metrics skipped |
| Pay-as-you-go subscriptions as a test account | MOSP subscriptions don't support FOCUS exports | Owner step 6: EA or MCA |

Matched the docs as built: the client assertion (PS256, `x5t#S256`, `aud` = the token endpoint,
`iss` = `sub` = client ID, short `exp`), the token request, the admin consent URL and its
`admin_consent=True` / `error` redirects, the export folder layout
(`<directory>/<export>/<YYYYMMDD-YYYYMMDD>/<run id>/`) and `manifest.json` fields
(`runInfo.runId`, `startDate`, `submittedTime`, `blobs[].blobName`, `dataRowCount`), the
`FocusCost` export type and `deliveryInfo.destination` fields, Parquet/snappy and CSV/gzip, the
List Blobs XML and `NextMarker` paging, the Resource Graph `$skipToken` paging and `data`, the
Advisor `$filter=Category eq 'Cost'`, `resourceMetadata.resourceId` and `nextLink`,
`Percentage CPU`, the role names (Reader, Cost Management Reader, Storage Blob Data Reader), Graph
`PATCH /applications/{id}` with `Application.ReadWrite.OwnedBy` (it replaces the whole
`keyCredentials` collection), and the GitHub federated credential (issuer
`https://token.actions.githubusercontent.com`, subject `repo:<org>/<repo>:environment:<name>`,
audience `api://AzureADTokenExchange`). The fake Azure answers in these documented shapes.

Not documented, read defensively: Advisor's `extendedProperties` is an open object; the keys used
(`savingsAmount`, `annualSavingsAmount`, `savingsCurrency`, `targetSku`) are what Advisor returns
for the VM right-size type `e10b1381-5f0a-47ff-8c7b-37bd13d7c974`, and a missing key falls back to
the bill. Advisor's savings use retail prices (documented), so AZ5 prefers the bill's cost. The
FOCUS schema page does not state the form of `SubAccountId`; both `/subscriptions/<guid>` and a
bare GUID are read. An EA billing account can span directories (documented); the scan covers only
subscriptions in the consenting directory.

## Sources (all read 20 Nov 2026)

- Certificate credentials (assertion format): https://learn.microsoft.com/en-us/entra/identity-platform/certificate-credentials
- Client credentials flow, admin consent, token request and errors: https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-client-creds-grant-flow
- Federated identity credential for GitHub Actions: https://learn.microsoft.com/en-us/entra/workload-id/workload-identity-federation-create-trust
- Graph, update application (keyCredentials): https://learn.microsoft.com/en-us/graph/api/application-update?view=graph-rest-1.0
- Cost Management exports (layout, manifest, formats, FOCUS limits, MOSP): https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/tutorial-improved-exports
- FOCUS dataset schema (1.0, 1.0r2, 1.2-preview): https://learn.microsoft.com/en-us/azure/cost-management-billing/dataset-schema/cost-usage-details-focus
- Exports - List (2026-06-01): https://learn.microsoft.com/en-us/rest/api/cost-management/exports/list
- Query example (2026-06-01): https://github.com/Azure/azure-rest-api-specs/blob/main/specification/cost-management/resource-manager/Microsoft.CostManagement/CostManagement/stable/2026-06-01/examples/SubscriptionQueryGrouping.json
- Query API QPU throttling headers: https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/manage-automation
- Scopes and roles (RBAC, EA, MCA, individual): https://learn.microsoft.com/en-us/azure/cost-management-billing/costs/understand-work-scopes
- EA roles for service principals (EnrollmentReader): https://learn.microsoft.com/en-us/azure/cost-management-billing/manage/assign-roles-azure-service-principals
- Advisor, Recommendations - List (2026-10-01): https://learn.microsoft.com/en-us/rest/api/advisor/recommendations/list
- Advisor cost recommendations (type IDs): https://learn.microsoft.com/en-us/azure/advisor/advisor-reference-cost-recommendations
- Advisor VM right-size and shutdown criteria: https://learn.microsoft.com/en-us/azure/advisor/advisor-cost-recommendations
- Resource Graph, Resources (2024-04-01): https://learn.microsoft.com/en-us/rest/api/azureresourcegraph/resourcegraph/resources/resources
- Monitor, Metrics - List (2023-10-01): https://learn.microsoft.com/en-us/rest/api/monitor/metrics/list
- VM metrics (`Percentage CPU`): https://learn.microsoft.com/en-us/azure/azure-monitor/reference/supported-metrics/microsoft-compute-virtualmachines-metrics
- List Blobs (paging, Storage Blob Data Reader): https://learn.microsoft.com/en-us/rest/api/storageservices/list-blobs
- ARM throttling (429, Retry-After): https://learn.microsoft.com/en-us/azure/azure-resource-manager/management/request-limits-and-throttling
- Stable API versions: https://github.com/Azure/azure-rest-api-specs (specification/cost-management, advisor, resourcegraph, storage, billing, resources)

## References

Sources above; multi-cloud market research §4, codebase assessment §B, ADR-007 amendment, ADR-017, ADR-018.
