# 11g — Runbooks & Operational Readiness

**Product:** Cloudoptics by Cloudboosta · **Phase:** 11 · **Date:** 22 October 2026, updated 19 November 2026 (Sprint 6: §4)
**Inputs:** `09_…`, `10_…` (security operations, observability, DR), `11b_…`, `11c_…`, `11f_…`
**Status:** Sections 1 to 3 are the target design for production on DOKS (ADR-008). **Staging today runs on App Platform (ADR-013): use §4**, which has the runbooks as built with exact commands, the on-call rota, alerting, backups and restore tests, worker scaling and our infrastructure budget. The release checklist (PRD §8) is in [release-checklist.md](release-checklist.md).

---

## 1. Key Runbooks

Each runbook follows: **Trigger → Impact → Diagnose → Mitigate → Resolve → Communicate → Follow-up**. RB-01 to RB-07 describe the production design: commands such as `coctl`, `kubectl` and Argo CD, and the dashboards, do not exist on staging yet. For staging use RB-08 to RB-19 in §4.

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

**Rota:** (target design; the rota in force for staging is §4.2)
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
Now written for staging in §4: backup restore and DB failover (RB-11), tenant data deletion verification (RB-14). Still to write:
- FX feed failure (stale rates).
- Price catalogue refresh failure.
- Notification delivery failure (Slack/Postmark).
- PDF renderer failures.
- Region DR (Phase 10 B5).
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

---

## 4. Staging on App Platform: runbooks as built (Sprint 6, 19 Nov 2026)

Sections 1 to 3 describe the target design on DOKS (ADR-008). Staging actually runs on **DigitalOcean App Platform with one managed PostgreSQL cluster** ([ADR-013](adr/013-digitalocean-app-platform-staging.md)): no Kubernetes, no Argo CD, no Grafana stack, no `coctl`. The runbooks below are the ones to use today. Kubernetes-only items of the backlog have an App Platform equivalent here: Kyverno (OPS-9) in §4.4, KEDA (OPS-10) in §4.5.

### 4.1 Access and handles

| Thing | Value or how to find it |
|---|---|
| App | `cloudoptics-staging`, ID `3294882e-35ce-45b0-bb33-e7ce55062f22`, region `lon` |
| URL | `https://cloudoptics.cloudboosta.com`: public site at `/` (also `/cfn/*` templates and the 404 page that forwards old app paths), app at `/app/`, demo at `/demo/`, API at `/api`. App Platform strips each prefix |
| Components | `api` (service, apps-s-1vcpu-1gb-fixed), `worker` (apps-s-1vcpu-0.5gb), `migrate` (PRE_DEPLOY job), `site`, `web` and `demo` (static) |
| Database | Managed PostgreSQL 16 `cloudoptics-pg`, database `cloudoptics`, owner `doadmin`, app role `cloudoptics_app` (no BYPASSRLS) |
| Image | `ghcr.io/akinwunmi-akinrimisi/cloudoptics-api:main`, also tagged `sha-<commit>`; signed (§4.4) |
| Spec and scripts | `infra/digitalocean/app.yaml`, `provision.py`, `restore_test.py` |
| Workflows | Container (build, push, sign), Deploy staging, Staging setup, Restore test |

One-time setup on the operator's machine (doctl 1.110 or later; a DigitalOcean token with app and database scopes):

```bash
doctl auth init --context cloudoptics          # paste a personal token; never commit it
doctl auth switch --context cloudoptics
export APP=3294882e-35ce-45b0-bb33-e7ce55062f22
export DB=$(doctl databases list --format ID,Name --no-header | awk '$2=="cloudoptics-pg"{print $1}')
```

Everyday reads (no change to anything):

```bash
doctl apps get $APP                                   # active and in-progress deployment
doctl apps list-deployments $APP --format ID,Phase,Cause,Created | head
doctl apps logs $APP api --type run --tail 200        # add --follow to stream
doctl apps logs $APP worker --type run --tail 200
doctl apps logs $APP api --type deploy                # last deployment's deploy log
doctl apps list-alerts $APP                           # alert rules and recipients
doctl databases get $DB                               # status, size, connection host
doctl databases backups $DB                           # daily backups (7 days kept)
```

**Shell in a running component** (for the SQL in these runbooks): `doctl apps console $APP worker`. The container has Python and psycopg, and `CLOUDOPTICS_APP_DATABASE_URL` in its environment, which connects as `cloudoptics_app` (row-level security applies; the queue tables have no RLS):

```bash
python - <<'PY'
import os, psycopg
url = os.environ["CLOUDOPTICS_APP_DATABASE_URL"].replace("postgresql+psycopg://", "postgresql://")
with psycopg.connect(url) as c:
    for row in c.execute("SELECT 1"):   # replace with the query you need
        print(row)
PY
```

**Owner (doadmin) access** is break-glass: the database firewall admits only the app. Add your address for the session, then remove it:

```bash
doctl databases firewalls append $DB --rule ip_addr:$(curl -s https://api.ipify.org)
doctl databases connection $DB --format URI --no-header   # prints the doadmin password: do not paste it anywhere
psql "<that URI with /cloudoptics instead of /defaultdb>"
doctl databases firewalls list $DB                        # note the UUID of your rule
doctl databases firewalls remove $DB --uuid <rule-uuid>
```

Workspace tables use FORCE row-level security, which binds the owner too: `doadmin` sees a workspace's rows only after `SELECT set_config('app.tenant_id', '<tenant-id>', false);` (the queue, rate-limit and migration tables have no RLS). Platform audit rows (`audit_log.tenant_id IS NULL`) are readable by the owner without scoping (policy `audit_platform_owner`, migration 0037). Every owner session is noted in the incident or change record (who, why, start, end). The next Staging setup run also resets the firewall to "only the app".

### 4.2 On-call rota (OPS-7; replaces the Grafana IRM plan in RB-05 for now)

Cloudoptics has no 24/7 team yet. Until the beta grows past the design partners, on-call is business hours with best-effort evenings, and pages are email (App Platform alerts and Uptime, §4.3).

| | Who | Hours | Notes |
|---|---|---|---|
| Primary | TODO (founder to name) | Mon to Fri 08:00 to 18:00 WAT (07:00 to 17:00 UTC) | Covers WAT (Lagos, UTC+1), SAST (Johannesburg, UTC+2) 09:00 to 19:00 and EAT (Nairobi, UTC+3) 10:00 to 20:00 |
| Secondary | TODO (founder to name) | Same hours; takes over when the primary is away | Holds the DigitalOcean, GitHub and WorkOS admin access too |
| Out of hours | Primary, best effort | Evenings and weekends | Only P1 (security incident, cross-tenant exposure, full outage, data loss) |
| Escalation | TODO: founder / CTO | Any time for P1 | Then security and legal (TODO: name) for a personal-data breach (72-hour notification, docs/10 A8) |

Severity and response times follow RB-05. Handover: the outgoing primary writes open issues, running deployments and anything left half-done (an owner firewall rule, a kept restore cluster) into the on-call channel (TODO: name it) on Friday.

Before the rota counts as staffed (docs/11g §3.3): every person on it is a member of the DigitalOcean team (alert emails go only to team members), is in `ALERT_EMAILS`, has GitHub write access to the repository, and has received one test alert (run Staging setup, then trigger a failed deployment on a branch or wait for the next real alert).

### 4.3 Alerting as built (OPS-7)

Defined in `infra/digitalocean/app.yaml`, applied by `provision.py` during **Staging setup** (not by an ordinary deploy, which reuses the app's stored spec). Rule names are from the [App Platform app spec reference](https://docs.digitalocean.com/products/app-platform/reference/app-spec/).

| Where | Rule | Fires when |
|---|---|---|
| App | `DEPLOYMENT_FAILED`, `DOMAIN_FAILED` | A deployment or the domain/certificate fails |
| `api` | `CPU_UTILIZATION` > 80 % for 10 min; `MEM_UTILIZATION` > 85 % for 10 min; `RESTART_COUNT` > 2 in 30 min; `REQUEST_DURATION_P95_MS` > 2000 for 10 min | Load, memory leak, crash loop, slow API |
| `worker` | `CPU_UTILIZATION` > 90 % for 1 h; `MEM_UTILIZATION` > 85 % for 10 min; `RESTART_COUNT` > 2 in 30 min | Stuck at full CPU, close to an out-of-memory kill, crash loop |
| Uptime | `https://cloudoptics.cloudboosta.com/api/readyz`: down for 2 min, latency > 2 s for 10 min. `https://cloudoptics.cloudboosta.com/`: down for 2 min, certificate expiring in < 14 days | Checked from eu_west and us_east (DigitalOcean Uptime has no African probe) |

**Recipients.** The app spec has no field for recipients. `provision.py` reads the GitHub secret `ALERT_EMAILS` (comma-separated) and, after applying the spec, assigns those addresses to every rule (`POST /v2/apps/{app}/alerts/{alert}/destinations`) and creates or updates the two Uptime checks and their alerts (`/v2/uptime/checks`), matched by name so a re-run never duplicates them. Each address must belong to a member of the DigitalOcean team. Without `ALERT_EMAILS`, the rules exist but keep the recipients they had (DigitalOcean emails the team for failed deployments and domains by default), and no Uptime checks are created. Slack is out of scope (ADR-014).

To change recipients: update the `ALERT_EMAILS` secret in the GitHub environment that Staging setup uses, then run **Actions → Staging setup**.

**Not covered by an alert yet** (check by hand, §4.5 and RB-12): queue depth and age, failed jobs, data freshness, audit-chain verification (only the restore test checks it), database CPU and storage (managed database alerts are configured in the DigitalOcean console under Monitoring, not in the app spec; add CPU > 80 %, disk > 75 % there).

### 4.4 Image signing and the admission check (OPS-9)

- **Signing.** `.github/workflows/container.yml` builds the image on every pull request and pushes it from `main`. A separate job, `sign`, runs only for pushes and is the only job with `id-token: write`. It signs the pushed digest with cosign keyless (Sigstore; the certificate names `container.yml` on `refs/heads/main`), verifies the signature, and attaches an SPDX SBOM as a signed attestation (best effort: an SBOM failure does not block the deploy, a signing failure does).
- **Kyverno equivalent.** App Platform has no admission controller, so `deploy-staging.yml` runs `cosign verify` on `ghcr.io/akinwunmi-akinrimisi/cloudoptics-api:main` before it starts the deployment. An unsigned or foreign image stops the deploy. Limits: App Platform pulls the tag itself a moment later, and a deploy started from the DigitalOcean console skips the check.
- **Verify by hand** (any machine with cosign 2.4 or later):

```bash
IMG=ghcr.io/akinwunmi-akinrimisi/cloudoptics-api
cosign verify $IMG:main \
  --certificate-identity https://github.com/akinwunmi-akinrimisi/cloudboosta-cloudoptics-app/.github/workflows/container.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
cosign verify-attestation $IMG:main --type spdxjson \
  --certificate-identity https://github.com/akinwunmi-akinrimisi/cloudboosta-cloudoptics-app/.github/workflows/container.yml@refs/heads/main \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com > /dev/null && echo "SBOM attested"
```

Images pushed before this change are unsigned; the first push to `main` after it produces the first signed image, which the deploy that follows verifies.

### 4.5 Worker scaling, resource limits and our cost budget (OPS-10 equivalent)

**No queue-based autoscaling.** KEDA does not exist on App Platform, and App Platform autoscaling works on CPU only and only for dedicated-CPU sizes (`apps-d-*`). Our shared sizes scale by hand. Procrastinate is safe with several worker processes or instances: jobs are claimed with row locks, the `lock`/`queueing_lock` keys keep the same work from running twice, and periodic tasks are deferred once per timestamp.

**Sizing.** `CLOUDOPTICS_WORKER_CONCURRENCY` (default 2) is the number of jobs one worker process runs at once. Each slot can hold one bill load in memory, so it follows the instance's memory:

| Worker size (`instance_size_slug`) | Memory | Price/month (5 Nov 2026) | `CLOUDOPTICS_WORKER_CONCURRENCY` |
|---|---|---|---|
| `apps-s-1vcpu-0.5gb` (today) | 512 MiB | $5 | 2 (default) |
| `apps-s-1vcpu-1gb` | 1 GiB | $12 | 3 to 4 |
| `apps-s-1vcpu-2gb` | 2 GiB | $25 | 6 |
| 2 × `apps-s-1vcpu-1gb` (`instance_count: 2`) | 2 × 1 GiB | $24 | 3 to 4 each |

Change it in `app.yaml` (size or `instance_count`; for the concurrency add `{ key: CLOUDOPTICS_WORKER_CONCURRENCY, value: "4" }` to the worker's `envs`), merge, and run Staging setup. Watch `MEM_UTILIZATION` for a day afterwards: the alert at 85 % is the signal that concurrency is too high for the size. Per-job deadlines (`platform/jobs.py`, 15 min default, 60 min for bill loads and scans) and the bill-size limits (`CLOUDOPTICS_CUR_MAX_*`) bound what one job can take.

**When to scale.** Scale up when the queue query below shows ready jobs older than 30 minutes in `ingestion`, `analysis` or `connections` on two consecutive days, or when the worker's memory alert fires; scale out (more instances) when CPU, not memory, is the limit. Move to DOKS with KEDA when queue-driven scaling is needed (ADR-013).

**Queue depth and age** (run in a worker console, §4.1):

```sql
-- Ready, running and failed jobs per queue, and how long the oldest ready job has waited.
SELECT j.queue_name,
       count(*) FILTER (WHERE j.status = 'todo' AND (j.scheduled_at IS NULL OR j.scheduled_at <= now())) AS ready,
       count(*) FILTER (WHERE j.status = 'todo' AND j.scheduled_at > now())                              AS scheduled,
       count(*) FILTER (WHERE j.status = 'doing')                                                        AS running,
       count(*) FILTER (WHERE j.status = 'failed')                                                       AS failed,
       max(now() - e.at) FILTER (WHERE j.status = 'todo' AND (j.scheduled_at IS NULL OR j.scheduled_at <= now())) AS oldest_ready
FROM procrastinate_jobs j
LEFT JOIN procrastinate_events e ON e.job_id = j.id AND e.type = 'deferred'
GROUP BY j.queue_name ORDER BY ready DESC;

-- What is running now, and for how long.
SELECT j.id, j.queue_name, j.task_name, j.attempts, now() - max(e.at) AS running_for
FROM procrastinate_jobs j JOIN procrastinate_events e ON e.job_id = j.id AND e.type = 'started'
WHERE j.status = 'doing' GROUP BY j.id ORDER BY running_for DESC;

-- Live workers (a worker beats every 10 s; stalled after 120 s).
SELECT id, last_heartbeat, now() - last_heartbeat AS since FROM procrastinate_workers ORDER BY last_heartbeat DESC;
```

**Resource limits in force.** App Platform caps each component at its instance size (a component over its memory is killed and restarted, which `RESTART_COUNT` reports). The application's own limits: worker concurrency, job deadlines, bill-file limits, API rate limits (`CLOUDOPTICS_RATE_LIMIT_*`, RB-13), 10 exports an hour per user and 1M rows / 50 MB per export (RB-15), the per-tenant AWS metrics budget (`CLOUDOPTICS_METRICS_DAILY_BUDGET`).

**Our infrastructure budget (staging, list prices as of 19 Nov 2026):**

| Item | Size | $/month | Source |
|---|---|---|---|
| `api` | apps-s-1vcpu-1gb-fixed | 10.00 | [App Platform pricing](https://docs.digitalocean.com/products/app-platform/details/pricing/) (updated 5 Nov 2026) |
| `worker` | apps-s-1vcpu-0.5gb | 5.00 | same |
| `migrate` | apps-s-1vcpu-0.5gb, billed only while it runs (about a minute per deploy) | < 0.10 | same |
| `web` | static site component | 0 to 3 (the page says only apps made solely of static sites are in the free tier; check the first invoice) | same |
| PostgreSQL | db-s-1vcpu-1gb, 1 node, 10 GiB, daily backups included | 15.15 ($0.02254/hour) | [Managed Databases pricing](https://www.digitalocean.com/pricing/managed-databases); the [docs](https://docs.digitalocean.com/products/databases/postgresql/details/pricing/) say "from $15.00" |
| Uptime | 2 checks (1 free per month, then $1 each) | 1.00 | [Uptime pricing](https://docs.digitalocean.com/products/uptime/details/pricing/) |
| Restore test | temporary db-s-1vcpu-1gb for about 1 hour, monthly | about 0.03 | Managed Databases pricing |
| Bandwidth | 150 GiB/month included across the two containers; $0.02/GiB beyond | 0 | App Platform pricing |
| GHCR, GitHub Actions | public repository | 0 | |
| **Total** | | **about $31 to $34** | ADR-013 estimated $35 |

Budget: **$50/month for staging**. Set a DigitalOcean billing alert at $40 (console → Billing → Billing alerts) so a forgotten restore cluster or a size change is noticed. Production (DOKS, ADR-008) is budgeted separately; the docs/11g §3.2 target of ≤ $5 of our cloud cost per active tenant applies there.

---

### RB-08: Deploy and rollback

**Normal deploy.** Merge to `main` → **Container** builds, pushes and signs `cloudoptics-api:main` → **Deploy staging** verifies the signature, starts a deployment (`force_build`), waits for `ACTIVE`, and smoke-tests `/api/readyz` and `/` (plus `scripts/check-staging-layout.sh` once the app has the `site` component). A deploy redeploys the spec already on App Platform: changes to `infra/digitalocean/app.yaml` (components, routes, env) take effect only when **Staging setup** or `provision.py app` applies it. The `migrate` PRE_DEPLOY job runs `alembic upgrade head` as `doadmin` before the new `api` and `worker` start; if it fails, the deployment fails and the old version keeps serving.

**Deploy by hand:** Actions → Deploy staging → Run workflow (main), or `doctl apps create-deployment $APP --force-rebuild --wait`.

**Watch a deployment:**

```bash
doctl apps list-deployments $APP --format ID,Phase,Progress,Cause --no-header | head -3
doctl apps logs $APP api --type deploy
curl -sSf https://cloudoptics.cloudboosta.com/api/readyz && echo
```

**Roll back.** Decide first whether a migration came in with the bad release (`git log --oneline <good>..<bad> -- apps/api/migrations/versions`).

1. *No new migration:* App Platform rollback, which redeploys an earlier deployment's code and spec (one of the ten most recent successful ones; database data is not touched):
   ```bash
   doctl apps list-deployments $APP --format ID,Phase,Created --no-header   # pick the last good ACTIVE one
   curl -sSf -X POST -H "Authorization: Bearer $DIGITALOCEAN_TOKEN" -H "Content-Type: application/json" \
     -d '{"deployment_id": "<good-id>"}' https://api.digitalocean.com/v2/apps/$APP/rollback
   ```
   (Console: Apps → cloudoptics-staging → Activity → Rollback.) Then revert the bad commit on `main` with a pull request, or the next merge redeploys it.
2. *A new migration came in:* do not roll back the app. The old release's `migrate` job would run `alembic upgrade head` against a database that is already at a revision the old code does not know, and the deployment would fail. Migrations are expand-only (docs/11f §2.4), so roll forward: revert the application change on `main` (keep the migration), let the normal deploy run. If the migration itself is the problem, write a new migration that undoes it.
3. *Pin an exact image* (rare): set `tag: sha-<good commit>` for the image in `app.yaml` on a branch and run `python infra/digitalocean/provision.py app --branch <branch>` with the Staging setup credentials, or run Staging setup from that branch. Put `tag: main` back afterwards.

**After any rollback:** note it in the incident record (RB-10), check the worker log for jobs that failed during the switch (RB-12), and confirm `/api/readyz`.

### RB-09: Staging setup (what it changes and rotates)

**Actions → Staging setup → Run workflow** (from `main`; inputs: the GitHub environment with the credentials, default `github-pages`; `kms`, `broker_credentials`, `certificate_days` and `delete_broker_user`, see RB-20; the defaults change nothing). Run it when settings change: a new WorkOS or Postmark value, `ALERT_EMAILS`, an `app.yaml` change other than the image, or a key rotation.

| Step | Effect |
|---|---|
| Credential checks | Fails early if AWS, WorkOS or DigitalOcean secrets are missing; checks the Postmark token and the WorkOS API key against their APIs |
| `infra/aws/bootstrap.py` | Template bucket `cloudoptics-public-<account>` and templates (re-uploaded), broker role `CloudopticsBroker`. With `broker_credentials=iam-user` (default): broker user `cloudoptics-staging-broker`, and **rotates its access key on every run** (creates a new key and deletes the older ones). With `roles-anywhere`: **rotates the Roles Anywhere certificate** instead (RB-20). With `kms`: the KMS key for connection secrets |
| `provision.py app` | Renders `app.yaml` (database URLs read from the DigitalOcean API; the existing data key kept), adds the broker key, template URLs, WorkOS and Postmark settings, validates and **replaces the app spec** (this is when alert rules and sizes change), resets the database firewall to "only the app", sets alert recipients and Uptime checks when `ALERT_EMAILS` is set |
| Wait and smoke test | Waits for the new deployment to be `ACTIVE`; checks `/api/readyz`, that sign-in is WorkOS, and that the template is reachable |

Not rotated: database passwords (RB-18), the data key `CLOUDOPTICS_LOCAL_DATA_KEY` (replacing it makes stored External IDs unreadable; once KMS is on it only reads secrets stored before KMS, RB-20), WorkOS and Postmark keys (re-applied from the secrets, which you rotate in RB-18).

Side effect to expect with `iam-user` (none with `roles-anywhere`, whose previous certificate stays valid): between the key rotation and the new deployment going live (a few minutes), the running app holds a deleted AWS key, so validations and syncs fail with an AWS authentication error and retry. Run it outside busy hours and check RB-12 for failed jobs afterwards.

### RB-10: Incident response (staging, practical)

1. **Declare.** Anyone who sees an alert or a report opens an incident note (TODO: where, for example a GitHub issue with the `incident` label in a private repository; never in this public repository) with the time, what is seen and a severity from RB-05. P1 and P2 go to the primary on call (§4.2) by phone as well as email.
2. **Stabilise first.**
   - Bad release → RB-08 rollback.
   - API down, release fine → `doctl apps get $APP`; `doctl apps logs $APP api --type run --tail 300`; is the database up (`doctl databases get $DB`)? Restart by redeploying: `doctl apps create-deployment $APP --wait`.
   - Worker stuck or backlog → RB-12.
   - Abuse or traffic spike → RB-13.
   - Suspected security incident (leaked key, unexpected admin action, failed audit-chain check, cross-tenant data seen) → treat as P1: rotate the affected secret now (RB-18), revoke WorkOS sessions of affected users (WorkOS dashboard → Users → the user → sessions), preserve logs (`doctl apps logs ... > file`, App Platform keeps them only briefly), then follow RB-06.
3. **Communicate.** Design partners hear from us by email within 1 hour for P1 and 4 hours for P2 if they are affected. A personal-data breach starts the 72-hour notification clock (docs/10 A8); involve the escalation contact at once.
4. **Resolve and verify.** `/api/readyz` green, queue drained (RB-12), no new errors in the last 15 minutes of logs.
5. **Review.** Blameless review within 5 working days for P1/P2, with actions as GitHub issues.

### RB-11: Database restore and disaster recovery

**What DigitalOcean gives us.** Daily automatic backups kept 7 days, and point-in-time restore within that window. A restore always creates a **new cluster**; the existing one is never overwritten. [Restore docs](https://docs.digitalocean.com/products/databases/postgresql/how-to/restore-from-backups/). Targets (docs/10 B5): RPO ≤ 24 h, RTO ≤ 8 h.

**Gap against the plan:** docs/09 and docs/10 B5 assume daily snapshots kept 30 days and a weekly encrypted dump in a second region. Staging has 7 days in one region (LON1). Open item for the owner: accept this for staging, or add a weekly `pg_dump` to Spaces in AMS3 before production data arrives.

**Restore test (monthly, owner approval each time).** `infra/digitalocean/restore_test.py` restores the latest backup into a temporary cluster `cloudoptics-pg-restore-test-<time>`, admits only the runner's address, runs `alembic current`, counts every table, checks that the core tables hold rows, verifies every workspace's audit-log hash chain, records the result and deletes the temporary cluster (also when a step fails). It writes only to the temporary copy, inside a transaction it rolls back. Cost: the temporary cluster for 30 to 60 minutes at $0.02254/hour.

```bash
# Read-only preview: which backup, which cluster it would create.
DIGITALOCEAN_TOKEN=... uv run python infra/digitalocean/restore_test.py --plan
# The test (from a machine with the repository and `uv sync` done):
DIGITALOCEAN_TOKEN=... uv run python infra/digitalocean/restore_test.py --output restore-test-$(date +%Y%m).json
```

Or **Actions → Restore test** (type `restore` to confirm; the schedule is commented out until the owner approves a monthly run). The job summary shows pass/fail and timings; row counts stay out of the public logs. Keep the monthly JSON record (with `restore_seconds`, the measured part of the RTO) in the private ops notes. If a run is interrupted, find and delete the leftover: `doctl databases list | grep restore-test` then `doctl databases delete <id> --force`.

**Real restore (data loss or corruption):**

1. Stop writes: scale the worker to zero (console → worker → 0 instances, or `instance_count: 0` via a spec update) and, if the API is writing bad data, put the app in maintenance by rolling back or removing the `api` route. Note the time of the damage.
2. Restore to a new cluster just before that time: console → Databases → `cloudoptics-pg` → Backups → Restore to new cluster (point in time), or `doctl databases create cloudoptics-pg-restored --restore-from-cluster-name cloudoptics-pg --restore-from-timestamp "2026-11-18 09:00:00 +0000 UTC" --region lon1 --size db-s-1vcpu-1gb --version 16 --wait`.
3. Check it as the restore test does (`alembic current`, counts, audit chains), using the owner access in §4.1 on the new cluster.
4. **Re-apply deletions** (ADR-015, docs/10 T13): workspaces or connections deleted after the restore point come back. List completed deletions on the old cluster (as owner: `SELECT set_config('app.scheduler', 'on', false);` then `SELECT tenant_id, kind, target_label, completed_at FROM data_deletion WHERE status = 'completed' AND completed_at > '<restore point>';`) or, if the old cluster is gone, from the "deletion completed" emails in Postmark's activity log; run each again (RB-14).
5. Point the app at the new cluster: rename is not possible, so set `DB_CLUSTER` in `provision.py` to the new name on a branch, run Staging setup from it (it reads the new passwords, sets the firewall and redeploys), then merge the change. Scale the worker back.
6. Keep the old cluster for 7 days for investigation, then delete it. Record RPO (data lost) and RTO (time to service) in the incident review.

**Region outage (LON1):** there is no second-region copy on staging. Recovery would be a fresh `provision.py database` and `app` in another region, with data from the last backup only if DigitalOcean can still restore it. Accepted risk for staging; production needs the second-region dump above.

### RB-12: Worker stuck or backlog

**Signs:** worker `CPU_UTILIZATION` or `RESTART_COUNT` alert, users see "pending" for long, budgets or digests late.

1. Is the worker running? `doctl apps get $APP` (worker instance status) and `doctl apps logs $APP worker --type run --tail 300`. Look for `ran for more than ... and was stopped` (deadline), `Stalled job ... queued again`, out-of-memory kills, AWS errors.
2. Run the queue queries in §4.5 from `doctl apps console $APP worker`. (`oldest_ready` counts from when a job was first queued, so a job retried by hand looks old.) Many ready jobs in one queue with nothing running there → the worker is busy elsewhere or dead; a job running far past its deadline → blocked in a thread.
3. **Mitigate:**
   - Worker dead or wedged: redeploy (`doctl apps create-deployment $APP --wait`). Jobs the old worker held are queued again by `jobs.recover_stalled` within about 10 minutes (at most twice per job; then marked failed).
   - One tenant's huge bill or scan crowding others: it is bounded by the deadlines and `CLOUDOPTICS_CUR_MAX_*`; if it keeps retrying, it fails after its retries. Lower the limit for the period (env var, then Staging setup) if needed.
   - General backlog: raise capacity (§4.5).
4. **Failed jobs:** `SELECT task_name, count(*) FROM procrastinate_jobs WHERE status = 'failed' AND id > (SELECT max(id) - 5000 FROM procrastinate_jobs) GROUP BY 1 ORDER BY 2 DESC;`. Polls re-create most work on their next run (bill polls, health checks, analysis). To run one failed job again, as the app role in the console: `UPDATE procrastinate_jobs SET status = 'todo', attempts = 0, scheduled_at = now() WHERE id = <id> AND status = 'failed';` (only for jobs that are safe to repeat; all Cloudoptics jobs re-read their inputs).
5. Do not delete rows from `procrastinate_jobs` by hand; failed rows are the record.

### RB-13: Rate limiting and abuse

**How it works** (WBS API-8): fixed one-minute windows in `rate_limit_counter`, shared by every API instance. Per signed-in user 300/min, per workspace 1,000/min, per client address on public routes 60/min (sign-in config, dev token) and 20/min on the stack callback. A limited request gets `429` with `code: rate_limited`, `Retry-After` and `RateLimit` headers. The client address is App Platform's `do-connecting-ip` header; `X-Forwarded-For` is never trusted.

**Diagnose:**

```sql
-- Busiest buckets in the last 5 minutes (user:<id>, tenant:<id>, ip:<route>:<hash>).
SELECT bucket, sum(hits) AS hits, max(window_start) AS last
FROM rate_limit_counter WHERE window_start > now() - interval '5 minutes'
GROUP BY bucket ORDER BY hits DESC LIMIT 20;
```

Addresses are stored hashed, so the counter shows which route is hit, not who; `doctl apps logs $APP api --type run` shows request lines.

**Mitigate:**
- A legitimate customer hitting limits (a script, a big team): raise `CLOUDOPTICS_RATE_LIMIT_TENANT_PER_MINUTE` or `..._USER_PER_MINUTE` for the api component (console → api → Environment variables, which redeploys; record it, and put it in `app.yaml` if it should stay).
- Abuse by a signed-in user: remove them from the workspace (Settings → Users & roles, or ask the workspace admin), and in WorkOS delete or block the user and revoke their sessions.
- Abuse from addresses on public routes: lower `CLOUDOPTICS_RATE_LIMIT_PUBLIC_PER_MINUTE` / `..._CALLBACK_PER_MINUTE`. App Platform has no IP block list; a CDN/WAF in front (Cloudflare, docs/10) is the long-term answer.
- Gap: workspace `status = 'suspended'` exists in the schema but is not enforced by the API yet.

### RB-14: Data deletion requests (ADM-7)

**Self-service (preferred):** a workspace admin uses Settings → Workspace → Danger zone:
- *Delete a connection's data*: the connection must be disconnected; the admin types its AWS account ID; runs at once (`POST /v1/tenants/{t}/connections/{id}/delete-data`).
- *Delete the workspace*: the admin types the workspace name; it runs after the grace period (`CLOUDOPTICS_TENANT_DELETION_GRACE_DAYS`, 7 days) and any admin can cancel until then (`POST` / `DELETE /v1/tenants/{t}/deletion`). Admins are emailed when it is scheduled, cancelled and completed.

**A request by email** (an admin who cannot sign in, or a regulator-driven request):
1. Verify the requester is an admin of that workspace (their WorkOS identity and membership), or get written authority from one. Log the request and the verification in the private ops notes.
2. If they can sign in, walk them through the self-service steps. If not, a staff member acts only through break-glass (RB-16), never by direct SQL deletes.
3. A request to delete a **person's account** (not a workspace) is not built (ADR-015): remove their memberships, delete the user in WorkOS, and record it; `app_user` and audit rows stay until the retention purge is built. Say so in the reply.

**Check progress and the certificate:**

```sql
-- As owner (§4.1). FORCE row-level security applies to doadmin too: scope to the workspace first.
SELECT set_config('app.tenant_id', '<tenant-id>', false);
SELECT id, kind, target_label, status, scheduled_for, completed_at, verification
FROM data_deletion WHERE tenant_id = '<tenant-id>' ORDER BY requested_at DESC;
```

`verification` holds the per-table counts before and after; it is the certificate of deletion (also in the audit log as `connection.data_deleted` / `tenant.data_deleted`). A `failed` row means rows were left: the job rolled back and retried 3 times; check the worker log for `privacy:deletion.run`, fix, and the next `deletion.poll` picks it up while it is `scheduled` (reschedule with `UPDATE data_deletion SET status = 'scheduled' WHERE id = '<id>' AND status = 'failed';` as owner).

**What stays:** the audit log (ADR-015), `data_deletion` records, backups until they age out (7 days on staging). The customer's own AWS stacks are theirs to delete; the completion email lists them.

### RB-15: Exports

- Up to 50,000 rows an export is made in the request (`201`); larger exports go to the `exports` queue (`202`, task `export.generate`) and show progress. Limits: 1M rows, 50 MB compressed, 10 exports an hour per user; files expire after 7 days (`410`) and `export.expire` deletes them daily. Only the user who made an export can download it, and membership is re-checked on download.
- **Stuck export** (`queued` or `running` for more than 30 minutes): check the `exports` queue (§4.5) and the worker log for `export.generate`. A worker restart re-queues it (RB-12); otherwise the user can start a new one.
- **Failed export:** usually the row or size cap; the user narrows the date range or grouping.
- **Status by hand:** in a worker console or as owner, scoped to the workspace (`SELECT set_config('app.tenant_id', '<tenant-id>', false);`), then `SELECT status, count(*) FROM export_job GROUP BY 1;`.
- Files are workspace data: they are removed with the workspace (RB-14).

### RB-16: Break-glass access (ADM-6)

Staff access to a workspace's data goes only through the break-glass tool (ADM-6, PRD FR-SEC-07: reason, time limit, entry in the workspace's audit log). It is being built in parallel; its own runbook section will be added with it. Until it ships, staff do not read workspace data. The owner database access in §4.1 is for platform repair (queues, migrations, restores), not for reading a customer's data, and every use is recorded in the incident or change note.

### RB-17: MFA lockout

Workspaces can require MFA (`mfaRequired`). The API checks the user's enrolled WorkOS authentication factor (cached 60 s) and answers `401 mfa_required` on workspace routes without it; an admin cannot turn the requirement on from a session without MFA (`409 mfa_not_enabled`).

- **User lost their authenticator:** verify the person (a call or video with a known admin of their workspace; never on email alone). In the WorkOS dashboard (the environment of `WORKOS_CLIENT_ID`) → Users → the user → remove the authentication factor and revoke sessions. They enrol a new factor at the next sign-in. Record who verified and when.
- **The only admin is locked out and the workspace requires MFA:** same factor reset in WorkOS; the admin enrols again and signs in. Changing `tenant.mfa_required` directly in the database is break-glass only (RB-16) and must leave an audit entry.
- **Everyone suddenly gets `mfa_required`:** check the WorkOS API key works (Staging setup does this) and the API logs for errors from `/user_management/users/{id}/auth_factors`; a wrong or revoked key looks like "no factor".

### RB-18: Secret rotation (staging)

Rotate on schedule (RB-04 periods), when someone leaves, and at once on suspected exposure. Every secret lives in GitHub environment secrets (logs are public, so never as variables) and, after Staging setup, as encrypted App Platform values.

| Secret | How to rotate | Downtime |
|---|---|---|
| AWS broker key (`cloudoptics-staging-broker`) | Run Staging setup: it creates a new key, deletes the old and deploys it. Gone once Roles Anywhere is on (RB-20 step 6) | A few minutes of AWS errors (RB-09) |
| Roles Anywhere certificate and key | Staging setup with `broker_credentials=roles-anywhere`, before `certificate_days` (default 30) runs out | None |
| KMS key (`alias/cloudoptics-staging-secrets`) | Automatic yearly rotation by KMS; old versions keep decrypting | None |
| AWS admin key used by Staging setup (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`) | IAM → the admin user → create a second key → update both GitHub secrets → run Staging setup to prove it → deactivate, then delete the old key | None |
| WorkOS API key | WorkOS dashboard (same environment as the client ID) → API Keys → create → update `WORKOS_API_KEY` → Staging setup (it checks the key) → revoke the old key | None |
| DigitalOcean token (`DIGITALOCEAN_TOKEN`) | DigitalOcean → API → Tokens → new token with the same scopes (apps, databases, uptime, monitoring) → update the secret in every environment that has it (Staging setup's environment, `staging`) → run Deploy staging by hand to prove it → delete the old token | None |
| Postmark server token (`POSTMARK_API`) | Postmark → the server → API Tokens → add a token → update the secret → Staging setup (it checks the token) → delete the old token | None (emails are logged, not sent, if it is missing) |
| Database app password (`cloudoptics_app`) | `doctl databases user reset $DB cloudoptics_app` then run Staging setup at once (it reads the new password and redeploys) | API and worker fail until the deploy is live (about 5 minutes) |
| Database owner password (`doadmin`) | `doctl databases user reset $DB doadmin`, then Staging setup (the `migrate` job uses it) | None for users; the next deploy needs the new value |
| Data key (`CLOUDOPTICS_LOCAL_DATA_KEY`) | Not rotatable: it reads External IDs stored before KMS. Turn KMS on and re-encrypt (RB-20); afterwards it is no longer needed | |
| GHCR, signing | No secrets: `GITHUB_TOKEN` and keyless signing | |

After any rotation: smoke test (`/api/readyz`, sign in, one connection Check), and note the date in the private rotation tracker.

### RB-20: Secrets under KMS and broker credentials from Roles Anywhere (ADR-007)

The full owner steps, in order, with costs, are in the ADR-007 amendment. In short, one Staging setup run each:

| # | Inputs | Result |
|---|---|---|
| 1 | `kms=optional` | KMS key created; new secrets encrypted under it |
| 2 | (none) | In the worker console: `python -m cloudoptics.connectors.reencrypt`, then `--check` until it exits 0. The hourly job does the same |
| 3 | `kms=required` | API and worker refuse to start without KMS |
| 4 | `broker_credentials=roles-anywhere` | Trust anchors, profile and certificate; the app uses Roles Anywhere |
| 5 | (none) | Check a connection on staging; CloudTrail shows `CreateSession`. To go back: `broker_credentials=iam-user` |
| 6 | `broker_credentials=roles-anywhere`, `delete_broker_user=true` | IAM user and its key deleted, and taken off the app |
| 7 | `broker_credentials=roles-anywhere`, every 3–4 weeks | New certificate before the old one expires |

**All AWS calls fail with `RolesAnywhereError`:** the message says why. `expired`: run step 7 now. `HTTP 403 Untrusted certificate`: the anchor no longer holds the certificate's CA (two rotations without a deployment going live); run step 7. `AccessDenied` on the broker role: check its trust policy names both anchors (re-run step 4). To stop a leaked certificate at once: disable its trust anchor in IAM Roles Anywhere, then run step 7.

**`InvalidCiphertextException` or `AccessDeniedException` from KMS:** the key policy must name `CloudopticsBroker` (re-run with `kms` set), and the key must be enabled. A ciphertext copied between workspaces fails this way by design.

### RB-21: Google Cloud connections (ADR-019)

**Set up (once per environment):** `PROJECT_ID=<our project> AWS_ACCOUNT_ID=<broker account> ./infra/gcp/setup.sh` in Cloud Shell, then set the two printed settings (`CLOUDOPTICS_GCP_WORKLOAD_PROVIDER`, `CLOUDOPTICS_GCP_SERVICE_ACCOUNT`) on the API and worker. Nothing is secret; there is no key to rotate.

| Symptom (check or `sync_run.error_code`) | Cause | Fix |
|---|---|---|
| `federation` / `GcpNotConfigured` | The two settings are not set | Set them and redeploy |
| `federation` / `invalid_grant` or `PERMISSION_DENIED` | The pool's AWS provider does not admit the broker role (wrong account, or the broker role was renamed), or the service account binding is missing | Re-run `infra/gcp/setup.sh`; check the broker works for AWS first (RB-20) |
| `ownership` / `OwnershipNotProven` | The customer has not run the label command, or labelled another dataset | Ask them to run command 1 exactly as shown (Set up again shows it) |
| `ownership` / `AccountInUse` | The billing account is proven in another workspace | As for AWS accounts (ADR-017): disconnect it there or invite the person |
| `query_jobs` error, or `cur.ingest` `accessDenied` | No BigQuery Job User on the query project | Command 3 |
| `cur.ingest` detail `too_large: ...` | A period's query would scan more than `CLOUDOPTICS_GCP_MAX_BYTES_BILLED` (20 GiB) or return more than 500,000 groups | Check the table is the FOCUS export (partitioned) and not a copy; raise the limit for that environment only after checking the customer's query cost |
| Batch failed with `fx` error | A non-USD bill and no Cloudoptics rate nor Google rate for those days | Staff set the rate (Exchange rates page), then Reload bills |
| `cost.reconcile` mismatch | BigQuery's total changed since the load (Google restates until the invoice closes) | The next 6-hourly poll reloads it; Reload bills to do it now |

Who pays: queries run in the customer's query project at BigQuery on-demand prices; `sync_run.scope.bytesBilled` and `est_customer_api_cost_usd` show each run's share.


### RB-22: DigitalOcean connections (ADR-021)

Symptoms, fixes and owner steps are in `apps/api/src/cloudoptics/providers/digitalocean/README.md`. In short: a `failed` connection whose "Read the team" check says the token was not accepted means the customer deleted the token or it expired, and they use **Replace token…**; a `degraded` one is missing a read scope (costs still load). Never ask a customer for their token or print one: the checks say what it can do.

**Signed-in connections ("Connect DigitalOcean", OAuth, since 20 Nov 2026).** The Connections page shows **Reconnect** instead of Replace token. Tokens are checked hourly and renew about every 23 days (`connectors:connections.refresh_tokens`, one connection at a time under a row lock). A `failed` connection with the check "Renew access" (`last_error.code = refresh_refused`) means DigitalOcean refused the refresh token, usually because the customer removed Cloudoptics in DigitalOcean: the admins were emailed, and an admin uses **Reconnect** (history stays). A renewal job retrying with `server_error` or `network:…` is DigitalOcean's token endpoint being down; nothing changes until it answers. Sign-in errors at onboarding: "expired or was already used" (click again; states are single-use, 10 minutes, per admin and workspace) and `oauth_refused` (check `CLOUDOPTICS_DO_OAUTH_CLIENT_ID` / `_SECRET` and the registered callback URL `https://cloudoptics.cloudboosta.com/app/connect/digitalocean/callback`). Setup, the owner steps and the one-hour scope test: the same README. Never print an access or refresh token.

### RB-23: Azure connections (ADR-020)

**Set up (once per environment):** the owner steps in ADR-020 (the Cloudoptics app, the deployer
app with its GitHub federated credential, four GitHub variables), then Staging setup with
`azure_certificate=true`. **Rotate** with the Roles Anywhere certificate (`certificate_days`).
Details per check: `apps/api/src/cloudoptics/providers/azure/README.md`.

| Symptom (check or `sync_run.error_code`) | Cause | Fix |
|---|---|---|
| `consent` "could not sign in" (AADSTS700027 / 7000215) | Our certificate expired or is not on the app | Staging setup, `azure_certificate=true` |
| `consent` AADSTS700016 / 65001 | The customer's admin has not consented | They open the consent link (Set up again shows it) |
| `ownership` error | The storage account lacks the `cloudoptics-verify` tag with this connection's code | They run the Cloud Shell script again |
| `ownership` "another workspace" | The scope is verified in another workspace (ADR-017) | Disconnect it there or invite the person |
| `blob_reader` / `cur.ingest` `AuthorizationPermissionMismatch` | No Storage Blob Data Reader on the container | The script; role assignments take up to 10 minutes |
| Batch failed with `fx` error | A non-USD bill, no Cloudoptics rate and no `x_BilledCostInUsd` | Staff set the rate, then Reload bills |
| `cost.reconcile` mismatch | Microsoft restated the month after the run | The next daily run reloads it; Reload bills to do it now |

Azure does not charge for these calls. Cost Management queries are limited per customer directory (QPU quotas; a 429 is retried after the wait Azure gives).

### RB-19: Security-audit follow-ups

The Sprint 6 audit (security-audit run 1, report kept outside the repository) had no confirmed findings: every candidate was left as "needs validation" because no target code could be run in a sandbox. Fixes have shipped for the leads in #76 (deploy guards: owned template URLs, push-only staging deploys, secrets-only setup, no public dev-auth port), #77 (MFA policy enforced), #78 (stack callback cannot act on the External ID alone; links expire) and #83 (bounded worker work per tenant, leads 11 to 17).

Still open (owner checks, outside the code):

| Check | Where | Owner |
|---|---|---|
| WorkOS MFA settings match the product promise (factor types, enrolment) | WorkOS dashboard | TODO |
| Fork pull requests need approval before workflows run; required reviewers on the `staging` and Staging setup environments | GitHub → Settings → Actions, Environments | TODO |
| No credentials in the Variables tab of any environment | GitHub → Settings → Environments | TODO |
| `cloudoptics-public-<account>` bucket is ours, and nothing refers to the old `cloudoptics-public` name | AWS S3, repository search | TODO |
| Branch protection on `main` (README "Repository settings to apply") | GitHub | TODO |
| Validate each lead that is still "needs validation" with a running copy, or mark it resolved by the fix PRs, in the audit report | Audit report | TODO |

When a new lead is reported (audit, disclosure, dependency alert): log it privately, triage within 24 hours, fix within the docs/10 A8 SLAs (Critical 7 days, High 30, Medium 90), and add a regression test with the fix.
