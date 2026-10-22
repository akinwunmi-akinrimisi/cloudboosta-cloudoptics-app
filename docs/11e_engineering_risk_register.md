# 11e — Engineering Risk Register

**Product:** Cloudoptics by Cloudboosta · **Phase:** 11 · **Date:** 22 October 2026
**Inputs:** `09_…`, `10_…` (threat model T1–T16), `11a_…`–`11d_…`
**Review cadence:** weekly in the engineering risk review; full re-score at each increment gate. **Owner** = accountable role.

**Scoring:** Likelihood (L) and Impact (I) are rated Low / Medium / High. **Priority** = High if either is High and the other at least Medium; otherwise Medium/Low.

---

## 1. Risk Register

### 1.1 Connector reliability & API limits

| ID | Risk | L | I | Priority | Mitigation strategies | Owner |
|---|---|---|---|---|---|---|
| R-01 | **AWS API throttling** (Describe*, CloudWatch, Cost Explorer) for customers with many accounts/regions delays data or fails jobs | M | M | Medium | Token buckets per tenant/service; botocore adaptive retries + jittered backoff; jittered schedules; regional concurrency caps; incremental scans; monitoring of throttle rates | Backend Engineer (connectors) |
| R-02 | **Customer-side AWS charges** from our access (CE $0.01/call, CloudWatch metrics, S3 egress) annoy customers or exceed expectations | M | M | Medium | Per-tenant monthly budget (≤ USD 5) with circuit breaker; candidate-only metrics; prefer CUR and free APIs; "access cost" shown in Settings; measured in Increment 1 | Backend Engineer (connectors) |
| R-03 | **IAM Roles Anywhere / Private CA** integration from DOKS harder than expected, or the CA/broker becomes a single point of failure | M | H | **High** | Sprint 0 spike; fallback (IAM user with rotated keys in Secrets Manager); cert renewal monitoring; alert on `CreateSession` failures; runbook; 12h freshness buffer | Tech Lead |
| R-04 | **CUR 2.0 / FOCUS format changes** or customer-specific export settings (e.g., hourly granularity, resource IDs off) break ingestion | M | M | Medium | Schema validation with clear errors; versioned mapping; detection of export settings at validation (warn if resource IDs disabled); support CUR 2.0 and FOCUS export paths; monthly review of AWS release notes | Data/Backend Engineer |
| R-05 | **Customer misconfiguration** (stack in the wrong account, no Organizations access, bucket policy changes) slows onboarding | H | M | **High** | One-click CFN with validation callback; per-permission diagnostics; "send to colleague" flow; guided fix content; solutions-engineer onboarding for design partners; funnel analytics | PM + Full-stack Engineer |
| R-06 | **Static egress IP** not available in DO LON1 as needed for customers using `aws:SourceIp` | M | L | Low | Verify in Sprint 0; egress proxy with reserved IPs; make source-IP pinning optional | DevOps Engineer |

### 1.2 Data accuracy & trustworthiness

| ID | Risk | L | I | Priority | Mitigation strategies | Owner |
|---|---|---|---|---|---|---|
| R-07 | **Cost totals don't match the AWS invoice** (amortisation, credits, refunds, taxes, restatements) → trust lost | M | H | **High** | Reconciliation (±1%) per period with banner; golden fixtures from real accounts; FinOps SME review of mappings; atomic version replacement; month-end restatement handling; per-partner accuracy review before alpha | Data/Backend Engineer |
| R-08 | **Inaccurate or unsafe recommendations** (e.g., rightsizing a memory-bound server, deleting a needed volume) cause incidents or credibility loss | M | H | **High** | Conservative thresholds; explainable risk factors; confidence capped without memory metrics; snapshot pre-steps; per-engine feature flags and kill switch; golden-dataset regression tests; outcome monitoring (dismissal "wrong/unsafe" rate ≤ 15%); ToS disclaimers | Data/Backend Engineer + FinOps SME |
| R-09 | **Savings estimates overstated** (list price vs discounted effective rates; double counting overlapping recommendations) | M | M | Medium | Effective-rate pricing from CUR; dedupe by resource; "estimated" labels; verification shows realised vs estimated; track realised ÷ estimated per engine | Data/Backend Engineer |
| R-10 | **FX data wrong, missing or contested** (official vs market rates, especially NGN) | M | M | Medium | **Rates are set by hand in the platform admin console (ADR-010).** Validation bounds (warn if more than 5% from the reference feed or the previous rate); second-person check for changes over 10%; staleness alert if a rate is more than 3 working days old; full history and audit; rate and date shown in UI and reports | Data/Backend Engineer + PM (rate owner) |
| R-11 | **Price catalogue gaps** for `af-south-1` / Local Zones / new instance types | M | M | Medium | Weekly Price List refresh; fall back to effective rates; alert on unknown SKUs; unit tests for African regions | Data/Backend Engineer |
| R-12 | **Verification false positives/negatives** (changes not detected; savings credited wrongly) | M | M | Medium | Combine resource state and cost signals; ≥ 7 days post-change data; "not confirmed" state; manual override with audit | Data/Backend Engineer |

### 1.3 Performance at scale

| ID | Risk | L | I | Priority | Mitigation strategies | Owner |
|---|---|---|---|---|---|---|
| R-13 | **Large tenants** (tens of millions of CUR rows/month, 100+ accounts) exceed DuckDB memory or job timeouts | M | M | Medium | Streaming + spill; per-file processing; large-job node pool; load tests with 5M–50M row fixtures; ClickHouse scale trigger defined | Data/Backend Engineer |
| R-14 | **Dashboard query latency** exceeds p95 2s as `cost_daily` grows | M | M | Medium | Partitioning + indexes; summary tables; query cache keyed by data version; query timeouts; scale trigger to ClickHouse behind `CostQueryService` | Tech Lead |
| R-15 | **Postgres contention** from job queue + API + aggregation writes | M | M | Medium | Dedicated `jobs` schema, connection pooling, tuned autovacuum, batch upserts; monitoring; split the job DB at v1 if needed | DevOps Engineer |
| R-16 | **Slow experience on African mobile networks** | M | M | Medium | Performance budgets in CI; Cloudflare edge; lean payloads; synthetic checks from Lagos/Nairobi/Johannesburg | Frontend Engineer |

### 1.4 Security vulnerabilities

| ID | Risk | L | I | Priority | Mitigation strategies | Owner |
|---|---|---|---|---|---|---|
| R-17 | **Cross-tenant data exposure** (missing tenant filter, cache-key or export bug) | L | H | **High** | RLS everywhere + migration lint; tenant-scoped query builder and cache keys; cross-tenant test suite; security-audit skill (DATA-ISOLATION classes) before alpha and beta; pen test before v1 | Tech Lead |
| R-18 | **Confused deputy / compromise of the access path** into customer AWS accounts | L | H | **High** | Unique ExternalIds; one AWS account per workspace; broker role scoped to `sts:AssumeRole` on the role-name pattern; Private CA short-lived certs; CloudTrail alerts; incident runbook to revoke the trust anchor | Tech Lead |
| R-19 | **Injection via customer-controlled metadata** (tag values, names) into UI, Slack, PDF, CSV | H | M | **High** | Output encoding everywhere; strict CSP; Slack/PDF sanitisation; CSV formula guard; tests with malicious fixtures | Frontend Engineer + Full-stack Engineer |
| R-20 | **Auth/session weaknesses** (CSRF, session fixation, missing MFA enforcement) | L | H | Medium | Managed auth provider; CSRF tokens; secure cookies; session rotation; MFA enforcement tests; pen test | Tech Lead |
| R-21 | **Secrets leakage** (logs, CI, images) | L | H | Medium | Secrets Manager + ESO; log redaction; gitleaks + push protection; no secrets in env dumps; rotation runbooks | DevOps Engineer |
| R-22 | **Supply-chain compromise** (dependency, GitHub Action, base image) | M | H | **High** | Lockfiles + Renovate; SCA; Actions pinned by SHA; OIDC deploys; cosign + Kyverno; SBOMs; minimal base images | DevOps Engineer |

### 1.5 Complexity of analysis logic & maintainability

| ID | Risk | L | I | Priority | Mitigation strategies | Owner |
|---|---|---|---|---|---|---|
| R-23 | **Engine logic sprawl:** rules, thresholds and exceptions become hard to reason about and test | M | M | Medium | Pure-function engines with typed contexts; one module per engine; versioning; golden datasets; documentation per engine (inputs, rules, risk factors); code owners | Data/Backend Engineer |
| R-24 | **Fix templates drift** from AWS CLI/Terraform provider changes | M | M | Medium | Templates versioned and validated in CI (`terraform validate`, CLI linting); quarterly execution tests in a test AWS account (incl. `af-south-1`) | FinOps SME + Data/Backend Engineer |
| R-25 | **Modular monolith boundaries erode** (cross-module table access) | M | M | Medium | Import-linter rules in CI; module interfaces; ADR reviews | Tech Lead |
| R-26 | **Scope creep** from design-partner requests jeopardises MVP dates | H | M | **High** | Strict MVP scope (Phase 5 §A5); a PM-owned change process; "later" backlog with partner visibility; increment gates | PM |

### 1.6 Team bandwidth & skill gaps

| ID | Risk | L | I | Priority | Mitigation strategies | Owner |
|---|---|---|---|---|---|---|
| R-27 | **Plan is ~12% over capacity** (11d §4.2) | M | M | Medium | **Decided 22 Oct: S7 added, beta moves to Week 16.** Re-estimate in Sprint 0; track velocity; if S7 overflows, apply the deferral list instead of moving the date again | Engineering Manager / Tech Lead |
| R-28 | **Key-person dependency** (Tech Lead on IAM/security; analysis engineer on engines) | M | H | **High** | Pairing; ADRs and design docs; runbooks; code ownership spread across ≥ 2 people per critical area | Engineering Manager |
| R-29 | **Skill gaps** in Kubernetes/GitOps, AWS billing domain or accessibility | M | M | Medium | App Platform fallback; Cloudboosta training; FinOps SME pairing; design-system components built early with accessibility checks | Engineering Manager |
| R-30 | **Hiring delays** in target hubs | M | M | Medium | Start with contractors and Cloudboosta alumni; staggered start (DevOps + Tech Lead first) | Engineering Manager |

### 1.7 Third-party dependencies

| ID | Risk | L | I | Priority | Mitigation strategies | Owner |
|---|---|---|---|---|---|---|
| R-31 | **DigitalOcean service limitations** (no African region; managed-service features; regional outage) | M | M | Medium | LON1 + AMS3 DR; GitOps rebuild; documented residency position; tenant-placement design for future African hosting | DevOps Engineer |
| R-32 | **Auth provider** outage, pricing change or lock-in | L | M | Low | Own user/tenant tables; store only `auth_subject`; session continuity during outages; documented exit to Keycloak | Tech Lead |
| R-33 | **Procrastinate** (smaller project) has bugs or stalls | L | M | Low | Thin internal `jobs` abstraction; integration tests; fallback plan to Celery/Dramatiq; Temporal evaluation at v2 | Tech Lead |
| R-34 | **FX provider** coverage/licensing for NGN/KES/EGP/GHS | M | M | Medium | Two providers; licence review (display rights); central-bank feeds | PM |
| R-35 | **Slack / Postmark** rate limits or delivery issues | L | L | Low | Rate-limit handling; retries; in-app fallback | Full-stack Engineer |
| R-36 | **AWS SDK / API deprecations** | L | M | Low | Monthly SDK upgrades; contract tests; AWS release-note watch | Backend Engineer (connectors) |
| R-37 | **Regulatory change** (e.g., CBN localisation scope, Kenya ODPC final guidance) requiring data-location changes | M | M | Medium | Legal monitoring; tenant-placement architecture; data minimisation; security pack updates | PM + Tech Lead |

---

## 2. Top Risks (heat summary)

| Rank | Risk | Why it matters now |
|---|---|---|
| 1 | R-08 Unsafe recommendations | Trust is the core differentiator (Pillar 1); one incident could end design-partner trust |
| 2 | R-07 Cost accuracy | Every screen depends on it; finance personas will check invoices |
| 3 | R-27 Capacity vs scope | Mitigated by the added S7; watch velocity at the end of S2 |
| 4 | R-03 Roles Anywhere / broker | Foundation for all data access; must be proven in Sprint 0 |
| 5 | R-17/R-18 Tenant isolation & access path | Catastrophic impact for a security-sensitive product |
| 6 | R-05 Onboarding misconfiguration | Activation metric O1 depends on it |
| 7 | R-19 Metadata injection | High likelihood; easy to prevent if designed in |

## 3. Risk Process
- New risks are logged by anyone; triaged weekly by the Tech Lead and PM.
- Each High risk has a named mitigation task in the current or next sprint.
- Accepted risks are recorded with a rationale and review date.
- Risks feed the release go/no-go checklist at each increment gate.
