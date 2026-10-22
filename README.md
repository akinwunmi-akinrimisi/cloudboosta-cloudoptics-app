# Cloudoptics by Cloudboosta

AWS cost optimisation for African teams: risk-scored recommendations with guided fixes that need only read access, verified savings, and every figure in USD and local currency.

## Live demo

**https://akinwunmi-akinrimisi.github.io/cloudboosta-cloudoptics-app/** (published from `main` by `.github/workflows/pages.yml`)

The demo is the real web app running on in-browser demo data: sign in with any email, create a workspace, walk through connecting an AWS account and see the permission checks. It makes no AWS calls and stores nothing outside your browser. Hosting plan: [ADR-012](docs/adr/012-github-first-hosting.md) (GitHub first, then DigitalOcean).

## Staging (DigitalOcean)

**https://cloudoptics.cloudboosta.com** runs on DigitalOcean App Platform in London with managed PostgreSQL ([ADR-013](docs/adr/013-digitalocean-app-platform-staging.md)). The API is under `/api`. `infra/digitalocean/provision.py` creates or updates everything from `infra/digitalocean/app.yaml`, reading secrets from the DigitalOcean API at deploy time. After each merge to `main`, `deploy-staging.yml` redeploys once the new image is published (repository secret `DIGITALOCEAN_TOKEN`).

Sign-in on staging requires the identity provider (WorkOS, ADR-006); until it is configured the API answers 503 for signed-in routes.

## Run the full stack

**In GitHub Codespaces:** Code → Codespaces → Create codespace on `main`. The dev container installs everything, starts PostgreSQL, runs migrations, starts the API and opens the web app pointed at it.

**Locally with Docker:** `docker compose up -d api` (PostgreSQL, migrations, API on :8000), then `pnpm install && pnpm --filter @cloudoptics/web dev` (web on :5173).

Sign-in uses development tokens until the identity provider is chosen (ADR-006). Validating a connection calls AWS with the credentials available to the API process; without any, it reports that the role could not be assumed.

## Repository layout

| Path | What it is |
|---|---|
| `docs/` | Product and engineering plan (phases 1–12), ADRs in `docs/adr/`, Sprint 0 spikes in `docs/spikes/` |
| `apps/api/` | Python backend: FastAPI API and the modular-monolith modules (`identity`, `connectors`, `ingestion`, `inventory`, `fx`, `analysis`, `recommendations`, `budgets`, `notifications`, `reporting`, `audit`, `platform`) |
| `apps/web/` | React + TypeScript single-page app (Vite) |
| `tools/cur-fixture/` | Synthetic AWS CUR 2.0 generator for ingestion and engine tests |
| `infra/cloudformation/` | Customer-facing read-only roles: `billing-role.yaml` (stage 1, with optional CUR 2.0 export) and `resource-role.yaml` (stage 2) |
| `.github/workflows/` | `ci.yml` (lint, types, tests with PostgreSQL, cfn-lint, bundle budget, audits, Gitleaks, Semgrep, Trivy, SBOM), `pages.yml` (demo), `container.yml` (API image to GHCR) |

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

# Synthetic CUR 2.0 data (add --restate for a version-2 restatement)
uv run cur-fixture generate --out ./.data/cur --period 2026-09
```

## Conventions

- Branches go through pull requests to `main`; CI must pass.
- Python: ruff, mypy strict, pytest (≥ 80% coverage, ≥ 90% for analysis, recommendations, ingestion mapping, fx and RBAC once they exist; docs/11f).
- TypeScript: strict mode, oxlint, Vitest; initial JS ≤ 300 KB gzipped.
- GitHub Actions are pinned to commit SHAs; Dependabot keeps them and the lockfiles current.
- Optional local hooks: `pre-commit install` (Gitleaks, ruff).

## Repository settings to apply (admin)

Branch protection on `main`: require the "Backend (lint, types, tests)", "Frontend (lint, types, tests, build, size)" and "Security (secrets, SAST, IaC/deps, SBOM)" checks, one approving review, linear history, and no force pushes. Turn on secret scanning with push protection and Dependabot alerts.

GitHub Pages: Settings → Pages → Source: **GitHub Actions** (the Pages workflow tries to enable this itself; on a private repository Pages needs a paid plan).
