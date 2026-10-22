# 11g — Runbooks & Operational Readiness

**Product:** Cloudoptics by Cloudboosta · **Phase:** 11 · **Date:** 22 October 2026
**Inputs:** `09_…`, `10_…` (security operations, observability, DR), `11b_…`, `11c_…`, `11f_…`
**Status:** Draft. Runbooks are living documents in the ops repo (`docs/runbooks/`), linked from every alert.

---

## 1. Key Runbooks

Each runbook follows: **Trigger → Impact → Diagnose → Mitigate → Resolve → Communicate → Follow-up**. Commands reference internal CLIs (`coctl`) and dashboards; exact commands are maintained in the ops repo.

### RB-01 — Handling connector failures

**Triggers:**
- Alert `ConnectorFailureRateHigh` (> 5% of connections Failed/Degraded in 1h).
- Alert `ConnectionFailed` (single tenant, escalated after 24h).
- Support ticket.

**Impact:** stale or missing data for affected tenants; recommendations not refreshed.

**Diagnose:**
1. Open the *Connector Health* dashboard. Is it one tenant, one AWS API/service, one region, or all tenants?
2. **All tenants failing at AssumeRole** → likely our side (broker/Roles Anywhere/CA). Go to RB-01a.
3. **One AWS API failing for many tenants** → AWS service issue or a breaking change. Check the AWS Health Dashboard and botocore version; check recorded error codes.
4. **Single tenant** → inspect `permission_check` and `sync_run` errors: `AccessDenied` on AssumeRole (role deleted / ExternalId mismatch), missing export, bucket policy changed, SCP blocking.
5. Check the throttling metrics and the per-tenant AWS API budget state.

**Mitigate:**
- Single tenant: the system has already emailed admins. If a design partner, the solutions engineer reaches out with the guided fix. Use `coctl conn validate <id>` after the customer's fix.
- AWS-wide issue: pause affected job types (flag `connector.<service>.enabled=false`) to avoid retry storms; post a status-page notice if freshness SLO is at risk.
- Throttling: reduce concurrency for the affected service (config), extend jitter.

**RB-01a (broker/CA failure):**
1. Check AWS Private CA status and certificate issuance logs.
2. Check Roles Anywhere `CreateSession` errors (CloudTrail in the broker account).
3. Check the cert-renewal sidecar logs in DOKS.
4. If the certs expired, force renewal (`kubectl rollout restart` worker deployments after fixing the issuer).
5. If the trust anchor or profile was modified unexpectedly, **treat as a security incident (RB-06 + incident response plan)**: disable the trust anchor, rotate, and investigate.
6. Last-resort fallback: enable the documented emergency IAM-user path (break-glass, time-limited, logged).

**Resolve:** confirm validations are green; trigger catch-up syncs (`coctl sync catchup --since <ts>`); verify freshness metrics recover.

**Communicate:** status page (if > 5% of tenants or > 6h delay); in-app banner auto-shown by state; post-incident note to affected tenants if data was delayed > 24h.

**Follow-up:** post-incident review if SLO impact; add tests or alerts for any new error code.

### RB-02 — Investigating data discrepancies

**Triggers:**
- Alert `ReconciliationMismatch` (> 1% vs invoice).
- A customer report ("numbers don't match AWS").
- DQ `Drift` warnings.

**Diagnose:**
1. Identify tenant, period and metric (unblended/amortised/net) and the currency view (USD vs local).
2. **If local currency:** check `fx_rate` for the dates (basis, estimated flag). Compare with the customer's rate expectation (official vs market). Often a rate-basis issue, not a cost issue.
3. Compare: our `cost_daily` sum vs normalised Parquet sum (`coctl cost compare --tenant --period`) vs raw CUR sum (DuckDB on `raw/`) vs Cost Explorer `GetCostAndUsage` for the same filters vs the invoice PDF total (customer-provided).
4. Locate the stage that diverges:
   - **Raw ≠ CE:** export settings (e.g., the customer compares unblended vs amortised; taxes; credits; mid-month restatement).
   - **Raw ≠ norm:** mapping bug.
   - **Norm ≠ aggregate:** aggregation bug or a stale version.
5. Check `billing_file` versions: was the latest delivery processed? Any `superseded`/`failed`?

**Mitigate:**
- Explain the metric differences to the customer (template answers: amortised vs unblended, credits, tax, FX basis).
- If ours is wrong: set the tenant banner "figures under review"; disable affected report sends.

**Resolve:**
- Fix the mapping/aggregation (new mapping version).
- **Reprocess from raw** (`coctl ingest reprocess --tenant --period --mapping <v>`).
- Verify reconciliation passes.
- Record a "data restated" note (shown in the next report).

**Communicate:** reply to the customer with findings; if a bug affected multiple tenants, notify affected tenants with the impact and correction.

**Follow-up:** add the case to the golden fixtures; regression test.

### RB-03 — Responding to performance degradation

**Triggers:** `APILatencySLOBurn` (p95 above target with fast burn), `QueueAgeHigh` (oldest job > 30 min in a critical queue), `DBCPUHigh` / `DBConnectionsSaturated`, synthetic checks failing from African probes.

**Diagnose:**
1. *System Health* dashboard: which endpoints/queues? When did it start? Was there a recent deploy (release markers)?
2. Traces (Tempo): slow spans: DB, DuckDB, external calls?
3. DB: `pg_stat_statements` top queries; locks; autovacuum; connection pool saturation.
4. Queues: depth/age per queue; KEDA scaling at max? A large tenant backfill?
5. Edge: Cloudflare analytics (errors, origin latency); regional network issues affecting Africa routes.

**Mitigate (fastest first):**
- Recent deploy suspected → roll back (Argo CD).
- Pause non-critical jobs (flags: metrics pulls, digests); move the large tenant to the large-job pool; raise KEDA max temporarily (within budget).
- DB: kill runaway queries; add the missing index via a hotfix migration (expand-only); scale the DB vertically (managed resize, with a brief failover warning).
- API: increase cache TTL; enable the stricter rate-limit profile.

**Resolve:** root cause fix (query plan, index, batching); confirm SLOs recover.

**Communicate:** status page if user-visible > 15 min; internal channel updates every 30 min during the incident.

**Follow-up:** post-incident review; capacity planning update; consider the ClickHouse scale trigger (Phase 9).

### RB-04 — Rotating credentials & managing secrets

**Scheduled rotations:**

| Secret | Rotation | Method |
|---|---|---|
| Roles Anywhere certs | Automatic (≤ 24h) | Issuer + sidecar; monitor expiry |
| Broker-account break-glass IAM user (if any) | 90 days or after use | Secrets Manager rotation + verification job |
| DB credentials (app roles) | 90 days | Create new user/password → update Secrets Manager → ESO sync → rolling restart → revoke old |
| Valkey password | 90 days | As above |
| Spaces access keys | 90 days | New key → ESO → restart → delete old |
| Postmark, FX, PostHog, Sentry tokens | 180 days or on staff change | Provider console → Secrets Manager → restart |
| Slack app signing secret / client secret | On compromise or staff change | Slack app config → Secrets Manager |
| KMS keys | Automatic yearly rotation (AWS KMS) | Old versions retained for decryption |
| Cloudflare / DO / GitHub tokens (CI) | Prefer OIDC (no static); otherwise 90 days | Scoped tokens |
| Tenant ExternalIds | Not rotated routinely (customer-side change); rotate on suspected exposure | Generate new ExternalId → customer updates stack parameter → validate |

**Procedure (generic):**
1. Create the new secret version in Secrets Manager (do not delete the old one).
2. Wait for the ESO sync, or force refresh.
3. Rolling restart of the consuming deployments.
4. Verify health (logs, metrics, synthetic checks).
5. Revoke/delete the old credential.
6. Record in the audit log (ops) and the rotation tracker.

**Emergency rotation (suspected compromise):** follow the incident response plan:
- Rotate immediately.
- Invalidate sessions if auth-related.
- Review access logs (CloudTrail, DO, provider logs) for misuse.
- Assess whether customer notification is needed (72h breach rules).

### RB-05 — On-call escalation flow

**Rota:**
- Weekly primary + secondary on-call from the engineering team (Grafana IRM).
- Business-hours coverage across WAT/EAT/SAST plus UK hours.
- Out-of-hours paging **only for P1/P2**.
- Compensation and handover per policy.

**Severity levels:**

| Severity | Definition | Response | Examples |
|---|---|---|---|
| **P1 — Critical** | Security incident; cross-tenant exposure; full outage; data corruption visible to customers | Page primary immediately; ack ≤ 15 min; incident commander assigned; updates every 30 min | Tenant data leak; app down; broker compromise |
| **P2 — High** | Major degradation; freshness SLO breach for > 10% of tenants; key feature broken | Page primary; ack ≤ 30 min (business hours) / ≤ 1h (out of hours) | Ingestion stopped; alerts not sending |
| **P3 — Medium** | Partial degradation; single-tenant failures; non-critical job failures | Ticket; next business day | One tenant's connection failing; PDF rendering failing |
| **P4 — Low** | Minor bugs; cosmetic | Backlog | — |

**Escalation path:**
1. Primary on-call.
2. Secondary on-call (if no ack in 15 min, or more help is needed).
3. Tech Lead (P1/P2 or no progress in 60 min).
4. Engineering Manager/CTO.
5. Security lead + legal/DPO (security/privacy incidents).
6. CEO/comms (customer-wide communication, regulator notification).

**Incident roles (P1/P2):** Incident Commander (coordinates), Ops Lead (hands-on), Comms Lead (status page, customer updates), Scribe (timeline).

**After the incident:** blameless post-incident review within 5 working days for P1/P2; action items tracked with owners and dates.

### RB-06 — Security event (summary)
Triggers: CloudTrail alert on the broker account (trust anchor/role/policy change), unusual AssumeRole volume, audit-chain verification failure, cross-tenant test failure in production checks, credential leak report.
Steps: declare P1 → contain (disable the trust anchor or broker role, revoke sessions, block IPs at the WAF) → preserve evidence → assess scope (which tenants/data) → eradicate → recover → notify per the legal timelines (72h) → post-incident review. Full plan per Phase 10 A8.

### RB-07 — Other runbooks to write before beta
- FX feed failure (stale rates).
- Price catalogue refresh failure.
- Notification delivery failure (Slack/Postmark).
- PDF renderer failures.
- DB failover.
- Backup restore.
- Region DR (Phase 10 B5).
- Tenant data deletion verification.
- Engine kill switch & recompute.
- Customer-side API budget exhausted.

---

## 2. Operational Metrics & SLOs

### 2.1 Service level objectives (MVP)

| SLO | Indicator (SLI) | Target | Window | Error budget policy |
|---|---|---|---|---|
| **Availability (web/API)** | Successful requests (non-5xx) ÷ total, excluding planned maintenance; plus synthetic login + Overview checks | **99.5%** | 30 days | Budget < 25% remaining → freeze risky releases; focus on reliability |
| **Data freshness** | % of active tenants with cost data < 24h old relative to the latest AWS delivery | **95%** | Rolling daily | Breach 2 days running → P2 |
| **API latency (non-analytical)** | p95 latency for non-cost endpoints | **≤ 500 ms** | 7 days | — |
| **API latency (cost queries)** | p95 for `/costs/summary` and `/costs/query` | **≤ 2 s** (p99 ≤ 4 s) | 7 days | Trigger performance review / scale plan |
| **Error rate** | 5xx rate per endpoint group | **< 0.5%** (page at > 2% for 5 min) | 1h / 30 days | — |
| **Analysis job success** | Successful `analysis.run` ÷ attempted (after retries) | **≥ 99%** | 7 days | Investigate each failure class |
| **Ingestion job success** | Successful `cur.ingest`/`cost.aggregate` ÷ attempted (after retries) | **≥ 99%** | 7 days | — |
| **Alert delivery** | Budget/anomaly notifications sent ≤ 1h after data | **≥ 99%** | 7 days | — |
| **Reconciliation** | Tenant-periods within ±1% of invoice | **≥ 99%** (closed months) | Monthly | Each miss investigated (RB-02) |

### 2.2 Error rate thresholds (alerting)

| Signal | Warning (ticket) | Critical (page) |
|---|---|---|
| API 5xx rate | > 1% for 10 min | > 2% for 5 min, or availability SLO fast burn (2% budget in 1h) |
| Job failure rate (per queue) | > 2% over 1h | > 10% over 15 min |
| DLQ size | > 20 jobs | > 100 jobs or any P1 job type in DLQ |
| Queue age (critical queues) | > 30 min | > 2h |
| Connector failures (all tenants) | > 5% Degraded/Failed | > 20% Failed (likely our side) |
| Freshness | < 97% tenants fresh | < 95% (SLO breach) |
| Auth failures | Spike > 3× baseline | Sustained credential-stuffing pattern |

---

## 3. Monitoring & Alerting Design

### 3.1 Alert routing (page vs ticket)

| Page (on-call, P1/P2) | Ticket (business hours, P3) |
|---|---|
| Availability SLO fast burn; synthetic login/Overview down from ≥ 2 regions | Slow SLO burn |
| API 5xx > 2% for 5 min | Single endpoint degradation |
| Ingestion stopped globally (no successful aggregate in 6h while deliveries exist) | Single tenant ingestion failure |
| Connector failures > 20% (broker/CA suspect) | Single tenant connection Failed > 24h |
| Broker-account security events (trust anchor/role/policy change; abnormal AssumeRole volume) | Certificate expiring in < 7 days (if not auto-renewed) |
| DB down / failover loop / storage > 90% | DB storage > 75%; slow queries |
| Audit-chain verification failure | FX feed stale > 24h |
| Cross-tenant check failure in production canary tests | Price catalogue stale > 10 days |
| Backup failure (2 consecutive) | DLQ > 20 |

All alerts link to a runbook, include tenant counts (not tenant data) and are de-duplicated and grouped.

### 3.2 Dashboards

| Dashboard | Audience | Panels |
|---|---|---|
| **System Health** | On-call, engineers | Availability and SLO burn; request rate/errors/latency by endpoint; pod health; DB (CPU, connections, replication lag, slow queries); Valkey; Spaces errors; Cloudflare edge errors; synthetic checks by African probe location; deploy markers |
| **Data Pipeline Status** | Engineers, on-call | Per-stage throughput and duration (poll → land → normalise → aggregate → reconcile → analyse); queue depth/age per queue; DLQ; freshness distribution (% tenants < 24h); reconciliation pass rate; ingestion lag top-10 tenants; DuckDB memory/spill; FX/price refresh status |
| **Connector Health** | Engineers, support | Connections by state (Healthy/Pending/Degraded/Stale/Failed); top error codes; AWS throttling by service; per-tenant AWS API cost (customer-side) vs budget; Roles Anywhere session success; cert expiry |
| **Recommendations Quality** | PM, FinOps SME, data engineer | Recommendations by type/risk; action rate; dismissal reasons ("wrong/unsafe" rate); realised ÷ estimated savings by engine version; verification lag; engine run failures |
| **Business Metrics** | Leadership, PM, GTM | Sign-ups; tenants with connected accounts; active tenants (weekly); connected AWS accounts; monitored spend (USD); identified vs realised savings (USD and by currency); budgets created; alerts sent; report opens; design-partner health; plan distribution (v1) |
| **Security** | Tech Lead, security | Auth failures/MFA adoption; admin actions; break-glass uses; WAF blocks; broker-account CloudTrail events; vulnerability backlog by severity |
| **Our Own Cloud Costs (FinOps on ourselves)** | Engineering manager | DO + AWS spend vs budget; cost per active tenant (target ≤ USD 5); top cost drivers |

### 3.3 Operational readiness checklist (before beta, end of S6)

| Area | Ready when |
|---|---|
| Monitoring | All dashboards above live; synthetic checks from Lagos/Nairobi/Johannesburg |
| Alerting | Page/ticket rules configured, tested (fire drill), linked to runbooks |
| On-call | Rota staffed; escalation contacts verified; paging tested end-to-end |
| Runbooks | RB-01–RB-06 written and reviewed; RB-07 list scheduled |
| Backups/DR | Restore test passed this month; DR plan reviewed; RPO/RTO measured |
| Security | Full security-audit skill run; Critical/High fixed; incident response plan approved; security pack published |
| Support | Support channels (email, in-app, Slack Connect for partners) with African-hours coverage; macros for common issues (connection setup, data differences, FX basis) |
| Status page | Public status page live and linked in-app |
| Data | Reconciliation passing for all design partners' last closed month |
| Legal/privacy | ToS, privacy policy, DPA (with NDPA/Kenya/POPIA clauses), sub-processor list published |
| Capacity | Load tests passed at 3× expected beta load |
