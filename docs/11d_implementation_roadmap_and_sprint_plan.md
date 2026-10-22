# 11d — Implementation Roadmap & Sprint Plan

**Product:** Cloudoptics by Cloudboosta · **Phase:** 11 · **Date:** 22 October 2026
**Inputs:** PRD increments (`06_…` §2.1), components (`11b_…`), stack (`11a_…`), validation plan (`04_…` §4), beta programme (`10_…` C7)
**Cadence:** 2-week sprints. **Sprint 0** (1 week) for inception, then **S1–S6** (12 weeks) to MVP beta, then **S7–S12** beta hardening → v1 paid launch.
**Capacity decision (22 Oct 2026):** the WBS is about 12% larger than the core team's capacity, so **one delivery sprint (S7) is added before the beta**. The beta now starts in **Week 16**, and Increment 1 alpha stays at the end of S3 (§4.2).

---

## 1. Work Breakdown Structure (WBS)

Story IDs (`CC-x`, `IN-x`, …) are backlog keys; PRD references in brackets. Estimates: S ≤ 2 days, M 3–5 days, L 6–10 days (one engineer).

### Epic E-CC: Cloud Connectors
| ID | Story / task | Size | PRD |
|---|---|---|---|
| CC-1 | Spike: IAM Roles Anywhere + AWS Private CA (short-lived) from DOKS; cert issuance and renewal sidecar | M | FR-CON-05 |
| CC-2 | Broker AWS account via Terraform (trust anchor, profile, broker role, SCPs, CloudTrail alerts) | M | FR-CON-05 |
| CC-3 | CloudFormation templates: read-only role, optional CUR 2.0 export + bucket; StackSet template; template hosting + versioning | L | FR-CON-01–03 |
| CC-4 | Connection API: create (ExternalId), callback, validate (async operation), list/detail, disconnect | M | US-1.2, US-1.5 |
| CC-5 | `SessionFactory` (broker → customer AssumeRole) + credential caching per job | M | FR-CON-04 |
| CC-6 | Permission health checks (per-permission matrix) every 6h | M | FR-CON-06 |
| CC-7 | Organizations discovery + account coverage | S | US-1.3 |
| CC-8 | CUR manifest poller + streaming copy to Spaces + `billing_file` registry + completeness check | L | FR-CON-07 |
| CC-9 | Cost Explorer MTD summary (budgeted) for first value | S | US-1.4 |
| CC-10 | Inventory scanners (EC2, EBS, snapshots, EIP, ELB, RDS, Lambda, S3) multi-region | L | FR-CON-07/08 |
| CC-11 | CloudWatch metrics puller (candidate selection, batching, backfill) | M | FR-CON-07 |
| CC-12 | Native recommendations pull (COH, Compute Optimizer, SP data) | M | FR-CON-07 |
| CC-13 | Rate limiting (token buckets) + backoff + per-tenant AWS API cost budget | M | FR-CON-09/10 |
| CC-14 | Error taxonomy → connection states + admin notifications | M | FR-CON-11 |

### Epic E-IN: Ingestion & Normalisation
| ID | Story / task | Size | PRD |
|---|---|---|---|
| IN-1 | Synthetic CUR 2.0 fixture generator (multi-account, credits, SP, Local Zones, restatements) | M | — |
| IN-2 | CUR→FOCUS mapping v1 in DuckDB + golden-file tests | L | FR-ING-02 |
| IN-3 | Data-quality checks (block/warn) | M | FR-ING-09 |
| IN-4 | Aggregation to `cost_daily` (partitioned) + atomic version replace | L | FR-ING-03/08 |
| IN-5 | Cost metrics: unblended / amortised / net; credits extraction | M | FR-ING-04/05 |
| IN-6 | Reconciliation job + banner flag | M | FR-ING-07 |
| IN-7 | `CostQueryService` (summary, query spec, what-changed inputs, line items) | L | US-2.1/2.2 |
| IN-8 | FX service: providers, history, carry-forward, decomposition helpers | M | FR-FX-01–05 |
| IN-9 | Price List bulk ingestion → `price_catalog` | M | FR-REC-02 |
| IN-10 | Retention/lifecycle rules + reprocess-from-raw backfill tool | M | NFR-CMP-04 |

### Epic E-AN: Analysis Engine & Recommendations
| ID | Story / task | Size | PRD |
|---|---|---|---|
| AN-1 | Engine framework (context loader, registry, runner, fingerprinting, upsert) | L | FR-REC-01 |
| AN-2 | Scoring: savings (effective vs list), risk factors, confidence, priority, dedupe | L | FR-REC-02–06 |
| AN-3 | R1 unattached EBS + old snapshots | M | FR-ANA-01 |
| AN-4 | R2 idle EIPs / public IPv4 + idle LBs | M | FR-ANA-02 |
| AN-5 | R3 gp2→gp3 | S | FR-ANA-03 |
| AN-6 | R4 idle EC2 / RDS | M | FR-ANA-04 |
| AN-7 | R5 EC2 rightsizing (CO merge + own checks + projection) | L | FR-ANA-05 |
| AN-8 | R6 previous-gen / Graviton mapping | M | FR-ANA-06 |
| AN-9 | R7 non-prod scheduling + scheduler template | M | FR-ANA-07 |
| AN-10 | R8 Savings Plan advisory (payback, FX exposure) | M | FR-ANA-10 |
| AN-11 | Anomaly detection + suppression | M | FR-ANA-08 |
| AN-12 | Forecast + FX scenario | M | FR-ANA-09, US-3.2 |
| AN-13 | What-changed explanations | M | FR-ANA-12 |
| AN-14 | Credits runway | S | FR-ANA-11 |
| AN-15 | Fix snippet templates (console/CLI/Terraform) + CI validation | L | US-4.3, FR-REC-10 |
| AN-16 | Lifecycle state machine + events + verification service + realised savings | L | US-5.1/5.2 |
| AN-17 | Tenant rule settings (type catalogue, thresholds, exclusion grammar, patterns, ignore tag) | M | FR-REC-08 |
| AN-18 | R6b Extended Support charges (CUR-only) | S | FR-ANA-06b |
| AN-19 | R10 idle NAT gateways (CUR-only) | S | FR-ANA-14 |

### Epic E-API: API Layer
| ID | Story / task | Size |
|---|---|---|
| API-1 | FastAPI skeleton: routers, middleware chain, problem+json, request IDs, OTel | M |
| API-2 | Session auth with AuthKit + CSRF + tenant resolution + RBAC dependencies | L |
| API-3 | OpenAPI publishing + TS client generation + breaking-change CI check | S |
| API-4 | Costs endpoints (summary, query, what-changed, forecast, credits, dimensions) | L |
| API-5 | Recommendations endpoints (list, summary, detail, patch, bulk, comments, events, commitments) | L |
| API-6 | Budgets & alert-settings endpoints | M |
| API-7 | Reports & exports endpoints + async operations | M |
| API-8 | Rate limiting, ETags, query cache with data-version keys | M |

### Epic E-FE: Frontend MVP
| ID | Story / task | Size |
|---|---|---|
| FE-1 | App scaffold (Vite, router, Query, Tailwind tokens from the "Cloudoptic" design system, theming, i18n) | M |
| FE-2 | Design-system components: MoneyValue (USD/local/both), DeltaChip, KpiTile, Badges, DataTable, FilterBar, Chart wrappers (ECharts), empty/loading/error states | L |
| FE-3 | App shell: nav, header (workspace, accounts, dates, currency toggle, data-as-of), banners | M |
| FE-4 | Onboarding wizard (7 steps, validation polling, colleague flow) | L |
| FE-5 | Overview page | L |
| FE-6 | Costs Explorer + Anomalies + Credits | L |
| FE-7 | Recommendations list + savings tracker + bulk actions | L |
| FE-8 | Recommendation detail (evidence charts, risk panel, guided fix tabs, workflow) + Commitments page | L |
| FE-9 | Budgets & alert settings | M |
| FE-10 | Reports (monthly view, forecast & FX, exports) + print route for PDF | M |
| FE-11 | Settings (connections, users, notifications/Slack, preferences, security/audit, workspace) | L |
| FE-12 | Accessibility pass (WCAG 2.2 AA), performance budgets, mobile layouts | M |

### Epic E-ADM: Auth, Multi-Tenancy, Admin
| ID | Story / task | Size |
|---|---|---|
| ADM-1 | Spike + integrate WorkOS AuthKit (email/password, Google, TOTP) vs Keycloak decision | M |
| ADM-2 | Tenancy schema + RLS policies + migration lint + cross-tenant test suite | L |
| ADM-3 | Tenants, memberships, invites, roles | M |
| ADM-4 | Audit log (hash-chained) + audit UI API + export | M |
| ADM-5 | Tenant settings model + validation + history | S |
| ADM-6 | Staff break-glass tool (internal) | M |
| ADM-7 | Data deletion flows (connection, tenant) with verification | M |
| ADM-8 | Notifications service: email (Postmark), Slack app (OAuth, channels, DMs, interactivity), de-dup, preferences, digests | L |
| ADM-9 | Monthly report generation (renderer worker, PDF, email) | M |
| ADM-10 | **Platform admin console: exchange rates.** Cloudoptics staff set the rate per currency by hand, with an effective date and reason. Includes rate history, a reference feed shown alongside, a staleness alert and audit (PRD FR-FX-06/07) | M |

### Epic E-OPS: Observability & DevOps
| ID | Story / task | Size |
|---|---|---|
| OPS-1 | Monorepo, CI pipeline (lint, type-check, tests, scans, SBOM), branch protection | M |
| OPS-2 | Terraform: DO (VPC, DOKS, Postgres, Valkey, Spaces, registry, LB), Cloudflare, AWS broker; remote state | L |
| OPS-3 | Helm charts + Argo CD; staging + prod; preview environments | L |
| OPS-4 | Secrets: AWS Secrets Manager + External Secrets Operator; KMS envelope encryption library | M |
| OPS-5 | Static egress IPs (NAT gateway or egress proxy) — verify in Sprint 0 | M |
| OPS-6 | Observability: OTel collector, Grafana Cloud dashboards, Sentry, synthetic checks from Africa | M |
| OPS-7 | Alerting rules + on-call rota (Grafana IRM) | S |
| OPS-8 | Backups, restore test automation, DR runbook draft | M |
| OPS-9 | Image signing (cosign) + Kyverno policies | S |
| OPS-10 | KEDA autoscaling on queue depth; resource limits; cost budgets for our infra | M |
| OPS-11 | Procrastinate setup (queues, periodic tasks, DLQ dashboard, replay CLI) | M |

### Epic E-QA: Quality & Release (cross-cutting; see 11f)
Test strategy setup, E2E suite (Playwright) for critical journeys, load tests (k6) for API and ingestion, security testing (SAST/DAST, pen test booking), security-audit skill run before beta.

### Epic E-UX: Design (cross-cutting)
UI designs on the design canvas using the **"Cloudoptic"** design system (after Phase 11), usability rounds 1–2 (Phase 7), design QA per sprint.

---

## 2. Sprint-by-Sprint Plan (MVP)

**Timeline anchor:** Sprint 0 starts Week 1. MVP Increment 1 alpha at the end of S3 (Week 7); Increment 2 beta at the end of S7 (Week 15), with design partners starting in Week 16. Business validation (Phase 4 V1–V8) and UX research (Phase 7) run in parallel.

### Sprint 0 (1 week) — Inception & foundations
- **Goals:** de-risk the two riskiest technical bets; set up the repo, CI and environments skeleton; finalise ADRs.
- **Status (22 Oct 2026):** done in the repo: monorepo + CI skeleton (OPS-1, branch protection still to be set by an admin), synthetic CUR fixture v0 (IN-1), ADR-001…011 drafted (`docs/adr/`). Pending, need real accounts: CC-1, ADM-1, OPS-5 and the fixture check against a real export (`docs/spikes/sprint-0.md`).
- **Deliverables:**
  - ADR-001…011 drafted.
  - Monorepo + CI skeleton (OPS-1).
  - Spike results: Roles Anywhere + Private CA from DOKS (CC-1), AuthKit vs Keycloak (ADM-1), static egress in LON1 (OPS-5).
  - Synthetic CUR fixture v0 (IN-1).
  - Backlog refined for S1–S3.
  - Design: wireframe → hi-fi plan with the "Cloudoptic" design system.
- **Dependencies:** DO and AWS accounts; Cloudflare; GitHub org; test AWS accounts with real CUR exports (incl. `af-south-1` resources).
- **Risks & mitigations:**
  - Roles Anywhere blocked → fallback to an IAM user with rotated keys (ADR-007 alternative).
  - No static egress option → egress proxy droplets.

### Sprint 1 (Weeks 2–3) — Platform & connection skeleton
- **Status (22 Oct 2026):** built and tested: migrations with row-level security, auth (OIDC + dev), workspaces, members, audit chain (API-1/2, ADM-2/3); CloudFormation v1, connection API, validation with specific errors, Procrastinate job (CC-3/4/6, OPS-11); web shell, tokens and onboarding steps 1–4 (FE-1/3/4). Hosting follows ADR-012 (GitHub first: Pages demo, GHCR image, Codespaces); Terraform/Helm/Argo CD to DigitalOcean (OPS-2/3/4) and the broker account (CC-2) wait for the Sprint 0 spikes.
- **Goals:** a deployable platform; users can sign up and create a workspace; the AWS role can be deployed and validated.
- **Deliverables:**
  - Terraform for staging (OPS-2), Helm + Argo CD to staging (OPS-3), secrets (OPS-4).
  - FastAPI skeleton (API-1), auth + tenancy + RLS (API-2, ADM-2, ADM-3).
  - CloudFormation templates v1 (CC-3), broker account (CC-2), SessionFactory (CC-5), connection API + validation (CC-4, CC-6).
  - Frontend scaffold + design tokens (FE-1), app shell (FE-3), onboarding steps 1–4 (FE-4 partial).
  - Procrastinate setup (OPS-11).
- **Dependencies:** S0 spike outcomes; design tokens from the "Cloudoptic" design system.
- **Risks & mitigations:**
  - RLS + async SQLAlchemy friction → a tenant-session helper plus tests early.
  - CFN template review by security → involve the tech lead in review.

### Sprint 2 (Weeks 4–5) — Billing ingestion end-to-end
- **Goals:** CUR data flows from customer S3 to `cost_daily`; first numbers in the UI.
- **Deliverables:**
  - CUR poller + landing (CC-8); FOCUS mapping v1 (IN-2); DQ checks (IN-3); aggregation (IN-4); cost metrics + credits (IN-5).
  - FX service (IN-8).
  - `CostQueryService` (IN-7) + costs endpoints (API-4 partial).
  - CE MTD summary for first value (CC-9).
  - Organizations discovery (CC-7).
  - FE: MoneyValue/KPI/Chart/DataTable components (FE-2); Overview v0 (KPIs + trend) (FE-5 partial).
- **Dependencies:** test AWS accounts with ≥ 1 month of CUR data; FX provider contract/API key (ADR-010 decided).
- **Risks & mitigations:**
  - CUR edge cases (SP amortisation, credits, Local Zones) → golden fixtures from real test accounts + the FinOps SME reviews mappings.
  - Ingestion performance → profile with a 5M-row synthetic dataset.

### Sprint 3 (Weeks 6–7) — "See it" + quick wins → **Increment 1 alpha**
- **Goals:** a complete cost-visibility experience plus the first low-risk recommendations; alpha with 3–5 design partners.
- **Deliverables:**
  - Inventory scanners (CC-10); native recs pull (CC-12); price catalogue (IN-9).
  - Engine framework + scoring (AN-1, AN-2); R1–R3 (AN-3–AN-5) with fix templates (AN-15 partial).
  - Reconciliation (IN-6).
  - Costs Explorer (FE-6 partial: explorer); recommendations list v0 + detail v0 (FE-7/FE-8 partial).
  - Onboarding complete (FE-4).
  - Audit log (ADM-4); email notifications for connection failures (ADM-8 partial; CC-14).
  - Production environment + observability baseline (OPS-2/3 prod, OPS-6).
  - Security: SAST/SCA gates; first **security-audit skill** run (focused: tenancy, connector IAM).
- **Release:** **Increment 1 alpha** to 3–5 design partners (production, feature-flagged).
- **Dependencies:** design-partner agreements signed (DPA, read-only access consent); usability round 1 findings (Phase 7 M6).
- **Risks & mitigations:**
  - Partner onboarding friction → the solutions engineer runs guided sessions.
  - Wrong numbers → reconciliation + an internal "data accuracy" review per partner before sharing.

### Sprint 4 (Weeks 8–9) — "Cut it safely" core
- **Goals:** usage-based recommendations with risk scoring, evidence and workflow.
- **Deliverables:**
  - Metrics puller (CC-11); rate limiting + API cost budget (CC-13).
  - R4 idle EC2/RDS, R5 rightsizing, R6 previous-gen (AN-6–AN-8); full fix templates (AN-15).
  - Lifecycle + events + assignment (AN-16 partial); tenant rule settings (AN-17).
  - Recommendations endpoints complete (API-5).
  - FE: recommendation detail with evidence charts and risk panel (FE-8), list filters + bulk (FE-7).
  - Slack app (ADM-8 partial).
- **Dependencies:** CloudWatch history in test accounts (14–30 days); FinOps SME review of thresholds.
- **Risks & mitigations:**
  - Rightsizing accuracy → conservative thresholds, engine feature flags, a golden dataset.
  - CloudWatch cost → candidate-only pulls; budget enforcement.

### Sprint 5 (Weeks 10–11) — Plan it: budgets, anomalies, FX, credits
- **Goals:** finance persona value and proactive alerts.
- **Deliverables:**
  - R7 scheduling, R8 SP advisory (AN-9, AN-10); R6b Extended Support and R10 idle NAT (AN-18, AN-19).
  - Anomaly detection (AN-11); forecast + FX scenario (AN-12); what-changed (AN-13); credits runway (AN-14).
  - Budgets & alerts (API-6, FE-9); notification preferences + de-dup + weekly digest (ADM-8).
  - Overview complete (FE-5); Commitments page (FE-8); Anomalies + Credits views (FE-6).
  - Verification service + realised savings (AN-16).
- **Dependencies:** FX rate basis decision implemented; Slack app approved for distribution (or unlisted install).
- **Risks & mitigations:**
  - Alert noise → minimum thresholds + design-partner tuning.
  - FX data gaps for some currencies → secondary provider + carry-forward flag.

### Sprint 6 (Weeks 12–13) — Reports, admin, hardening → **release candidate**
- **Goals:** complete the MVP; harden quality, security and operations; start the full beta.
- **Deliverables:**
  - Monthly report PDF + email (ADM-9, FE-10); exports (API-7).
  - Settings complete (FE-11); data deletion flows (ADM-7); break-glass tool (ADM-6).
  - Accessibility + performance pass (FE-12).
  - Rate limiting + caching (API-8).
  - Backups/restore test (OPS-8), alerting + on-call (OPS-7), KEDA (OPS-10), image signing (OPS-9).
  - Load tests; E2E suite green.
  - **Full security-audit skill run** + fixes for all Critical/High findings.
  - Runbooks (11g) and the release checklist (PRD §8).
- **Release:** a release candidate to staging and to the alpha partners. The full beta follows S7.
- **Dependencies:** usability round 2; legal: ToS, privacy policy, DPA with NDPA/Kenya/POPIA clauses; security pack.
- **Risks & mitigations:**
  - Scope creep → strict MVP scope (Phase 5 §A5). S7 absorbs carry-over only; new requests go to S8+.
  - Last-minute critical security findings → S7 buffer.

### Sprint 7 (Weeks 14–15) — Buffer, admin rates console, hardening → **Increment 2 beta**
- **Goals:** absorb the ~12% scope overrun; finish the platform admin console for exchange rates; close beta blockers.
- **Deliverables:**
  - Carry-over stories from S1–S6, in priority order.
  - Platform admin console: manual exchange rates (ADM-10).
  - Fixes from usability round 2.
  - A second full security-audit skill run; all Critical/High findings fixed.
  - Load and soak tests at 3× beta load.
  - Operational readiness checklist (11g §3.3) signed off.
- **Release:** **Increment 2 beta** to all 8–12 design partners at the end of Week 15 (beta go/no-go).
- **Dependencies:** legal documents final; first official naira rate entered in the admin console.
- **Risks & mitigations:** if carry-over exceeds S7 capacity, apply the deferral list in §4.2 option 2 rather than moving the beta date.

### Sprints 8–13 (Weeks 16–27) — Beta operations → v1 paid launch (outline)

| Sprint | Focus |
|---|---|
| S8 | Beta support, bug fixes, threshold tuning from outcome data; self-serve billing (Paystack + Stripe) start; Jira integration start |
| S9 | **Gate G-C prep**; Placement analysis v0 (CBN 2027 use case); Teams alerts; pricing/entitlements |
| S10 | Opt-in write role design + Temporal evaluation (ADR); advanced allocation (virtual tags UI); SAML SSO |
| S11 | Approved one-click actions (non-prod schedules, delete unattached volumes) behind flags; AI assistant spike (with Phase 10 T15 controls) |
| S12 | Partner console (beta); external **pen test**; SOC 2 readiness review |
| S13 | **v1 paid launch (Nigeria)**; marketing site; onboarding automation; case studies |

---

## 3. MVP Path Check (acceptance at end of S7)

| Required MVP capability | Delivered in |
|---|---|
| Connects to at least one cloud provider (AWS, read-only, Organizations) | S1–S3 |
| Ingests cost/usage data (CUR 2.0 → FOCUS → aggregates, reconciled) | S2–S3 |
| Runs basic analysis (R1–R8, anomalies, forecast, what-changed) | S3–S5 |
| Simple dashboard with recommendations (Overview, Explorer, Recommendations, dual currency) | S2–S6 |

---

## 4. Resourcing & Role Mapping

### 4.1 Required roles (core team ≈ 8 FTE)

| Role | FTE | Primary epics |
|---|---|---|
| Tech Lead / Staff Engineer (backend + architecture + security owner) | 1.0 | API, ADM, CC (IAM), reviews |
| Backend Engineer — connectors & ingestion | 1.0 | CC, IN |
| Data/Backend Engineer — analysis & recommendations | 1.0 | AN, IN (FX, prices) |
| Frontend Engineer (senior) | 1.0 | FE |
| Full-stack Engineer | 1.0 | FE + API + notifications/reporting |
| DevOps / Platform Engineer | 1.0 (S0–S3), 0.6 (S4–S6) | OPS, security tooling |
| QA / SDET | 0.5 (S1–S2), 1.0 (S3–S6) | Test strategy, E2E, load tests |
| Product Designer | 1.0 (S0–S3), 0.6 (S4–S6) | E-UX, design QA |
| Product Manager | 1.0 | Backlog, partners, research ops, decisions |
| FinOps / AWS SME (Cloudboosta) | 0.5 | Recommendation rules, thresholds, fix templates, concierge assessments |
| UX Researcher (can be the designer + PM) | 0.5 (S0–S4) | Phase 7 plan |

### 4.2 Effort per role per sprint (person-days; 10 working days per 2-week sprint; S0 = 5 days)

| Role | S0 | S1 | S2 | S3 | S4 | S5 | S6 | Total |
|---|---|---|---|---|---|---|---|---|
| Tech Lead | 5 | 10 | 10 | 10 | 10 | 10 | 10 | 65 |
| Backend (connectors/ingestion) | 5 | 10 | 10 | 10 | 10 | 8 | 8 | 61 |
| Data/Backend (analysis) | 3 | 6 | 10 | 10 | 10 | 10 | 8 | 57 |
| Frontend (senior) | 3 | 10 | 10 | 10 | 10 | 10 | 10 | 63 |
| Full-stack | 2 | 8 | 10 | 10 | 10 | 10 | 10 | 60 |
| DevOps/Platform | 5 | 10 | 10 | 10 | 6 | 6 | 8 | 55 |
| QA/SDET | 0 | 5 | 5 | 10 | 10 | 10 | 10 | 50 |
| Product Designer | 5 | 10 | 10 | 8 | 6 | 6 | 6 | 51 |
| PM | 5 | 10 | 10 | 10 | 10 | 10 | 10 | 65 |
| FinOps SME | 2 | 3 | 5 | 6 | 6 | 5 | 3 | 30 |

**Capacity vs scope (honest check):**
- Engineering capacity (Tech Lead, two backend, frontend, full-stack, DevOps) is **≈ 360 person-days** across S0–S6.
- The WBS adds up to **≈ 408 person-days**, using S = 2, M = 4, L = 8 days: CC 64 · IN 52 · AN 84 · API 42 · FE 76 · ADM 42 · OPS 48. QA/SDET effort (E2E, load tests) is extra and not counted here.
- So the plan is **about 12% over capacity with no buffer**. This is typical for first estimates, but must be resolved before S1.

**Decision (22 Oct 2026): option 1, add one delivery sprint.** The other options are kept for reference.
1. **Add one delivery sprint (S7, Weeks 14–15) before the beta.** ✅ *Chosen.* This gives ≈ 60 extra person-days, which covers the gap (now ≈ 416 person-days with ADM-10, AN-18 and AN-19; the threshold refinements from doc 12 add R1b and RDS gp3 to AN-3/AN-5 and are absorbed by the S7 buffer) plus a small buffer. Beta starts in Week 16. Increment 1 alpha stays at the end of S3.
2. **Fallback, if S7 is not enough: keep 6 sprints and move ≈ 50 person-days to S7–S8 (after the beta starts).** Candidates:
   - R6 previous-gen/Graviton (4)
   - R7 non-prod scheduling (4)
   - Staff break-glass tool (4; use a manual DB procedure with audit in the meantime)
   - Preview environments (part of OPS-3, ~4)
   - Monthly PDF rendering (4; send an HTML email report first)
   - Tenant rule settings UI (4; defaults only)
   - Credits runway (2)
   - Bulk actions (part of FE-7, ~4)
   - KEDA autoscaling (4; fixed replicas first)
   - Image signing/Kyverno (2; before paid launch)
   - Partial what-changed (4)
   - Simplified Settings › Preferences (≈ 8)
3. **Smaller team (e.g., 4 engineers):** plan **S1–S9** (18 weeks), apply option 2's deferrals, and use DO App Platform instead of DOKS.

Re-estimate with planning poker in Sprint 0 and track velocity from S1. Adjust the beta date at the end of S2, once real velocity is known.

### 4.3 Hiring/sourcing notes
- Hire or contract in African tech hubs (Lagos, Nairobi, Cape Town) for time-zone alignment with customers, plus Cloudboosta alumni.
- The DevOps and FinOps SME roles can draw on Cloudboosta's trainers/consultants.
- Plan a **security advisor** (≈ 2 days/month) and **legal counsel** (NDPA, Kenya DPA, POPIA, ToS) as external support.

### 4.4 Ceremonies & governance
- Sprint planning, a mid-sprint design/tech review, sprint review with design partners (from S3), and a retrospective.
- Weekly risk review (11e); architecture/ADR review fortnightly.
- Increment gates: alpha go/no-go (end of S3), beta go/no-go (end of S6) against PRD §8 release criteria.
