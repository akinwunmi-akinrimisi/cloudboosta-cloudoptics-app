# DigitalOcean provider: runbook

Design and decisions: [ADR-021](../../../../../../docs/adr/021-digitalocean-provider.md).

## Files

| File | What |
|---|---|
| `client.py` | Read-only HTTP client: token handling, rate limit, retries, paging; `set_transport` for the fake |
| `connect.py` | Token checks (`permission_checks`), write probes, storing a check |
| `routes.py` | `POST /connections/digitalocean`, `PUT /connections/{id}/token`, the sign-in routes `GET /connections/digitalocean/oauth`, `POST .../oauth/start`, `POST .../oauth/callback` |
| `oauth.py` | Sign-in (OAuth): authorize URL with state and PKCE, code exchange, renewal under a row lock, revocation |
| `billing.py` | Invoices → `cost_daily`, month to date → `cost_snapshot_ce`, reconciliation |
| `inventory.py` | Resource scan and Droplet CPU → `resource` |
| `rules.py` | DO1-DO5 |
| `apps/api/tests/fake_digitalocean.py` | The fake API (tests and the E2E server) |

## Jobs

| Job | Schedule | Does |
|---|---|---|
| `ingestion:cur.poll` (shared) | every 6 h | loads any invoice DigitalOcean closed since the last poll |
| `ingestion:ce.poll` (shared) | daily 06:41 UTC | month to date (at most every 6 hours) |
| `inventory:inventory.poll` (shared) | every 12 h | the resource scan, then the analysis |
| `connectors:connections.health` (shared) | every 6 h | checks the token again; a worse result emails the admins |
| `connectors:connections.refresh_tokens` | hourly at :29 | queues `connection.refresh_token` for each signed-in connection whose access token expires within `CLOUDOPTICS_DO_OAUTH_REFRESH_DAYS` (7) |
| `connectors:connection.refresh_token` | queued | renews one sign-in (row lock; a racing run returns `fresh`); a refusal fails the connection and emails the admins |

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
| Signed-in connection `failed`, check "Renew access" (`last_error.code = refresh_refused`) | Connections page, `sync_run` `connection.refresh` | DigitalOcean refused the refresh token: the customer removed Cloudoptics' access in DigitalOcean, or a token was lost (a refresh that DigitalOcean accepted but we did not store). An admin chooses **Reconnect** and approves again; the connection and its history stay. |
| `connection.refresh_token` job failing and retrying, connection still healthy | worker logs (`network:…`, `server_error`) | DigitalOcean's token endpoint is down. Nothing changes until it answers; the old token is good until `token_expires_at`. |
| Sign-in ends on "This sign-in has expired or was already used" | onboarding | The state is single-use, 10 minutes, for the admin and workspace that clicked. They click **Connect DigitalOcean** again. |
| "DigitalOcean did not complete the sign-in" (`oauth_refused`) | onboarding | The code was refused: wrong client secret (`invalid_client`), callback URL differs from the registered one, or the code is older than 10 minutes. Check the two env vars and the registered callback URL. |
| Sign-in refused with "…cannot use billing:read" | onboarding | DigitalOcean granted fewer scopes than requested (see the scope test below). Set `CLOUDOPTICS_DO_OAUTH_SCOPE=read`. |
| Month to date shows a total but no daily chart | Overview | The team has no insights data yet (DigitalOcean's starts in December 2025) or `billing:read` does not reach it. Costs from invoices are unaffected. |

Reload a connection's invoices with the shared reload route (`POST /v1/tenants/{t}/connections/{id}/reload`), which runs the DigitalOcean plug-in.

## Never

- Print, log or copy a token (pasted, access or refresh). Support never needs it: the checks say what it can do.
- Call DigitalOcean's API from a developer machine with a customer token, or use the staging team's own token for tests (staging runs on DigitalOcean).

## Owner steps

1. **Test team.** Create a separate DigitalOcean team for Cloudoptics testing (not the staging team): Team settings → Create team. Give it a small monthly spend: one `s-1vcpu-512mb` Droplet, a 1 GiB volume left unattached, a snapshot and an unassigned reserved IP (about USD 15 a month). Wait for one closed invoice.
2. **Token.** In that team: Account → API → Generate New Token → Custom Scopes, tick `account:read`, `billing:read`, `droplet:read`, `block_storage:read`, `snapshot:read`, `reserved_ip:read`, `load_balancer:read`, `database:read`, `monitoring:read`, `tag:read`, expiry 90 days. Keep it in the password manager, not in the repository or CI.
3. **Try it on staging.** Connect the test team on staging through the Connect step (DigitalOcean) and check: every check `ok`, last month's invoice equal to the Costs page, month to date on Overview, DO1/DO2/DO3 on Recommendations. Then Replace token and Disconnect, and check the connection row's `external_id_enc` is NULL. **Confirm the write check (ADR-021):** a Read Only token must connect ("Token is read-only" ok) and a Full Access token must be refused. If the Read Only token is refused, DigitalOcean validates the request body before the scope: report it, because the probes then need changing before any customer connects.
4. **KMS.** Nothing new: the token uses the existing connection-secret key and encryption context.
5. **Egress.** The worker and API call `api.digitalocean.com` over HTTPS; allow it if egress is ever restricted.
6. **"Connect DigitalOcean" (sign-in, OAuth).** Until both settings below are set, onboarding shows only the token paste.
   1. In DigitalOcean, signed in to Cloudboosta's own account: **API → OAuth Applications → Register OAuth Application** (control panel page `https://cloud.digitalocean.com/account/api/applications`).
      - Name: `Cloudoptics`. Homepage URL: `https://cloudoptics.cloudboosta.com`. Description: "Read-only cloud cost analysis".
      - Callback URL, exactly: `https://cloudoptics.cloudboosta.com/app/connect/digitalocean/callback` (staging). For production add its own app URL + `/connect/digitalocean/callback`; a local test can use `http://localhost:5173/connect/digitalocean/callback`.
      - Save, then copy the **Client ID** and the **Client Secret** (the secret is shown on the application's page; treat it like a password).
   2. In GitHub, environment `github-pages` (the one Staging setup reads): add the secret `CLOUDOPTICS_DO_OAUTH_CLIENT_SECRET` and the client ID as `CLOUDOPTICS_DO_OAUTH_CLIENT_ID` (a variable or a secret; it is not secret).
   3. Run **Actions → Staging setup** from main. `provision.py` sets `CLOUDOPTICS_DO_OAUTH_CLIENT_ID` (plain) and `CLOUDOPTICS_DO_OAUTH_CLIENT_SECRET` (type SECRET) on the API and worker.
   4. Run the one-hour scope test below before telling customers.

## Scope test (one hour, before customers use the sign-in)

DigitalOcean's OAuth page links the `scope` parameter to the token scopes page, but neither page says whether an OAuth application may ask for granular scopes such as `billing:read`, or only `read` / `read write`. Cloudoptics asks for the ten read scopes it needs (`CLOUDOPTICS_DO_OAUTH_SCOPE`) and checks every scope by using it, so the result shows on screen. Use the test team from step 1, on staging, after step 6.

| # | Minutes | Do | Expect | If not |
|---|---|---|---|---|
| 1 | 10 | New workspace → DigitalOcean → **Connect DigitalOcean**. On DigitalOcean's page note what it lists (the ten scopes, or "Read"), whether it asks which team, and approve. | Back on Check access: every check `ok`, "Token is read-only" `ok`. | An "invalid scope" error page at DigitalOcean: granular scopes are not accepted. Set `CLOUDOPTICS_DO_OAUTH_SCOPE=read` (App Platform env, API and worker), redeploy, repeat. A check with "cannot use …": that scope was not granted; note which. |
| 2 | 5 | In the database (read-only console): `SELECT credential_type, token_expires_at FROM cloud_connection WHERE provider = 'digitalocean'`, and the `connection.created` audit row's `after.scope`. | `oauth`, about 30 days ahead; `scope` is what DigitalOcean granted. | Record the actual `expires_in` and scope in ADR-021. |
| 3 | 10 | Costs and Overview for the team. | Last closed invoice equal to the Costs page; month to date on Overview; DO1-DO3 after the scan. | `billing:read` not reaching the billing endpoints with an OAuth token: report it; keep the token paste as the main way (hide the button by unsetting the two env vars). |
| 4 | 10 | Renewal: `UPDATE cloud_connection SET token_expires_at = now() + interval '1 day' WHERE id = '<id>'`, wait for the hourly job (:29), then **Check again**. | `sync_run` `connection.refresh` succeeded, new expiry about 30 days ahead, check `ok`. | `invalid_client` / `invalid_request`: DigitalOcean wants the refresh without (or with only) client credentials (the docs' example has none). Report it: `oauth.refresh` sends `client_id` and `client_secret`. |
| 5 | 10 | **Disconnect…**. In DigitalOcean, find where the account lists the applications it has authorized (under API; note the exact place for RB-22). | Audit `connection.token_revoked` with `revoked: true`; Cloudoptics no longer listed as authorized (or its token gone). | `revoked: false`: check the API logs for the revoke error code; the token is wiped either way. If the app is still authorized, revoke it by hand and note that revoking the access token does not end the grant. |
| 6 | 10 | Sign in again choosing **Manage permissions** (leave the scope empty: `CLOUDOPTICS_DO_OAUTH_SCOPE=""` on a test deploy) and tick write too. | Refused: "This sign-in gave Cloudoptics write access…", nothing stored. | Report: the write probes need checking for OAuth tokens. |
| 7 | 5 | Write the results (scope accepted, team picker seen, `expires_in`, refresh parameters, revoke) into ADR-021 "Checked against DigitalOcean's documentation". | | |
