# 11b — Detailed Component Design Specification

**Product:** Cloudoptics by Cloudboosta · **Phase:** 11 · **Date:** 22 October 2026
**Inputs:** `09_architecture_data_and_api_design.md` (modules, data model, API), `06_mvp_prd.md` (FR/NFR IDs), `11a_technology_stack_and_platform_choices.md`
**Status:** Draft for engineering review

Each component lists: **purpose and responsibilities · key interfaces · data owned/accessed · dependencies · scalability and performance · failure modes and mitigations**. Components map to Phase 9 modules; all run in the modular monolith (`api`, `worker`, `scheduler`, `renderer` processes).

---

## 1. Cloud Connector Service (`connectors` module)

### 1.1 Purpose & responsibilities
- Create, validate, monitor and remove customer cloud connections (AWS now; Azure/GCP via the same interface later).
- Broker short-lived credentials into customer accounts (Roles Anywhere → broker role → customer role + ExternalId).
- Pull billing files, inventory, metrics, native recommendations and pricing; enforce rate limits and per-tenant AWS API cost budgets.
- Record sync runs and per-permission health.

### 1.2 Per-provider connectors

```python
class CloudProvider(Protocol):
    def validate(self, conn: Connection) -> ValidationResult: ...
    def list_accounts(self, conn: Connection) -> list[CloudAccountInfo]: ...
    def list_billing_files(self, conn: Connection, since: datetime) -> list[BillingFileRef]: ...
    def fetch_billing_file(self, conn: Connection, ref: BillingFileRef) -> StreamingBody: ...
    def scan_inventory(
        self, acct: CloudAccount, region: str, types: set[ResourceType]
    ) -> Iterator[ResourceRecord]: ...
    def fetch_metrics(
        self, acct: CloudAccount, region: str, queries: list[MetricQuery]
    ) -> list[MetricSeries]: ...
    def fetch_native_recommendations(self, acct: CloudAccount) -> list[NativeRecommendation]: ...
    def cost_summary(
        self, conn: Connection, period: DateRange
    ) -> CostSummary: ...  # Cost Explorer (budgeted)
```
- **AwsProvider (MVP):** implements all methods using boto3 clients built from a `SessionFactory`.
- **AzureProvider / GcpProvider (v2):** same protocol; exports in FOCUS format.

### 1.3 Authentication flows

| Provider | Flow |
|---|---|
| **AWS (MVP)** | 1) The worker pod holds a short-lived X.509 cert (issued by AWS Private CA, ≤ 24h, renewed by a sidecar/init job). 2) `rolesanywhere:CreateSession` → broker role credentials (≤ 1h). 3) `sts:AssumeRole(RoleArn=customer, ExternalId=decrypt(conn.external_id_enc), RoleSessionName=co-{tenant}-{job}, DurationSeconds=3600)`. 4) Customer credentials are cached **in memory per job** only. Optional: customer trust policy restricts `aws:SourceIp` to our egress IPs |
| **Azure (v2)** | Multi-tenant Entra ID app + admin consent; federated credential (workload identity) → Cost Management Reader + Reader roles |
| **GCP (v2)** | Workload Identity Federation → service account impersonation with `billing.viewer` / BigQuery data viewer on the export dataset |

**Connection setup (AWS):**
1. `POST /connections` generates the ExternalId (48 chars, CSPRNG), stores it envelope-encrypted, and returns the CloudFormation quick-create URL (template version pinned, hosted on Spaces/CDN with an integrity hash).
2. The stack outputs the RoleArn. The UI polls `validate`; optionally the template's custom resource calls back to `POST /connections/{id}/callback`. The callback is signed with a per-connection token and carries no secrets.

### 1.4 Data pulled

| Data | API | Cadence | Notes |
|---|---|---|---|
| Billing (CUR 2.0) | S3 `ListObjectsV2`/`GetObject` on export bucket/prefix; BCM Data Exports `GetExport` | Manifest poll every 6h | Streamed to Spaces `raw/` without full buffering (multipart upload) |
| MTD summary (early value + cross-check) | Cost Explorer `GetCostAndUsage` | Onboarding (≤ 20 calls) + daily (≤ 10) | Paid API: counts against tenant budget |
| Accounts | Organizations `ListAccounts` | Every 12h | — |
| Inventory | EC2 (`DescribeInstances`, `DescribeVolumes`, `DescribeSnapshots(OwnerIds=self)`, `DescribeAddresses`, `DescribeImages(Owners=self)`), ELBv2/ELB, RDS, Lambda, S3 (bucket list + location + tags + lifecycle), Auto Scaling | Every 12h per account × enabled region | Paginated; concurrency ≤ 4 regions per account |
| Metrics | CloudWatch `GetMetricData` (batched, ≤ 500 queries per call) | Daily, candidates only, 1h period, 14–30 days (backfill once) | Paid per metric: budgeted |
| Native recs | Cost Optimization Hub `ListRecommendations`; Compute Optimizer `GetEC2InstanceRecommendations`, `GetEBSVolumeRecommendations`, `GetEnrollmentStatus` | Daily | Free (enhanced metrics optional) |
| Commitments | Savings Plans `DescribeSavingsPlans`; CE `GetSavingsPlansCoverage/Utilization/PurchaseRecommendation` | Daily | CE calls budgeted |
| Prices | Price List bulk offer files (global job) | Weekly | Stored in `price_catalog` + Spaces cache |

### 1.5 Polling, backoff & error handling
- **Scheduling:** periodic tasks enqueue per-connection jobs with jitter (spread across the hour) and Procrastinate queueing locks (`conn:{id}:{job_type}`) to prevent overlap.
- **Rate limiting:**
  - Token buckets in Valkey keyed `t:{tenant}:aws:{service}`.
  - botocore `adaptive` retry mode (max 5 attempts) + our backoff (exponential with full jitter, base 1s, cap 60s) for `Throttling`, `RequestLimitExceeded` and 5xx.
- **Customer API cost budget:**
  - Each paid call increments `sync_run.est_customer_api_cost_usd` and a monthly counter.
  - At 80% of budget, non-essential calls are deferred (e.g., metrics for low-value candidates).
  - At 100%, paid calls are suspended until the next month, with an internal alert and a UI note.
- **Error taxonomy → handling:**

| Error | Example | Handling |
|---|---|---|
| Auth: role missing / trust mismatch | `AccessDenied` on AssumeRole | Connection → **Failed**; no retries beyond 3; email admins; guided fix |
| Permission missing (partial) | `AccessDenied` on `compute-optimizer:Get*` | Mark that permission ✗; continue other data types (**Degraded**) |
| Data missing | Export not found / no files yet | **Pending data** state; retry on next poll; UI explains the 24h delay |
| Throttling / transient | `ThrottlingException`, 5xx, timeouts | Retry with backoff; reschedule job |
| Opt-in required | Compute Optimizer not enrolled | Record and show guidance; skip |
| Region disabled | `UnauthorizedOperation` in opt-in region | Mark region skipped |
| Corrupt file | Parquet read error | Quarantine file; alert; retry next delivery |

### 1.6 Data owned / accessed
- **Owns:** `cloud_connection`, `cloud_account`, `permission_check`, `sync_run`, `billing_file` (registry).
- **Writes:** raw objects in Spaces `raw/`; `resource` and `resource_metric_daily` via the `inventory` module interface; `native_recommendation` staging.
- **Reads:** tenant settings (excluded accounts/regions), secrets (broker config).

### 1.7 Dependencies
AWS (Roles Anywhere, STS, customer APIs), Secrets Manager/KMS (ExternalId decryption), Valkey (rate limits), Postgres (jobs, state), Spaces, `audit` (connection changes), `notifications` (failure alerts), `ingestion` (event `billing.files_landed`).

### 1.8 Scalability & performance
- Work is sharded per (connection, account, region); worker autoscaling via KEDA on queue depth.
- Streaming transfers (no full file in memory); multipart uploads to Spaces (chunk 16–64 MB).
- Target capacity: 1,000 accounts × 15 regions scanned within 2h of schedule with 10–20 worker replicas.

### 1.9 Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| Broker/CA outage (Roles Anywhere unavailable) | Alert; jobs back off and retry; data freshness SLO absorbs ≤ 12h; runbook for CA/trust-anchor issues (11g) |
| Customer deletes role/export | Detected on validation; Failed/Degraded state; admin email; data retained until disconnect |
| AWS API changes / new instance types | Monthly botocore upgrades; contract tests against recorded responses; unknown fields tolerated |
| Cost budget exhausted | Graceful degradation (skip metrics), UI note, internal alert |
| Duplicate/overlapping jobs | Queueing locks + idempotent writes (file registry by key + checksum) |

---

## 2. Ingestion & Normalisation Pipeline (`ingestion` module)

### 2.1 Purpose & responsibilities
- Land raw billing files immutably.
- Normalise to the FOCUS-aligned schema (Parquet).
- Run data-quality checks.
- Build daily aggregates in Postgres and reconcile to invoice totals.
- Handle restatements idempotently and publish freshness state.

### 2.2 Raw data landing
- Path: `raw/tenant={t}/conn={c}/period=YYYY-MM/version={exportExecutionId}/{original_key}`.
- Write-once: no overwrites. A new AWS delivery creates a new `version`.
- `billing_file` row per object: checksum (SHA-256), size, rows (if known), status `landed`.
- Manifest-driven: the export manifest lists the files that make up a complete delivery. A period is processed only when all manifest files have landed (**completeness check**).

### 2.3 Normalisation logic
- A DuckDB job reads all Parquet files for (tenant, connection, period, version) and maps CUR 2.0 → FOCUS columns using a **versioned mapping** (`mapping_version`).
  - Derived fields: `ChargeCategory`, `PricingCategory`, `CommitmentDiscount*`, `x_CreditType`, `RegionId` incl. Local Zones, `ServiceCategory`.
  - Tags: `resource_tags` map → `Tags` map, with normalised keys plus original casing.
  - Cost metrics: `BilledCost` (unblended), `EffectiveCost` (amortised: SP/RI effective cost + amortised upfront), `ListCost`, net cost (after credits/discounts).
- Output: `norm/tenant={t}/period=YYYY-MM/version={v}/part-*.parquet` (zstd, row groups ~128 MB, sorted by `ChargePeriodStart, SubAccountId, ServiceName`).
- If the customer exports **FOCUS 1.x directly** (AWS supports a FOCUS export), use the identity mapping plus extensions.

### 2.4 Data quality checks (block vs warn)

| Check | Rule | Severity |
|---|---|---|
| Schema | Required columns present with expected types | Block |
| Completeness | All manifest files landed; row count matches manifest (if provided) | Block |
| Nulls | `BilledCost`, `ChargePeriodStart`, `SubAccountId` null ratio = 0 | Block |
| Period bounds | Charge periods within billing period (± 1 day for timezone edges) | Warn |
| Negative costs | Only allowed for Credit/Refund/Discount categories | Warn |
| Duplicates | Duplicate line-item IDs within a version | Block |
| Drift | Daily total deviates > 50% from the previous version for the same days (unexpected restatement) | Warn + alert |
| Reconciliation | Sum of costs vs invoice-level totals (or CE `GetCostAndUsage` for closed months) within ±1% | Warn (banner) + internal alert |

Blocking failures leave the previous good version active (dashboards unaffected) and raise an alert.

### 2.5 Idempotency & deduplication
- **Unit of work:** (tenant, connection, billing_period, export_version). Each stage records completion in `billing_file`/`ingest_batch` tables.
- **Aggregation upsert:** within a transaction, delete `cost_daily` rows for (tenant, connection accounts, period) **where `data_version` ≠ new version**, then insert the new aggregates. This is atomic, so readers see either the old or the new version.
- Superseded raw/normalised versions are marked `superseded` and deleted after 30 days.
- Re-running any stage with the same inputs produces the same outputs (deterministic sorting, no `now()` in data columns).

### 2.6 Aggregation
- `cost_daily` grain: tenant × usage_date × account × service × region × usage_type × charge_category × tag_set_hash (Phase 9 §2.2).
- Also: `cost_monthly_service` materialised summary for the Overview, and `credit_daily` for the credits runway.
- After aggregation: emit `cost.updated(tenant, periods)` → triggers analysis, budgets and the cache-version bump (invalidates the query cache by incrementing `t:{tenant}:cost_ver`).

### 2.7 Interfaces
- **Consumes:** `billing.files_landed` event (from connectors).
- **Produces:** `cost.updated`, `ingest.failed`.
- **Exposes:** `CostQueryService` (internal) for the API: `summary()`, `query(spec)`, `what_changed()`, `line_items(spec, limit)` (DuckDB on Parquet).

### 2.8 Data owned / accessed
Owns `billing_file` status, `ingest_batch`, `cost_daily`, `tag_set`, `cost_monthly_service`, `credit_daily`, `norm/` objects. Reads `raw/`, `fx_rate` (for non-USD billing currencies).

### 2.9 Scalability & performance
- DuckDB memory limit per job (e.g., 2–4 GB), spill to local disk; job concurrency per node bounded.
- Target: ≥ 5M line items per tenant-month in ≤ 30 min (NFR-PERF-04); typical tenant < 5 min.
- Partitioned `cost_daily` (monthly) with indexes `(tenant_id, usage_date)`, `(tenant_id, service_code, usage_date)`, `(tenant_id, account_id, usage_date)`.

### 2.10 Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| Partial delivery | Completeness check waits; retries next poll; alert after 36h |
| Restatement mid-month (common at month end) | New version replaces old atomically |
| OOM on huge files | Memory limits + spill; split by file; dedicated large-job queue/node pool |
| Mapping bug | Mapping versioned; reprocess from `raw/` with a fixed version (backfill job); golden-file tests |
| Reconciliation mismatch | Banner + investigation runbook (11g) |

---

## 3. Analysis & Recommendation Engine (`analysis` + `recommendations` modules)

### 3.1 Purpose & responsibilities
Generate explainable, risk-scored recommendations (R1–R8), anomalies, forecasts, "what changed" and credits runway. Manage the recommendation lifecycle, verification and outcome data.

### 3.2 Engine framework

```python
@dataclass(frozen=True)
class EngineContext:
    tenant: TenantSettings  # thresholds, non-prod patterns, ignore tags, currency
    inventory: InventorySnapshot  # resources for the tenant (by account/region)
    metrics: MetricStore  # daily summaries (avg/p95/max), coverage
    prices: PriceCatalog  # region/SKU on-demand prices
    rates: EffectiveRateProvider  # tenant effective rates from CUR (discount-aware)
    native: NativeRecs  # COH / Compute Optimizer findings
    now: date


class Engine(Protocol):
    id: str  # "R5_ec2_rightsizing"
    version: str  # "1.3.0"

    def run(self, ctx: EngineContext) -> list[Candidate]: ...
```
- Engines are **pure functions** (no I/O): a deterministic output for given inputs, which makes them easy to unit test with fixtures.
- The **Engine runner** loads the context, runs the registered engines, passes candidates to the **Scorer**, then **upserts** recommendations by `fingerprint`.
- Inputs are snapshotted (hash stored on the recommendation) for reproducibility and the M2 dataset.

### 3.3 Modules

| Module | Logic summary (thresholds per PRD §4.4) | Key inputs |
|---|---|---|
| **Right-sizing (R5)** | Merge Compute Optimizer options with own checks: p95/max CPU over 14–30 days, memory if present, network, burst credits; project utilisation on the target size (vCPU ratio); accept only if projected p95 ≤ 60% and max ≤ 85%. Prefer same family, smaller size; show Graviton as an alternative (R6 overlap via dedupe) | Inventory, metrics, Compute Optimizer, prices |
| **Idle/underutilised (R1, R1b, R2, R4, R10)** | Unattached EBS ≥ 7 days (High confidence ≥ 32); idle attached EBS < 1 op/day for 14 days; snapshots > 90 days, orphaned, not used by AMIs/Backup/DLM or retain tags; unassociated EIPs ≥ 3 days (High ≥ 7) incl. CUR `PublicIPv4:IdleAddress`; LBs ≥ 7 days old with no listeners/healthy targets or low requests/flows; EC2 idle (max CPU < 5% and net < 5 MB/day for 14 days; p95-only = low-utilisation candidate); RDS zero connections for 7 days plus low CPU/IOPS; idle NAT (hours billed, 0 bytes for 30 days). Exact rules: PRD FR-ANA-01 to -04b, -14 | Inventory, metrics, CUR |
| **Modernisation (R3, R6, R6b)** | gp2→gp3 (EBS and RDS, plus io1 that fits gp3) with IOPS/throughput adjustments; previous-generation mapping table (versioned) with price deltas and region availability; Extended Support charges from CUR | Inventory, prices, CUR |
| **Scheduling (R7)** | Non-prod detection (tags/regex) + 24/7 running; hours saved under the default schedule | Inventory, metrics, settings |
| **Pricing model (R8)** | Savings Plan advisory from CE recommendations + own simulation on the CUR hourly baseline after netting R4/R5/R7; SP/RI × term × payment matrix; ladder; payback; FX stress test (20% / 40%). **Advisory only** (PRD FR-ANA-10) | CE SP APIs, FX |
| **Architectural hints (v1)** | Storage class/lifecycle (S3 Intelligent-Tiering candidates from storage-class mix and access patterns via CUR), NAT Gateway data-processing hotspots (CUR usage types), cross-AZ/inter-region data transfer patterns, log retention, region placement (af-south-1 vs EU vs Local Zones) | CUR aggregates, prices |
| **Anomalies** | Per (account, service) daily: baseline = median of the same weekday over 4 weeks; MAD threshold; anomaly if cost > baseline + max(3×MAD, 30%) and excess ≥ tenant minimum; suppression after "Expected" | `cost_daily` |
| **Forecast** | Month-end: MTD + 7-day average × remaining days + known monthly fixed charges; 3-month: bounded linear trend ± range | `cost_daily` |
| **What changed** | Contribution analysis by dimension; top 3; sentence templates | `cost_daily` |
| **Credits runway** | 30-day average credit burn; manual balance; depletion date | `credit_daily`, balance entries |

### 3.4 Scoring & ranking (impact vs risk)
- **Savings:** `cost_now − cost_after`, using the **tenant effective rate** from the CUR when available (accounts for SP/RI coverage and discounts), otherwise list price (`savings_basis = list_price`, labelled "estimated").
- **Risk score (0–100):** additive, explainable factors (PRD FR-REC-03) → Low/Med/High; every factor stored as `{code, points, text}`.
- **Confidence:** data coverage (PRD FR-REC-05).
- **Priority** = savings × confidence factor × risk factor × effort factor (PRD FR-REC-04).
- **De-duplication:** group candidates by resource; the primary is the highest priority; others are linked as alternatives; totals count only primaries.
- **Stability:** a recommendation's savings are updated only if they change > 10% (avoids churn); `last_evaluated_at` always updated.
- **Expiry:** if the condition no longer holds (e.g., volume attached again) → status `expired` (not counted as savings).

### 3.5 Configurable rules/policies per tenant
- **Settings:** idle thresholds (bounded ranges), lookback days (14–30), non-prod patterns, ignore tag, excluded accounts/regions, anomaly minimums, default schedule hours, risk appetite (e.g., hide High risk by default).
- Stored in `tenant_settings` (JSON schema validated) and versioned; the version is stored with recommendations.
- Guardrails: values outside safe bounds are rejected (e.g., the idle CPU threshold cannot exceed 20%).

### 3.6 Lifecycle & verification
- State machine: `new → assigned → in_progress → done → verified | not_verified`; side states `dismissed`, `snoozed`, `expired`, `resolved_externally`. Transitions are validated server-side and each emits a `recommendation_event`.
- **Verifier** (every 12h):
  - Detects state change (resource deleted / type changed / schedule observed via stopped-hours pattern) → `verified` (change detected).
  - After ≥ 7 days of post-change CUR → computes `realised_usd_month`.
  - Implemented without the user marking done → auto-verified and credited (`resolved_externally` if no prior status).

### 3.7 Interfaces
- **Consumes:** `cost.updated`, `inventory.refreshed`, `metrics.refreshed`, `settings.changed`.
- **Produces:** `recommendations.updated`, `anomaly.detected`, `forecast.updated`.
- **API:** recommendations, commitments, anomalies, forecast and what-changed endpoints (Phase 9 §3.2).

### 3.8 Data owned / accessed
Owns `recommendation`, `recommendation_event`, `verified_saving`, `anomaly`, `forecast_snapshot`, `engine_registry`. Reads inventory, metrics, `cost_daily`, prices, FX, settings.

### 3.9 Scalability & performance
- Per-tenant runs serialised (queueing lock `tenant:{id}:analysis`), different tenants run in parallel.
- Target ≤ 15 min for 5,000 resources (typically < 2 min).
- Engines vectorised where useful (DuckDB/Arrow over metric tables); context loaded once per run.

### 3.10 Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| Wrong/unsafe recommendation | Conservative thresholds; risk scoring; golden-dataset regression tests per engine version; feature-flag per engine; "report a problem" → fast disable |
| Missing data (metrics gaps) | Confidence lowered; candidates suppressed below minimum coverage (e.g., < 50% of days) |
| Price catalogue stale | Weekly refresh + staleness alert; fall back to effective rates |
| Engine exception | Isolated per engine (others still run); error recorded; alert |
| Recommendation churn | Fingerprints + stability rule + snooze |

---

## 4. API Layer (`api` process)

### 4.1 Purpose & responsibilities
Serve the versioned REST API (Phase 9 Part 3) for the SPA (and later public API clients). Enforce authentication, tenant scoping, RBAC, validation, rate limits, caching and auditing.

### 4.2 Design
- **FastAPI routers per module** (`/v1/tenants/{t}/costs`, `/recommendations`, …). Pydantic request/response models generate the OpenAPI 3.1 spec, which is published as a build artefact and used to generate the TS client.
- **Middleware/dependency chain:**
  1. Request ID + OTel span.
  2. Authentication: session cookie validated with the auth provider (JWKS cached), mapped to `app_user`.
  3. CSRF check (state-changing).
  4. Tenant resolution: path `{t}` → membership lookup (cached 60s, invalidated on change).
  5. RBAC: `require_role("member")` dependency per route.
  6. DB session with `SET LOCAL app.tenant_id` (RLS).
  7. Rate limiting (Valkey token buckets per user and tenant).
  8. Audit hook for mutating endpoints.
  9. Error mapping to RFC 9457 problem+json.

### 4.3 Endpoints
Accounts & projects (workspaces), connections, resources & usage, cost data, recommendations, budgets & alerts, auth & user management, integrations, reports/exports: as specified in Phase 9 §3.2.
*Projects:* the MVP models "projects/teams" through tags and virtual-tag rules (`/settings` → `virtualTagRules`). A first-class `project` resource is a v1 candidate if research demands it.

### 4.4 Authentication/authorisation model
- **Web:** session cookie issued after the OIDC code flow with the auth provider (AuthKit). The backend stores the session (id → user, expiry) in Valkey with a signed cookie; idle/absolute timeouts per PRD.
- **API tokens (v1):** `Authorization: Bearer co_pat_…`; hashed (SHA-256 + pepper) in DB; scopes such as `costs:read`, `recommendations:write`; tenant-bound; expiry ≤ 1 year.
- **RBAC:** role → permission set in code (`Permission.RECS_WRITE` …); checked per route; tests generated from the OpenAPI `x-required-role` annotations.
- **Defence in depth:** RLS in Postgres; 404 for foreign resources.

### 4.5 Data owned / accessed
No tables of its own; it calls module services. Owns API-level caches in Valkey (`t:{tenant}:q:{hash}:{cost_ver}`).

### 4.6 Dependencies
Auth provider (JWKS/session), Postgres, Valkey, Spaces (signed URLs), module services.

### 4.7 Scalability & performance
- Stateless pods; HPA on CPU/RPS.
- Cost queries hit pre-aggregated tables with a query cache (TTL 10 min, invalidated by data-version bump).
- `ETag` for GETs; gzip/br compression; pagination limits (max 200).
- Statement timeout 10s for API DB sessions; heavier work returns `202` + operation.

### 4.8 Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| Auth provider outage | Existing sessions continue (local session store); new logins fail gracefully with a status message; alert |
| DB failover | Managed failover (< 1–2 min); connection retry with backoff; readiness probes |
| Slow query storm | Timeouts, cache, rate limits, circuit breaker on line-item queries |
| Breaking API change | OpenAPI diff check in CI; `/v1` stability policy |

---

## 5. Frontend Application (`apps/web`)

### 5.1 App structure

```
apps/web/src/
  app/            (router, providers: QueryClient, Intl, Theme, Auth)
  routes/         (overview, costs/{explorer,anomalies,credits}, recommendations/{list,detail,tracker,commitments},
                   budgets/{list,detail,new}, reports/{monthly,forecast,exports}, learn, settings/{…}, onboarding/*)
  features/       (domain logic + hooks per area: costs, recommendations, budgets, fx, connections)
  components/     (design-system components: MoneyValue, DeltaChip, KpiTile, DataTable, Chart*, FilterBar, Badge…)
  api/            (generated OpenAPI types + typed fetch client + query keys)
  lib/            (formatting, currency, dates, analytics, feature flags)
  styles/         (tokens → CSS variables; Tailwind config)
  print/          (report print routes for PDF rendering)
```

### 5.2 Key views & data requirements

| View | API calls (Phase 9 §3.2) | Notes |
|---|---|---|
| Overview | `costs/summary`, `costs/what-changed`, `recommendations/summary` (top 5), `budgets?status=at_risk`, `anomalies?status=open`, `costs/credits`, `connections` (health) | Parallel queries; skeletons per card; first-run state |
| Costs Explorer | `POST costs/query` (debounced on filter changes), `dimensions/{dim}/values` | URL search params hold filters; chart + table share the query result |
| Recommendation list | `recommendations` (cursor pagination), `recommendations/summary` | Optimistic updates for assign/dismiss |
| Recommendation detail | `recommendations/{id}`, `resources/{r}/metrics`, events POST (`viewed`, `fix_copied`) | Charts lazy-loaded |
| Budgets | `budgets`, `budgets/{id}`, `alert-settings` | Live FX conversion uses `fx/rates?latest` |
| Onboarding | `tenants`, `connections`, `connections/{id}/validate` (poll operation), `invites`, `integrations/slack/install` | Resumable wizard state (server-side) |
| Settings | `members`, `invites`, `connections`, `settings`, `audit-log`, `integrations` | Admin-only routes guarded |

### 5.3 State management
- **Server state:** TanStack Query.
  - Query keys include `tenantId` and relevant filters.
  - `staleTime` 60s for cost data; invalidation on mutations and on `dataAsOf` change (poll `/costs/summary` every 5 min while the tab is visible).
- **URL state:** filters, date range, grouping and sort via TanStack Router search params (shareable, back-button safe).
- **Client state (Zustand):** currency view (`usd | local | both`, persisted via `PATCH /me`), theme, sidebar state.
- **Money rendering:** `MoneyValue` reads the currency view and renders `usd`/`local`/both from the API money objects. No client-side FX maths except budget-form previews.

### 5.4 Integration with backend APIs
- Generated types (`openapi-typescript`) + `openapi-fetch` client with interceptors: CSRF header, request ID, 401 → re-auth redirect, 403 → permission screen, 429 → retry-after toast.
- Errors are parsed as RFC 9457 objects and mapped to user-friendly messages by `code`.

### 5.5 Error handling & loading states
- **Route-level error boundaries** (friendly message, request ID, retry).
- **Per-component skeletons;** no full-page spinners.
- **Empty states** with next actions.
- **"Data as of" component** on all data views; banners for degraded connections, reconciliation and first-load.
- **Offline/slow network:** show cached data with a "showing data from 10:05" badge; retry with backoff; requests time out after 20s with a message.
- **Accessibility:** focus management on route change; `aria-live` for async updates; chart table fallbacks.

### 5.6 Scalability & performance
Code-split per route; charts lazy-loaded; table virtualisation; performance budgets enforced in CI (bundle size, Lighthouse CI on key routes with throttled network).

### 5.7 Failure modes
API unavailable (status page link + cached data); partial data (per-card errors don't break the page); feature-flagged features hidden on flag-service failure (safe defaults).

---

## 6. Background Jobs & Schedulers (`worker`, `scheduler` processes)

### 6.1 Periodic jobs

| Job | Schedule | Queue | Lock | Timeout |
|---|---|---|---|---|
| `connection.validate` | every 6h per connection (+ on demand) | `connectors` | `conn:{id}:validate` | 2 min |
| `cur.poll` | every 6h per connection (jittered) | `connectors` | `conn:{id}:cur` | 10 min |
| `cur.ingest` → `cost.normalise` → `cost.aggregate` → `cost.reconcile` | event-driven | `ingestion` (large-job pool for big tenants) | `tenant:{id}:cost` | 45 min |
| `inventory.scan` | every 12h per account (jittered) | `connectors` | `acct:{id}:inventory` | 20 min |
| `metrics.pull` | daily per account | `connectors` | `acct:{id}:metrics` | 30 min |
| `native.recs.pull` | daily per account | `connectors` | `acct:{id}:native` | 10 min |
| `analysis.run` | event-driven + daily 02:00 tenant TZ | `analysis` | `tenant:{id}:analysis` | 15 min |
| `recs.verify` | every 12h | `analysis` | `tenant:{id}:verify` | 10 min |
| `budgets.evaluate` | after `cost.updated` + hourly safety net | `analysis` | `tenant:{id}:budgets` | 5 min |
| `fx.fetch` | daily 06:00 UTC + retries (every 2h until success) | `platform` | `global:fx` | 5 min |
| `prices.refresh` | weekly (Sun 01:00 UTC) | `platform` | `global:prices` | 60 min |
| `notify.dispatch` | continuous | `notifications` | per (tenant, channel) rate limits | 1 min |
| `digest.weekly` | Mon 07:00 tenant TZ | `reporting` | `tenant:{id}:digest` | 5 min |
| `report.monthly` | 3rd working day 06:00 tenant TZ | `reporting` (renderer) | `tenant:{id}:report:{month}` | 10 min |
| `export.generate` | on demand | `reporting` | `user:{id}:export` (max 2 concurrent) | 30 min |
| `retention.purge`, `tenant.delete` | daily 03:00 UTC / on request | `platform` | `global:retention` / `tenant:{id}:delete` | 60 min |

### 6.2 Orchestration design
- **Procrastinate** queues: `connectors`, `ingestion`, `analysis`, `notifications`, `reporting`, `platform`.
- Separate worker Deployments per queue group (so ingestion spikes don't delay notifications); `renderer` Deployment includes Chromium.
- **Event chaining:** a job's final step enqueues downstream jobs **in the same DB transaction** as its state update (transactional outbox via Procrastinate), so there are no lost events.
- **Scheduler:** Procrastinate periodic tasks run by a single `scheduler` replica (leader lock); per-tenant schedules are expanded by a dispatcher job every 10 min (enqueues due work with jitter).

### 6.3 Concurrency, backpressure & failure handling
- **Concurrency:**
  - Per-queue worker concurrency (e.g., connectors 16 async tasks per pod; ingestion 2 per pod due to memory).
  - Per-tenant locks; global caps on AWS calls per service.
- **Backpressure:**
  - KEDA scales workers on queue depth/age, up to max replicas.
  - When at max, the dispatcher reduces the frequency of non-critical jobs (metrics pulls for low-value candidates) and alerts.
  - Fair scheduling: the dispatcher enqueues round-robin across tenants; a large tenant's backfill runs on the large-job pool.
- **Retries:** exponential backoff with jitter (max 5); non-retryable errors (auth, validation) fail fast.
- **Dead-letter:** failed jobs are kept with error context; a DLQ dashboard and alert; a replay tool (admin CLI) after fixes.
- **Idempotency:** job keys `(type, scope, data_version)`; handlers check completion markers before acting.
- **Timeouts:** per job type (table above); stuck jobs reclaimed by Procrastinate's stalled-job detection.

### 6.4 Data owned
Procrastinate tables (`procrastinate_jobs`, `procrastinate_periodic_defers`, events) in a dedicated `jobs` schema; `sync_run` records.

### 6.5 Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| Postgres overload from queue | Dedicated schema; indexes; connection limits; monitor; move to a separate DB cluster if needed (v1) |
| Scheduler pod crash | Leader lock with TTL; Kubernetes restart; dispatcher catches up on missed windows |
| Thundering herd at the top of the hour | Jitter on all per-tenant schedules |
| Long-running job blocks tenant | Timeouts + lock TTL; large-job pool |

---

## 7. Admin & Multi-Tenancy Services (`identity`, `audit`, `platform` modules)

### 7.1 Tenant provisioning
- `POST /tenants` (after sign-up) runs a transaction:
  1. Create `tenant`, `membership(admin)`, default `tenant_settings`, default alert settings and the audit entry.
  2. Enqueue `tenant.bootstrap` (welcome email, analytics identify).
- Default currency from the country; the tenant time zone from the browser/country.
- Limits by plan (accounts, users) are enforced by a `plan_limits` service (feature flags/entitlements).

### 7.2 Isolation model
- Pooled tenancy with `tenant_id` + **RLS** on all tenant tables, plus tenant-prefixed object paths, namespaced cache keys and tenant-scoped DuckDB queries (Phase 9 §1.5).
- **Tenant placement table** (`tenant_placement`: db cluster, storage bucket) to support dedicated isolation for regulated tenants later.
- **Partner hierarchy:** `parent_tenant_id`. Partner users get memberships in child tenants via delegated-admin grants (v1).

### 7.3 RBAC & permissions
- MVP roles: Admin / Member / Viewer.
- Permissions catalogue (examples): `connections.manage`, `members.manage`, `settings.manage`, `costs.read`, `recs.read`, `recs.write`, `budgets.write`, `reports.read`, `audit.read`, `exports.create`.
- Role → permission mapping in code (versioned), with unit tests for each endpoint.
- v1 adds account-scoped roles (a `membership_scope` table restricting accounts) and custom roles.

### 7.4 Audit logging
- `audit` module API: `audit.record(actor, action, target, before, after, ctx)`, called by mutating services inside the same transaction.
- Append-only table with hash chaining (`row_hash = sha256(prev_hash || canonical_json(row))`); a daily verification job; monthly export to write-once storage.
- Staff break-glass:
  - An internal admin tool requires a reason and ticket ID, and grants time-boxed access (≤ 2h).
  - Every access is recorded with `actor_type = staff` and is visible to the tenant.

### 7.4a Platform admin console (internal)
- A separate, staff-only web area at `/admin`. It requires a platform-admin role, SSO plus MFA, and is reachable only through the admin VPN or IP allow-list.
- **MVP pages:**
  - **Exchange rates:** manual rate entry with effective date, basis and reason. Shows history, the reference-feed comparison, staleness indicators, and second-admin approval for changes over 10% (PRD FR-FX-06/07).
  - **Tenants:** list, plan and status.
  - **Break-glass access** (§7.4).
  - **Job and DLQ overview.**
- Every action goes to the platform audit log. A rate change also bumps the FX cache version, so all tenants pick it up within 5 minutes.

### 7.5 Configuration management per tenant
- `tenant_settings` (JSONB, validated by a JSON Schema/Pydantic model; version field; change history in audit).
- Entitlements (`plan`, limits, feature flags) from the billing/plan service (v1); PostHog flags for gradual rollouts (tenant-level targeting).
- Platform configuration via environment + secrets manager; no tenant secrets in config maps.

### 7.6 Interfaces
`/tenants`, `/members`, `/invites`, `/settings`, `/audit-log` endpoints; internal `TenantContext` dependency; `Entitlements.check(tenant, feature)`.

### 7.7 Scalability & performance
Membership/role lookups are cached in Valkey (60s TTL; busted on change). Audit inserts are lightweight; audit queries are paginated with indexes `(tenant_id, occurred_at DESC)`.

### 7.8 Failure modes & mitigations

| Failure | Mitigation |
|---|---|
| RLS misconfiguration on a new table | Migration lint: CI fails if a table with `tenant_id` lacks RLS policies; cross-tenant test suite |
| Removed user retains access via cached role | Cache busting on membership change; session revocation |
| Audit chain break (bug or tampering) | Daily verification alert; investigation runbook |
| Tenant deletion partially completes | Idempotent deletion job with checkpoints per store; verification step before the certificate email |

---

## 8. Cross-Cutting: Notifications & Reporting (summary)

- **Notifications:**
  - Events are routed to channels by the tenant's alert matrix.
  - De-dup key per event; per-channel rate limits; templates with untrusted-content escaping (Slack mrkdwn, email HTML).
  - Delivery via Postmark/Slack with retries and a status record.
- **Reporting:**
  - The `renderer` worker opens `/print/reports/{month}` with a signed short-lived service token, renders A4 PDF via Chromium, stores it in Spaces and emails a signed link (15-min URL; the in-app link re-issues).
  - CSV exports stream from DuckDB/Postgres with a formula-injection guard.
