# 11f — Quality, Testing & Release Strategy

**Product:** Cloudoptics by Cloudboosta · **Phase:** 11 · **Date:** 22 October 2026
**Inputs:** `06_…` (NFRs, release criteria §8), `10_…` (CI/CD, environments), `11a_…`–`11e_…`
**Status:** Draft for engineering review

---

## 1. Testing Strategy

### 1.1 Test pyramid & ownership

| Level | Share of tests | Owner | Runs |
|---|---|---|---|
| Unit | ~70% | Engineers (author) | Every commit (CI) |
| Integration (DB, queue, storage, AWS mocks, API) | ~20% | Engineers + SDET | Every PR |
| Contract (OpenAPI, AWS response recordings) | small | Engineers | Every PR |
| End-to-end (critical journeys) | ~5–10% | SDET | PR (smoke subset) + merge to main (full) + nightly |
| Performance/load | targeted | SDET + Tech Lead | Nightly (small) + before each increment (full) |
| Security | continuous + periodic | DevOps + Tech Lead | Every PR (SAST/SCA/secrets/IaC) + periodic audits/pen test |
| Accessibility | continuous + manual | Frontend + Designer | Every PR (axe) + manual each increment |

### 1.2 Unit testing

| Area | Framework | Coverage expectation | Focus |
|---|---|---|---|
| Backend (Python) | **pytest**, `pytest-asyncio`, **Hypothesis** (property-based), `freezegun`/`time-machine` | **≥ 80% line coverage overall; ≥ 90% for `analysis`, `recommendations`, `ingestion` mapping, `fx` and RBAC policy modules** (diff coverage ≥ 80% enforced per PR) | Engine rules and edge cases; scoring maths; FX conversion and decomposition; state-machine transitions; permission checks; money rounding |
| Frontend (TS) | **Vitest** + Testing Library | ≥ 70% for `features/` and `components/` (MoneyValue, formatting at 100%) | Currency formatting (NGN/KES/ZAR/EGP/GHS), toggles, filter-state logic, error mapping |

**Engine testing approach:**
- **Golden datasets:** versioned fixture sets (inventory + metrics + prices) with expected recommendations per engine version. Any change to the outputs must be reviewed and approved.
- **Property tests:** e.g., savings ≥ 0; risk score within 0–100; dedupe never double-counts; a recommendation never targets a resource tagged `cloudoptics:ignore`.

### 1.3 Integration testing

| Target | Approach |
|---|---|
| **Database** | Testcontainers PostgreSQL 16 with migrations applied; **RLS tests**: every tenant table queried under another tenant's context returns 0 rows; migration lint (tables with `tenant_id` must have RLS policies) |
| **Job queue** | Procrastinate against the test DB: chaining, locks, retries, DLQ, idempotent re-runs |
| **Object storage** | MinIO container (S3 API) for raw/norm paths; DuckDB `httpfs` against MinIO |
| **AWS connectors** | **moto** (and LocalStack where moto lacks coverage) for S3, STS, EC2, RDS, ELB, CloudWatch, Organizations; **recorded responses** (botocore stubber/VCR) for Cost Explorer, Cost Optimization Hub, Compute Optimizer, Savings Plans; error-path tests (AccessDenied, throttling, opt-in required) |
| **Real AWS (scheduled)** | Nightly job against Cloudoptics **test AWS accounts** (incl. `af-south-1` and a Lagos Local Zone resource where feasible): connect, ingest real CUR, scan inventory, run engines; compare with expected ranges |
| **Ingestion** | Golden-file tests: synthetic and anonymised real CUR → FOCUS Parquet → `cost_daily` totals that match expected sums (to the cent); restatement scenario; credits/SP amortisation; Local Zones |
| **API** | FastAPI TestClient/httpx with a real DB: auth, RBAC matrix (generated from OpenAPI `x-required-role`), problem+json errors, pagination, ETags, rate limits |
| **Notifications** | Slack/Postmark mocked at HTTP level; templates rendered with malicious metadata (escaping tests) |
| **FX** | Provider adapters with recorded responses; fallback and carry-forward behaviour |

### 1.4 End-to-end testing (critical user journeys)

**Framework:** Playwright (TypeScript) against staging (and preview environments for the smoke subset), using seeded tenants and test AWS accounts.

| E2E journey | Phase 7 ref | Key assertions |
|---|---|---|
| Sign up → create workspace → choose currency → connect AWS (test account) → validation passes | J1 | Connection Healthy; first insights within the test SLA |
| Overview → "What changed" → Explorer drill-down → export CSV | J2 | Totals match the fixture; CSV contents and formula guard |
| Recommendation list → detail → assign → mark done → (simulated change) → verified | J3 | Status transitions; Slack DM mocked; verified savings shown |
| Create local-currency budget → simulate overspend data → alert delivered | J4 | Alert once per threshold; correct amounts in both currencies |
| Admin: invite user (Viewer) → Viewer cannot change settings → remove user → access revoked | J5 | RBAC enforced in UI and API |
| Monthly report generation → PDF content | J6 | PDF renders; both currencies; FX basis disclosed |
| Currency toggle USD / Local / Both across pages | — | Consistent values and formatting |

Run on Chromium + WebKit (desktop) and a mobile viewport (Pixel/Android emulation) with network throttling for the Overview.

### 1.5 Performance & load testing

| Test | Tool | Target (from NFRs) |
|---|---|---|
| API load (cost summary, explorer queries, recommendations list) | **k6** | p95 ≤ 2s for cost queries, p95 ≤ 500 ms for other endpoints at 3× expected peak (e.g., 200 tenants, 50 concurrent users) |
| Ingestion throughput | Python harness with synthetic CUR (5M, 20M, 50M rows/month) | 5M rows ≤ 30 min (NFR-PERF-04); memory under limits; no OOM |
| Analysis runtime | Synthetic inventories (1k, 5k, 20k resources) | ≤ 15 min at 5k resources (NFR-PERF-05) |
| Queue/backpressure | Burst of 1,000 tenants' scheduled jobs | Fair scheduling; no tenant starved > 1h; KEDA scaling behaviour |
| Frontend performance | **Lighthouse CI** with throttled "Fast 3G" | Overview LCP ≤ 4s; JS budget ≤ 300 KB gz |
| Soak | 24h staging run | No memory leaks; stable latency |

### 1.6 Security testing

| Activity | Tooling / approach | When |
|---|---|---|
| SAST | Semgrep (Python, TS rulesets + custom rules: missing RBAC dependency, raw SQL without tenant context) | Every PR (blocking on High) |
| SCA | Dependabot/Renovate + `pip-audit` + `npm audit` | Every PR + daily |
| Secrets | Gitleaks + GitHub push protection | Pre-commit + every PR |
| IaC & containers | Trivy (Terraform, K8s manifests, images), Kyverno policy tests | Every PR + nightly image rescans |
| DAST | OWASP ZAP baseline against staging (authenticated) | Nightly; full scan before each increment |
| Tenant-isolation suite | Automated cross-tenant API + DB tests | Every PR |
| Security code audit | **Cloudflare `security-audit` skill**, full audit mode, multiple runs; findings triaged into the backlog | Before alpha (S3), before beta (S6), before v1, then quarterly |
| Penetration test | External firm (web app, API, tenancy, AWS access path) | Before v1 paid launch (S11) |
| Threat model review | Update Phase 10 threat model | Each increment + new integrations |

---

## 2. Environments & Release Flow

### 2.1 Environments
As defined in Phase 10 B2: **Local → Preview (per PR) → Staging → Production**. No customer data outside production.

### 2.2 Promotion criteria

| From → To | Criteria |
|---|---|
| **PR → main** | Review by ≥ 1 engineer (2 for `connectors`, `identity`, RLS/migrations, IAM templates); all CI checks green (tests, coverage gates, scans, OpenAPI diff, bundle budget, template validation); linked story; DoD checklist |
| **main → Staging** (automatic) | Build signed; migrations applied (expand-only); smoke tests pass |
| **Staging → Production** (manual approval) | Full E2E suite green; no open Critical/High bugs or security findings for the release; nightly real-AWS test green within 24h; release notes; feature flags configured; rollback plan noted for risky changes; approval by the Tech Lead (or delegate) |
| **Production canary → 100%** | Canary (10% of pods) for 15–30 min: error rate and latency within SLO, no new Sentry issue spikes, job success rates normal; automatic rollback on breach |

### 2.3 Feature flag strategy
- **Tool:** PostHog feature flags behind an OpenFeature-style interface. Server-side evaluation for the API/engines; client SDK for UI.
- **Flag types:**
  - **Release flags** (temporary): hide incomplete features; removed within 2 sprints of full rollout.
  - **Ops/kill switches** (permanent): per recommendation engine (`engine.R5.enabled`), paid AWS API usage (`connector.metrics.enabled`), notifications per channel, PDF rendering.
  - **Entitlements:** plan-based features (v1).
  - **Experiments:** onboarding copy, pricing page (v1).
- **Targeting:** by tenant (design partners, internal tenant first), then percentage rollout.
- **Rules:**
  - Default to safe values if the flag service is unavailable.
  - Flags are listed in a registry with owner and expiry.
  - Stale flags are flagged in CI.
- **Engine rollout pattern:**
  1. Internal tenant (dogfooding).
  2. One design partner, with the FinOps SME reviewing the outputs.
  3. All design partners.
  4. GA.
  - New engine versions can run in **shadow mode** (computed, logged, not shown) for comparison.

### 2.4 Rollback plans

| Change type | Rollback |
|---|---|
| Application code | Argo CD rollback to the previous image (one click / automated on canary failure); target < 10 min |
| DB migrations | Expand → migrate → contract: releases are backward-compatible with the previous app version; contract steps ship one release later; down-migrations tested for expand steps; PITR as last resort |
| Feature behaviour | Disable via flag (seconds) |
| Recommendation engine output | Kill switch per engine + revert engine version; recompute; affected recommendations auto-expired with an in-app note |
| Ingestion mapping bug | Revert the mapping version; reprocess affected periods from `raw/`; "data restated" notice |
| Infrastructure (Terraform) | Revert PR + apply; state backups; destructive changes require a 2-person review |
| CloudFormation template (customer-facing) | Versioned templates; previous version remains available; customers are not forced to update; changes that need customer action are communicated via in-app + email |

### 2.5 Release cadence & communication
- Beta: deploys to production up to daily, behind flags; a weekly release summary to design partners (Slack Connect).
- v1: weekly release train (Tuesday–Thursday, 09:00–15:00 WAT); no Friday deploys except fixes; maintenance windows outside 07:00–20:00 WAT/EAT/SAST.
- Changelog in-app ("What's new") and on the docs site.

---

## 3. Definition of Done (DoD)

A story is **Done** only when all applicable items are true:

**Code & review**
- [ ] Code merged to `main` via PR with the required reviewers (2 for security-critical areas).
- [ ] Follows module boundaries (import-linter passes) and coding standards (lint/format/type-check clean).
- [ ] No new High/Critical findings from SAST/SCA/secrets/IaC scans.

**Tests**
- [ ] Unit tests added/updated; coverage gates met (diff ≥ 80%; ≥ 90% for critical modules).
- [ ] Integration tests for DB/queue/AWS interactions where touched; RLS/tenant-isolation tests for new tenant tables or endpoints.
- [ ] E2E updated if a critical journey changed; all tests green in CI.
- [ ] For engines: golden datasets updated and reviewed; FinOps SME sign-off on rule changes.

**Observability**
- [ ] Structured logs with `tenant_id`/`request_id`/`job_id`; no secrets or PII in logs.
- [ ] Metrics (RED/job metrics) and traces for new endpoints/jobs; dashboards/alerts updated if a new failure mode exists.
- [ ] Product analytics events added per PRD §7 where relevant.

**Security & privacy**
- [ ] RBAC requirement declared (`x-required-role`) and tested.
- [ ] Untrusted input/metadata encoded in all outputs (UI, Slack, PDF, CSV).
- [ ] Audit log entries for state-changing actions.
- [ ] Threat model note if a trust boundary changed.

**UX & accessibility** (UI stories)
- [ ] Matches the approved design (design QA passed), including empty/loading/error states and both currency views.
- [ ] axe checks pass; keyboard navigation verified; responsive at the defined breakpoints.

**Documentation**
- [ ] OpenAPI updated (generated) and API docs reviewed; changelog entry.
- [ ] Runbook created/updated for new operational behaviour (11g).
- [ ] ADR written for significant decisions.
- [ ] User-facing help/"Learn" content updated if behaviour changed.

**Release**
- [ ] Behind a feature flag if incomplete or risky; flag registered with owner/expiry.
- [ ] Deployed to staging and verified by the author (and QA for risky changes).
- [ ] PM acceptance against the story's acceptance criteria (Given/When/Then).

### Definition of Ready (summary)
Story has acceptance criteria, designs (if UI), API contract changes identified, dependencies resolved, estimated, and test approach noted.
