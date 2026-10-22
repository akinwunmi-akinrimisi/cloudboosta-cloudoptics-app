# 11a — Technology Stack & Platform Choices

**Product:** Cloudoptics by Cloudboosta
**Phase:** 11 — Detailed Engineering Design & Implementation Planning
**Role:** Staff Software Engineer / Engineering Manager
**Date:** 22 October 2026
**Inputs (approved):** `05_…`, `06_mvp_prd.md`, `07_…`, `08_…`, `09_architecture_data_and_api_design.md`, `10_security_devops_and_gtm_foundations.md`
**Status:** Draft for engineering leadership sign-off

> Version numbers are "minimum major line" targets. Pin exact versions in lockfiles at project start and track them with Renovate. Everything below resolves the open items from Phase 9 Appendix A (O1–O4).

---

## 1. Summary

| Layer | Choice |
|---|---|
| Backend language/framework | **Python 3.12+ · FastAPI · Pydantic v2 · SQLAlchemy 2.x (Core + ORM) · Alembic** |
| Background jobs | **Procrastinate** (PostgreSQL-backed task queue) for MVP–v1; reassess **Temporal** at v2 |
| Analytical engine | **DuckDB 1.x** (embedded) over Parquet on DO Spaces; **ClickHouse** at the Phase 9 scale trigger |
| Frontend | **React 19 + TypeScript 5 · Vite · TanStack Router · TanStack Query · Zustand · Tailwind CSS 4 · Radix UI (shadcn/ui pattern) · TanStack Table · Apache ECharts** |
| Primary database | **PostgreSQL 16+** (DO Managed, HA) |
| Object storage | **DO Spaces** (S3 API) |
| Cache | **Valkey 8** (DO Managed) |
| Cloud SDKs | **boto3/botocore** (AWS); later `azure-mgmt-costmanagement` / `azure-identity`, `google-cloud-bigquery` |
| Hosting | **DigitalOcean**: DOKS (Kubernetes), LON1 primary, AMS3 secondary |
| Containers & deploy | **Docker · Helm · Argo CD (GitOps)** |
| IaC | **Terraform 1.x** (DO, AWS, Cloudflare providers); OpenTofu-compatible |
| CI | **GitHub Actions** |
| Observability | **OpenTelemetry → Grafana Cloud (Loki, Prometheus/Mimir, Tempo)** + **Sentry** |
| Auth | **Managed OIDC provider: WorkOS AuthKit** (alternative: Keycloak self-hosted) |
| AWS identity from DO | **IAM Roles Anywhere** with **AWS Private CA (short-lived certificate mode)** |
| Secrets & keys | **AWS Secrets Manager + AWS KMS** (broker account, `eu-west-2`) via **External Secrets Operator** |
| Edge | **Cloudflare** (CDN, WAF, DNS) |
| Email | **Postmark** (transactional) |
| Product analytics & flags | **PostHog (EU cloud)** |
| PDF rendering | **Playwright (headless Chromium)** rendering a print route of the web app |
| FX data | Commercial FX API (e.g., Open Exchange Rates) + central-bank feeds for the "official" basis |
| Payments (v1) | **Paystack** (NGN, KES, GHS, ZAR…) + **Stripe** (USD/GBP/EUR) |

---

## 2. Backend

### 2.1 Language & framework: Python + FastAPI

| Aspect | Detail |
|---|---|
| **Recommendation** | Python 3.12+ (3.13 when all dependencies support it), FastAPI, Pydantic v2 for validation and settings, Uvicorn/Gunicorn workers, SQLAlchemy 2.x (async with psycopg 3), Alembic migrations |
| **Rationale: data-heavy analytics** | Python leads for billing-data work: DuckDB, PyArrow/Parquet, pandas/Polars for one-off analysis, statistics libraries for anomaly detection and forecasting, and later scikit-learn/LightGBM for the outcome-trained risk model (Moat M2). The same language serves ingestion, analysis and API, so there are no cross-language contracts |
| **Rationale: AWS** | boto3 is AWS's most complete and best-documented SDK; Cost Explorer, Cost Optimization Hub, Compute Optimizer, BCM Data Exports and Pricing are all first-class |
| **Rationale: team & ecosystem** | Cloud/DevOps engineers (Cloudboosta's talent pool) commonly know Python; large hiring pool in Lagos, Nairobi and Cape Town; FastAPI produces OpenAPI 3.1 automatically (contract-first with generated TS clients) |
| **Trade-offs** | Slower raw CPU than Go/JVM (mitigated: heavy maths runs in DuckDB/Arrow native code); GIL limits threads (use processes and async I/O); typing discipline needed (mypy strict + Pydantic) |
| **Alternatives considered** | **Node.js + NestJS:** one language with the frontend, but a weaker data ecosystem. **Go:** excellent performance and concurrency, but slower to build analytics and fewer data libraries; consider for a high-throughput collector at v2. **Kotlin/Spring:** robust, but heavier for a small team |
| **Versioning/support** | Python versions get ~5 years of security support; plan an annual minor upgrade. FastAPI/Pydantic follow semver; pin and upgrade monthly via Renovate |

**Key backend libraries:** `httpx` (outbound HTTP), `tenacity` (retries), `structlog` (JSON logs), `opentelemetry-*`, `cryptography` (envelope encryption helpers), `pyarrow`, `duckdb`, `boto3`/`aioboto3` (where async helps), `jinja2` (fix-snippet templates), `python-slugify`, `pytest` stack (11f).

### 2.2 Background jobs: Procrastinate (PostgreSQL-backed)

| Aspect | Detail |
|---|---|
| **Recommendation** | **Procrastinate 3.x**: Python task queue backed by PostgreSQL (LISTEN/NOTIFY + row locks) |
| **Rationale** | No extra stateful infrastructure (uses the managed Postgres we already run); **transactional enqueue** (job created in the same transaction as the data change); built-in **queueing locks** for per-tenant serialisation (`lock=f"tenant:{id}:cur"`); periodic tasks (replaces a separate cron); retries with backoff; async-native; jobs visible in SQL (easy ops) |
| **Trade-offs** | Throughput lower than Redis-based queues (sufficient for MVP: thousands of jobs/hour); adds load on Postgres (mitigate with a dedicated schema, connection pooling and tuning); smaller community than Celery |
| **Alternatives** | **Celery + Valkey:** most popular, but weaker per-tenant locking, operational quirks, no transactional enqueue. **Dramatiq:** simpler than Celery, same limits. **Temporal:** best for long, multi-step workflows with durable state (e.g., future remediation with approvals); heavier to operate. **Reassess at v2** (approved one-click actions and multi-step remediation workflows) |
| **Abstraction** | A thin internal `jobs` module (`enqueue`, `schedule`, decorators) so the queue can be swapped without touching business modules |

### 2.3 Analytical processing: DuckDB

- **DuckDB 1.x** embedded in workers (ingestion/normalisation/reconciliation) and, with strict limits, in the API for bounded line-item drill-downs.
- Reads/writes Parquet on Spaces via the `httpfs` extension (S3-compatible endpoint, credentials from the secrets manager).
- **Trade-off:** single-process engine; for concurrency we pre-aggregate into Postgres and use DuckDB mainly in batch jobs.
- **Scale path:** ClickHouse (self-hosted on DOKS or ClickHouse Cloud) when the Phase 9 triggers are met (`cost_daily` > ~1B rows or p95 > 2s), behind `CostQueryService`.

---

## 3. Frontend

| Concern | Recommendation | Rationale | Alternatives / trade-offs |
|---|---|---|---|
| **Framework** | **React 19 + TypeScript 5**, built with **Vite** as a **single-page app** | Largest talent pool; rich data-viz/table ecosystem; SPA suits an authenticated, data-dense app served from CDN (no SSR needed; marketing site is separate) | Next.js (SSR not needed for the app; adds server runtime); Vue/Svelte (smaller ecosystems for enterprise tables/charts) |
| **Routing** | **TanStack Router** | Type-safe routes and **search params** (filters live in the URL → shareable views, PRD FR-UI-02) | React Router 7 (good, less type-safe search params) |
| **Server state** | **TanStack Query** | Caching, background refresh, request de-duplication, optimistic updates for assign/dismiss | SWR (lighter, fewer features) |
| **Client state** | **Zustand** (small stores: currency view, UI prefs) | Minimal boilerplate; most state is server state | Redux Toolkit (heavier) |
| **Styling & tokens** | **Tailwind CSS 4** with **design tokens** from the "Cloudoptic" design system (Boosta Blue `#2C68FC`, Boosta Yellow `#FAC812`, Optics Navy `#0B1F4B`, semantic colours) exported via Tokens Studio → CSS variables | 1:1 tokens between Figma/design canvas and code; light/dark themes via CSS variables | CSS Modules/vanilla-extract (fine, slower iteration) |
| **Component library** | **Radix UI primitives** composed into our own components (shadcn/ui pattern) | Accessible primitives (focus, ARIA, keyboard) we fully own and style; no vendor lock | MUI/Ant Design (fast, but heavy bundles and hard to brand); Mantine |
| **Tables** | **TanStack Table** + **TanStack Virtual** | Headless, supports sorting, grouping, expansion (drill-down), selection, virtualisation for 10k+ rows | AG Grid (powerful, larger bundle; enterprise licence for some features) |
| **Charts** | **Apache ECharts** (tree-shaken, SVG renderer by default) | Handles large series; built-in ARIA descriptions and decal patterns (colour-blind support); rich interactions (anomaly markers, budget lines, bands); the same options render in the PDF route | Recharts (simpler, weaker with large data and accessibility); visx (flexible, more custom work) |
| **Forms** | **React Hook Form + Zod** (schemas shared with generated OpenAPI types where possible) | Performance and type-safe validation | Formik |
| **API client** | **openapi-typescript + openapi-fetch** generated from the FastAPI OpenAPI spec | Types always match the backend; CI fails on breaking changes | Orval (generates hooks; more opinionated) |
| **i18n & formatting** | **FormatJS (react-intl)**; `Intl.NumberFormat` for currencies (NGN, KES, ZAR, EGP, GHS, GBP, EUR, USD) | ICU messages; locale-correct money and dates; RTL readiness | i18next |
| **Testing** | Vitest + Testing Library; Playwright (E2E); Storybook (component catalogue + visual tests) | — | — |
| **Performance budgets** | Initial JS ≤ 300 KB gz (app shell), route-level code splitting, lazy-load charts | PRD FR-UI-10 / NFR-PERF-03 (3G-class networks) | — |

**Versioning:** React 19 is the current major line; upgrade majors within 6 months of release once the ecosystem supports them. Node.js LTS (22+) for the build toolchain.

---

## 4. Data Storage

| Store | Choice | Rationale | Trade-offs / alternatives | Version & support |
|---|---|---|---|---|
| **Primary DB** | **DO Managed PostgreSQL 16+** (17 if available in LON1), primary + standby, PITR | RLS for tenancy; partitioning for aggregates; JSONB for evidence/config; also hosts the job queue; managed HA/backups | TimescaleDB (extension availability on DO managed to be confirmed; not required); BigQuery/Snowflake (another cloud, egress, cost); self-managed Postgres (ops burden) | Upgrade major versions yearly with DO's in-place/major-version upgrade, tested in staging |
| **Connection pooling** | DO connection pool (PgBouncer, transaction mode) for API; direct connections for workers needing `LISTEN` | Protects DB connections under autoscaling | — | — |
| **Object storage** | **DO Spaces** (S3-compatible) + replication to AMS3 | Cheap; works with DuckDB `httpfs`, boto3, Parquet tooling | AWS S3 (better features, but cross-cloud egress and residency story) | Lifecycle rules per Phase 9 §2.4 |
| **Analytical** | **DuckDB 1.x** now → **ClickHouse** at scale trigger | See §2.3 | Snowflake/BigQuery (costly, cross-cloud) | — |
| **Cache** | **DO Managed Valkey 8** | Query cache, rate limiting (token buckets), distributed locks, short-lived state; Redis-compatible clients (`redis-py`) | Memcached (no data structures) | — |
| **Search** | None in MVP (Postgres full-text/trigram for resource search) | Avoids another service | OpenSearch at v2 if needed | — |

---

## 5. Cloud Connectors & Ingestion

| Concern | Choice | Notes |
|---|---|---|
| **AWS SDK** | **boto3/botocore** (sync in worker processes; `aioboto3` where concurrency helps, e.g., multi-region Describe scans) | Use botocore's adaptive retry mode + our own token buckets; pin botocore and refresh monthly (new instance types/APIs) |
| **AWS data** | BCM Data Exports (CUR 2.0) Parquet in customer S3; Cost Explorer; Cost Optimization Hub; Compute Optimizer; CloudWatch `GetMetricData`; EC2/EBS/ELBv2/RDS/Lambda/S3 Describe; Organizations; Savings Plans; **Price List bulk API** (offer files) | Price List bulk files are cached in Spaces and refreshed weekly |
| **Credentials** | IAM Roles Anywhere **credential helper** (`aws_signing_helper`) or a Python implementation of the `CreateSession` flow, with certs from AWS Private CA | Broker credentials refreshed every ≤ 1h; customer sessions ≤ 1h |
| **Azure (v2)** | `azure-identity` (multi-tenant app / federated credentials), `azure-mgmt-costmanagement` (exports in FOCUS format) | — |
| **GCP (v2)** | `google-cloud-bigquery` (billing export, FOCUS view), Workload Identity Federation | — |
| **Orchestration** | Procrastinate jobs + periodic tasks (§2.2). Pipeline stages chained by events (`cur.loaded` → `cost.aggregate` → …) | Temporal at v2 for long-running, approval-based remediation workflows |
| **Alternatives** | Airflow / Prefect / Dagster | Strong for data-team DAGs, but heavy for a multi-tenant SaaS job model (thousands of small per-tenant jobs). Revisit Dagster if an internal analytics/benchmark team forms |

---

## 6. Infrastructure

| Concern | Choice | Rationale / trade-offs |
|---|---|---|
| **Hosting provider** | **DigitalOcean** (stakeholder decision) | Simple, predictable pricing; no African region (mitigated by Cloudflare edge; data residency position in Phase 10 A7.4) |
| **Regions** | LON1 primary; AMS3 secondary (backups, DR) | Phase 9 §1.2 |
| **Containers** | Docker (multi-stage, distroless/slim Python base, non-root, read-only FS) | — |
| **Orchestration** | **DOKS** (managed Kubernetes, HA control plane) with node pools `general` and `workers`; HPA (API: CPU/RPS; workers: KEDA on queue depth via a Postgres scaler) | Fallback: **DO App Platform** if the team lacks Kubernetes skills (loses KEDA scaling, egress control and node pools) |
| **Static egress IPs** | DO VPC NAT Gateway if generally available in LON1; otherwise an egress proxy on Droplets with **reserved IPs** for outbound AWS traffic | Enables customer `aws:SourceIp` pinning (Phase 9 O4). **Verify in Sprint 0** |
| **IaC** | **Terraform 1.x** (OpenTofu-compatible syntax) with DO, AWS, Cloudflare, GitHub providers; remote state in Spaces (S3 backend with locking) or Terraform Cloud | Pulumi/CDK (fine, but Terraform is most familiar to cloud engineers and Cloudboosta's training) |
| **App deployment** | **Helm** charts + **Argo CD** (GitOps) | Declarative, auditable deploys; easy rollback |
| **Policy** | Kyverno (admission: signed images, non-root, resource limits) | — |
| **Container registry** | DO Container Registry | Image signing with **cosign** (keyless via GitHub OIDC) |

---

## 7. Observability

| Concern | Choice | Notes |
|---|---|---|
| **Instrumentation** | **OpenTelemetry** SDKs (Python auto + manual spans; browser SDK for web vitals) | Vendor-neutral |
| **Collection** | OTel Collector (DaemonSet/Deployment) + Grafana Alloy | — |
| **Backends** | **Grafana Cloud**: Loki (logs), Prometheus/Mimir (metrics), Tempo (traces), Grafana (dashboards, alerting, OnCall/IRM) | Free tier initially; managed means no ops. Alternative: self-hosted kube-prometheus-stack + Loki (cheaper at scale, more ops) |
| **Errors** | **Sentry** (backend + frontend, release tracking, PII scrubbing) | — |
| **Uptime/synthetics** | Grafana synthetic monitoring or Checkly, with probes in/near Lagos, Nairobi, Johannesburg | — |
| **Product analytics** | **PostHog EU** (events per PRD §7; feature flags) | Self-host later if required |

---

## 8. Security Tooling

| Concern | Choice | Rationale / alternatives |
|---|---|---|
| **Auth provider** | **WorkOS AuthKit** (hosted login, email/password, Google, MFA; SAML/OIDC SSO and SCIM available for v1 enterprise customers) | Fast to integrate; enterprise SSO when needed. Pricing scales with SSO connections (check current pricing). **Alternatives:** Auth0 (mature, higher cost), Clerk (great DX, B2B features improving), **Keycloak/Zitadel** self-hosted (no per-user cost, data in our region, more ops), AWS Cognito (cheap, weaker UX/B2B). **Exit plan:** keep our own user/tenant tables; store only `auth_subject` |
| **Workload identity to AWS** | IAM Roles Anywhere + **AWS Private CA in short-lived certificate mode** | Avoids long-lived keys on DO and avoids self-managing CA keys (addresses Phase 10 T2). Cost is a small monthly CA fee (confirm current pricing) |
| **Secrets** | **AWS Secrets Manager** (broker account, `eu-west-2`) synced to Kubernetes via **External Secrets Operator** | Same trust root as the broker account; audit via CloudTrail. Alternatives: Doppler, HCP Vault, 1Password Secrets Automation |
| **Envelope encryption** | **AWS KMS** CMK (per environment) for data keys | Phase 10 A5 |
| **App security scanning** | Semgrep (SAST), Trivy (containers + IaC), GitHub Dependabot/Renovate + `pip-audit`/`npm audit` (SCA), Gitleaks + GitHub push protection (secrets) | Plus the Cloudflare `security-audit` skill for periodic full audits (Phase 10 A7.3) |
| **WAF/DDoS** | Cloudflare WAF managed rules, rate limiting, bot management | — |

---

## 9. Supporting Services

| Service | Choice | Notes |
|---|---|---|
| Email | Postmark (separate message streams: transactional vs broadcast) | SPF/DKIM/DMARC `p=reject` |
| Slack | Slack app (OAuth v2, Bolt for Python) | Signing-secret verification on interactivity |
| FX rates | **Set by hand by Cloudoptics staff** in the platform admin console (ADR-010; PRD FR-FX-06/07). A commercial API (e.g., Open Exchange Rates) is fetched daily as a **reference and fallback only** | Each rate stores value, effective date, basis, source note and author; changes are audited |
| PDF | Playwright (Chromium) in a dedicated `renderer` worker | Reuses web components and ECharts for brand-consistent reports |
| Payments (v1) | Paystack (local currencies, subscriptions) + Stripe (USD/GBP/EUR) | Bank transfer/invoice fallback for cards that can't auto-renew |
| Feature flags | PostHog flags (server + client SDKs) | OpenFeature interface in code to allow switching |

---

## 10. Decision Log (ADRs to create in Sprint 0)

| ADR | Decision | Status |
|---|---|---|
| ADR-001 | Modular monolith + workers (Phase 9 D1) | Accepted |
| ADR-002 | Python/FastAPI backend | Proposed |
| ADR-003 | Procrastinate job queue (Temporal at v2 review) | Proposed |
| ADR-004 | PostgreSQL + Spaces/Parquet + DuckDB; ClickHouse trigger | Proposed |
| ADR-005 | React SPA stack (TanStack, Tailwind, Radix, ECharts) | Proposed |
| ADR-006 | WorkOS AuthKit | Proposed (spike in Sprint 0 vs Keycloak) |
| ADR-007 | IAM Roles Anywhere + AWS Private CA; Secrets Manager + KMS | Proposed (spike in Sprint 0) |
| ADR-008 | DOKS + Argo CD + Terraform | Proposed |
| ADR-009 | Grafana Cloud + Sentry + PostHog | Proposed |
| ADR-010 | Exchange rates: set by hand by Cloudoptics staff in the platform admin console; a commercial feed is shown as a reference and used only if no manual rate exists | **Decided 22 Oct 2026** |
| ADR-011 | Static egress approach in LON1 | Open (Sprint 0 verification) |
| ADR-012 | GitHub first (Pages demo, GHCR image, Codespaces), then DigitalOcean | **Decided 22 Oct 2026** |
| ADR-013 | Staging on DigitalOcean App Platform (API, worker, web, managed PostgreSQL) at cloudoptics.cloudboosta.com; DOKS later | **Decided 22 Oct 2026** |

## 11. Skills & Team Fit

| Skill | Needed by | Mitigation if missing |
|---|---|---|
| Python data engineering (Parquet, DuckDB, SQL) | Backend/data engineers | Pairing; DuckDB is approachable |
| AWS billing domain (CUR, pricing, Savings Plans) | Backend, FinOps SME | Cloudboosta FinOps SME; AWS docs; synthetic fixtures |
| Kubernetes/DOKS, Terraform, GitOps | DevOps engineer | Managed services; App Platform fallback; Cloudboosta training |
| React + data viz + accessibility | Frontend engineer | Design-system components built early; Storybook |
| Security (IAM, AppSec) | Tech lead + DevOps | External pen test; security-audit skill; advisor |
