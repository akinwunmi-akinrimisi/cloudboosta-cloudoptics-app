# 12 — Competitor Build Teardown: How They Built It and What We Build

**Product:** Cloudoptics by Cloudboosta
**Scope:** AWS native tools, Vantage, CloudZero, nOps, DoiT, Flexera One (with ProsperOps and Spot Eco), and IBM Cloudability (Apptio/Kubecost)
**Date:** 2026-10-22
**Inputs:** Phase 1–2 research (docs 01–02), the PRD (06), architecture (09, 11a–11c)

---

## 0. How this teardown was done

| Source | What we looked at | Competitors |
|---|---|---|
| **REA (`rea-agents` v6.7.0)** with a headless Chromium (CDP on port 9222) | `analyze-web-bundle` and `analyze-javascript-application` on the public web-app bundles, then a crawl of every lazy-loaded chunk | Vantage (824 chunks, 13.6 MB), nOps (744 chunks, 16.4 MB), CloudZero (275 chunks, 26 MB), DoiT (CRA main chunk), Flexera (shell + prefetched app) |
| **Published source maps** | Flexera ships full source maps on its login page. We recovered 162 maps covering 616 packages and 6,760 TypeScript source files of Flexera One, including the Optima cost module | Flexera |
| **Open-source code** | Flexera `policy_templates` (the actual AWS cost rules and their default thresholds), OpenCost (Apache-2.0), the Vantage Terraform provider | Flexera, Kubecost/IBM, Vantage |
| **Public docs, IAM templates, pricing pages, reviews** | CloudFormation templates, IAM policy JSON, onboarding guides, threshold documentation, G2/Reddit complaints | All seven |

Limits: everything here comes from what each company ships to the browser or publishes. Backend behaviour (ML models, pipelines) is inferred from the API calls the front ends make. Minified bundles gave reliable routes, API paths, enums, field names, feature flags and UI copy, but not algorithms; REA's call graph over minified code was not useful on its own. Flexera is the exception because its real source is public through the source maps.

Working notes (not in the repo): `scratchpad/teardown/{flexera_frontend, nops_vantage_frontend, cloudzero_doit_frontend, vantage_cloudzero_nops, doit_prosperops_flexera_ibm_aws, oss}.md`.

---

## 1. One-page summary

**What every competitor has in common**

1. Onboarding is a CloudFormation quick-create link that makes a cross-account IAM role with an External ID, plus a CUR export. The good ones (CloudZero, DoiT) poll for the role and give precise errors ("could not assume role", "no CUR 2.0 manifest found").
2. Recommendations are a flat record: a type, a monthly savings figure, the resource, and a bag of metrics. Most of the logic is AWS Compute Optimizer / Cost Optimization Hub re-presented, plus a few billing-only rules (idle IP, gp2→gp3, old snapshots).
3. A status workflow (open / in progress / snoozed / dismissed / done), Jira and Slack.
4. One display currency per organisation or workspace.
5. A chat assistant and an MCP server (CloudZero, DoiT, nOps, Vantage all ship one; Flexera's is Q&A only).

**What none of them has**

| Gap | Evidence |
|---|---|
| A **risk score + confidence** on each cost recommendation, with the reason in plain words | Flexera's recommendation model has no risk, confidence or effort field (source code). CloudZero has effort only. nOps has effort + a yes/no "rollback possible". Vantage has none. DoiT has severity + fix complexity but no confidence or lookback shown. |
| **Verified** realised savings | Flexera's "realized" is a user click. CloudZero and nOps measure the 30 days after; nobody ties it to the original evidence and shows the before/after in the customer's currency. |
| **Guided fixes that need no write access and no Terraform** | Flexera's only in-product action (delete unused volume) is enabled for 2 internal test orgs. Vantage and nOps act through write access in the customer account. DoiT's fixes are Terraform pull requests (needs a VCS connection and an IaC estate). CloudZero has no fix action at all. |
| **USD and local currency side by side**, with admin-set, dated, auditable rates | Flexera: one base currency, ECB fallback (no NGN, KES, GHS, EGP), 48-hour lock, "$" hard-coded in alerts. Vantage: one currency per workspace, custom rates CSV-only and MSP-only. CloudZero: one org currency, history not reconverted. DoiT: 22 fixed currencies, no NGN/KES/GHS, silent fallback to USD. nOps: USD only for AWS. |
| **Africa context** | No af-south-1 type-availability checks, no African timezones in schedules, no FX stress test on commitments, no local payment rails, no WhatsApp. |
| **SME price point** | Flexera ~2.7–5% of spend, Cloudability $30k/yr floor, ProsperOps $10k/month spend floor, nOps and ProsperOps take a share of savings, Vantage Autopilot 5% of savings. |

**Our position in one line:** the only cost tool that tells an African engineering or finance team *how safe* each saving is, *how to do it* without giving us write access, and *what it means in naira (or shillings, cedis, rand)*, then proves it happened.

---

## 2. How each competitor is built

### 2.1 AWS native (Compute Optimizer, Cost Optimization Hub, Trusted Advisor, Cost Anomaly Detection, Budgets)

| Area | How it works |
|---|---|
| Access | Service opt-in and service-linked roles. No cross-account vendor role. |
| Idle EC2 | Compute Optimizer: **peak CPU < 5% and network < 5 MB/day over 14 days**. Trusted Advisor (Business support and above): avg CPU ≤ 10% and network ≤ 5 MB on 4 of 14 days. |
| Rightsizing | P99.5 / P95 / P90 CPU with 0–30% headroom, 14 / 32 / 93-day lookback. Performance risk 0–4 per option. |
| Hub | Cost Optimization Hub deduplicates overlapping savings, normalises to 730 hours, and carries effort, "restart needed" and "rollback possible" flags. |
| Commitments | Savings Plan purchase recommendations over 7 / 30 / 60 days. Advisory only. |
| Anomalies | ML; default alert > 40% and > $100; needs ≥ 10 days of history. |
| Currency | USD. |
| **Strengths** | Free, authoritative data, good taxonomy (effort / restart / rollback). |
| **Weaknesses** | Five consoles, no business context, Trusted Advisor cost checks need a paid support plan, CLB-only ELB check, no FX, no workflow. |

**We use it as a feeder**: ingest Compute Optimizer and Cost Optimization Hub findings where enabled and show "AWS says / we say" side by side.

### 2.2 Vantage (Rails + Inertia + Vue 3, Tailwind, Chart.js)

| Area | How it is built |
|---|---|
| Front end | Rails with Inertia.js and Vue 3 single-file components, a Rails UJS legacy layer, web components, Tailwind with a `tw-` prefix, Monaco for SQL billing rules and VQL. One filter language (VQL) across reports, budgets, policies and virtual tags. |
| Onboarding | Payer account recommended; one role with External ID via CloudFormation, CLI, Terraform or StackSet. **Default role includes writes**: RI purchase/modify, `savingsplans:*`, `support:*`, quota increase, `ec2:CreateTags`. Detects competitors' CUR exports in the customer account. |
| Data | CUR 2.0 created by their stack; Cost Explorer totals for instant first view; refresh every 8–12 h; FOCUS 1.3 import/export. |
| Recommendations | Active → Approved → In Progress → Completed / Archived. "Approve" means the **Vantage Agent makes the change**. Autopilot buys Savings Plans automatically (5% of savings). A GitHub agent writes rightsizing PRs. No risk, effort or confidence fields. Thresholds: snapshots > 60 d, mostly Compute Optimizer below that. |
| Anomalies / budgets | Own ML per service on 6 months of data, documented noise filters, single-resource attribution if > 50%. Hierarchical budgets with roll-up. Scenario models. |
| Currency | One display currency per workspace (daily or end-of-month public rate). Custom dated rates only by CSV upload, only in MSP currency management. |
| AI | "Scout" FinOps Agent (Vercel AI SDK UI ported to Vue) with model picker, citations, tool calls, MCP connectors (GitHub, Linear, Notion, Datadog). "Canvases" turn prompts into live cost tables. |
| Pricing | Free to $2.5k spend; $30 (≤ $7.5k); $200 (≤ $20k); Enterprise %. |
| **Strengths** | Fast self-serve onboarding, broad provider list, one filter language, cheap entry tier, good docs per recommendation type. |
| **Weaknesses** | Write permissions in the default role; automation-first; no risk model; single currency; 2-day display lag; retention limits by tier. |

### 2.3 CloudZero (React, TanStack Router + Query, Jotai, Chakra UI v3, AG Grid/Charts Enterprise)

| Area | How it is built |
|---|---|
| Front end | About 60 routes; mid-rewrite from `legacy.cloudzero.com` to "cz-next" behind LaunchDarkly flags. Auth0. REST only, everything under `/organizations/{id}/…`. Embedded Looker for analytics. Amplitude session replay at 100%. |
| Onboarding | CloudFormation from `cz-provision-account.s3…/connected_account.yaml`; separate **billing** (payer) and **resource** (member) stacks; IAM policy JSON fetched live; precise validation errors; generates a bucket-scoped policy from the bucket name. **No writes in the default connection.** |
| Data | Hourly CUR; AnyCost / Common Bill Format for non-AWS; telemetry streams for unit cost; data does not reconvert when currency changes. |
| Recommendations | Types come from the server (`/recommendation_types`) with an org-level **type catalogue** to switch types on/off. Status: new / in_progress / on_hold / addressed / ignored. **Effort** (not_set / low <1 day / medium 3–5 days / high 1+ weeks) set by the user. Savings per 30 days. Realised savings = actual saving over the 30 days after "addressed". AI summary with thumbs up/down. **No risk, no confidence, no fix action.** Thresholds: snapshots > 180 d and ≥ $500, idle EIP via CUR `PublicIPv4:IdleAddress` ≥ 7 d, prev-gen ≥ $500. |
| Anomalies / budgets | Hourly detection; anomalies stored as insights; monitors with normal range and threshold. Budgets attach to a View with same / split / custom monthly limits, forecast thresholds, pacing, CSV bulk import. |
| AI | MCP server with OAuth clients for Claude Code, Cursor, Codex; "AI Signals" for LLM spend. |
| Pricing | Quote; ~$19 per $1k AWS spend on Marketplace; median contracts ~$43–57k/yr. |
| **Strengths** | Allocation and unit economics, clean org-scoped API, best onboarding error messages, server-driven recommendation catalogue. |
| **Weaknesses** | Enterprise price, no fix path, no risk, one currency, navigation rewritten more than once, heavy client telemetry. |

### 2.4 nOps (React 18, Redux + redux-saga, Ant Design, AG Grid Enterprise, ECharts + Chart.js)

| Area | How it is built |
|---|---|
| Front end | Large legacy monolith (one 710 KB chunk, class and hook components mixed). Menu: Inform / Optimize / Operate, Feed as landing page. Hasura GraphQL IDE embedded. ConfigCat flags. |
| Onboarding | Automatic (CloudFormation, payer) or manual (role ARN, External ID, CUR details). Access level **"Full Access" or "Minimum Access"**; users told to attach `ReadOnlyAccess`; inline policy includes `support:CreateCase`, `cur:PutReportDefinition`, `events:CreateEventBus`, S3 bucket-policy writes. Commitment management needs a **new AWS account** plus a stack, then "the nOps team will contact you". |
| Data | Legacy CUR hourly Parquet required; assets hourly, billing daily. |
| Recommendations | EC2/ASG rightsizing, idle EC2/EBS, gp2→gp3, snapshot archive (latest ≥ 30 d or > 25% unreferenced blocks), **scheduler (the only competitor with R7)**, RDS scheduler, Karpenter, log group underuse. Fields: **effort L/M/H** and **rollback possible yes/no**. Dismiss = "Override". Fixes run through a customer-deployed EventBridge / CloudFormation stack and in-account Lambdas. |
| Currency | AWS is USD only; formatters hard-code `en-US`/`USD`. No FX. |
| AI | "Clara" assistant (threads, scheduled prompts, MCP), metered per month. |
| Pricing | Share of savings (rate optimisation), fixed fee by spend (visibility), plan gating (BC+ / Inform / Operate / Essentials). |
| **Strengths** | Real automation (one-click EBS delete, gp3, scheduler), scheduling with ML, Karpenter. |
| **Weaknesses** | Write access and in-account Lambdas; sales-assisted commitment flow; complex tiers; USD only; heavy front end. |

### 2.5 DoiT (CRA, React, MUI + MUI X, Firebase/Firestore, Zustand, ECharts)

| Area | How it is built |
|---|---|
| Front end | About 670 route entries; reads mostly straight from Firestore, plus REST under `/v1/…` and tRPC for the Ava assistant. Unleash flags; per-customer `earlyAccessFeatures`. Internal staff surfaces (NetSuite, collections, COGS) ship in the same bundle. |
| Onboarding | Two stages: **read-only billing role + CUR 2.0** first, then an optional second stack for resource metadata. Connection states: active, curExportWaiting, handshakePending, roleValidationFailed. Base read uses broad managed policies (`Billing`, `SecurityAudit`). |
| Data | CUR 2.0 (default since June 2026); CUR every 6–8 h; anomalies every 2 h. |
| Recommendations ("Insights") | Status active / resolved / dismissed / inactive; **severity** (critical–low) and **fixComplexity** (simple / intermediate / complex) kept separate; six categories; `easyWin` flag; snooze until a date. Fix = **Terraform PR agent** (statuses pr_created, pr_merged, plan_failed, resource_not_in_repo, no_vcs_connection…) or "Ask an expert". |
| Commitments | Flexsave (DoiT-owned commitments, needs reseller relationship); PerfectScale for Commitments with policy knobs: target 65/80/90%, 60-day lookback, 7-day steps, risk level, simulator. |
| Anomalies / budgets | Prediction interval on time series; floors **$50 per SKU / $100 per service**; 14-day baseline; AI root-cause narrative; per-service sensitivity with audit. Budgets: fixed or recurring, seasonal limits, 50/85/100% alerts, forecasted breach date, **budget suggestions**. |
| Currency | Fixed list of 22 currencies; includes EGP and ZAR, **no NGN, KES or GHS**; falls back to USD with a banner when a rate fails. |
| **Strengths** | Separate risk and effort axes, two-stage read-only onboarding, best anomaly explanations, commitment policy knobs, budget suggestions. |
| **Weaknesses** | Built around reselling; Terraform-only fixes; no African currencies that matter most; broad managed policies; real-time anomalies don't support af-south-1 CloudTrail buckets. |

### 2.6 Flexera One (with Spot Eco and ProsperOps) — built from its real source

| Area | How it is built |
|---|---|
| Front end | Monorepo of React **micro-frontends** (`@flexera/<area>.<pkg>`) composed by Webpack Module Federation and import maps. Context + hooks, older Redux + redux-saga. AG Grid Enterprise (server-side row model), amCharts 4, Formik, Luxon (UTC), `ttag` i18n, **Split.io** flags, capability + `service:resource:action` privilege permissions. Four or more API generations side by side (RightScale-era hosts to `api.flexera.com/*/v1`). |
| Onboarding | External ID, then CloudFormation quick-create of a **read-only** role with CUR bucket parameters. Policy credentials are separate and per-policy; write actions are opt-in and flagged with `*`. |
| Recommendation record | `id, rule_id, source (rule|policy), policySet, generationId, account, region, service, resourceType, resourceID, tags, billingCenter, savings (per month), type (Usage Reduction | Rate Reduction), status (active | snoozed | rejected | realized), statusReason, snoozedTargetDate, detailsExtended{cpu/mem avg,p95,max; iops; threshold; lookback; newResourceType…}, ruleConfiguration`. **No risk, confidence, effort, blast radius or rollback field** (grep confirmed). `generationId` rejects stale status writes. |
| Status workflow | Reject needs a reason; snooze needs a date and a reason; "realized" is a user click and terminal. Rate reductions have no status at all. |
| Grouping | Server-side `…/recommendations/groups?groupBy=a,b` returns `{value,count,totalSavings,children}`; one OData-like filter string drives grid and CSV export. Savings summary includes a **SP/RI × 1y/3y × payment-option matrix**. |
| Action | Only "delete unused volumes", enabled for 2 internal test orgs, no preview or rollback. Real remediation needs the Policy Engine: templates, credentials, RRULE schedules, incidents with approve/deny. |
| Tuning | Feature-flagged admin page: per-rule typed parameters (number / enum / text with min, max, default), `min_savings_threshold > 0`, "applies on next refresh", change history, reset to defaults. |
| Rule defaults (policy templates) | Idle EC2: avg CPU ≤ 5% (or mem ≤ 5%) over 30 d. Rightsizing: avg CPU/mem ≤ 40% over 30 d (averages, not percentiles). Snapshots > 30 d. Idle LB: no listeners or no healthy targets ≥ 7 d. Tag schedules like `08:00-19:00;MO-FR;Africa/Lagos`. |
| Cost model | Moving to FOCUS column names (`BilledCost`, `EffectiveCost`, `ListCost`, `Modified*Cost`). Billing centres (8 levels), effective-dated rule-based dimensions, shared-cost splits, adjustments and billing plans. |
| Anomalies / budgets | Legacy: rolling mean ± 2σ, not user-tunable, ≥ $1k/period. Budgets typed into a monthly grid by hand. |
| Currency | One base currency per org, **locked for 48 hours after each change**; opt-in conversion; **admin "fixed rates" with validFrom/validTo periods**, several currencies per period, strict validation (> 0, ≤ 8 decimals); fallback to **ECB rates** (no NGN, KES, GHS, EGP); each cost row carries `RateSource` (platform / fixed / mixed) and `RateToBaseCurrency`; reconciliation view shows original vs converted. Alert copy hard-codes "$". |
| Commitments | Spot Eco and ProsperOps: autonomous buying, measured by Effective Savings Rate, waste, coverage, utilisation. ProsperOps always has write access, no per-action approval, ~25–35% of savings, $10k/month floor. |
| Pricing | ~2.7–5% of spend (third-party). |
| **Strengths** | Deep allocation and MSP billing; tunable rules; closest currency model to ours; open, documented rule thresholds. |
| **Weaknesses** | Enterprise complexity and legacy debt; no risk; action is fragmented; one currency; averages instead of percentiles; manual budgets; crude anomalies. |

### 2.7 IBM Cloudability (Apptio, with Kubecost)

| Area | How it is built |
|---|---|
| Onboarding | `CloudabilityRole` via CloudFormation (historically IAM-user trust); hourly CUR. |
| Rightsizing | Proprietary "clipping" risk per option, multiple options; idle = < 2% CPU time over 10 (or 30) days. |
| Commitments | Planner plus Savings Automation add-on (SP + convertible RI, flexibility %), 5 extra write permissions. |
| Containers | Kubecost (OpenCost core is Apache-2.0; CUR reconciliation of node costs is the commercial part). |
| Pricing | ~2.2–3% of spend, $30k/yr minimum (third-party). |
| **Strengths** | Container allocation, risk per rightsizing option. |
| **Weaknesses** | Price floor, enterprise UX, mostly manual action (Jira), very conservative idle rule. |

---

## 3. Feature-by-feature comparison

| Capability | AWS | Vantage | CloudZero | nOps | DoiT | Flexera | Cloudability | **Cloudoptics (plan)** |
|---|---|---|---|---|---|---|---|---|
| Default role is read-only | n/a | No | **Yes** | No | Yes (broad) | **Yes** | Yes | **Yes, tight allow-list** |
| Two-stage onboarding (billing, then resources) | n/a | No | Yes | No | **Yes** | No | No | **Yes** |
| Risk score per recommendation | Perf risk (R5 only) | No | No | No | Severity | No | Per option | **Yes, with reason** |
| Confidence / lookback shown | Partly | No | No | No | No | In details bag | No | **Yes** |
| Effort | Yes | No | User-set | L/M/H | Complexity | No | No | **Yes** |
| Rollback info | Yes/no | No | No | Yes/no | No | No | No | **Rollback steps** |
| Fix path | CO automation (EBS) | Agent with writes | None | In-account Lambdas | Terraform PR | PoC + policy engine | Jira | **Console + CLI + Terraform + rollback, read-only** |
| Realised savings verified | No | Partial | 30-day actual | Ledger | No | User click | No | **Evidence-linked, both currencies** |
| Non-prod scheduling (R7) | No | No | No | **Yes** | Spot only | Tag policy | No | **Yes, African timezones** |
| SP/RI advice | Yes | Autopilot | Advisory | Managed | Flexsave / policy | Managed | Planner | **Advisory + FX stress test** |
| Dual USD + local display | No | No | No | No | No | No | No | **Yes** |
| Admin-set dated FX rates | No | CSV, MSP only | No | No | No | **Yes** | ? | **Yes, with approval and audit** |
| NGN / KES / GHS supported | No | Display only | Display only | Symbol only | **No** | Manual only | ? | **Yes, first-class** |
| Tunable thresholds | Some | No | Type on/off | No | Sensitivity | **Yes** | No | **Yes (v1)** |
| Entry price for a $5k/month AWS bill | Free | ~$30/mo | Quote | Quote / % | Free tier | Quote | $30k/yr floor | **Flat, local currency** |

---

## 4. Strengths we match (table stakes)

These are not differentiators, but a buyer will expect them. Each maps to an existing PRD or design item, or a new one marked **NEW**.

1. **One-click CloudFormation onboarding with External ID, live role polling and precise errors** (CloudZero, DoiT). PRD Epic E1. **NEW detail:** reuse CloudZero's error strings as a pattern ("We could not assume the role", "No CUR 2.0 manifest at that path", "Export exists but first delivery pending — usually within 24 h").
2. **Cost Explorer totals for an instant first view, CUR for detail** (Vantage). Already in PRD §2 (60-minute first value).
3. **Status workflow with reasons**: snooze needs a date and reason; dismiss needs a reason (Flexera); in progress / on hold states (CloudZero). PRD Epic E5.
4. **Server-side grouping with totals** and **one filter expression for grid and export** (Flexera). Add to 11b recommendation API.
5. **Recommendation type catalogue**: org admins can switch types off; rule definitions come from the server so new rules ship without a front-end release (CloudZero). **NEW** FR-REC-08.
6. **Rule tuning** with typed parameters, defaults, min/max, change history and "applies on next refresh" (Flexera). PRD v1 (post-MVP); data model must carry `rule_config_snapshot` from day one.
7. **Budgets**: same / split / custom monthly limits, forecast alerts, pacing chart, CSV import, forecasted breach date (CloudZero, DoiT). PRD Epic E6. **NEW:** auto-suggest a budget from the trailing 3 months (DoiT); Flexera makes users type every month.
8. **Anomaly floors and feedback**: absolute floors ($50 SKU / $100 service, DoiT) plus %, "ignore with feedback" (nOps), plain-language root cause (DoiT). PRD Epic E6. **NEW:** floors also expressed in local currency.
9. **Jira and Slack** from a recommendation or anomaly. PRD §6.
10. **Savings Plan option matrix** (SP vs RI × 1y/3y × payment option) with break-even months (Flexera). PRD FR-ANA-10.
11. **FOCUS column names** in our cost model (`BilledCost`, `EffectiveCost`, `ListCost`, `BillingCurrency`, `SubAccountId`) (Flexera, Vantage, OpenCost). Doc 09 already FOCUS-aligned; confirm names.
12. **Org-scoped REST API** `/organizations/{id}/…` (CloudZero). Matches our tenant model.

---

## 5. Weaknesses we exploit — the moat

### Moat 1: The trust layer (risk, evidence, guided fix, rollback, proof)

No competitor scores risk on cost recommendations and explains it. We ship every recommendation with:

- **Evidence**: metric, statistic (p95, max), lookback days, observed value vs threshold, and a small chart. Flexera has this in a hidden details bag; Vantage and CloudZero show none.
- **Risk score (1–5) with the driving reason** in one line: "Memory unknown (no CloudWatch agent): risk raised one level", "Tagged env=prod", "Detached 3 days ago", "Peaks at 92% CPU on the 25th (payroll run)".
- **Confidence** (Low / Medium / High) from lookback length and data completeness. Detection uses AWS-aligned thresholds; *high* confidence only when the longer window is met (e.g. EBS unattached ≥ 32 d).
- **Effort** (CloudZero's scale: < 1 day / 3–5 days / 1+ week) and **reversibility** (reversible / reversible with snapshot / irreversible).
- **Guided fix**: console steps, CLI, Terraform/CloudFormation snippet, **rollback steps**, all generated for the specific resource. No write access, no VCS connection, no in-account Lambda.
- **Verified realised savings**: after "done", we check the next CUR periods for the resource, link the result to the original evidence, and show before/after in USD and local currency. Flexera and Vantage rely on user claims.

Why it compounds: every executed fix adds to a per-customer, per-rule history of outcomes. That history lets us later offer "auto-approve low-risk classes" rules that competitors cannot justify, and tighten thresholds per customer.

### Moat 2: Local currency as the core, not a setting

| Competitor behaviour | Our behaviour |
|---|---|
| One currency at a time (all) | **USD and local side by side**, toggle USD / Local / Both (doc 08) |
| ECB / xe.com / "public rate" feeds without NGN, KES, GHS | **Admin-set daily rate** (FR-FX-06/07), feed only as a reference, 5% warning and 10% second-admin approval |
| Rates changed silently; history not reconverted (CloudZero) | Rates are **dated and versioned**; each figure carries `rate_source` and `rate_id` (Flexera's pattern, made visible) |
| Silent fallback to USD (DoiT) | Values from the feed are flagged "estimated"; never silent |
| "$" hard-coded in alerts (Flexera) | Every alert and budget in both currencies |
| No FX view on commitments | **FX stress test** on Savings Plans (local cost if the currency weakens 20% / 40%); default 1-year No Upfront for FX-exposed customers |
| No FX decomposition | **"Spend +12% in NGN, +3% in USD: 9 points are FX"** on overview and anomalies; FX-only movements are not raised as usage anomalies |

### Moat 3: Africa-specific engineering

- af-south-1 instance-type availability checked before any R5/R6 suggestion; af-south-1 pricing in every savings figure.
- R7 schedules in the customer's IANA timezone (Africa/Lagos, Africa/Nairobi, Africa/Johannesburg, Africa/Cairo, Africa/Accra).
- R2 asks "Is this IP allow-listed by a bank or partner?" before suggesting release — common in African fintech integrations.
- Region trade-off view: af-south-1 vs eu-west-1 / eu-south-1 / me-central-1 cost and latency, and data-residency notes (NDPA, Kenya DPA, POPIA) for moving *into* af-south-1.
- Low-bandwidth front end: small bundles, server-side aggregation, PDF and email digests (competitors ship 13–26 MB of JS; nOps has a single 710 KB chunk).
- Local payments (Paystack / Flutterwave) and local-time support.

### Moat 4: Least privilege, published

- Default role is a **tight allow-list**, not `ReadOnlyAccess` (nOps) or `Billing` + `SecurityAudit` (DoiT), and has **no writes** (unlike Vantage and nOps).
- The CUR / BCM Data Export is created by the CloudFormation stack, not by our role.
- We publish the exact action list per recommendation type (Flexera does this in its policy READMEs; it builds trust with banks and telcos).
- Any future automation role is a separate, optional stack scoped by tag condition.

### Moat 5: SME price and simplicity

- Priced for $2k–50k/month AWS bills, flat, in local currency; no share of savings, no exit fees, no $30k floors.
- Seven top-level areas (Overview, Cost Explorer, Recommendations, Anomalies, Budgets, Reports, Settings), not Flexera's billing centres + cost centres + RBDs + adjustments + billing plans.
- Value in under 60 minutes (PRD §2).

### What we deliberately do not copy

| Competitor feature | Why we skip it (for now) |
|---|---|
| Autonomous commitment buying (Vantage Autopilot, ProsperOps, Spot Eco) | Needs write access; FX-exposed customers should not have currency bets made for them. Advisory + FX stress test instead. |
| Broad AI agent with tool execution (Vantage Scout, DoiT Ava, nOps Clara) | Expensive, and an agent that acts conflicts with our read-only trust story. Ship narrow "Explain this cost change" and "Explain this risk" in v1; MCP server later. |
| Terraform PR agent (DoiT) | Most of our ICP has little IaC. Terraform snippets in the guided fix cover the teams that do. |
| Micro-frontend monorepo (Flexera) | Their own debt signals (four API generations, duplicated helpers) show the cost. One React app with route-level code splitting (doc 11a). |
| 100% session replay (CloudZero) | Conflicts with our privacy-first posture; PostHog with masking and sampling (doc 11a). |

---

## 6. Concrete changes to our build

These are proposals for the PRD (06), data model (09) and component spec (11b). They need PM sign-off before they replace the current PRD text.

### 6.1 Recommendation thresholds (R1–R8)

| Rule | Current PRD | Proposed change | Source |
|---|---|---|---|
| **R1 Unattached EBS** (FR-ANA-01) | `available` ≥ 7 d | Keep ≥ 7 d as candidate; **High confidence at ≥ 32 d**. Use our own daily state snapshots for detach time (CloudTrail lookup only covers 90 d). Snapshot-before-delete on by default. | Trusted Advisor, Compute Optimizer, Flexera |
| **R1b Idle attached EBS** | — | **NEW**: non-root volume with < 1 read+write op/day over 14 d (extend to 32). Medium risk (may be cold DR storage). | Compute Optimizer |
| **R1 Old snapshots** (FR-ANA-01b) | > 90 d, source gone, not AMI/Backup | Keep 90 d (configurable 30/60/90/180). Also exclude DLM-managed and `retain`/`compliance` tags. Offer **EBS Snapshot Archive** as the lower-risk fix. | Flexera (30 d), Vantage (60 d), CloudZero (180 d) |
| **R2 Idle EIP / IPv4** (FR-ANA-02) | Unassociated ≥ 3 d | Keep 3 d as candidate; **High confidence ≥ 7 d**. Detect from CUR `PublicIPv4:IdleAddress`. Also flag EIPs on stopped instances. Ask the allow-list question; release needs typed confirmation (irreversible). | CloudZero, AWS pricing |
| **R2 Idle LB** (FR-ANA-02b) | 0 healthy targets or < 100 req/day for 14 d | Add "no listeners ≥ 7 d"; ALB `RequestCount` < 100/day on 7 of 14 days; NLB `NewFlowCount`/`ProcessedBytes` ≈ 0 over 14 d; minimum LB age 7 d. Check Route 53 for records pointing at it (raises risk). Rollback = exported listener/rule JSON. | Flexera, Trusted Advisor |
| **R3 gp2 → gp3** (FR-ANA-03) | Price difference incl. extra IOPS/throughput | gp3 IOPS = max(3000, 3 × GiB) capped at 16,000; throughput from observed peak (≥ 125 MiB/s; 250 MiB/s for ≥ 170 GiB). **Extend to io1 → gp3 where IOPS fit, and RDS gp2 → gp3.** Warn about the modification cooldown. | AWS docs, Flexera, nOps |
| **R4 Idle EC2** (FR-ANA-04) | CPU p95 < 5%, network < 5 MB/day avg, 14 d | Use **peak (max) CPU < 5%** for "idle" (Compute Optimizer); keep p95 < 5% as a "low utilisation" candidate. High confidence at 32 d. Keep ASG and `cloudoptics:ignore` exclusions; exclude instances < 14 d old. Fix = stop first; terminate only after N days stopped and a final AMI. | Compute Optimizer, Cost Optimization Hub |
| **R4b Idle RDS** (FR-ANA-04b) | 0 connections 7 d | Add CPU and IOPS low over 14 d; exclude read replicas and Aurora global secondaries; warn **stopped RDS restarts after 7 days**. | Trusted Advisor, Compute Optimizer |
| **R5 Rightsizing** (FR-ANA-05) | p95 / max checks, projected p95 ≤ 60%, max ≤ 85% | Add: **32-day default lookback** (captures month-end payroll and salary cycles); **prod-tagged = P99.5 + 20% headroom**, non-prod = P95 + 30%; **never average CPU** (Flexera's default causes Cloudability-style clipping). Show Compute Optimizer's `performanceRisk` next to ours. Normalise savings to 730 h; say when SP/RI coverage reduces the real saving. | Compute Optimizer, Flexera (anti-pattern) |
| **R6 Prev-gen / Graviton** (FR-ANA-06) | Family map + Graviton separately | Add af-south-1 type-availability check. Graviton only labelled Low effort when Compute Optimizer migration effort is Very low/Low or the workload is known multi-arch; otherwise a test checklist, not a one-click fix. **NEW R6b: RDS/EKS Extended Support charges** (billing-only, high value). | Compute Optimizer, Flexera policies |
| **R7 Non-prod schedule** (FR-ANA-07) | Weekdays 07:00–20:00 local | Keep. Add Flexera-style schedule tag grammar (`07:00-20:00;MO-FR;Africa/Lagos`) so the guided fix is a single tag plus EventBridge Scheduler. RDS: note 7-day auto-start. | Flexera, nOps |
| **R8 SP advisory** (FR-ANA-10) | AWS recommendation, 30-day lookback, conservative = × 0.8 | Add our own simulation: **target = 60–80% of the trailing 60-day hourly p10–p20 baseline**, computed **after** R4/R5/R7 savings are netted out (no double counting). Show the SP/RI × term × payment matrix with break-even months, a ladder suggestion (20–35% first step, then ~10% monthly), expiry alerts at 60/30/15 days, and remind about the 7-day return window. Default 1-year No Upfront; FX stress test at 20% and 40%. | DoiT PS4C, Flexera, ProsperOps, COH |

### 6.2 New recommendation types (post-Increment 2, ranked by value ÷ effort)

| ID | Type | Rule | Why |
|---|---|---|---|
| R9 | **Idle NAT Gateway** | `NatGateway-Hours` > 0 and `NatGateway-Bytes` = 0 over 30 d | Billing-only, trivial, NAT is a top bill line for African startups |
| R9b | **NAT → VPC gateway endpoint** | NAT bytes to S3/DynamoDB above a floor | Often the single largest quick win |
| R10 | **CloudWatch Logs retention** | "Never expire" log groups with stored bytes above a floor | Flexera has no AWS template for it |
| R11 | **S3 incomplete multipart uploads / no lifecycle** | Abort-lifecycle rule as the fix; savings from per-bucket `TimedStorage-ByteHrs` | Low risk |
| R12 | **Stopped EC2 still paying for EBS and IPv4** | Stopped > 30 d | Common leftover |
| R13 | **Region trade-off (af-south-1)** | Cost and latency comparison, residency notes | Unique to us |

### 6.3 Data model changes (doc 09)

Add to the `recommendation` table:

| Column | Type | Purpose | From |
|---|---|---|---|
| `rule_id`, `rule_version` | text, int | Which rule and version produced it | Flexera |
| `rule_config_snapshot` | jsonb | Thresholds and lookback in effect at generation time (reproducibility, tuning later) | Flexera `ruleConfiguration` |
| `generation_id` | bigint | Reject stale status writes after a refresh | Flexera |
| `evidence` | jsonb | `{metric, statistic, lookback_days, observed, threshold, series_ref}` | OSS notes, our moat |
| `risk_score` (1–5), `risk_reasons` (text[]) | smallint, text[] | Score plus the plain-language drivers | **Our addition** |
| `confidence` (low/med/high) | enum | From lookback length and data completeness | **Our addition** |
| `effort` (lt_1d / d3_5 / gt_1w) | enum | CloudZero's scale | CloudZero, DoiT |
| `reversibility` (reversible / with_snapshot / irreversible), `restart_required` | enum, bool | Drives typed confirmation in the UI | Cost Optimization Hub |
| `savings_method` (billing / list_ratio / aws_api) | enum | How the number was computed | OSS notes |
| `monthly_savings_usd`, `monthly_savings_local`, `fx_rate_id` | numeric, numeric, fk | Dual currency with the exact rate used | Our FX model |
| `commitment_interaction` | text | "Covered by SP: real saving lower" | COH |
| `status` add `on_hold`, `verified` | enum | CloudZero states plus our verification | CloudZero, our moat |
| `snooze_until`, `status_reason` (required for dismiss/snooze) | date, text | Enforced reasons | Flexera |

Identity rule: `rec_id` = hash of (tenant, rule_id, resource ARN); it must not change when the savings figure changes (Flexera's `hash_exclude` pattern).

Add to the cost fact table: `RateSource` (manual / feed_estimated) and `RateToPresentationCurrency`, alongside the FOCUS cost columns, so reconciliation against AWS Cost Explorer is possible (Flexera's reconciliation cards).

Add an `exclusion` table with Flexera's tag grammar (`Key`, `Key==V`, `Key!=V`, `Key=~/re/`, `Key!~/re/`) at org, account and rule level.

### 6.4 IAM (doc 10 / connector spec)

- **Stage 1 (required):** billing read role — CUR/BCM export bucket read (scoped to bucket and prefix), `ce:Get*/Describe*/List*`, `bcm-data-exports:Get*/List*`, `cur:Describe*`, `budgets:View*`, `organizations:Describe*/List*`, `savingsplans:Describe*`, `pricing:Get*`. External ID trust. The export itself is created by the stack (`AWS::BCMDataExports::Export`).
- **Stage 2 (optional, per account):** resource read — curated `Describe*/List*` allow-list, CloudWatch `GetMetricData`, `compute-optimizer:Get*`, `cost-optimization-hub:Get*/List*`, `ebs:ListSnapshotBlocks`.
- **Never in either stage:** `support:*`, `*:Purchase*`, `PutBucketPolicy`, `ec2:CreateTags`, managed `ReadOnlyAccess` / `SecurityAudit`.
- Support member-account-only onboarding (customers who cannot touch the payer) with Cost Explorer backfill.
- Publish the action list per recommendation type in the docs and in the onboarding screen (DoiT shows permissions in the UI).

### 6.5 Front-end decisions confirmed by the teardown

| Decision (doc 11a) | Evidence |
|---|---|
| React + TanStack Router/Query | CloudZero's rewrite moved to exactly this stack |
| ECharts | nOps and DoiT use it; works well at low bandwidth with tree-shaking |
| Avoid AG Grid Enterprise licence at MVP; TanStack Table + server-side grouping endpoint | Every enterprise competitor pays for AG Grid; Flexera's server-side group endpoint shows grouping can live on the API instead |
| Feature flags from day one | All five use a flag service (Split.io, LaunchDarkly, Unleash, ConfigCat); PostHog flags cover this |
| No secrets or licence keys in the client env | CloudZero ships its AG Grid key and analytics keys in plain text |

---

## 7. Positioning lines for sales and the website

- "Flexera tells you what to cut. Cloudoptics tells you how safe it is, how to do it, and what it saves in naira."
- "Read-only, always. We never ask for permission to change your account."
- "Every saving shown in dollars and in your currency, at the rate your finance team sets."
- "Built for af-south-1, Lagos time and your bank's allow-listed IPs."
- "Flat price in local currency. No share of savings. No $30k minimums."

---

## 8. Open items

1. PM sign-off on §6.1 threshold changes before editing FR-ANA-01 to FR-ANA-10.
2. Decide whether R6b (Extended Support) and R9 (idle NAT) move into Increment 2; both are billing-only and cheap.
3. Verify the current EBS modification cooldown and Compute Optimizer automation scope in af-south-1 (flagged in research as unverified).
4. Confirm the competitor price points quoted from third-party sources before using them in sales material.
