# Cloudoptics by Cloudboosta

AWS cost optimisation for African teams: risk-scored recommendations with guided fixes that need only read access, verified savings, and every figure in USD and local currency.

## Repository layout

| Path | What it is |
|---|---|
| `docs/` | Product and engineering plan (phases 1–12), ADRs in `docs/adr/`, Sprint 0 spikes in `docs/spikes/` |
| `apps/api/` | Python backend: FastAPI API and the modular-monolith modules (`identity`, `connectors`, `ingestion`, `inventory`, `fx`, `analysis`, `recommendations`, `budgets`, `notifications`, `reporting`, `audit`, `platform`) |
| `apps/web/` | React + TypeScript single-page app (Vite) |
| `tools/cur-fixture/` | Synthetic AWS CUR 2.0 generator for ingestion and engine tests |
| `.github/workflows/ci.yml` | CI: lint, types, tests, coverage, bundle-size budget, dependency audits, Gitleaks, Semgrep, Trivy, SBOM |

## Getting started

Requirements: Python 3.12+ with [uv](https://docs.astral.sh/uv/), Node.js 22+ with pnpm 10.

```bash
# Backend and tools
uv sync --all-packages
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
uv run uvicorn cloudoptics.main:app --reload        # http://localhost:8000/healthz

# Web
pnpm install
pnpm lint && pnpm typecheck && pnpm test && pnpm build && pnpm size
pnpm --filter @cloudoptics/web dev                  # http://localhost:5173

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
