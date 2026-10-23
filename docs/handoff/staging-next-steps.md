# Handoff: staging next steps (22 Oct 2026)

**Update:** the credentials live in the GitHub environment `github-pages`, so sections 2 and 3 are now automated: run **Actions → Staging setup** from `main` (`.github/workflows/staging-setup.yml`, code in `infra/aws/bootstrap.py` and `infra/digitalocean/provision.py`). WorkOS sign-in is implemented (API `WorkOSVerifier`, web `AuthGate`). What remains manual is listed under "Before running Staging setup" below.

## Before running Staging setup

1. Merge the open PR so `main` has the code and the API image.
2. Add `DIGITALOCEAN_TOKEN` to the same GitHub environment (and as a repository secret for `deploy-staging.yml`).
3. In the WorkOS dashboard (Redirects): add `https://cloudoptics.cloudboosta.com/` as a redirect URI, the sign-in endpoint `https://cloudoptics.cloudboosta.com/sign-in`, and `https://cloudoptics.cloudboosta.com` as an allowed origin (CORS).
4. After a successful run, delete the admin AWS key from the GitHub environment (the app uses the narrowly scoped broker key the workflow creates; re-add the admin key only to run setup again).

The sections below record the design the automation follows.

## Where things stand

| Item | State |
|---|---|
| Public demo | https://akinwunmi-akinrimisi.github.io/cloudboosta-cloudoptics-app/ (GitHub Pages, demo data) |
| Staging | https://cloudoptics.cloudboosta.com: DigitalOcean App Platform `cloudoptics-staging` (id `3294882e-35ce-45b0-bb33-e7ce55062f22`), London, API under `/api` |
| Database | Managed PostgreSQL 16 `cloudoptics-pg`, database `cloudoptics`, app user `cloudoptics_app`; firewall admits only the app |
| Deploys | `uv run python infra/digitalocean/provision.py app` (idempotent; `--branch` for trial deploys). After PR #6 merges, `deploy-staging.yml` redeploys on every merge once the repository secret `DIGITALOCEAN_TOKEN` exists |
| Sign-in on staging | Not configured: API answers 503 on signed-in routes, the sign-in page says "being set up" |
| CloudFormation | Quick-create disabled (`CLOUDOPTICS_CFN_QUICK_CREATE_ENABLED=false`); onboarding offers the template download from `/cfn/` |

## 1. Check the credentials (read-only)

```bash
uv run python -c "import boto3; print(boto3.client('sts').get_caller_identity()['Arn'])"
```

The keys must be ordinary environment variables. If their values start with `prox`, they were saved as network secrets, which cannot work for AWS request signing.

## 2. AWS: Cloudoptics' own account (ADR-007)

In the account behind the keys (region `eu-west-2` for our resources; CloudFormation for customers stays in `us-east-1`):

1. **Template bucket** `cloudoptics-public-<account-id>`: public read of `cfn/v1/*.yaml` only, versioning, TLS-only policy. Upload `infra/cloudformation/billing-role.yaml` and `resource-role.yaml`. Add a CI step that uploads them on merge when they change.
2. **Broker role** `CloudopticsBroker`: may only `sts:AssumeRole` on `arn:aws:iam::*:role/CloudopticsBillingReadOnly` and `.../CloudopticsResourceReadOnly`. For staging, a dedicated IAM user `cloudoptics-staging-broker` whose only permission is assuming `CloudopticsBroker` (access key stored as App Platform secrets `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` on `api` and `worker`). Replace with IAM Roles Anywhere when we move to DOKS (spike CC-1).
3. Update `infra/digitalocean/app.yaml` envs: `CLOUDOPTICS_CLOUDOPTICS_PRINCIPAL_ARN` (the broker role ARN), `CLOUDOPTICS_CFN_BILLING_TEMPLATE_URL`, `CLOUDOPTICS_CFN_RESOURCE_TEMPLATE_URL`, `CLOUDOPTICS_CFN_QUICK_CREATE_ENABLED=true`. `SessionFactory` must assume the broker role first, then the customer role (role chaining keeps the customer trust policy pointing at one stable ARN).
4. Test end to end against a real account with `infra/cloudformation/billing-role.yaml`.

Never commit keys. Rotate the admin key you were given once the broker user exists.

## 3. WorkOS AuthKit sign-in (ADR-006)

1. In the WorkOS dashboard: add redirect URI `https://cloudoptics.cloudboosta.com/auth/callback` (and `http://localhost:5173/auth/callback`). Add a JWT template that includes `email` (AuthKit access tokens do not carry it by default), or have the API fetch the user's email with the WorkOS API key on first sign-in.
2. Web: add `@workos-inc/authkit-react` (pin an exact version) behind the existing `Api` interface; `HttpApi.signIn` redirects to AuthKit when `/v1/auth/config` says `oidc`.
3. API: set `CLOUDOPTICS_OIDC_ISSUER`, `CLOUDOPTICS_OIDC_AUDIENCE` (client ID) and `CLOUDOPTICS_OIDC_JWKS_URL` (`https://api.workos.com/sso/jwks/<client_id>`) in `app.yaml`; check the issuer value against a real token before relying on it. Keep the API key as an App Platform secret only if the email lookup path is used.
4. Tests: an OIDC verifier test with a locally generated RSA key and a fake JWKS endpoint.

## 4. Other open items

- Resolve the GitGuardian incident 38868057 (placeholder password in old commits) in the dashboard.
- Branch protection on `main` (README).
- Exchange rates: owner of daily updates and a backup (PRD Q3).
- Sprint 2: billing ingestion (CUR 2.0 → FOCUS → daily aggregates) is in `apps/api/src/cloudoptics/ingestion`. The worker polls every 6 hours and right after a connection validates; admins can also call `POST /v1/tenants/{t}/connections/{c}/ingest`. Aggregates live in PostgreSQL only (decision of 23 Oct 2026): raw and normalised Parquet in Spaces come later. Cost Explorer first-value summary (CC-9): right after a connection validates, and daily until the month's CUR is loaded, the worker stores a provisional month-to-date snapshot (`cost_snapshot_ce`, two `GetCostAndUsage` calls, at most every 6 hours per connection); the Overview labels it provisional. `GET /costs/daily` and `GET /costs/breakdown` feed the Overview trend chart and top-spend list. Exchange rates (IN-8): table `fx_rate` (append-only, global). Staff listed in `CLOUDOPTICS_PLATFORM_ADMIN_EMAILS` (comma-separated, set on the `api` component in App Platform) set rates with `POST /v1/admin/fx/rates`; moves over 5% need `confirm`, every change goes to the platform audit chain. A manual rate is carried forward at most 5 days (flagged estimated). The reference feed is off until `CLOUDOPTICS_FX_FEED_URL` is set (any JSON URL answering `{"base":"USD","rates":{...}}`, for example open.er-api.com); it is used only for a currency that has never had a manual rate. The admin console page and two-person approval for moves over 10% are ADM-10 (Sprint 7). Still to do: Organizations discovery (CC-7); reconciliation (IN-6) is Sprint 3.
