# DigitalOcean provider: runbook

Design and decisions: [ADR-021](../../../../../../docs/adr/021-digitalocean-provider.md).

## Files

| File | What |
|---|---|
| `client.py` | Read-only HTTP client: token handling, rate limit, retries, paging; `set_transport` for the fake |
| `connect.py` | Token checks (`permission_checks`), write probes, storing a check |
| `routes.py` | `POST /connections/digitalocean`, `PUT /connections/{id}/token`; Scan now for DigitalOcean |
| `billing.py` | Invoices → `cost_daily`, month to date → `cost_snapshot_ce`, reconciliation |
| `inventory.py` | Resource scan and Droplet CPU → `resource` |
| `rules.py` | DO1-DO5 |
| `jobs.py` | `digitalocean:invoices.poll`, daily 05:29 UTC |
| `apps/api/tests/fake_digitalocean.py` | The fake API (tests and the E2E server) |

## Jobs

| Job | Schedule | Does |
|---|---|---|
| `digitalocean:invoices.poll` | daily 05:29 UTC | queues `ingestion:cur.ingest` for each working DigitalOcean connection |
| `ingestion:ce.poll` (shared) | daily 06:41 UTC | month to date (at most every 6 hours) |
| `inventory:inventory.poll` (shared) | every 12 h | the resource scan, then the analysis |
| `connectors:connections.health` (shared) | every 6 h | checks the token again; a worse result emails the admins |

## Symptoms

| Symptom | Where | Likely cause and fix |
|---|---|---|
| Connection `failed`, check "Read the team" says the token was not accepted | Connections page | The customer deleted or let the token expire. They choose **Replace token…** with a new one. |
| `failed`, "This token belongs to a different DigitalOcean team" | Connections page | A token from another team was stored by hand. Replace it with one from the connection's team. |
| `degraded`, a resource check is a warning | Connections page | A read scope was not ticked. Costs still load; that resource kind is not scanned. Replace the token with one that has the scope. |
| No Droplet CPU (`sync_run.scope.cpu = "no_access"`) | `sync_run` for `inventory.scan` | `monitoring:read` missing. DO4 stays quiet. |
| `ingest_batch` failed, `quality_check_failed` | `ingest_batch.checks` | The invoice CSV changed shape (missing column or unreadable amount). The previous month stays. Fix the mapping in `billing.py`, bump `MAPPING_VERSION`, reload. |
| `cost_reconciliation.status = mismatch` | Overview banner, logs | Loaded rows and the invoice differ by over 1% and USD 1. Compare `ingest_batch.checks` (`invoice_total`) with the invoice in DigitalOcean. |
| `sync_run.error_code = too_many_requests` | `sync_run` | The token hit DigitalOcean's limit (250/min, 5,000/h), usually because the customer uses it elsewhere too. The next run catches up; suggest a token for Cloudoptics only. |
| Month to date shows a total but no daily chart | Overview | The team has no insights data yet (DigitalOcean's starts in December 2025) or `billing:read` does not reach it. Costs from invoices are unaffected. |

Reload a connection's invoices from a Python shell on the worker (the Reload bills button is AWS-only for now):

```python
from cloudoptics.providers.digitalocean import billing

await billing.reload_connection(tenant_id, connection_id, None)  # every loaded month
```

## Never

- Print, log or copy a token. Support never needs it: the checks say what it can do.
- Call DigitalOcean's API from a developer machine with a customer token, or use the staging team's own token for tests (staging runs on DigitalOcean).

## Owner steps

1. **Test team.** Create a separate DigitalOcean team for Cloudoptics testing (not the staging team): Team settings → Create team. Give it a small monthly spend: one `s-1vcpu-512mb` Droplet, a 1 GiB volume left unattached, a snapshot and an unassigned reserved IP (about USD 15 a month). Wait for one closed invoice.
2. **Token.** In that team: API → Generate New Token → Custom Scopes, tick `account:read`, `billing:read`, `droplet:read`, `block_storage:read`, `snapshot:read`, `reserved_ip:read`, `load_balancer:read`, `database:read`, `monitoring:read`, expiry 90 days. Keep it in the password manager, not in the repository or CI.
3. **Try it on staging.** Connect the test team on staging through the Connect step (DigitalOcean) and check: every check `ok`, last month's invoice equal to the Costs page, month to date on Overview, DO1/DO2/DO3 on Recommendations. Then Replace token and Disconnect, and check the connection row's `external_id_enc` is NULL.
4. **KMS.** Nothing new: the token uses the existing connection-secret key and encryption context.
5. **Egress.** The worker and API call `api.digitalocean.com` over HTTPS; allow it if egress is ever restricted.
