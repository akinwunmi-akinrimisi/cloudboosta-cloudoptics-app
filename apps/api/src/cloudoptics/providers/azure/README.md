# Azure plug-in: runbook

Design and owner steps: [ADR-020](../../../../../../docs/adr/020-azure-provider-and-certificate-credential.md).

| File | What it does |
|---|---|
| `credentials.py` | Client assertion (PS256, `x5t#S256`) and token cache per customer directory |
| `client.py` | REST over httpx: Resource Manager, Resource Graph, Blob Storage; `set_transport` for fakes |
| `connection.py` | Settings row, ownership code, consent link, Cloud Shell script, portal and export steps |
| `validate.py` | The seven checks and the stored result |
| `billing.py` | FOCUS export runs -> `cost_daily` / `cost_resource_daily` (native + USD) |
| `reconcile.py` | Cost Management query: reconciliation and month-to-date snapshot |
| `inventory.py` | Resource Graph scan, Advisor, Azure Monitor CPU |
| `rules.py` | AZ1-AZ5 |
| `routes.py` | `/v1/tenants/{t}/providers/azure/connections`, `/v1/azure/consent` |

## A check fails

| Check | Usual cause | What the customer does |
|---|---|---|
| `consent` (AADSTS700016/65001) | No admin consent in that directory | A Global Administrator opens the consent link |
| `consent` (AADSTS90002) | Wrong directory ID | Entra ID -> Overview -> Tenant ID |
| `consent` (AADSTS700027/7000215, "could not sign in") | **Ours**: certificate expired or not on the app | Re-run Staging setup with `azure_certificate=true` |
| `reader`, `cost_reader` | Role missing on the scope | Run the Cloud Shell script again |
| `export` | No FOCUS export into that storage account and container | Create the export (step 3); check the container name |
| `ownership` | Tag missing or another connection's code; storage account in a subscription without Reader | Run the script again (it adds Reader on the storage account and the tag) |
| `ownership` "already connected to another workspace" | The scope is verified in another workspace (ADR-017) | Disconnect it there, or join that workspace |
| `blob_reader` (AuthorizationPermissionMismatch) | No Storage Blob Data Reader on the container | Run the script again; role assignments can take up to 10 minutes |
| `export_data` warning | The export has not run yet | Exports -> Run now |

## Bills look wrong

- `ingest_batch.checks` of the run says why a month was blocked: `fx` error (no staff rate and no
  `x_BilledCostInUsd` in the export: set a rate on the FX page, then the next poll loads it),
  `currency` (two currencies in one export), `completeness` (the manifest's row count differs:
  usually a run still being written; the next run fixes it), `schema`.
- `fx` warning: the month was converted with Microsoft's rate; once staff set Cloudoptics' rate,
  reload the month (queued mode: Reload bills on the Connections page) to use it.
- `cost_reconciliation.status = mismatch`: compare the month in Cost analysis (Actual cost,
  grouped by subscription) with `sum(billed_cost)` for the connection and period.

## Credential

- Expiry: the certificate lives `certificate_days` (default 30). Rotate with Staging setup
  `azure_certificate=true`; the running certificate stays on the app until the new deployment is
  live. If both lapse, every Azure check shows "could not sign in".
- Revoke at once: Entra ID -> App registrations -> Cloudoptics -> Certificates & secrets -> delete
  the certificate; then rotate to issue a new one.

## Tests and the fake

`apps/api/tests/fake_azure.py` plays Entra ID (it verifies the assertion's signature, audience,
issuer, expiry and thumbprint), Resource Manager, Cost Management, Resource Graph, Advisor,
Monitor and Blob Storage, and writes synthetic FOCUS exports (Parquet, CSV, gzipped CSV).
`tests/test_azure.py` covers the plug-in; `apps/e2e/tests/azure.spec.ts` connects a
subscription in the browser against the same fake (the E2E server routes Azure to it).
