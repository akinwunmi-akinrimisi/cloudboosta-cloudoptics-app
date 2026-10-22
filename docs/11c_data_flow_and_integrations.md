# 11c — Data Flow & Integration Design

**Product:** Cloudoptics by Cloudboosta · **Phase:** 11 · **Date:** 22 October 2026
**Inputs:** `09_…` (architecture §1.4), `11a_…`, `11b_…`, PRD (`06_…`)
**Status:** Draft for engineering review

---

## 1. End-to-End Data Flow (textual diagrams)

### 1.1 Cost data path: cloud provider → UI

```
┌──────────────────────────── CUSTOMER AWS ─────────────────────────────┐
│ AWS Billing ──(BCM Data Exports, up to ~3×/day)──► S3 export bucket    │
│   (CUR 2.0 Parquet + manifest per billing period)                      │
└───────────────────────────────┬───────────────────────────────────────┘
                                │ [F1] cur.poll (every 6h): AssumeRole → List/Get manifest
                                ▼
┌──────────────────────────── CLOUDOPTICS (DO LON1) ─────────────────────┐
│ [F2] connectors: stream files ──► Spaces raw/…/version=v/ (immutable)  │
│        billing_file rows (status=landed, sha256) ── completeness check │
│                                │ event: billing.files_landed (txn)     │
│ [F3] ingestion: DuckDB map CUR→FOCUS ──► Spaces norm/…/version=v/      │
│        data-quality checks (block/warn)                                │
│ [F4] aggregate ──► Postgres cost_daily (atomic replace by version),    │
│        cost_monthly_service, credit_daily, tag_set                     │
│ [F5] reconcile vs invoice totals (±1%) ── mismatch → banner + alert    │
│                                │ event: cost.updated (txn)             │
│ [F6] analysis.run: R1–R8 + anomaly + forecast + what-changed           │
│        ──► recommendation / anomaly / forecast_snapshot                │
│ [F7] budgets.evaluate ──► notifications (email/Slack)                  │
│ [F8] cache version bump t:{tenant}:cost_ver                            │
│ [F9] API (CostQueryService, FX join at read time) ──► SPA (USD/local)  │
└────────────────────────────────────────────────────────────────────────┘
```

### 1.2 Inventory & metrics path (resource-level recommendations)

```
inventory.scan (12h, per account × region) ─AssumeRole→ Describe* APIs
   └─► resource (upsert by tenant/account/region/resource_id; deleted_at on disappearance)
         └─ event inventory.refreshed
metrics.pull (daily) ─► candidate selection (idle/rightsize/schedule rules on inventory)
   └─► CloudWatch GetMetricData (batched, budgeted) ─► resource_metric_daily (avg/p95/max)
         └─ event metrics.refreshed
native.recs.pull (daily) ─► Cost Optimization Hub / Compute Optimizer ─► native_recommendation (staging)
prices.refresh (weekly, global) ─► Price List bulk files ─► price_catalog
   ⇒ analysis.run consumes inventory + metrics + native + prices + effective rates (from cost_daily)
```

### 1.3 First-value path (onboarding, before the first CUR delivery)

```
connect validated ─► ce.mtdSummary (≤ 20 CE calls) ─► cost_snapshot_ce (provisional, labelled)
                  ─► inventory.scan + native.recs.pull ─► R1–R3 analysis (no CUR needed)
UI: "Provisional data from Cost Explorer — full detail within 24h"
first CUR delivered ─► normal path [F1–F9]; provisional snapshot retired
```

### 1.4 FX path

```
fx.fetch (daily 06:00 UTC) ─► provider API (+ central-bank feed for 'official' basis)
   ─► fx_rate(currency, date, basis, rate, source)  [carry-forward ≤ 5 days if missing, is_estimated=true]
API read: money = usd (stored) + local = usd × rate(charge_date | latest for forecasts/budgets)
Reports: store rate basis + rates used (auditability)
```

### 1.5 Action & verification loop

```
UI assign/dismiss/done ─► recommendation (status) + recommendation_event ─► notifications (Slack DM/email)
Customer applies fix in AWS (outside Cloudoptics)
inventory.scan detects change ─► recs.verify ─► status=verified (change detected)
≥ 7 days post-change CUR ─► verified_saving.realised_usd_month ─► savings tracker / monthly report
All events ─► outcome dataset (engine_version, inputs hash) ─► future risk-model training (M2, consent-based)
```

---

## 2. Integration Points

| # | Integration | Direction | Protocol / auth | Data | Frequency | Failure handling |
|---|---|---|---|---|---|---|
| I1 | **AWS IAM Roles Anywhere** (our broker account) | Out | `CreateSession` with X.509 (AWS Private CA, ≤ 24h certs) | Temporary broker credentials | ≤ hourly per worker | Retry/backoff; alert after 15 min of failures; runbook (11g) |
| I2 | **AWS STS** (customer accounts) | Out | `AssumeRole` + ExternalId, SigV4 | Customer session credentials | Per job | Classify `AccessDenied` → connection Failed; transient → retry |
| I3 | **AWS S3** (customer CUR bucket) | Out | SigV4, `GetObject`/`ListObjectsV2` on export prefix | CUR Parquet + manifests | Poll every 6h | Missing → pending state; corrupt → quarantine |
| I4 | **AWS Cost Explorer / COH / Compute Optimizer / Savings Plans / CloudWatch / Describe APIs / Organizations / Pricing** | Out | SigV4 | Costs, recommendations, metrics, inventory, prices | 6h–weekly (11b §1.4) | Rate limits + per-tenant cost budget; partial-failure tolerance |
| I5 | **Auth provider (WorkOS AuthKit)** | Out/In | OIDC code flow; JWKS; webhooks for user events (signed) | Identity, MFA status | Per login; webhooks | Existing sessions survive outages; login status message |
| I6 | **Postmark (email)** | Out | HTTPS API token; inbound bounce/complaint webhooks (signed/basic auth) | Alerts, invites, reports, digests | Event-driven | Retry 3× with backoff; bounce → mark address; fallback to in-app notification |
| I7 | **Slack** | Out/In | OAuth v2 (bot token, encrypted); Web API; interactivity endpoint (signing secret) | Alerts, digests, DMs; button actions (acknowledge, in progress) | Event-driven | Rate-limit aware (tier limits); token revoked → integration disabled + admin email |
| I8 | **FX providers** | Out | HTTPS API key; central-bank pages/APIs | Daily rates | Daily + retries | Secondary provider fallback; carry-forward ≤ 5 days with "estimated" flag; alert after 24h |
| I9 | **PostHog** | Out | API key | Product events (no PII), flags | Continuous | Non-blocking; flags have safe defaults |
| I10 | **Sentry / Grafana Cloud** | Out | Tokens | Errors, telemetry | Continuous | Buffered; non-blocking |
| I11 | **Cloudflare** | In (edge) | DNS/CDN/WAF | Static assets, API proxy | — | Origin fallback documented |
| I12 | **Paystack / Stripe (v1)** | Out/In | API keys; signed webhooks | Subscriptions, invoices, payment status | Event-driven | Idempotent webhook handling; dunning flows |
| **Future** | | | | | | |
| I13 | **Jira Cloud (v1)** | Out/In | OAuth 2.0 (3LO); webhooks | Create issue from recommendation; status sync | Event-driven | Retry; link stored on recommendation |
| I14 | **Microsoft Teams (v1)** | Out | Bot Framework / incoming webhooks (workflows) | Alerts, digests | Event-driven | As Slack |
| I15 | **GitHub/GitLab (IaC pull requests, v2)** | Out | GitHub App (fine-grained, repo-scoped) / GitLab OAuth | Branch + PR with Terraform change | On user action | PR creation errors surfaced; never pushes to default branch |
| I16 | **ServiceNow / Linear (v2+)** | Out | OAuth/API tokens | Tickets | Event-driven | As Jira |
| I17 | **Azure / GCP billing (v2)** | Out | Entra ID federated credentials; GCP Workload Identity Federation | FOCUS exports | Daily | Same connector framework |
| I18 | **Accounting (Xero/QuickBooks/Sage/Zoho, v2)** | Out | OAuth 2.0 | Monthly cloud cost journals by cost centre | Monthly | Draft journals only (customer posts) |

**Integration principles:**
- All inbound webhooks are signature-verified, replay-protected (timestamp ≤ 5 min) and idempotent (event ID store).
- All outbound tokens are envelope-encrypted and least-privilege.
- Third-party content is treated as untrusted.
- Each integration has an adapter interface, so providers can be switched (e.g., email or FX).

---

## 3. Data Consistency & Freshness Guarantees

### 3.1 Latency from cloud spend to UI

| Stage | Typical | Worst case (MVP) | Notes |
|---|---|---|---|
| Usage occurs → AWS includes it in a CUR delivery | 8–24h | up to ~24–48h (AWS-side) | AWS updates exports up to ~3×/day; month-end and credits can arrive later |
| CUR delivered → our poll picks it up | ≤ 6h (avg 3h) | 6h + retries | Poll interval configurable; v1: S3 event notifications via EventBridge in the customer account to trigger faster pulls (opt-in) |
| Landed → visible in dashboards | 10–30 min | ≤ 2h (FR-ING-10) | Normalise + aggregate + cache bump |
| **End-to-end (usage → UI)** | **~12–30h** | **~48h+ (AWS delays)** | The UI shows "data as of" per source; current-day data is labelled partial |
| Inventory changes → recommendations | ≤ 12h (scan) + ≤ 15 min (analysis) | 24h | On-demand rescan button (rate-limited) in v1 |
| Metrics → rightsizing evidence | Daily | 36h | CloudWatch data itself is near real-time |
| Budget/anomaly alert after relevant data lands | ≤ 1h | 2h | PRD US-6.2 |
| Verification of implemented fixes | ≤ 72h (change detected) + 7 days (realised savings) | — | PRD US-5.2 |

**SLO:** 95% of active tenants have cost data < 24h old (relative to the latest AWS delivery), measured as `now − max(landed_at of the last aggregated delivery)`.

### 3.2 Batch vs streaming
- **Batch is the right model.** The sources (CUR, Cost Explorer, Compute Optimizer) are themselves batch and delayed; streaming would add cost and complexity without fresher truth.
- **Near-real-time where cheap:**
  - Event-driven chaining between stages (no fixed cron waits).
  - Optional S3 event notification triggers (v1).
  - Inventory rescan on demand.
- **Real-time spend signals** (e.g., a CloudWatch-based estimated hourly burn for large tenants) are a v2 option, if users need same-day anomaly detection.

### 3.3 Consistency model

| Data | Guarantee |
|---|---|
| Cost aggregates per (tenant, period) | **Atomic version switch:** readers see a complete old or complete new version; never mixed |
| Cross-period views | Eventually consistent (periods updated independently); "data as of" shown |
| Recommendations vs cost data | Eventually consistent (analysis runs after `cost.updated`); each recommendation shows `last_evaluated_at` |
| Lifecycle actions (assign/dismiss) | Strongly consistent (single Postgres transaction + audit) |
| FX | Historical rates are immutable once final; carried-forward rates are flagged and replaced when real rates arrive (reports state the rates used at generation) |
| Monthly reports | Snapshot at generation (data version recorded). Later AWS restatements don't change a sent report; the next report notes "previous month restated by $X" if > 1% |
| Caches | Keys include the tenant data version, so they invalidate on the version bump; TTL as a safety net |

---

## 4. Error Propagation & User-Facing Messaging

### 4.1 Connection health states

| State | Definition | Ingestion behaviour | UI | Notification |
|---|---|---|---|---|
| **Healthy** | All required checks pass; data < 24h old | Normal | Green check; "Data as of …" | — |
| **Pending data** | Connected but no CUR delivered yet | Provisional CE data | Banner: "Your first billing export is on its way (up to 24h). Showing provisional data." | Email when ready |
| **Degraded** | Some permissions/data types failing (e.g., no Compute Optimizer access, member accounts missing) | Other data continues | Amber badge; per-permission list with fix steps; affected features show "limited data" chips | Email Admins once per issue (daily max) |
| **Stale** | Data > 36h old for unknown reasons | Retries | Banner: "Cost data is older than usual (last update …). We're on it." | Admin email at 36h; internal alert at 24h |
| **Failed** | Cannot assume role / export removed | Stops for that connection | Red banner with a "Fix connection" guided flow; dashboards show the last good data, clearly dated | Email Admins immediately + Slack if connected; reminders at 24h and 72h |
| **Disconnected** | Removed by an admin | Stopped | Account greyed out; data retained until deletion request | Audit + confirmation email |

### 4.2 Partial failure scenarios

| Scenario | System behaviour | User-facing message |
|---|---|---|
| Role works, CUR bucket access denied | Cost data stops; inventory continues | "We can't read your billing export (s3:GetObject denied on bucket X). Re-deploy the stack or update the bucket policy. [Show fix]" |
| Member accounts lack resource role | Cost visible; resource recommendations missing for those accounts | Coverage chip: "Recommendations cover 8 of 12 accounts. [Extend access]" |
| Compute Optimizer not enrolled | Rightsizing uses CloudWatch only; lower confidence | On R5 items: "Confidence reduced: Compute Optimizer is not enabled (free). [How to enable]" |
| Memory metrics absent | Confidence capped at Medium | "We couldn't see memory use — install the CloudWatch agent for better advice." |
| AWS throttling / outage | Retries; data delay | Only shown if freshness SLO is at risk: "Updates are delayed due to AWS API limits." |
| Customer-side API budget reached | Paid calls paused (metrics) | Settings › Connections: "Metric refresh paused until 1 Nov to keep our access cost under $5/month. [Adjust]" |
| Reconciliation mismatch > 1% | Data shown with warning | Banner: "October totals differ from your AWS invoice by 1.8%. We're reconciling; figures may change." |
| FX rate missing | Carry-forward rate | Tooltip/badge: "Estimated rate (latest available: 20 Oct)." |
| Ingestion blocked by a data-quality failure | Previous version kept | "Latest billing update is being checked; showing data as of …" + internal alert |
| Slack token revoked | Slack disabled; email continues | Settings banner: "Slack was disconnected. [Reconnect]" |

### 4.3 Recovery
- **Automatic:** transient errors retry with backoff; the next scheduled run catches up (jobs process all undelivered versions in order); missed periods are backfilled from `raw/` without re-downloading.
- **User-assisted:** guided fix flows with "Re-validate" buttons; a successful validation triggers an immediate catch-up sync.
- **Operator-assisted:** runbooks (11g) for systemic issues (broker/CA, mapping bugs → reprocess from `raw/` with a corrected mapping version and record a "data restated" note).
- **Communication:** a status page for platform-wide incidents; in-app incident banner; post-incident summary to affected tenants when data was wrong or delayed > 24h.
