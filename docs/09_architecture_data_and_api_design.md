# 09 — Architecture, Data Model & API Design

**Product:** Cloudoptics by Cloudboosta
**Phase:** 9 of 11 — Technical Architecture & Data Design
**Role:** Staff Software Architect / Cloud Solutions Architect
**Date:** 22 October 2026
**Inputs:** Phase 5 (MVP, technical considerations T1–T12), Phase 6 PRD (FR/NFR), Phase 7–8 (IA, screens)
**Status:** Draft for engineering review

**Fixed constraints:**
- Cloudoptics itself is hosted on **DigitalOcean (DO)**.
- Customers connect **AWS** first, **read-only**.
- The primary market is **Africa**, and DO has **no African region**.
- Tenant data is shown in **USD and local currency** (switchable USD / local / both).
- Final technology versions and libraries are chosen in Phase 11a. This document fixes the architecture and the contracts between parts.

---

# Part 1 — System Architecture Overview

## 1.1 Architectural style: modular monolith + workers

**Decision:** one codebase and deployable application, split into **well-bounded modules** with explicit interfaces. It runs as **separate process types**: `api` (HTTP), `worker` (background jobs) and `scheduler` (cron-like triggers). The frontend is a separate single-page application served from a CDN.

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Modular monolith + workers** | Fast to build with a small team; one deploy pipeline; simple transactions; easy local dev; can extract services later along module boundaries | Needs discipline on module boundaries; a single language runtime | ✅ **Chosen for MVP → v1** |
| Microservices | Independent scaling and deploys | High operational cost on DO (service mesh, tracing, many pipelines) for a team of ~5–8; slows MVP | ❌ Revisit at v2 for ingestion/analysis if scale demands it |
| Serverless (DO Functions) | No servers | Execution limits; poor fit for long-running ingestion and analysis; vendor-specific | ❌ |

**Module boundaries** (each owns its tables and exposes an internal interface; no cross-module table access):

| Module | Responsibility |
|---|---|
| `identity` | Users, sessions, MFA, tenants, memberships, roles, invites |
| `connectors` | Cloud connections, credentials brokering, permission checks, sync runs (AWS now; Azure/GCP later behind the same interface) |
| `ingestion` | Raw file landing, normalisation to FOCUS, data quality, aggregates, reconciliation |
| `inventory` | Resources, metrics, price catalogue |
| `fx` | Exchange rates and conversion |
| `analysis` | Engines R1–R8, anomalies, forecasts, "what changed", credits runway |
| `recommendations` | Recommendation store, scoring, lifecycle, verification, outcomes |
| `budgets` | Budgets, thresholds, evaluations |
| `notifications` | Email, Slack, digests, de-duplication |
| `reporting` | Monthly summary, PDF rendering, exports |
| `audit` | Append-only audit log |
| `platform` | Config, feature flags, job framework, observability helpers |

## 1.2 Hosting & deployment topology (DigitalOcean)

**Primary region:** **DO LON1 (London).**
- Lowest typical latency to West, East and Southern Africa among DO regions; Europe–Africa subsea routes land in Europe.
- Close to AWS `eu-west-2`/`eu-west-1`, where many target customers' CUR buckets live.
- UK GDPR jurisdiction, matching Cloudboosta's UK base.

**Secondary region (backups/DR):** AMS3 or FRA1. Cross-border transfer terms are documented in Phase 10.

```
                            Users (Africa, mobile & desktop)
                                         │ HTTPS
                       ┌─────────────────▼──────────────────┐
                       │ CDN + WAF (e.g. Cloudflare)         │  static SPA assets,
                       │ TLS, DDoS, bot protection           │  edge caching near African PoPs
                       └───────┬───────────────────┬─────────┘
                               │ /app (static)     │ /api
               ┌───────────────▼───┐     ┌─────────▼──────────────────────────────┐
               │ Spaces + CDN       │     │ DO Load Balancer (LON1)                │
               │ (SPA bundle)       │     └─────────┬──────────────────────────────┘
               └────────────────────┘               │
   ┌────────────────────────── DO VPC (LON1) ───────┼───────────────────────────────────────┐
   │  DOKS (Kubernetes) cluster                     ▼                                       │
   │   ┌───────────────┐  ┌────────────────────┐  ┌──────────────┐                          │
   │   │ api pods (HPA) │  │ worker pods (HPA,   │  │ scheduler    │                          │
   │   │ REST /api/v1   │  │ queue-depth based)  │  │ (1 replica,  │                          │
   │   └──────┬────────┘  └──────┬─────────────┘  │ leader lock) │                          │
   │          │                  │                 └──────┬───────┘                          │
   │   ┌──────▼──────────────────▼────────────────────────▼───────┐                          │
   │   │ Managed PostgreSQL (primary + standby, PITR)             │ app data + aggregates    │
   │   │ Managed Valkey (cache, rate limits, locks, queue*)       │                          │
   │   │ Spaces (S3-compatible): raw CUR, normalised Parquet,     │                          │
   │   │   reports, exports  (+ replication to AMS3/FRA1)         │                          │
   │   └──────────────────────────────────────────────────────────┘                          │
   │   Secrets: external secrets manager (e.g., Vault/Doppler/1Password) → K8s secrets       │
   │   Egress: static IP(s) via NAT gateway / egress proxy  ──────────────┐                  │
   └───────────────────────────────────────────────────────────────────────┼──────────────────┘
                                                                           │ HTTPS (SigV4)
          ┌────────────────────────────────────────────────────────────────▼────────────────┐
          │ Cloudoptics AWS "broker" account                                                │
          │  IAM Roles Anywhere trust anchor (our CA) → role `cloudoptics-broker`            │
          │  (may only sts:AssumeRole into customer CloudopticsReadOnlyRole + ExternalId)    │
          └────────────────────────────────────────────────────────────────┬────────────────┘
                                                                           │ sts:AssumeRole
              Customer AWS accounts: CloudopticsReadOnlyRole → CUR S3, Cost Explorer,
              Cost Optimization Hub, Compute Optimizer, CloudWatch, Describe* APIs, Pricing
External SaaS: email provider · Slack API · FX rate providers · Google OIDC · error tracking · analytics
```
`*` Queue technology is decided in Phase 11a (options: a PostgreSQL-backed durable queue, Valkey-based queue or Temporal). The architecture requires **durable, retryable, idempotent jobs with per-tenant concurrency limits**.

**Why DOKS (managed Kubernetes) rather than App Platform:** worker autoscaling on queue depth, controlled static egress (needed for customers who restrict role assumption by source IP), node pools for heavier analysis jobs, and a clean path to self-hosted ClickHouse later. App Platform is a fallback if the team lacks Kubernetes skills (decision in 11a).

## 1.3 Major components

### 1.3.1 Cloud connectors / ingestion layer

- **Credential broker (AWS):**
  1. Workers get short-lived AWS credentials for our broker role via **IAM Roles Anywhere** (X.509 client certificates issued by our CA; trust anchor in our AWS broker account). **No long-lived AWS keys on DO.**
  2. The broker role can only call `sts:AssumeRole` on `arn:aws:iam::*:role/CloudopticsReadOnlyRole*`.
  3. Each job then assumes the customer role with the tenant's **ExternalId** (session ≤ 1h, session name `co-<tenant>-<job>`).
  - Credentials are cached in memory per job only, never persisted.
  - **CA protection:** the CA private key is kept in an HSM-backed or managed key store (e.g., AWS Private CA in short-lived mode, or Vault PKI with the root offline). Certificates are short-lived (≤ 24h) and issued per pod. Trust-anchor changes are monitored with CloudTrail alerts. (External-CA trust anchors are a known persistence risk if the CA is compromised.)
- **Fallback (decision in 11a):** an IAM user with keys in the secrets manager, rotated every ≤ 90 days. Use only if Roles Anywhere is blocked.
- **Static egress IPs:** customers can optionally add an `aws:SourceIp` condition to their role trust policy.
- **AWS adapters:** BCM Data Exports/CUR 2.0 reader (S3), Cost Explorer client (budgeted calls), Cost Optimization Hub and Compute Optimizer clients, CloudWatch `GetMetricData` batcher, Describe* inventory scanners (per region), Organizations, Savings Plans, Price List (bulk files).
- **Rate control:** a token bucket per (tenant, AWS API) in Valkey; exponential backoff with jitter; **per-tenant monthly customer-side cost budget** for paid APIs (PRD FR-CON-09).
- **Provider abstraction:** a `CloudProvider` interface (`listAccounts`, `fetchBillingFiles`, `listResources`, `fetchMetrics`, `fetchNativeRecommendations`). Azure/GCP adapters are added in v2 without changing downstream modules.

### 1.3.2 Data storage

| Store | Technology (DO) | Holds | Why |
|---|---|---|---|
| **Relational** | DO Managed PostgreSQL (16+), HA standby, PITR | Tenants, users, connections, resources, recommendations, budgets, alerts, audit, FX, **daily cost aggregates** (partitioned) | Strong consistency, row-level security, mature tooling; aggregates are small enough for Postgres at MVP scale |
| **Object storage** | DO Spaces (S3 API) | Raw CUR files (as delivered), **normalised FOCUS Parquet** (partitioned by tenant/billing period), metric snapshots, PDFs, CSV exports | Cheap, immutable source of truth; enables full rebuild and reprocessing |
| **Analytical engine (embedded)** | DuckDB inside workers (and the API for bounded queries) reading Parquet from Spaces | Normalisation transforms, line-item drill-downs, reconciliation | No extra infrastructure; very fast on Parquet; fits MVP volume |
| **Cache / coordination** | DO Managed Valkey | Query cache, rate limits, distributed locks, sessions (if used), short-lived job state | Low latency; Redis-compatible |
| **Search/logs** | Managed logging (e.g., Grafana Cloud / Better Stack / DO OpenSearch) | App logs, traces, metrics | Phase 11a decides |

**"Time-series" needs:** daily cost and daily metric summaries are stored in partitioned PostgreSQL tables (by month) with BRIN/B-tree indexes. A dedicated time-series database is not needed for the MVP.

**Scale-out trigger to a columnar database:** move aggregates and line items to **ClickHouse** (self-managed on DOKS or ClickHouse Cloud) when any of these is true:
- `cost_daily` exceeds ~1B rows;
- p95 of cost-explorer queries exceeds 2s with tuning;
- line-item interactive queries become a core feature.

The module interface (`CostQueryService`) hides the store, so the migration doesn't affect the API.

### 1.3.3 Analysis & recommendation engines

- Implemented as **pure, versioned functions** over well-defined inputs: inventory snapshot, metric summaries, prices, tenant settings, effective rates. Outputs are recommendation candidates.
- An **engine registry** (`R1…R8`, anomaly, forecast, what-changed, credits) with an `engine_version` stored on every output, for reproducibility and the M2 outcome dataset.
- Engines run as jobs triggered by data events (`cur.loaded`, `inventory.refreshed`, `metrics.refreshed`) and a daily schedule.
- The **Scoring service** applies risk, confidence and priority (PRD FR-REC-03/04/05) and de-duplicates by resource.
- The **Verification service** compares resource state and CUR cost before and after for items in Done or with detected change.
- Rules are explainable for the MVP. An ML risk model is introduced later behind the same scoring interface, using the outcome dataset.

### 1.3.4 API layer

- REST/JSON, OpenAPI 3.1 contract-first, versioned `/api/v1` (Part 3).
- **Backend-for-frontend:** the SPA uses the same public API with session-cookie authentication; personal access tokens and public API keys arrive in v1.
- Cross-cutting middleware: authentication, tenant resolution, RBAC, rate limiting, request IDs, audit hooks, error mapping, ETags/caching headers, compression.

### 1.3.5 Frontend application

- SPA (framework in 11a; e.g., React + TypeScript) served from Spaces + CDN.
- Uses the design-system tokens from Phase 8 (Boosta Blue #2C68FC, Boosta Yellow #FAC812, Optics Navy #0B1F4B).
- **Currency toggle (USD / Local / Both)** is a user preference sent as a query parameter. The API returns both USD and local amounts so the client can switch instantly without refetching.
- Code-split per route; performance budgets from PRD FR-UI-10.

### 1.3.6 Background jobs & schedulers

| Job | Trigger | Frequency (default) | Concurrency key |
|---|---|---|---|
| `connection.validate` | connect, schedule | on connect + every 6h | per connection |
| `cur.poll` → `cur.ingest` → `cost.aggregate` → `cost.reconcile` | schedule / new manifest | poll every 6h | per tenant |
| `ce.mtdSummary` | schedule | daily (+ at onboarding) | per tenant |
| `inventory.scan` | schedule | every 12h per account × region | per account |
| `metrics.pull` | after inventory | daily, candidates only | per account |
| `native.recs.pull` (COH, Compute Optimizer, SP) | schedule | daily | per account |
| `analysis.run` (R1–R8, anomalies, forecast) | events + daily | ≥ daily | per tenant |
| `recs.verify` | schedule | every 12h | per tenant |
| `budgets.evaluate` | after `cost.aggregate` | ≤ 1h after data | per tenant |
| `fx.fetch` | schedule | daily 06:00 UTC + retry | global |
| `prices.refresh` | schedule | weekly (+ on demand) | global |
| `notify.dispatch` | events | continuous | per tenant/channel |
| `digest.weekly`, `report.monthly` | schedule | Mon 07:00 tenant TZ; 3rd working day | per tenant |
| `export.generate` | user request | on demand | per user |
| `retention.purge`, `tenant.delete` | schedule / request | daily | global |

**Job framework rules:**
- Jobs are idempotent (keyed by tenant, scope and data version).
- Retries use exponential backoff up to 5 attempts, then go to a dead-letter queue with alerting.
- Concurrency limits per tenant; fair scheduling (round-robin across tenants) so a large tenant can't starve others.
- Every job emits start/finish/failure events with duration and record counts (`sync_run`).

## 1.4 High-level data flow

```
[1] Connect      Customer deploys CFN stack → role + (optional) CUR 2.0 export to customer S3
[2] Collect      worker ─(Roles Anywhere → broker role → AssumeRole+ExternalId)→ customer AWS
                   • CUR manifest/files (S3 GetObject, export bucket only)
                   • Describe* inventory, CloudWatch metrics, COH/Compute Optimizer, SP data, CE summary
[3] Land         Raw files copied as-is → Spaces  raw/{tenant}/{export}/{billing_period}/{version}/…
[4] Normalise    DuckDB transform → FOCUS-aligned Parquet  norm/{tenant}/{billing_period}/…
                   + data-quality checks (schema, row counts, nulls, negative-cost sanity)
[5] Aggregate    Daily aggregates → PostgreSQL cost_daily (upsert by tenant+period+version)
[6] Reconcile    Compare to invoice totals (±1%) → flag/alert if not
[7] Convert      fx_rate joined at query time (USD stored; local computed per charge date)
[8] Analyse      Engines R1–R8 + anomaly + forecast + what-changed → candidates
[9] Score        Risk/confidence/priority, dedupe → recommendations (upsert by fingerprint)
[10] Act/Notify  budgets.evaluate, anomalies → notifications (email/Slack); UI via REST API
[11] Verify      Resource state + post-change CUR → verified savings → outcome events (Moat M2)
```

**Freshness targets:** dashboards update ≤ 2h after CUR delivery (PRD FR-ING-10); 95% of tenants have data < 24h old (NFR-REL-02).

## 1.5 Multi-tenancy strategy

| Layer | Strategy |
|---|---|
| **Model** | Pooled multi-tenancy (shared infrastructure). Every tenant-owned row has `tenant_id`. Tenant hierarchy (`parent_tenant_id`) supports partners/MSPs later |
| **PostgreSQL** | **Row-level security (RLS)** on all tenant tables. The app sets `app.tenant_id` per transaction (`SET LOCAL`); the application DB role has no `BYPASSRLS`. A separate privileged role is used only for migrations and platform jobs (audited) |
| **Object storage** | Tenant-prefixed paths; access only through the storage service that enforces the tenant prefix; per-tenant encryption context in object metadata; no public buckets |
| **DuckDB/analytics** | Query builder injects a mandatory tenant path/filter; queries can't be passed raw from clients |
| **Cache** | Keys namespaced `t:{tenant_id}:…` |
| **Jobs** | Each job payload carries `tenant_id`; per-tenant concurrency and API budgets |
| **Testing** | Automated cross-tenant access tests in CI (API and DB level); periodic RLS policy linting |
| **Noisy neighbour** | Fair scheduling, per-tenant rate limits, query timeouts (e.g., 10s API, 15 min jobs) |
| **Isolation upgrade path** | For large/regulated tenants later: dedicated schema or database (and storage bucket) via a tenant-placement table, with no API change |

## 1.6 Security model

| Area | Design |
|---|---|
| **User authentication** | OIDC-based auth (managed provider or self-hosted; chosen in 11a). Email/password + Google; TOTP MFA; breached-password check. Web sessions: httpOnly, Secure, SameSite=Lax cookies; CSRF tokens on state-changing requests; idle 12h / absolute 7d |
| **Authorisation** | RBAC (Admin/Member/Viewer) checked in API middleware with policy definitions in code; resource ownership checks (tenant scope) on every query; RLS as defence in depth |
| **Customer cloud access** | Read-only role, ExternalId (confused-deputy protection), short-lived STS, optional source-IP condition; access cost monitoring; permission health checks; disconnect stops all jobs immediately |
| **Our cloud identity** | IAM Roles Anywhere with a short-lived certificate per pod; broker role limited to `sts:AssumeRole` on the customer role name pattern; CloudTrail and alerts in the broker account |
| **Encryption** | TLS 1.2+ (HSTS) in transit, including to the DB/Valkey (TLS) and Spaces. At rest: DO-managed encryption for DB, volumes and Spaces, **plus application-level envelope encryption** for sensitive fields (ExternalIds, Slack tokens, webhook secrets) using a KMS-held key (e.g., Vault Transit) |
| **Secrets management** | External secrets manager → Kubernetes secrets via an operator; no secrets in images, Git or logs; rotation runbooks (Phase 11g) |
| **Network** | Private VPC for DB/Valkey; only the load balancer is public; Kubernetes network policies (deny by default); WAF/CDN in front; admin access via SSO + VPN/bastion with MFA |
| **Application security** | OWASP ASVS L2; CSP; dependency, SAST and container scanning; signed images; pen test before paid launch |
| **Audit** | Append-only `audit_log` (no UPDATE/DELETE grants; hash-chained rows for tamper evidence); staff break-glass access logged and visible to the tenant |
| **Data minimisation** | We do not read customer application data; only billing, metadata, metrics and tags. Tags are treated as potentially personal data |

---

# Part 2 — Data Model & Schema

## 2.1 Core entities and relationships

```
Tenant 1──* Membership *──1 User
Tenant 1──* Invite
Tenant 1──* CloudConnection 1──* CloudAccount 1──* Resource 1──* ResourceMetricDaily
CloudConnection 1──* PermissionCheck
CloudConnection 1──* SyncRun
CloudConnection 1──* BillingFile (raw CUR file registry)
Tenant 1──* CostDaily (aggregate; FK to CloudAccount)
Tenant 1──* Recommendation *──0..1 Resource ; Recommendation 1──* RecommendationEvent
Recommendation 0..1──1 VerifiedSaving
Tenant 1──* Budget 1──* BudgetThreshold ; Budget 1──* BudgetEvaluation
Tenant 1──* Anomaly ; Tenant 1──* Notification ; Tenant 1──* AlertRule
Tenant 1──* Report ; Tenant 1──* Export ; Tenant 1──* AuditLog
Tenant 1──* Integration (Slack, later Jira/Teams)
Tenant 1──1 TenantSettings ; Tenant 1──* VirtualTagRule ; Tenant 1──* CreditBalanceEntry
FxRate (global: currency, date, source) ; PriceCatalog (global: region, sku)
Tenant 0..1──* Tenant (parent_tenant_id; partners/MSP, future)
```

| Entity | Purpose | Key cardinality notes |
|---|---|---|
| **Tenant** | Customer organisation (workspace) | Has display currency, country, time zone; optional parent (partner) |
| **User / Membership** | Person; role per tenant | User ↔ Tenant many-to-many via Membership (role) |
| **CloudConnection** | One CFN-deployed role (+ export) in an AWS account (management or standalone) | 1 tenant → many connections |
| **CloudAccount** | AWS account (management, member or standalone) discovered via a connection | 1 connection → many accounts; `resource_access` flag per account |
| **BillingFile** | Registry of CUR files landed (period, version, checksum, status) | Supports idempotent restatements |
| **CostLineItem** | Normalised FOCUS row (**Parquet in Spaces**, not Postgres) | Millions per tenant-month |
| **CostDaily** | Daily aggregate for dashboards | tenant × date × account × service × region × usage type × charge category × tag-set hash |
| **TagSet** | De-duplicated set of tags for aggregates | Referenced by hash |
| **Resource** | Inventory item (EC2, EBS, snapshot, EIP, ELB, RDS, Lambda, S3 bucket) | Unique (tenant, account, region, resource_id) |
| **ResourceMetricDaily** | Daily metric summaries (avg/p95/max) for candidate resources | Resource × metric × day |
| **PriceCatalog** | On-demand prices by region and SKU (incl. `af-south-1`, Local Zones) | Global |
| **FxRate** | Daily FX rate per currency and source | Global; unique (currency, date, source) |
| **Recommendation** | Current recommendation state | Unique by `fingerprint` (tenant + type + resource + target config) |
| **RecommendationEvent** | Lifecycle and outcome log (viewed, assigned, dismissed, done, verified…) | Append-only; feeds Moat M2 |
| **VerifiedSaving** | Realised savings calculation | 1:1 with a verified recommendation |
| **Budget / BudgetThreshold / BudgetEvaluation** | Budgets and their alert state | Thresholds fire once per period |
| **Anomaly** | Detected anomaly with status | Tenant × scope × date |
| **AlertRule / Notification** | Routing config and sent notifications | De-dup key per event |
| **Integration** | Slack (and later Jira/Teams) installation, encrypted tokens | 1 per tenant per type |
| **Report / Export** | Generated artefacts in Spaces | — |
| **AuditLog** | Append-only security log | — |
| **TenantSettings / VirtualTagRule / CreditBalanceEntry** | Configuration, account→team rules, manual credit balances | — |

## 2.2 Sample schemas (PostgreSQL)

> Conventions: UUIDv7 primary keys; `tenant_id` on all tenant tables with RLS; `timestamptz` in UTC; money stored as `numeric(18,6)` in **USD**; currency codes ISO 4217.

```sql
-- Tenancy & identity -------------------------------------------------------
CREATE TABLE tenant (
  id               uuid PRIMARY KEY,
  parent_tenant_id uuid REFERENCES tenant(id),          -- partners/MSP (future)
  name             text NOT NULL,
  country_code     char(2) NOT NULL,                    -- NG, KE, ZA, EG, GH…
  display_currency char(3) NOT NULL,                    -- NGN, KES, ZAR, EGP, GHS, GBP, EUR, USD
  fx_rate_basis    text NOT NULL DEFAULT 'official',    -- see PRD Q3
  time_zone        text NOT NULL DEFAULT 'Africa/Lagos',
  fiscal_month_start smallint NOT NULL DEFAULT 1,
  mfa_required     boolean NOT NULL DEFAULT false,
  status           text NOT NULL DEFAULT 'active',      -- active|suspended|deleting
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_user (
  id            uuid PRIMARY KEY,
  email         citext UNIQUE NOT NULL,
  display_name  text,
  auth_subject  text UNIQUE NOT NULL,                   -- OIDC subject
  mfa_enabled   boolean NOT NULL DEFAULT false,
  preferred_currency_view text NOT NULL DEFAULT 'both', -- usd|local|both
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE membership (
  tenant_id  uuid NOT NULL REFERENCES tenant(id),
  user_id    uuid NOT NULL REFERENCES app_user(id),
  role       text NOT NULL CHECK (role IN ('admin','member','viewer')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (tenant_id, user_id)
);

-- Connections --------------------------------------------------------------
CREATE TABLE cloud_connection (
  id               uuid PRIMARY KEY,
  tenant_id        uuid NOT NULL REFERENCES tenant(id),
  provider         text NOT NULL DEFAULT 'aws',         -- aws|azure|gcp (later)
  aws_account_id   char(12) NOT NULL,
  role_arn         text NOT NULL,
  external_id_enc  bytea NOT NULL,                      -- envelope-encrypted
  is_management    boolean NOT NULL DEFAULT false,
  cur_bucket       text, cur_prefix text, cur_export_arn text, cur_region text,
  status           text NOT NULL DEFAULT 'pending',     -- pending|healthy|degraded|failed|disconnected
  last_validated_at timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (provider, aws_account_id)                     -- one workspace per AWS account
);

CREATE TABLE cloud_account (
  id              uuid PRIMARY KEY,
  tenant_id       uuid NOT NULL REFERENCES tenant(id),
  connection_id   uuid NOT NULL REFERENCES cloud_connection(id),
  aws_account_id  char(12) NOT NULL,
  name            text,
  account_type    text NOT NULL,                        -- management|member|standalone
  resource_access boolean NOT NULL DEFAULT false,       -- member role deployed?
  UNIQUE (tenant_id, aws_account_id)
);

CREATE TABLE sync_run (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  connection_id uuid NOT NULL REFERENCES cloud_connection(id),
  job_type      text NOT NULL,                          -- cur.ingest|inventory.scan|metrics.pull…
  scope         jsonb,                                  -- account/region/period
  status        text NOT NULL,                          -- running|succeeded|partial|failed
  started_at    timestamptz NOT NULL, finished_at timestamptz,
  records       bigint, error_code text, error_detail text,
  est_customer_api_cost_usd numeric(10,4) DEFAULT 0
);

-- Costs --------------------------------------------------------------------
CREATE TABLE billing_file (
  id             uuid PRIMARY KEY,
  tenant_id      uuid NOT NULL,
  connection_id  uuid NOT NULL,
  billing_period date NOT NULL,                         -- first day of month
  export_version text NOT NULL,                         -- manifest execution id
  object_key     text NOT NULL,                         -- Spaces raw path
  sha256         char(64) NOT NULL,
  status         text NOT NULL,                         -- landed|normalised|aggregated|superseded|failed
  row_count      bigint,
  UNIQUE (tenant_id, connection_id, billing_period, export_version, object_key)
);

CREATE TABLE tag_set (
  tenant_id uuid NOT NULL,
  hash      bytea NOT NULL,                             -- sha256 of sorted k=v pairs
  tags      jsonb NOT NULL,
  PRIMARY KEY (tenant_id, hash)
);

CREATE TABLE cost_daily (
  tenant_id        uuid NOT NULL,
  usage_date       date NOT NULL,
  billing_period   date NOT NULL,
  account_id       char(12) NOT NULL,
  service_code     text NOT NULL,                       -- e.g. AmazonEC2
  service_name     text NOT NULL,                       -- FOCUS ServiceName
  region_id        text,                                -- e.g. af-south-1, af-south-1-los-1 (Local Zone)
  usage_type       text,
  charge_category  text NOT NULL,                       -- Usage|Purchase|Tax|Credit|Adjustment
  tag_set_hash     bytea,
  billed_cost_usd    numeric(18,6) NOT NULL,            -- unblended
  effective_cost_usd numeric(18,6) NOT NULL,            -- amortised
  net_cost_usd       numeric(18,6) NOT NULL,            -- after credits/discounts
  list_cost_usd      numeric(18,6),
  usage_quantity     numeric(24,6),
  data_version     text NOT NULL                        -- export version that produced it
) PARTITION BY RANGE (usage_date);
-- Monthly partitions; index: (tenant_id, usage_date), (tenant_id, service_code, usage_date),
-- (tenant_id, account_id, usage_date); BRIN on usage_date.

-- FX -----------------------------------------------------------------------
CREATE TABLE fx_rate (
  currency   char(3) NOT NULL,                          -- quote currency per 1 USD
  rate_date  date NOT NULL,
  basis      text NOT NULL,                             -- official|market
  rate       numeric(18,8) NOT NULL,
  source     text NOT NULL,
  fetched_at timestamptz NOT NULL,
  is_estimated boolean NOT NULL DEFAULT false,          -- carried forward (FR-FX-04)
  PRIMARY KEY (currency, rate_date, basis)
);

-- Inventory ----------------------------------------------------------------
CREATE TABLE resource (
  id            uuid PRIMARY KEY,
  tenant_id     uuid NOT NULL,
  account_id    char(12) NOT NULL,
  region_id     text NOT NULL,
  resource_type text NOT NULL,                          -- ec2_instance|ebs_volume|ebs_snapshot|eip|elb|rds_instance|lambda|s3_bucket
  resource_id   text NOT NULL,                          -- i-…, vol-…, arn
  arn           text,
  name          text,
  state         text,
  config        jsonb NOT NULL,                         -- type, size, family, iops, attachments, asg, lb…
  tags          jsonb NOT NULL DEFAULT '{}',
  environment   text,                                   -- prod|nonprod|unknown (derived)
  first_seen_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
  deleted_at    timestamptz,
  UNIQUE (tenant_id, account_id, region_id, resource_id)
);

CREATE TABLE resource_metric_daily (
  tenant_id   uuid NOT NULL,
  resource_id uuid NOT NULL REFERENCES resource(id),
  metric      text NOT NULL,                            -- cpu|mem|net_in|net_out|db_conn|iops|lb_requests
  day         date NOT NULL,
  avg_value   double precision, p95_value double precision, max_value double precision,
  sample_count integer,
  PRIMARY KEY (tenant_id, resource_id, metric, day)
) PARTITION BY RANGE (day);

-- Recommendations ----------------------------------------------------------
CREATE TABLE recommendation (
  id               uuid PRIMARY KEY,
  tenant_id        uuid NOT NULL,
  fingerprint      bytea NOT NULL,                      -- type+resource+target
  type             text NOT NULL,                       -- R1_unattached_ebs … R8_savings_plan
  resource_ref     uuid REFERENCES resource(id),
  account_id       char(12), region_id text,
  title            text NOT NULL,
  current_config   jsonb, recommended_config jsonb,
  cost_now_usd_month   numeric(18,6) NOT NULL,
  cost_after_usd_month numeric(18,6) NOT NULL,
  savings_usd_month    numeric(18,6) GENERATED ALWAYS AS (cost_now_usd_month - cost_after_usd_month) STORED,
  savings_basis    text NOT NULL,                       -- effective_rate|list_price
  effort           text NOT NULL CHECK (effort IN ('S','M','L')),
  risk_score       smallint NOT NULL,                   -- 0–100
  risk_level       text NOT NULL,                       -- low|medium|high
  risk_factors     jsonb NOT NULL,                      -- [{code, points, text}]
  confidence       text NOT NULL,                       -- high|medium|low
  confidence_notes jsonb,
  priority_score   numeric(18,6) NOT NULL,
  evidence         jsonb NOT NULL,                      -- metric refs, facts
  fix              jsonb NOT NULL,                      -- {console, cli, terraform, pre, rollback, template_version}
  primary_of       uuid REFERENCES recommendation(id),  -- alternatives point to primary
  status           text NOT NULL DEFAULT 'new',         -- new|assigned|in_progress|done|verified|dismissed|snoozed|resolved_externally|expired
  owner_user_id    uuid REFERENCES app_user(id),
  due_date         date,
  dismiss_reason   text, snooze_until date,
  engine_version   text NOT NULL,
  first_detected_at timestamptz NOT NULL, last_evaluated_at timestamptz NOT NULL,
  UNIQUE (tenant_id, fingerprint)
);
CREATE INDEX ON recommendation (tenant_id, status, priority_score DESC);

CREATE TABLE recommendation_event (
  id                uuid PRIMARY KEY,
  tenant_id         uuid NOT NULL,
  recommendation_id uuid NOT NULL REFERENCES recommendation(id),
  actor_user_id     uuid,                               -- null = system
  event_type        text NOT NULL,                      -- viewed|assigned|status_changed|dismissed|commented|verified|reopened
  payload           jsonb,
  engine_version    text,
  occurred_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE verified_saving (
  recommendation_id uuid PRIMARY KEY REFERENCES recommendation(id),
  tenant_id         uuid NOT NULL,
  change_detected_at timestamptz NOT NULL,
  baseline_cost_usd_30d numeric(18,6) NOT NULL,
  post_cost_usd_30d     numeric(18,6),                  -- normalised to 30 days
  realised_usd_month    numeric(18,6),
  status            text NOT NULL                       -- pending_data|confirmed|not_confirmed
);

-- Budgets, anomalies, notifications ---------------------------------------
CREATE TABLE budget (
  id          uuid PRIMARY KEY,
  tenant_id   uuid NOT NULL,
  name        text NOT NULL,
  scope       jsonb NOT NULL,                           -- {type: tenant|accounts|tag, accounts:[], tag:{k,v}}
  amount      numeric(18,2) NOT NULL,
  currency    char(3) NOT NULL,                         -- budget currency (USD or local)
  period      text NOT NULL DEFAULT 'monthly',
  recipients  jsonb NOT NULL,
  is_active   boolean NOT NULL DEFAULT true,
  created_by  uuid NOT NULL
);

CREATE TABLE budget_threshold (
  id        uuid PRIMARY KEY,
  budget_id uuid NOT NULL REFERENCES budget(id) ON DELETE CASCADE,
  kind      text NOT NULL CHECK (kind IN ('actual','forecast')),
  percent   smallint NOT NULL,
  last_fired_period date                                -- fire once per period
);

CREATE TABLE anomaly (
  id          uuid PRIMARY KEY,
  tenant_id   uuid NOT NULL,
  usage_date  date NOT NULL,
  account_id  char(12) NOT NULL,
  service_code text NOT NULL,
  actual_usd  numeric(18,6) NOT NULL,
  expected_usd numeric(18,6) NOT NULL,
  impact_usd  numeric(18,6) NOT NULL,
  contributors jsonb NOT NULL,
  status      text NOT NULL DEFAULT 'open',             -- open|investigating|expected|resolved
  UNIQUE (tenant_id, usage_date, account_id, service_code)
);

CREATE TABLE notification (
  id         uuid PRIMARY KEY,
  tenant_id  uuid NOT NULL,
  event_type text NOT NULL,
  channel    text NOT NULL,                             -- email|slack|webhook
  target     text NOT NULL,
  dedup_key  text NOT NULL,
  status     text NOT NULL,                             -- queued|sent|failed|suppressed
  sent_at    timestamptz,
  UNIQUE (tenant_id, channel, target, dedup_key)
);

-- Audit --------------------------------------------------------------------
CREATE TABLE audit_log (
  id          bigserial PRIMARY KEY,
  tenant_id   uuid,                                     -- null for platform events
  actor_type  text NOT NULL,                            -- user|system|staff
  actor_id    uuid,
  action      text NOT NULL,                            -- e.g. connection.created, role.changed
  target_type text, target_id text,
  before      jsonb, after jsonb,
  ip          inet, user_agent text,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  prev_hash   bytea, row_hash bytea NOT NULL            -- tamper-evident chain
);
-- Grants: INSERT/SELECT only for the app role.

-- RLS example (applied to every tenant table) -------------------------------
ALTER TABLE recommendation ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON recommendation
  USING (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
```

### 2.2.1 Normalised line items (Parquet, FOCUS-aligned)

Path: `norm/tenant={tenant_id}/billing_period=YYYY-MM/version={v}/part-*.parquet`

| Column (FOCUS name where available) | Type | Notes |
|---|---|---|
| `BillingAccountId`, `SubAccountId` | string | payer / linked account |
| `BillingPeriodStart`, `ChargePeriodStart`, `ChargePeriodEnd` | timestamp | UTC |
| `ChargeCategory`, `ChargeClass`, `ChargeDescription` | string | Usage/Purchase/Tax/Credit/Adjustment; correction flag |
| `ServiceName`, `ServiceCategory`, `ProviderName` | string | ProviderName = AWS |
| `RegionId`, `AvailabilityZone` | string | incl. Local Zones |
| `ResourceId`, `ResourceType` | string | |
| `SkuId`, `PricingCategory`, `PricingUnit`, `PricingQuantity` | mixed | On-Demand / Commitment / Spot |
| `ConsumedQuantity`, `ConsumedUnit` | decimal/string | |
| `BilledCost`, `EffectiveCost`, `ListCost`, `ContractedCost` | decimal(18,6) | USD (`BillingCurrency`) |
| `CommitmentDiscountId`, `CommitmentDiscountType` | string | SP/RI |
| `Tags` | map<string,string> | user + AWS cost allocation tags |
| `x_UsageType`, `x_Operation`, `x_LineItemType`, `x_CreditType` | string | AWS-specific extensions (FOCUS `x_` convention) |
| `_tenant_id`, `_source_file`, `_ingested_at`, `_data_version` | lineage | |

## 2.3 Data normalisation across clouds

1. **Canonical model = FOCUS** (FinOps Open Cost & Usage Specification). AWS supports a FOCUS export directly; until the MVP adopts it fully, CUR 2.0 columns are mapped to FOCUS names with a versioned mapping table.
2. **Provider extensions** use the `x_` prefix and never leak into the core UI except via friendly names.
3. **Service taxonomy:** keep provider `ServiceName` and map to a cross-cloud `ServiceCategory` (Compute, Storage, Databases, Networking, AI/ML…) for later multi-cloud views.
4. **Resource identity:** canonical `ResourceId` (ARN for AWS; resource IDs for Azure/GCP later) plus `provider` and `account` for uniqueness.
5. **Currency:** store the billing currency (USD for most AWS accounts). If a customer is billed in another currency (e.g., ZAR invoicing), store `BillingCurrency` as delivered and convert to USD with the same FX service. **The internal reporting base is USD**; display currencies are derived.
6. **Tags:** keys normalised for matching (trim, case-folding configurable); original casing kept for display; virtual-tag rules applied at aggregation time.
7. **Time:** all timestamps UTC; tenant time zone applied at presentation and for "daily" boundaries in budgets/digests (documented: AWS charge days are UTC).

## 2.4 Data retention & archival

| Data | Hot location | Retention (default) | Archival / deletion |
|---|---|---|---|
| Raw CUR files | Spaces `raw/` | 13 months (configurable to 25) | Lifecycle rule deletes; superseded versions deleted after 30 days |
| Normalised Parquet | Spaces `norm/` | 25 months | Lifecycle rule; rebuildable from raw within the raw window |
| `cost_daily` aggregates | PostgreSQL (monthly partitions) | 25 months | Detach and drop old partitions monthly (optional export to Parquet archive) |
| `resource` (deleted items) | PostgreSQL | 13 months after deletion | Purge job |
| `resource_metric_daily` | PostgreSQL partitions | 6 months | Drop partitions |
| Recommendations & events | PostgreSQL | Life of tenant (events: 3 years) | Anonymised outcome features may be kept for model training **only with consent** (DPA clause), with no tenant identifiers |
| Notifications | PostgreSQL | 90 days | Purge |
| Reports / exports | Spaces | Reports 25 months; exports 7 days | Lifecycle rules |
| Audit log | PostgreSQL (+ monthly export to WORM-style storage) | ≥ 12 months online; 3 years archived | Tamper-evident chain |
| Backups | Managed DB PITR 7 days + daily snapshots 30 days; Spaces replicated to the secondary region | 30 days | Encrypted |
| Tenant deletion | All stores | Within 30 days of request | Deletion job with verification and certificate email; backups age out within 30 days |

---

# Part 3 — API Specification Outline

## 3.1 Style & conventions

| Topic | Decision |
|---|---|
| **Style** | **REST/JSON**, resource-oriented, OpenAPI 3.1 contract-first (generated clients for the SPA). GraphQL is not used for the MVP (simpler caching, authorisation and rate limiting); reconsider for the partner API in v2 |
| **Base URL** | `https://app.cloudoptics.io/api/v1` (domain TBC) |
| **Versioning** | URI major version (`/v1`). Additive changes are non-breaking; breaking changes → `/v2` with ≥ 6 months deprecation; `Deprecation` and `Sunset` headers |
| **Tenancy** | Tenant in path: `/v1/tenants/{tenantId}/…`. The server verifies membership on every call |
| **Authentication** | Web: session cookie (httpOnly, Secure, SameSite) + CSRF token header. **v1:** personal access tokens / API keys (Bearer, scoped, hashed at rest). OAuth2 client credentials for partners in v2 |
| **Authorisation** | RBAC per endpoint (Admin/Member/Viewer) documented in OpenAPI via `x-required-role` |
| **Money format** | Every amount returned as an object: `{"usd": 1234.56, "local": 1890000.00, "currency": "NGN", "fxRate": 1531.2, "fxDate": "2026-10-21", "fxBasis": "official", "estimated": false}`. The client switches USD / Local / Both without refetching |
| **Pagination** | Cursor-based: `?limit=50&cursor=…`; response `{"data": [...], "nextCursor": "...", "hasMore": true}` |
| **Filtering/sorting** | `filter[field]=value` (repeatable), `sort=-priorityScore` |
| **Errors** | RFC 9457 `application/problem+json`: `type`, `title`, `status`, `detail`, `instance`, `code`, `errors[]` (field-level), `requestId` |
| **Idempotency** | `Idempotency-Key` header on POSTs that create things or trigger jobs; keys stored 24h |
| **Rate limiting** | Per user: 300 req/min; per tenant: 1,000 req/min; exports: 10/hour; `RateLimit-Policy`/`RateLimit` headers (IETF draft) and `429` + `Retry-After` |
| **Caching** | `ETag` + `If-None-Match` on GETs; `Cache-Control: private, max-age=60` for cost queries; server cache in Valkey keyed by tenant + query + data version |
| **Async operations** | Long-running work returns `202 Accepted` with an operation resource (`/operations/{id}`) |
| **Timestamps** | ISO 8601 UTC; dates as `YYYY-MM-DD` |
| **Webhooks (outbound, S)** | Signed (HMAC-SHA256, timestamped) JSON events for alerts |

## 3.2 Endpoint groups

### Auth & session
| Method | Path | Purpose | Role |
|---|---|---|---|
| GET | `/v1/me` | Current user, memberships, preferences | any |
| PATCH | `/v1/me` | Update preferences (e.g., `preferredCurrencyView: usd\|local\|both`) | any |
| POST | `/v1/auth/mfa/totp/setup`, `/verify` | Enrol TOTP | any |
| POST | `/v1/auth/logout` | End session | any |
*(Login, sign-up and OIDC flows are handled by the auth provider; callback at `/auth/callback`.)*

### Tenants, users & settings
| Method | Path | Purpose | Role |
|---|---|---|---|
| POST | `/v1/tenants` | Create workspace | authenticated |
| GET/PATCH | `/v1/tenants/{t}` | Get/update workspace (name, currency, time zone, MFA policy) | Admin (PATCH) |
| GET | `/v1/tenants/{t}/members` | List members | Member+ |
| PATCH/DELETE | `/v1/tenants/{t}/members/{userId}` | Change role / remove | Admin |
| POST | `/v1/tenants/{t}/invites` | Invite user | Admin |
| GET/PUT | `/v1/tenants/{t}/settings` | Preferences (non-prod patterns, ignore tag, thresholds, virtual-tag rules) | Admin (PUT) |
| GET | `/v1/tenants/{t}/audit-log` | Query audit log | Admin |

### Cloud accounts & connections
| Method | Path | Purpose | Role |
|---|---|---|---|
| POST | `/v1/tenants/{t}/connections` | Start a connection → returns CFN quick-create URL + ExternalId | Admin |
| POST | `/v1/tenants/{t}/connections/{c}/validate` | Validate (async → operation) | Admin |
| GET | `/v1/tenants/{t}/connections` | List with health | Member+ |
| GET | `/v1/tenants/{t}/connections/{c}` | Detail: permission checks, sync status, access-cost estimate | Member+ |
| DELETE | `/v1/tenants/{t}/connections/{c}` | Disconnect (`?deleteData=true`) | Admin |
| GET | `/v1/tenants/{t}/accounts` | AWS accounts with coverage | Viewer+ |
| GET | `/v1/tenants/{t}/connections/{c}/stackset-template` | StackSet URL/template for member accounts | Admin |

### Resources & usage
| Method | Path | Purpose | Role |
|---|---|---|---|
| GET | `/v1/tenants/{t}/resources` | Inventory list (filters: type, account, region, env, tag) | Viewer+ |
| GET | `/v1/tenants/{t}/resources/{r}` | Resource detail + cost + recommendations | Viewer+ |
| GET | `/v1/tenants/{t}/resources/{r}/metrics` | Metric series (`metric`, `from`, `to`) | Viewer+ |

### Cost data
| Method | Path | Purpose | Role |
|---|---|---|---|
| GET | `/v1/tenants/{t}/costs/summary` | Overview KPIs: MTD, forecast, last month, deltas (usage vs FX), credits, data-as-of | Viewer+ |
| POST | `/v1/tenants/{t}/costs/query` | Explorer query (group by, filters, granularity, metric, date range) — POST because of complex bodies; cacheable by hash | Viewer+ |
| GET | `/v1/tenants/{t}/costs/what-changed` | Top drivers (period vs comparison) | Viewer+ |
| GET | `/v1/tenants/{t}/costs/forecast` | Forecast + FX scenario (`fxShiftPct`) | Viewer+ |
| GET | `/v1/tenants/{t}/costs/credits` | Credits burn, runway | Viewer+ |
| PUT | `/v1/tenants/{t}/costs/credits/balance` | Manual credit balance entry | Admin |
| GET/PATCH | `/v1/tenants/{t}/anomalies[/{id}]` | List / update status | Viewer+ / Member |
| GET | `/v1/tenants/{t}/dimensions/{dim}/values` | Values for filters (services, regions, tag keys/values) | Viewer+ |
| GET | `/v1/fx/rates?currency=NGN&from=&to=` | FX history | authenticated |

### Recommendations
| Method | Path | Purpose | Role |
|---|---|---|---|
| GET | `/v1/tenants/{t}/recommendations` | List (filters: type, risk, effort, status, owner, account, region, env; sort) + totals | Viewer+ |
| GET | `/v1/tenants/{t}/recommendations/summary` | Identified / in progress / realised totals; top 5 | Viewer+ |
| GET | `/v1/tenants/{t}/recommendations/{id}` | Detail incl. evidence, risk factors, fix, alternatives, activity | Viewer+ |
| PATCH | `/v1/tenants/{t}/recommendations/{id}` | Assign, status, due date, dismiss (reason), snooze | Member+ |
| POST | `/v1/tenants/{t}/recommendations/bulk` | Bulk assign/dismiss | Member+ |
| POST | `/v1/tenants/{t}/recommendations/{id}/comments` | Comment | Member+ |
| POST | `/v1/tenants/{t}/recommendations/{id}/events` | Client events (e.g., `viewed`, `fix_copied`) | Viewer+ |
| GET | `/v1/tenants/{t}/commitments/savings-plans` | SP advisory (options, payback, FX scenario) | Viewer+ |

### Budgets & alerts
| Method | Path | Purpose | Role |
|---|---|---|---|
| GET/POST | `/v1/tenants/{t}/budgets` | List / create | Viewer+ / Member+ |
| GET/PATCH/DELETE | `/v1/tenants/{t}/budgets/{id}` | Detail (status, evaluations) / update / delete | Viewer+ / Member+ |
| GET/PUT | `/v1/tenants/{t}/alert-settings` | Anomaly sensitivity, channel matrix | Viewer+ / Admin |
| POST | `/v1/tenants/{t}/alert-settings/test` | Send a test notification | Admin |
| GET | `/v1/tenants/{t}/notifications` | In-app notification feed | Viewer+ |

### Integrations
| Method | Path | Purpose | Role |
|---|---|---|---|
| GET | `/v1/tenants/{t}/integrations` | List installed | Member+ |
| GET | `/v1/tenants/{t}/integrations/slack/install` | Begin Slack OAuth | Admin |
| POST | `/v1/integrations/slack/events` | Slack interactivity endpoint (signed) | Slack |
| DELETE | `/v1/tenants/{t}/integrations/{id}` | Uninstall | Admin |

### Reports & exports
| Method | Path | Purpose | Role |
|---|---|---|---|
| GET | `/v1/tenants/{t}/reports` | Monthly report list | Viewer+ |
| GET | `/v1/tenants/{t}/reports/{month}` | Report (JSON for HTML view) + PDF link (signed URL, 15 min) | Viewer+ |
| PUT | `/v1/tenants/{t}/reports/settings` | Recipients, logo | Admin |
| POST | `/v1/tenants/{t}/exports` | Create CSV export (async) | Viewer+ |
| GET | `/v1/tenants/{t}/exports/{id}` | Status + signed download URL | Viewer+ |
| GET | `/v1/operations/{id}` | Async operation status | owner |

## 3.3 Example requests & responses (outline)

**Start a connection**
```http
POST /api/v1/tenants/7f2c…/connections
Idempotency-Key: 6b1d…
Content-Type: application/json

{ "type": "aws_management", "createCurExport": true }
```
```json
201 Created
{
  "id": "c_01J…",
  "status": "pending",
  "externalId": "co-9QxV…(48 chars)",
  "cloudFormationUrl": "https://console.aws.amazon.com/cloudformation/home?region=eu-west-2#/stacks/quickcreate?templateURL=…&param_ExternalId=…",
  "templateVersion": "2026.10.1",
  "permissionsDocUrl": "https://docs.cloudoptics.io/security/iam-policy"
}
```

**Cost summary (overview)**
```http
GET /api/v1/tenants/7f2c…/costs/summary?period=mtd
```
```json
200 OK
{
  "dataAsOf": "2026-10-22T05:10:00Z",
  "currency": "NGN",
  "mtd":        { "usd": 18420.11, "local": 28204000.00, "currency": "NGN", "fxRate": 1531.2, "fxDate": "2026-10-21", "fxBasis": "official", "estimated": false },
  "forecast":   { "usd": 26900.00, "local": 41189000.00, "currency": "NGN", "fxRate": 1531.2, "fxDate": "2026-10-21", "fxBasis": "official", "estimated": false, "range": { "lowUsd": 25100, "highUsd": 28400 } },
  "vsLastMonth": { "deltaUsd": 2140.55, "deltaPct": 12.1, "usageEffectLocal": 3290000, "fxEffectLocal": 2210000 },
  "credits":    { "appliedMtdUsd": 2100.00, "remainingUsd": 8200.00, "depletionDate": "2026-12-24" },
  "savingsRealisedMonthUsd": 1310.00
}
```
*(The FX rate shown is illustrative.)*

**List recommendations**
```http
GET /api/v1/tenants/7f2c…/recommendations?filter[status]=new&filter[risk]=low&sort=-priorityScore&limit=2
```
```json
200 OK
{
  "totals": { "openMonthly": {"usd": 6110.0, "local": 9355000, "currency": "NGN"}, "count": 37 },
  "data": [
    {
      "id": "rec_01J…",
      "type": "R1_unattached_ebs",
      "title": "Delete unattached EBS volume vol-0a1b…",
      "resource": { "id": "vol-0a1b…", "accountId": "123456789012", "region": "eu-west-1", "environment": "prod" },
      "savingsMonthly": { "usd": 38.40, "local": 58798.08, "currency": "NGN" },
      "risk": { "level": "low", "score": 22, "factors": [{ "code": "snapshot_exists", "points": -10, "text": "Recent snapshot exists" }] },
      "confidence": "high",
      "effort": "S",
      "status": "new",
      "owner": null,
      "priorityScore": 38.4
    }
  ],
  "nextCursor": "eyJw…",
  "hasMore": true
}
```

**Update a recommendation (assign)**
```http
PATCH /api/v1/tenants/7f2c…/recommendations/rec_01J…
{ "status": "assigned", "ownerUserId": "u_01H…", "dueDate": "2026-10-31" }
```
```json
200 OK  { "id": "rec_01J…", "status": "assigned", "owner": {"id":"u_01H…","name":"Wanjiru"}, "updatedAt": "2026-10-22T09:12:03Z" }
```

**Create a budget (local currency)**
```http
POST /api/v1/tenants/7f2c…/budgets
{
  "name": "Company total",
  "scope": { "type": "tenant" },
  "amount": 45000000, "currency": "NGN", "period": "monthly",
  "thresholds": [ {"kind":"actual","percent":50}, {"kind":"actual","percent":80}, {"kind":"actual","percent":100}, {"kind":"forecast","percent":100} ],
  "recipients": { "users": ["u_01H…"], "emails": ["finance@example.com"], "slackChannel": "C04…" }
}
```
```json
201 Created { "id": "bud_01J…", "status": "on_track", "amountUsdAtCurrentRate": 29388.71 }
```

**Error example**
```json
403 Forbidden
Content-Type: application/problem+json
{
  "type": "https://docs.cloudoptics.io/errors/insufficient-role",
  "title": "Insufficient role",
  "status": 403,
  "detail": "Changing connections requires the Admin role.",
  "code": "insufficient_role",
  "requestId": "req_01J…"
}
```

**Connection validation failure (operation result)**
```json
{
  "id": "op_01J…", "status": "failed",
  "result": {
    "checks": [
      { "permission": "sts:AssumeRole", "ok": true },
      { "permission": "bcm-data-exports:GetExport", "ok": false, "code": "export_not_found",
        "fix": "Create a CUR 2.0 export or re-run the stack with CreateCurExport=true." }
    ]
  }
}
```

## 3.4 Error handling (standard codes)

| HTTP | `code` examples | When |
|---|---|---|
| 400 | `validation_failed`, `invalid_filter` | Bad input (field errors in `errors[]`) |
| 401 | `unauthenticated`, `mfa_required` | No or expired session; MFA step-up needed |
| 403 | `insufficient_role`, `tenant_access_denied` | RBAC / tenant membership |
| 404 | `not_found` | Also returned for other tenants' resources (no existence leak) |
| 409 | `conflict`, `account_already_connected`, `idempotency_conflict` | State conflicts |
| 422 | `unprocessable`, `connection_invalid` | Semantically invalid |
| 429 | `rate_limited` | With `Retry-After` |
| 503 | `data_not_ready`, `maintenance` | E.g., first CUR not delivered yet; retry hints |
| 500 | `internal_error` | Generic; correlated by `requestId` |

---

## Appendix A — Decisions, Open Items & Hand-offs

| # | Decision / item | Status | Next phase |
|---|---|---|---|
| D1 | Modular monolith + workers on DOKS (LON1) | Proposed | 11a confirms tooling |
| D2 | PostgreSQL + Spaces/Parquet + DuckDB; ClickHouse when scale triggers are met | Proposed | 11a/11b |
| D3 | IAM Roles Anywhere broker; CA key protection approach | Proposed | 10 (security), 11b |
| D4 | FOCUS-aligned canonical model | Proposed | 11b |
| D5 | REST + OpenAPI, cookie sessions for web | Proposed | 11b |
| O1 | Queue/orchestration technology (Postgres queue vs Valkey vs Temporal) | Open | 11a |
| O2 | Auth provider (managed vs self-hosted OIDC) | Open | 11a |
| O3 | FX providers and rate basis per currency (PRD Q3) | Open | 11a + PM |
| O4 | Confirm DO NAT gateway / static egress option in LON1 | Open | 11a |
| O5 | Data location messaging for regulated African customers (UK hosting) | Open | 10 |

## Appendix B — Sources

- DigitalOcean regional availability (no African region) — https://docs.digitalocean.com/products/platform/availability-matrix
- DigitalOcean Managed Databases (PostgreSQL, MySQL, Valkey, Kafka, OpenSearch; PITR, standby nodes, VPC by default) — https://docs.digitalocean.com/products/data-services/ ; https://docs.digitalocean.com/reference/terraform/reference/resources/database_cluster/
- AWS IAM Roles Anywhere (trust anchors with AWS Private CA or an external CA) — https://docs.aws.amazon.com/rolesanywhere/latest/userguide/introduction.html ; https://aws.amazon.com/about-aws/whats-new/2022/07/aws-identity-access-management-iam-roles-anywhere-workloads-outside-aws
- External-CA trust anchor risk (detection guidance) — https://www.elastic.co/guide/en/security/current/aws-iam-roles-anywhere-trust-anchor-created-with-external-ca.html
