# Cloudoptics by Cloudboosta

AWS cost optimisation for African teams: risk-scored recommendations with guided fixes that need only read access, verified savings, and every figure in USD and local currency.

## Live demo

**https://cloudoptics.cloudboosta.com/demo/** (the `demo` component in `infra/digitalocean/app.yaml`), mirrored at https://akinwunmi-akinrimisi.github.io/cloudboosta-cloudoptics-app/ (published from `main` by `.github/workflows/pages.yml`)

The demo is the real web app running on in-browser demo data. It opens on a sample workspace ("Kola Pay", marked "Sample data"); sign out and sign in with any email to create your own workspace and walk through connecting an AWS account. It makes no AWS calls, sends no analytics and stores nothing outside your browser. Hosting plan: [ADR-012](docs/adr/012-github-first-hosting.md) (GitHub first, then DigitalOcean).

## Staging (DigitalOcean)

**https://cloudoptics.cloudboosta.com** runs on DigitalOcean App Platform in London with managed PostgreSQL ([ADR-013](docs/adr/013-digitalocean-app-platform-staging.md)). One origin, split by path ([ADR-016](docs/adr/016-public-site-astro-static-and-path-split-hosting.md)): the public site (`apps/site`) at `/`, the web app at `/app/`, the demo at `/demo/`, the API at `/api`, and the CloudFormation templates at `/cfn/` (served by the site). Old app links such as `/overview` forward to `/app/overview`. `infra/digitalocean/provision.py` creates or updates everything from `infra/digitalocean/app.yaml`, reading secrets from the DigitalOcean API at deploy time. After each merge to `main`, `deploy-staging.yml` redeploys once the new image is published (repository secret `DIGITALOCEAN_TOKEN`).

Sign-in uses WorkOS AuthKit (ADR-006). **Actions → Staging setup** (run from `main`) sets up Cloudoptics' AWS account (template bucket, broker role and user) and pushes the broker key and WorkOS settings to App Platform; see `docs/handoff/staging-next-steps.md` for the one-time WorkOS dashboard settings.

## What works today (18 Nov 2026)

| Area | What you can do |
|---|---|
| Sign-in and workspaces | Sign in with WorkOS, create a workspace, invite people and change or remove members |
| Connecting AWS | One CloudFormation stack with a one-click link: billing access, resource access and member-account rollout together. Later permission changes are one-click in-place updates ("Update CloudOptics access"). CloudOptics only reads |
| Connecting Google Cloud | Billing account + the BigQuery FOCUS export: four copy-paste commands (a dataset label proves it is yours, then read-only roles). No Google key is stored: Workload Identity Federation from the AWS broker ([ADR-019](docs/adr/019-google-cloud-provider.md)). Costs from BigQuery, projects as accounts, resources, Recommender and seven starter rules. Tested against a fake Google Cloud; owner set-up in `infra/gcp/setup.sh` |
| Connecting DigitalOcean | Paste a read-only API token once (custom read scopes; a token that can write is refused). Stored only encrypted under KMS, wiped on disconnect, never shown again ([ADR-021](docs/adr/021-digitalocean-provider.md)). Invoices as closed months, balance and daily insights as month to date, the resource scan with Droplet CPU and five starter rules. Tested against a fake DigitalOcean API |
| Connecting Azure | Subscription (or billing account) + the Cost Management FOCUS export: admin consent, one Cloud Shell script (read-only roles and a storage-account tag that proves it is yours) and the export. No client secret: the multi-tenant Entra app signs in with a certificate rotated by Staging setup ([ADR-020](docs/adr/020-azure-provider-and-certificate-credential.md)). Costs in the bill's currency and USD, subscriptions as accounts, Resource Graph, Advisor, Azure Monitor and five starter rules. Tested against a fake Azure; owner steps in ADR-020 |
| Costs | Month-to-date spend, month-end and 3-month forecast with a currency scenario, "what changed" since last month, daily trend, top spend, Costs Explorer, every figure in USD and local currency |
| Recommendations | Unattached volumes, old snapshots, idle IPs and load balancers, gp2 to gp3, idle servers and databases, rightsizing, older and x86 server types, office-hours schedules for non-production, Extended Support charges, idle NAT gateways, Savings Plan advice; each with a risk score, usage charts, an "Open in AWS console" fix and rollback steps. Assign, comment and act in bulk; Cloudoptics checks AWS for the change and measures the realised savings from the bills |
| Planning and alerts | Budgets with alerts, credits runway (how long AWS credits will last), anomaly detection, Commitments page (Savings Plans and Reserved Instances, with expiry alerts at 60/30/15 days), connection-failure and assignment emails, notification preferences and a weekly digest. Email is the only channel (ADR-014) |
| Reports | Monthly executive report; audit log under Settings |

Progress against the sprint plan is in [docs/11d](docs/11d_implementation_roadmap_and_sprint_plan.md) (Sprints 2 to 5 complete; Sprint 6 next).

## Run the full stack

**In GitHub Codespaces:** Code → Codespaces → Create codespace on `main`. The dev container installs everything, starts PostgreSQL, runs migrations, starts the API and opens the web app pointed at it.

**Locally with Docker:** `docker compose up -d api` (PostgreSQL, migrations, API on :8000), then `pnpm install && pnpm --filter @cloudoptics/web dev` (web on :5173).

Locally, sign-in uses development tokens; staging uses WorkOS AuthKit (ADR-006). Validating a connection calls AWS with the credentials available to the API process; without any, it reports that the role could not be assumed.

## Repository layout

| Path | What it is |
|---|---|
| `docs/` | Product and engineering plan (phases 1–12), ADRs in `docs/adr/`, Sprint 0 spikes in `docs/spikes/` |
| `apps/api/` | Python backend: FastAPI API and the modular-monolith modules (`identity`, `connectors`, `ingestion`, `inventory`, `metrics`, `fx`, `analysis`, `recommendations`, `budgets`, `anomalies`, `notifications`, `reporting`, `audit`, `platform`) |
| `apps/web/` | React + TypeScript single-page app (Vite) |
| `apps/site/` | Public site (Astro, static): landing, how it works, security with the generated permission reference ([ADR-016](docs/adr/016-public-site-astro-static-and-path-split-hosting.md), docs/14) |
| `packages/design-tokens/` | Colour, font and radius tokens shared by `apps/web` and `apps/site` |
| `tools/cur-fixture/` | Synthetic AWS CUR 2.0 generator for ingestion and engine tests |
| `infra/cloudformation/` | Customer-facing read-only access: `cloudoptics-access.yaml` is the single onboarding stack (setup step in `setup/`). `billing-role.yaml`, `resource-role.yaml` and `resource-stackset.yaml` are the older stacks, kept for connections that have not moved yet |
| `infra/aws/`, `infra/digitalocean/` | Staging setup: Cloudoptics' own AWS account (template bucket, broker role) and the App Platform app |
| `infra/gcp/` | `setup.sh`: Cloudoptics' own Google Cloud side (workload identity pool trusting the AWS broker, keyless service account), run by the owner once per environment |
| `infra/azure/` | `entra_certificate.py`: rotates the Cloudoptics Entra app's certificate (GitHub OIDC to Graph, no secret), run by Staging setup |
| `.github/workflows/` | `ci.yml` (lint, types, tests with PostgreSQL, cfn-lint, bundle budget, audits, Gitleaks, Semgrep, Trivy, SBOM), `pages.yml` (demo), `container.yml` (API image to GHCR), `deploy-staging.yml` (redeploy staging after each merge), `staging-setup.yml` (one-off staging setup) |

## Getting started

Requirements: Python 3.12+ with [uv](https://docs.astral.sh/uv/), Node.js 22+ with pnpm 10.

```bash
# Backend and tools (tests need PostgreSQL; set CLOUDOPTICS_TEST_ADMIN_URL if not
# postgres:postgres@localhost:5432)
uv sync --all-packages
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
uv run alembic -c apps/api/alembic.ini upgrade head
uv run uvicorn cloudoptics.main:app --reload        # http://localhost:8000/docs
uv run python -m cloudoptics.worker                 # queued jobs (CLOUDOPTICS_JOBS_MODE=queue)

# Web
pnpm install
pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm size
pnpm --filter @cloudoptics/web dev                  # http://localhost:5173
VITE_API_MODE=demo pnpm --filter @cloudoptics/web dev   # same app on demo data, no API

# Public site (apps/site): build, then serve dist/ with real 404s
pnpm --filter @cloudoptics/site build && pnpm --filter @cloudoptics/site preview   # http://localhost:4321
pnpm --filter @cloudoptics/site dev                 # live reload (no CSP in dev)

# Synthetic CUR 2.0 data (add --restate for a version-2 restatement)
uv run cur-fixture generate --out ./.data/cur --period 2026-09
```

## Conventions

- Branches go through pull requests to `main`; CI must pass.
- Python: ruff, mypy strict, pytest (≥ 80% coverage, ≥ 90% for analysis, recommendations, ingestion mapping, fx and RBAC once they exist; docs/11f).
- TypeScript: strict mode, oxlint, Vitest; initial JS ≤ 300 KB gzipped.
- GitHub Actions are pinned to commit SHAs; Dependabot keeps them and the lockfiles current.
- Tenant isolation is checked in CI: the RLS lint, the every-table cross-tenant test and the route membership test (docs/09 §2.3). A new table with `tenant_id` needs RLS ENABLE + FORCE and a `tenant_isolation` policy; a new workspace route needs `ViewerAccess`, `MemberAccess` or `AdminAccess`. An intended exception goes in the test's allowlist with its reason.
- Optional local hooks: `pre-commit install` (Gitleaks, ruff).

## Repository settings to apply (admin)

Branch protection on `main`: require the "Backend (lint, types, tests)", "Frontend (lint, types, tests, build, size)", "Site (permissions, build, links, budgets, accessibility)" and "Security (secrets, SAST, IaC/deps, SBOM)" checks, one approving review, linear history, and no force pushes. Turn on secret scanning with push protection and Dependabot alerts.

GitHub Pages: Settings → Pages → Source: **GitHub Actions** (the Pages workflow tries to enable this itself; on a private repository Pages needs a paid plan).
