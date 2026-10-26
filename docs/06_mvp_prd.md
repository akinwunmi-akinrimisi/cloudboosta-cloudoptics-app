# 06 — MVP Product Requirements Document (PRD)

**Product:** Cloudoptics by Cloudboosta
**Phase:** 6 of 11 — PRD for MVP
**Role:** Technical Product Manager / Solutions Architect
**Date:** 22 October 2026
**Inputs:** `05_mvp_and_full_product_vision.md` (MVP scope, features F1–F10, success criteria), Phases 1–4
**Status:** Draft v0.1 for engineering review

**Requirement IDs:** `FR-xxx` = functional, `NFR-xxx` = non-functional, `INT-xxx` = integration. Priority: **M** = Must (MVP blocker), **S** = Should (MVP, can slip to a fast-follow), **C** = Could.

---

## 1. Overview & Objectives

### 1.1 Summary
Cloudoptics MVP is a multi-tenant web application. Customers connect AWS accounts **read-only**, and Cloudoptics ingests billing, usage and resource data. It shows costs in **USD and a local currency**, produces **risk-scored, evidence-backed savings recommendations** with **guided fixes**, tracks their implementation and **verifies realised savings**. It also offers budgets, anomaly alerts and a monthly executive report.

### 1.2 Objectives (from Phase 5 §A1/A6)

| # | Objective | Key metric (target) |
|---|---|---|
| O1 | Fast, low-friction activation | Median sign-up → connected ≤ 15 min; ≥ 60% connect |
| O2 | Trusted recommendations | Action rate ≥ 35%; "wrong/unsafe" dismissals ≤ 15%; 0 incidents |
| O3 | Measurable value | Identified ≥ 15% of spend; verified ≥ 8–10% within 60 days |
| O4 | Finance relevance | ≥ 50% of tenants with monthly finance activity |
| O5 | Commercial validation | ≥ 50% design partner → paid |
| O6 | Data trust | Cost totals within ±1% of AWS invoice |

### 1.3 Personas & product roles

| Persona (Phase 3) | Typical product role | Main surfaces |
|---|---|---|
| A — CTO | Admin or Member | Overview, Recommendations summary, Executive report |
| B — DevOps/Platform Lead | Admin (owner of connection) | Onboarding, Recommendations, Workflow, Alerts |
| C — Finance Manager/CFO | Member or Viewer | Overview (local currency), Budgets, Reports |
| D — FinOps Lead | Admin | All |
| Cloudoptics staff | Internal support (break-glass) | Admin console (internal) |

---

## 2. Scope

### 2.1 Build increments

The MVP is delivered in two increments (detailed sprint plan in Phase 11).

| Increment | Goal | Contents | Used by |
|---|---|---|---|
| **Increment 1 — "See it" (first build increment)** | Connect AWS and show trustworthy cost data plus the first, lowest-risk recommendations | Auth + tenancy + RBAC; AWS connector (CloudFormation, CUR 2.0, inventory); ingestion & normalisation; dual-currency cost overview; FX rates; recommendations **R1–R3** (unattached EBS/snapshots, idle IPs/LBs, gp2→gp3) with guided fixes; CSV export; audit log | First 3–5 design partners (internal alpha) |
| **Increment 2 — "Cut it safely"** | Complete the MVP | Recommendations **R4–R8** (idle EC2/RDS, rightsizing, previous-gen, non-prod schedule, Savings Plan advisory) plus **R6b** (Extended Support charges) and **R10** (idle NAT gateways), with risk scoring; workflow (assign, status, Slack); verification of realised savings; budgets; anomaly detection; credits runway; executive report; in-product learning; product analytics | All 8–12 design partners (beta) |

### 2.2 In scope (MVP)
- Everything in Phase 5 §A4 (F1–F10), as specified in Section 4 below.

### 2.3 Out of scope (MVP)
Write access and automated execution; Azure/GCP/SaaS; Jira/Teams/ServiceNow; EKS cost allocation; placement/region analysis; advanced allocation and chargeback; unit economics; partner console UI (data model only); SAML/SCIM; AI chat assistant; self-serve payments (manual invoicing); native mobile apps; shift-left estimates. (Phase 5 §A5.)

---

## 3. User Stories & Acceptance Criteria

Format: *As a [role], I want [capability], so that [benefit].* Acceptance criteria use Given/When/Then.

### Epic E1 — Sign-up, onboarding & connection

**US-1.1 Sign up and create a workspace** (M)
*As a DevOps lead, I want to sign up and create my company workspace, so that my team can start using Cloudoptics.*
- Given a new visitor, when they sign up with email/password or Google, then a verified user and a new tenant are created and they become **Admin**.
- Email verification is required before connecting AWS.
- During setup the user selects **country** and **display currency** (NGN, KES, ZAR, EGP, GHS, GBP, EUR, USD); the default comes from the country.
- MFA (TOTP) setup is offered; the tenant Admin can make MFA mandatory.

**US-1.2 Connect an AWS account with one click** (M)
*As a DevOps lead, I want to connect AWS through a CloudFormation stack, so that I don't have to write IAM policies.*
- Given an Admin on the "Connect AWS" screen, when they click "Launch stack", then the AWS console opens a pre-filled CloudFormation quick-create link containing a tenant-specific **ExternalId** and our principal ARN.
- The stack creates: (a) the read-only IAM role `CloudopticsReadOnlyRole` (policy in FR-CON-03); (b) optionally, an S3 bucket and a **CUR 2.0 data export** (if the customer has none).
- After stack completion, the user pastes or confirms the Role ARN (or it is auto-detected via a stack callback); Cloudoptics validates with `sts:AssumeRole` within 60 seconds.
- On failure, the UI shows a specific reason (role not found, ExternalId mismatch, missing permission X, export missing) and a fix.
- Time from clicking "Connect" to validated connection is ≤ 10 minutes for a user familiar with AWS (measured in usability tests).

**US-1.3 Connect an AWS Organization** (M)
*As a DevOps lead, I want to connect our management (payer) account and see all linked accounts, so that I get a complete view.*
- Given the role is deployed in the management account, when connected, then linked accounts are discovered via `organizations:ListAccounts` and listed with names and IDs.
- Cost data covers all linked accounts (CUR is payer-level).
- Resource-level recommendations for linked accounts require the role in each member account; the UI offers a **CloudFormation StackSet** template and shows per-account coverage ("12 of 15 accounts have resource access").

**US-1.4 See first value quickly** (M)
*As a CTO, I want to see something useful within an hour, so that I know this is worth my time.*
- Within **60 minutes** of connection: month-to-date spend by service (via Cost Explorer `GetCostAndUsage`, a limited number of calls), resource inventory and R1–R3 recommendations are shown.
- A banner states "Full detail arrives when your first billing export is delivered (up to 24h)".
- Within **24 hours** of the first CUR delivery, all dashboards use CUR data.

**US-1.5 Connection health** (M)
*As an Admin, I want to see the health of each connection, so that I can fix problems before data goes stale.*
- Each account shows: status (Healthy / Degraded / Failed), last successful sync per data type, and errors with remediation steps.
- Email to Admins if a connection fails for > 6 hours or data is > 36 hours old.

### Epic E2 — Cost visibility

**US-2.1 Cost overview** (M)
*As a CTO, I want a single overview of spend, so that I can understand our cloud costs in minutes.*
- The overview shows: month-to-date (MTD) spend, forecast to month end, last month total, % change vs the same period last month, credits applied and the top 5 services. Each figure is shown in **USD and display currency** (toggle: USD / Local / Both).
- A daily spend chart for the current and previous month.
- "**What changed**": the top 3 drivers of change (service/account/region/usage type) with amount and a generated plain-language sentence.
- "Data as of" timestamp is visible.

**US-2.2 Breakdown and filter** (M)
*As a DevOps lead, I want to break down costs by service, account, region, tag key/value and usage type, so that I can find what drives spend.*
- Group by one dimension and filter by any combination; time range presets (MTD, last month, last 3/6/12 months) plus custom; daily/monthly granularity.
- Charts: stacked bar and table; the table is sortable and exportable to CSV.
- Untagged spend is shown explicitly as "(untagged)".
- Response time per NFR-PERF-01.

**US-2.3 Cost metric choice** (S)
*As a FinOps lead, I want to choose between unblended, amortised and net costs, so that numbers match how finance reports.*
- Metric selector: Unblended (default), Amortised (spreads upfront commitments), Net (after credits/discounts). The selected metric is shown in each chart's legend.

**US-2.4 Credits runway** (M)
*As a CTO at a startup, I want to see how our AWS credits are being used and when they run out, so that I can plan runway.*
- Show credits applied MTD, remaining balance (from user input if not available from data), average daily burn, estimated depletion date, and projected monthly spend after credits.
- If depletion is < 90 days away, show a warning and include it in the weekly digest.

### Epic E3 — Currency & FX

**US-3.1 Dual-currency display** (M)
*As a finance manager, I want all costs in our local currency as well as USD, so that I can budget and report locally.*
- All money values are stored in USD (source) and converted using the **daily rate for the cost date** (historical) or the **latest rate** for forecasts.
- Each converted value has a tooltip showing the rate and rate source/date.
- Reports state the FX basis (e.g., "Converted at daily CBN official rate; month average ₦X/$").

**US-3.2 FX scenario on forecast** (M)
*As a CFO, I want to see how currency movement affects next months' cost, so that budgets survive FX swings.*
- A forecast panel with a slider/input for ±0–50% currency movement; shows local-currency forecast for the next 1–3 months at the current rate and the scenario rate.
- Separates the change into "usage change" and "FX change" for last month vs the month before (**FX impact decomposition**, simple version).

### Epic E4 — Recommendations

**US-4.1 Prioritised recommendation list** (M)
*As a DevOps lead, I want a prioritised list of recommendations, so that I work on the most valuable, safest items first.*
- Default sort by priority score (FR-REC-04). Filters: type, account, region, risk, effort, status, owner, environment.
- Each row: title, resource, account/region, monthly savings (USD + local), risk badge, confidence, effort, status, owner.
- Header totals: identified monthly savings (open), in progress, realised (verified) MTD and all time.
- A "**Top 5 this week**" card on the overview links here.

**US-4.2 Recommendation detail with evidence** (M)
*As a DevOps lead, I want to see why a recommendation was made and how risky it is, so that I can trust it.*
- Detail view shows: summary; current vs recommended configuration; savings breakdown (current cost, new cost, monthly/annual savings in both currencies); **risk score with listed reasons**; **confidence** with data coverage (e.g., "29 of 30 days of metrics; memory not available"); **metric charts** (CPU avg/p95/max, network, memory if available, IOPS for volumes) over the lookback; resource facts (tags, launch date, attachments, ASG/LB membership).
- A "Why this matters" explanation and a "Learn more" link (FR-LRN-01).

**US-4.3 Guided fix** (M)
*As a DevOps lead, I want copy-paste fixes for console, CLI and Terraform, so that I can implement quickly and safely.*
- Tabs: Console steps · AWS CLI · Terraform (and CloudFormation where relevant).
- Pre-steps (e.g., "Create snapshot first") and **rollback steps** are always shown for Medium/High risk items.
- Snippets are filled in with the real resource ID/region; a copy button is provided.
- Disclaimer: "Review before applying. Cloudoptics does not make changes in your account."

**US-4.4 Dismiss with reason** (M)
*As a DevOps lead, I want to dismiss a recommendation with a reason, so that it stops appearing and the system learns.*
- Reasons: Needed (DR/backup) · False positive / data wrong · Too risky · Planned for later (snooze 30/60/90 days) · Other (free text).
- Dismissed items are hidden from defaults and kept in history; they can be restored.
- If the same resource changes materially (e.g., size or attachment), the system may re-evaluate after 30 days.

**US-4.5 Savings Plan advisory** (M)
*As a CFO, I want to understand whether to buy a Savings Plan and what it means for cash flow, so that I can decide safely.*
- Shows: current on-demand spend eligible for Compute Savings Plans, current coverage and utilisation, AWS-recommended hourly commitment (1-year, no upfront and all upfront options), estimated savings, **payback period**, total commitment in USD and local currency (at current rate and under an FX scenario), and the risk of usage decline.
- Clear label: "Advisory only. Purchases are made in your AWS console."

### Epic E5 — Action workflow & verification

**US-5.1 Assign and track** (M)
*As a FinOps lead, I want to assign a recommendation to an engineer with a due date, so that savings get implemented.*
- Assign to any tenant user; optional due date and comment. Status: New → Assigned → In progress → Done (user-declared) → **Verified** / Not verified.
- The assignee receives email and Slack (if connected) notifications.
- All changes are recorded in the item's activity timeline and the audit log.

**US-5.2 Automatic verification** (M)
*As a CTO, I want Cloudoptics to confirm that savings really happened, so that I can trust the numbers I report.*
- Within 72 hours of a change being detectable, the system checks resource state (e.g., volume deleted, instance type changed, instance stopped on schedule) and marks the item **Verified** with a timestamp.
- Realised savings are calculated from CUR costs (cost before vs after, normalised to 30 days) after ≥ 7 days of post-change data. Until then, "Estimated realised savings" is shown.
- If a recommendation is implemented without being marked Done (detected change), it is auto-marked Verified and credited.

### Epic E6 — Budgets, anomalies & notifications

**US-6.1 Create budget** (M)
*As a finance manager, I want to set monthly budgets for the tenant, an account or a tag value, in USD or local currency, so that we control spend.*
- Budget fields: name, scope (tenant / account(s) / tag key=value), amount, currency, thresholds (default 50/80/100% of actual, 100% of forecast), recipients (users, email addresses, Slack channel).
- Local-currency budgets are evaluated using the current rate; the UI shows both currencies.

**US-6.2 Budget alert** (M)
- When actual or forecast spend crosses a threshold, an alert is sent within **1 hour of the data update** that caused it; each threshold triggers at most once per budget period.

**US-6.3 Anomaly alert** (M)
*As a DevOps lead, I want to be alerted to unusual spend, so that I can stop overruns quickly.*
- Daily detection per (account, service) using a rolling baseline (FR-ANA-08). An alert includes amount above expected, % deviation, top contributing usage types/resources, and a link to a pre-filtered cost view.
- Users can mark an anomaly as Expected / Investigating / Resolved; "Expected" feeds tuning.
- Minimum impact threshold (default: USD 50/day and 30% above baseline; configurable per tenant) to avoid noise.

**US-6.4 Weekly digest** (S)
- Weekly email/Slack digest: spend vs last week, new top recommendations, savings realised, budget status, credits warnings.

### Epic E7 — Reporting & export

**US-7.1 Monthly executive summary** (M)
*As a CTO, I want an automatic monthly summary I can forward to my board, so that reporting takes no effort.*
- Generated on the 3rd working day of each month (after the previous month's data stabilises) as PDF + HTML email to chosen recipients.
- Contents: total spend in both currencies, change vs previous month with FX vs usage split, top 5 drivers, budget status, savings identified/realised, top 5 open recommendations, credits runway (if applicable), data-accuracy note.
- Branded with Cloudoptics and the tenant name/logo (logo upload optional).

**US-7.2 CSV export** (M)
- Any cost table and the recommendation list can be exported to CSV with the active filters; files up to 1M rows are delivered via a download link (async for > 50k rows).

### Epic E8 — Administration, security & audit

**US-8.1 Invite users and assign roles** (M)
- Admin invites by email; roles **Admin** (all, incl. connections, billing, users), **Member** (view all; manage recommendations, budgets, alerts), **Viewer** (read-only).
- Invites expire after 7 days.

**US-8.2 Audit log** (M)
- Admins can view and export (CSV) an audit log of: logins, user/role changes, connection changes, budget/alert changes, recommendation status changes, exports and data deletion requests. Retained for at least 12 months.

**US-8.3 Disconnect and delete data** (M)
- An Admin can disconnect an AWS account (stops ingestion immediately) and request deletion of its data (completed within 30 days; confirmation emailed). Tenant deletion deletes all tenant data within 30 days, except billing/legal records.

### Epic E9 — Learning

**US-9.1 Contextual learning** (S)
- Each recommendation type has a "Why this matters" panel (≤ 150 words: how AWS charges, typical savings, common pitfalls) and a link to a Cloudboosta lesson/lab.

---

## 4. Functional Requirements

### 4.1 Cloud connector (AWS)

| ID | Requirement | Pri |
|---|---|---|
| FR-CON-01 | Generate a CloudFormation quick-create URL per connection with parameters: `ExternalId` (random, ≥ 32 chars, unique per tenant connection), `CloudopticsPrincipalArn`, `CreateCurExport` (bool), `CurBucketName` | M |
| FR-CON-02 | Provide templates: single-account role; StackSet for member accounts; optional CUR 2.0 export plus S3 bucket with a bucket policy allowing the BCM Data Exports service to write | M |
| FR-CON-03 | **Read-only IAM policy** (least privilege), limited to: `ce:Get*`, `ce:Describe*`, `ce:List*`; `cur:DescribeReportDefinitions`; `bcm-data-exports:GetExport`, `ListExports`; `s3:GetObject`, `s3:ListBucket` (**scoped to the export bucket/prefix only**); `cost-optimization-hub:List*`, `Get*`; `compute-optimizer:Get*`, `Describe*`; `savingsplans:Describe*`; `ec2:Describe*`; `rds:Describe*`, `rds:ListTagsForResource`; `elasticloadbalancing:Describe*`; `autoscaling:Describe*`; `lambda:List*`, `lambda:GetFunctionConfiguration`; `s3:GetBucketLocation`, `s3:ListAllMyBuckets`, `s3:GetBucketTagging`, `s3:GetLifecycleConfiguration`; `cloudwatch:GetMetricData`, `cloudwatch:ListMetrics`; `organizations:ListAccounts`, `DescribeOrganization`; `tag:GetResources`; `pricing:GetProducts`. **No** `*:Create*`, `Put*`, `Delete*`, `Modify*` or data-plane read (e.g., no S3 GetObject outside the export bucket, no Secrets Manager/SSM parameter access) | M |
| FR-CON-04 | Assume role with `sts:AssumeRole` + ExternalId, session duration ≤ 1 hour, session name `cloudoptics-<tenantId>-<job>`; never store customer long-term keys | M |
| FR-CON-05 | Our AWS principal: a dedicated Cloudoptics AWS account and IAM principal used **only** for assuming customer roles; its credentials are held in the secrets manager and rotated ≤ 90 days. Preferred alternative: OIDC/IAM Roles Anywhere so no long-lived keys exist on DigitalOcean (decision in Phase 9) | M |
| FR-CON-06 | Validation check on connect and every 6 hours: assume role, list export, list a sample of each required API; record per-permission status | M |
| FR-CON-07 | Data pulls and frequency (defaults): **CUR 2.0** — poll the export manifest every 6h, ingest new/updated files; **Cost Explorer** — at onboarding only (≤ 20 calls) plus daily MTD summary (≤ 10 calls/day/tenant); **Inventory** (EC2, EBS, snapshots, EIP/public IPv4, ELB, RDS, Lambda, S3 bucket metadata) — every 12h; **CloudWatch metrics** — daily incremental pull for candidate resources only (14–30 day history, 1h period); **Cost Optimization Hub + Compute Optimizer** — daily; **Savings Plans** — daily | M |
| FR-CON-08 | Regions: discover enabled regions per account (`ec2:DescribeRegions`) and scan all enabled regions incl. `af-south-1` and Local Zones | M |
| FR-CON-09 | Rate limiting and backoff: honour AWS throttling (exponential backoff with jitter, max 5 retries), per-tenant concurrency limits, and a **per-tenant monthly API cost budget** (default: estimated customer-side charges ≤ USD 5/month; alert internally if exceeded) | M |
| FR-CON-10 | Show customers an "**Access cost**" estimate (Cost Explorer calls, CloudWatch metrics, S3 egress) in Settings | S |
| FR-CON-11 | Handle partial failure: if one data type fails, others continue; status per data type (FR-CON-06) | M |
| FR-CON-13 | **Two-stage connection:** stage 1 (required) is the billing read role on the payer or single account (Cost Explorer, CUR/BCM export bucket, Savings Plans, Organizations, pricing); stage 2 (optional, per account) adds the resource and metrics reads in FR-CON-03. The CUR/BCM export is created by the CloudFormation stack, never by our role. Neither stage includes `support:*`, purchase actions, `PutBucketPolicy`, `ec2:CreateTags`, or the managed `ReadOnlyAccess`/`SecurityAudit` policies. The onboarding screen lists every permission and which recommendation types need it | M |
| FR-CON-14 | Connection validation errors are specific and actionable, e.g. "We could not assume the role (check the External ID)", "No CUR 2.0 manifest at that bucket and prefix", "Export exists; first delivery usually arrives within 24 hours" | M |
| FR-CON-12 | Compute Optimizer enrolment: detect whether the account is opted in; if not, show guidance (opt-in is free; enhanced metrics are paid and optional) | S |

### 4.2 Ingestion & normalisation

| ID | Requirement | Pri |
|---|---|---|
| FR-ING-01 | Land raw CUR 2.0 Parquet files unchanged in object storage (per tenant, per billing period, per export version) for reprocessing | M |
| FR-ING-02 | Normalise to an internal **FOCUS-aligned** schema (e.g., BilledCost, EffectiveCost, ListCost, ChargeCategory, ServiceName, RegionId, ResourceId, Tags, BillingPeriod, ChargePeriod, SubAccountId) | M |
| FR-ING-03 | Handle restatements: when AWS re-delivers a billing period, replace that period's data **idempotently** (by billing period + export version) | M |
| FR-ING-04 | Cost metrics: compute Unblended, Amortised (incl. SP/RI amortisation) and Net (after credits, refunds, discounts) | M |
| FR-ING-05 | Credits: identify credit line items (incl. AWS Activate) and expose them separately | M |
| FR-ING-06 | Tags: ingest user and AWS-generated cost allocation tags; normalise keys (case-insensitive matching configurable) | M |
| FR-ING-07 | **Reconciliation:** monthly totals per account must match the AWS invoice total (from CUR invoice-level rows) within ±1%; discrepancies raise an internal alert and a "data being reconciled" banner | M |
| FR-ING-08 | Daily aggregates (tenant × day × account × service × region × usage type × tag set) are pre-computed for fast dashboards | M |
| FR-ING-09 | Data quality checks per load: schema validation, row counts vs manifest, null ratio on key columns, negative-cost sanity checks, duplicate detection | M |
| FR-ING-10 | Freshness: the dashboard reflects new CUR data within **2 hours** of file delivery | M |

### 4.3 FX service

| ID | Requirement | Pri |
|---|---|---|
| FR-FX-01 | **Rates are set by hand.** Cloudoptics staff enter the rate for each currency (USD → NGN, KES, ZAR, EGP, GHS, GBP, EUR) in the platform admin console (FR-FX-06). Each rate is stored with its effective date, basis (official or market), source note, who entered it and when. A daily commercial feed is fetched as a **reference only** | M |
| FR-FX-02 | A manual rate always wins. The reference feed is used only for a currency that has **never** had a manual rate, and values converted that way are flagged "estimated" | M |
| FR-FX-03 | Historical costs are converted using the rate for the charge date; forecasts and budgets use the latest rate | M |
| FR-FX-04 | If a rate is missing for a date, use the last available rate (max 5 days back) and flag the value as estimated | M |
| FR-FX-05 | FX decomposition: month-over-month local-currency change = usage effect (Δ USD × old rate) + FX effect (new USD × Δ rate) | M |
| FR-FX-06 | **Platform admin console: Exchange rates page** (Cloudoptics staff only; platform-admin role with MFA). For each currency it shows the current rate, effective date, basis, who set it, days since update, and the reference-feed rate with the % difference. Staff can set a new rate (value, effective date (today or later; back-dating needs a reason), basis, source note, reason). It shows 12 months of history with a chart, and supports CSV import for back-filling | M |
| FR-FX-07 | **Controls on manual rates:** warn and require confirmation if the new rate differs by more than 5% from the previous rate or the reference feed; changes over 10% need a second admin to approve; every change goes to the platform audit log; alert the rate owner if a currency's rate is more than 3 working days old; caches and open reports refresh within 5 minutes of a change | M |

### 4.4 Analysis engines

Defaults are tenant-configurable within safe bounds. Thresholds are starting points, to be tuned from design-partner feedback.

| ID | Engine | Logic (MVP) | Pri |
|---|---|---|---|
| FR-ANA-01 | **R1 Unattached EBS volumes** | State `available`: flagged from the first scan that finds it (Low confidence), Medium at ≥ 7 days, **High confidence at ≥ 32 days** (decision of 26 Oct 2026). Detach time comes from our own daily inventory snapshots (CloudTrail lookup covers only 90 days). Savings = monthly volume cost (size × price + provisioned IOPS/throughput). Risk: Low if a recent snapshot exists or the volume is < 30 GB non-prod; Med otherwise (possible data loss); +risk if tagged prod/backup or detached < 7 days ago. Guided fix: snapshot first (on by default), wait for completion, delete; rollback = restore from snapshot (keep 30 days) | M |
| FR-ANA-01b | **R1 Old EBS snapshots** | Snapshots older than 90 days (tenant-configurable 30/60/90/180) whose source volume no longer exists. Exclude snapshots referenced by an AMI, managed by AWS Backup or DLM, or tagged `retain`/`compliance` (configurable keys). Risk: Med (backup/retention); savings from snapshot storage (estimated incremental size where available, else full size, labelled "upper bound"). Offer **EBS Snapshot Archive** as the lower-risk alternative to delete | M |
| FR-ANA-01c | **R1b Idle attached EBS** | Non-root volume attached to a running instance with < 1 read+write operation per day over 14 days (High confidence at 32 days). Risk: Med (may be cold or DR storage). Guided fix: owner confirmation, detach, snapshot, delete | M |
| FR-ANA-02 | **R2 Idle public IPv4 / Elastic IPs** | EIPs not associated: flagged from the first scan (Low confidence), Medium at ≥ 3 days, **High confidence at ≥ 7 days** (decision of 26 Oct 2026); detect from CUR usage type `PublicIPv4:IdleAddress` plus inventory. Also flag EIPs attached to instances stopped ≥ 7 days. Risk: Low; the UI asks "Is this IP allow-listed by a bank or partner?" and notes DNS cannot be verified. Release is **irreversible** (the address is lost), so it requires typed confirmation | M |
| FR-ANA-02b | **R2 Idle load balancers** | Any of: no listeners or 0 healthy targets, flagged from the first scan (Low confidence, Medium at ≥ 3 days, High at ≥ 7 days; a load balancer under 7 days old adds risk, decision of 26 Oct 2026); ALB `RequestCount` < 100/day on ≥ 7 of the last 14 days; NLB `NewFlowCount` and `ProcessedBytes` ≈ 0 over 14 days; CLB < 100 requests/day for 14 days. Risk: Low–Med (+risk if a Route 53 record in a connected account points at it). Guided fix: remove DNS, wait 24 h, delete; rollback = exported listener/rule/target-group JSON | M |
| FR-ANA-03 | **R3 gp2 → gp3 (EBS and RDS)** | All gp2 EBS volumes, plus io1 volumes whose observed IOPS fit gp3 limits, plus **RDS gp2 storage**. Recommended gp3 IOPS = max(3,000, 3 × GiB) capped at 16,000; throughput = observed p99 throughput, minimum 125 MiB/s (250 MiB/s for volumes ≥ 170 GiB). Savings = price difference including extra gp3 IOPS/throughput. Risk: Low (online change, no downtime); Med for gp2 > 1 TiB or burst-credit-dependent volumes. Warn that a volume cannot be modified again until the AWS cooldown ends | M |
| FR-ANA-04 | **R4 Idle EC2** | Running ≥ 14 days with **max (peak) CPU < 5%** and network in+out < 5 MB/day over 14 days = idle (High confidence when it also holds over 32 days). p95 CPU < 5% without the peak condition = "low utilisation" candidate (Low confidence, not labelled idle). Excludes instances in ASGs (separate note), instances launched < 14 days ago and instances tagged `cloudoptics:ignore`. Savings include attached EBS and public IPv4; say so when SP/RI coverage lowers the real saving. Risk: Med (prod) / Low (non-prod). Guided fix: stop first (reversible); terminate only after N days stopped and a final AMI | M |
| FR-ANA-04b | **R4 Idle RDS** | 0 database connections for 7 days (`DatabaseConnections` max = 0) **and** low CPU and IOPS over 14 days. Excludes read replicas and Aurora global secondaries. Recommend snapshot + delete, or stop with the warning that **stopped RDS restarts automatically after 7 days**. Risk: Med | M |
| FR-ANA-05 | **R5 EC2 rightsizing** | Use Compute Optimizer findings when available (shown side by side with its `performanceRisk`) and combine with our own check over a **32-day default lookback** (captures month-end payroll and salary cycles): **prod-tagged = P99.5 CPU with 20% headroom; non-prod = P95 with 30% headroom; never average CPU**. Also memory (if the CloudWatch agent is present), network and burst credits; recommend the next size down in the same family (or Compute Optimizer's option) only if projected p95 CPU ≤ 60% and max ≤ 85% after resizing, and only types available in the resource's region (incl. `af-south-1`). Savings normalised to 730 h. Risk scored by FR-REC-03. If memory data is absent, confidence is capped at Medium and the UI says so | M |
| FR-ANA-06 | **R6 Previous-generation / Graviton** | Map families (e.g., t2→t3/t4g, m4/m5→m6i/m7i/m7g, c4/c5→c6i/c7g, r4/r5→r6i/r7g); recommend a same-architecture newer generation (Low–Med risk) and separately Graviton. Graviton effort is Low only when Compute Optimizer's migration effort is Very low/Low or the workload is known multi-arch; otherwise Med/High effort with a test checklist instead of a one-step fix. Check target-type availability in the region (incl. `af-south-1`). Include price differences from the Price List API for the specific region | M |
| FR-ANA-06b | **R6b Extended Support charges** | Billing-only: CUR line items for RDS (MySQL/PostgreSQL) and EKS Extended Support. Recommend upgrading to a standard-support version; savings = the Extended Support charge over the last 30 days. Risk: Med (engine/version upgrade needs testing). Guided fix: upgrade path, pre-checks, snapshot, rollback via snapshot restore | M |
| FR-ANA-07 | **R7 Non-prod scheduling** | Instances/RDS tagged or named as dev/test/staging/qa/sandbox (configurable regex and tag keys), running 24/7; recommend a schedule (default weekdays 07:00–20:00 in the tenant's IANA timezone, e.g. Africa/Lagos, Africa/Nairobi, Africa/Johannesburg). Savings = hours saved × hourly rate. Guided fix = one schedule tag in the form `07:00-20:00;MO-FR;Africa/Lagos` plus an AWS Instance Scheduler / EventBridge Scheduler template. RDS: note the 7-day automatic restart. Risk: Low–Med | M |
| FR-ANA-08 | **Anomaly detection** | Per (account, service) daily cost: baseline = median of the same weekday over the past 4 weeks, with an MAD-based threshold; anomaly if cost > baseline + max(3×MAD, 30%) **and** the excess ≥ tenant minimum (default USD 50/day). Suppress for 7 days after a user marks "Expected" for the same scope | M |
| FR-ANA-09 | **Forecast** | Month-end forecast = MTD + (average of the last 7 days' daily cost × remaining days), adjusted for known monthly charges (e.g., support, SP fees). 3-month forecast = linear trend over the last 3 months' daily data (bounded), with a ±range. Upgrade to better models later | M |
| FR-ANA-10 | **R8 Savings Plan advisory** | Use `ce:GetSavingsPlansPurchaseRecommendation` (Compute SP, 1-year; no upfront and all upfront; 30- and 60-day lookbacks) and `GetSavingsPlansCoverage`/`Utilization`, **plus our own simulation**: hourly SP-eligible on-demand cost from the CUR, computed **after** open R4/R5/R7 savings are netted out (no double counting); conservative target = 60–80% of the trailing 60-day hourly p10–p20 baseline. Show AWS's option and ours in a matrix (SP vs RI × 1 / 3 years × payment option) with break-even months. Suggest a ladder (first step 20–35% of the target, then about 10% per month) and remind users of the 7-day return window. Default recommendation: **1-year, No Upfront** for FX-exposed tenants. FX: show the commitment in local currency at the current rate and in a **stress test at 20% and 40% currency weakening**. Expiry alerts at 60, 30 and 15 days | M |
| FR-ANA-11 | **Credits runway** | Daily credit burn = 30-day average of credit line items; remaining = user-provided balance (or AWS-reported where available) − credits used since entry; depletion date = remaining ÷ burn | M |
| FR-ANA-12 | **Explain "what changed"** | Compare MTD vs the same days last month by dimension; pick the top 3 positive/negative contributors; render a sentence template ("{service} in {region} rose {amount} ({pct}) mainly due to {usage type}") | M |
| FR-ANA-14 | **R10 Idle NAT gateways** | Billing-only: CUR `NatGateway-Hours` > 0 and `NatGateway-Bytes` = 0 over 30 days. Savings = NAT hourly charge. Risk: Low–Med (check route tables in inventory). Guided fix: remove the route, wait, delete; rollback = recreate from exported configuration | M |
| FR-ANA-13 | All engines run after each relevant data update (CUR load, daily metrics pull) and at least once per day per tenant; total analysis for a tenant with 5,000 resources completes in < 15 minutes | M |

Thresholds in FR-ANA-01 to FR-ANA-14 were revised on 22 Oct 2026 from the competitor build teardown (doc 12 §6.1). **Post-MVP recommendation types** (v1 backlog, doc 12 §6.2): R9 region/placement analysis (already planned, see Q9), R10b NAT traffic to S3/DynamoDB → VPC gateway endpoint, R11 CloudWatch Logs retention, R12 S3 incomplete multipart uploads and missing lifecycle rules, R13 stopped EC2 still paying for EBS and public IPv4.

### 4.5 Recommendation engine (ranking, risk, savings)

| ID | Requirement | Pri |
|---|---|---|
| FR-REC-01 | Common recommendation object: id, tenant, account, region, resource ARN/ID, type, rule ID and version, **snapshot of the thresholds and lookback used**, generation ID (rejects stale status writes after a refresh), title, current config, recommended config, monthly cost now, monthly cost after, savings (USD; local with the exact FX rate ID used), effort (S < 1 day / M 3–5 days / L 1+ week), **reversibility** (reversible / reversible with snapshot / irreversible) and restart required, risk level and reasons, confidence and reasons, evidence (metric, statistic, lookback, observed value, threshold), commitment interaction note, fix snippets incl. rollback, status, owner, timestamps, engine version. Identity is stable: a change in the savings figure never creates a new recommendation | M |
| FR-REC-02 | **Savings estimate** uses public on-demand prices from the Price List API for the resource's region (incl. `af-south-1` and Local Zone pricing) **and** the tenant's actual effective rate from the CUR where available (to account for discounts/SP coverage). Show "estimated" when based on list prices | M |
| FR-REC-03 | **Risk score** (0–100 → Low < 35, Med 35–65, High > 65). Explainable, additive factors (MVP rules): environment = prod (+25) / unknown (+15); data-loss potential (+30 for delete actions without a recent snapshot); utilisation headroom after change (+0–25 based on projected p95/max); missing memory metrics (+10); resource age < 30 days (+10); part of ASG/LB/cluster (+10); historical dismissals for this type in this tenant marked "too risky" (+0–10). Each applied factor is listed in the UI | M |
| FR-REC-04 | **Priority score** = monthly savings (USD) × confidence factor (High 1.0 / Med 0.7 / Low 0.4) × risk factor (Low 1.0 / Med 0.6 / High 0.3) × effort factor (S 1.0 / M 0.8 / L 0.6). Ties broken by savings | M |
| FR-REC-05 | **Confidence** from data coverage: High = ≥ 90% of lookback days with metrics and all needed metrics present; Med = ≥ 60% or one metric missing; Low = otherwise | M |
| FR-REC-06 | De-duplicate overlapping recommendations on the same resource (e.g., idle vs rightsize vs schedule): show the highest-priority one as primary and the others as "alternatives"; savings are never double-counted in totals | M |
| FR-REC-07 | Recommendation lifecycle (Section 3, US-4.4, US-5.1, US-5.2); auto-close as "Resolved externally" if the resource disappears without action through us (counted as realised savings only if the cost drop is confirmed) | M |
| FR-REC-08 | Tenant settings: ignore by tag (`cloudoptics:ignore=true`) and by exclusion rules (`Key`, `Key==V`, `Key!=V`, `Key=~/re/`, `Key!~/re/`) at tenant, account and type level; exclude accounts; **switch recommendation types on or off** (type catalogue); adjust thresholds within bounds; define non-prod patterns; set anomaly minimum (in USD or local currency). Threshold changes apply on the next run and are kept in a change history | M |
| FR-REC-09 | **Outcome logging for M2:** store every view, assign, dismiss (with reason), done, verified and realised amount with engine version and the inputs used | M |
| FR-REC-10 | Fix snippet templates are versioned and tested (lint for CLI syntax and `terraform validate` on templates in CI) | M |

### 4.6 UI requirements

| ID | Requirement | Pri |
|---|---|---|
| FR-UI-01 | Primary navigation: Overview · Costs · Recommendations · Budgets & Alerts · Reports · Settings (Connections, Users, Notifications, Preferences, Audit log) | M |
| FR-UI-02 | Global controls: tenant switcher (for multi-tenant users), account filter, date range, currency toggle (USD / Local / Both) persisted per user | M |
| FR-UI-03 | Onboarding wizard: currency & country → connect AWS → (optional) Org StackSet → invite team → Slack connect; with progress and resumable state | M |
| FR-UI-04 | Empty, loading and error states for every view; "data as of" timestamp on every data view | M |
| FR-UI-05 | Responsive web (desktop-first; usable at ≥ 375 px width for Overview, Recommendation detail and Budgets) | M |
| FR-UI-06 | Accessibility: WCAG 2.2 AA for colour contrast, keyboard navigation and screen-reader labels; charts with table alternatives | M |
| FR-UI-07 | Number formatting: local currency symbols and thousand separators; large numbers abbreviated (₦3.3M, $2.1k) with full value on hover | M |
| FR-UI-08 | Language: English for MVP; strings externalised for future French/Arabic/Portuguese localisation | M |
| FR-UI-09 | Recommendation detail page has a stable shareable URL (access still requires login and tenant permission) | S |
| FR-UI-10 | Performance-sensitive design for African networks: initial JS bundle ≤ 300 KB gzipped for the main app shell; dashboard data payloads ≤ 200 KB; works on 3G-class connections (NFR-PERF-03) | M |

### 4.7 Notifications & alerting

| ID | Requirement | Pri |
|---|---|---|
| FR-NOT-01 | Channels: email (all), Slack (via a Slack app, OAuth install per tenant; post to chosen channels and DMs by email match) | M |
| FR-NOT-02 | Event types: budget threshold, anomaly, connection failure/stale data, recommendation assigned/status change, weekly digest, monthly report ready, credits depletion warning | M |
| FR-NOT-03 | User notification preferences per event type and channel; Admin-level defaults | M |
| FR-NOT-04 | De-duplication and rate limiting: max one alert per budget threshold per period; anomaly alerts grouped per tenant per day; max 20 Slack messages per tenant per hour (excess batched) | M |
| FR-NOT-05 | Every alert has a deep link to the relevant filtered view | M |
| FR-NOT-06 | Email deliverability: SPF/DKIM/DMARC on the sending domain; unsubscribe for non-critical emails | M |

### 4.8 RBAC, multi-tenancy & audit logging

| ID | Requirement | Pri |
|---|---|---|
| FR-SEC-01 | **Tenant model:** Organisation (tenant) → AWS connections → accounts. Data model includes `parent_tenant_id` for future partner/MSP hierarchies (no UI in MVP) | M |
| FR-SEC-02 | **Isolation:** every data row carries `tenant_id`; enforced at the data-access layer (e.g., PostgreSQL row-level security) and in analytical queries (mandatory tenant filter in the query layer); automated tests ensure no cross-tenant reads | M |
| FR-SEC-03 | Roles and permissions matrix (Admin/Member/Viewer) enforced server-side on every API endpoint | M |
| FR-SEC-04 | A user may belong to multiple tenants with different roles | S |
| FR-SEC-05 | Authentication: email/password (min 12 chars, breached-password check), Google OIDC, TOTP MFA; session timeout 12h idle, 7d absolute; tenant-enforced MFA | M |
| FR-SEC-06 | **Audit log:** append-only; records actor, tenant, action, target, timestamp, IP, user agent, before/after for settings; exportable; retained ≥ 12 months | M |
| FR-SEC-07 | Internal staff access to tenant data only via a break-glass tool with reason, time-limited and logged in the tenant-visible audit log | M |
| FR-SEC-08 | Secrets (our AWS principal, Slack tokens, API keys) stored only in the secrets manager; never logged | M |

---

## 5. Non-Functional Requirements

### 5.1 Performance

| ID | Requirement |
|---|---|
| NFR-PERF-01 | Dashboard and cost-explorer queries over ≤ 13 months of daily aggregates: p95 ≤ 2.0 s, p99 ≤ 4 s for tenants up to USD 250k/month spend |
| NFR-PERF-02 | API p95 latency ≤ 500 ms for non-analytical endpoints |
| NFR-PERF-03 | Overview page usable (largest contentful paint) ≤ 4 s on a simulated "Fast 3G" connection from Lagos/Nairobi to our hosting region |
| NFR-PERF-04 | CUR ingestion: ≥ 5M line items processed per tenant-month within 30 minutes |
| NFR-PERF-05 | Full analysis run per tenant (≤ 5,000 resources) ≤ 15 minutes |

### 5.2 Scalability (MVP design targets)
- NFR-SCA-01: 200 tenants, 1,000 AWS accounts, 500M CUR rows (13 months), without architectural change.
- NFR-SCA-02: horizontal scaling of ingestion and analysis workers; per-tenant job isolation so one large tenant cannot starve others.

### 5.3 Reliability & availability
- NFR-REL-01: Web app/API availability **99.5%** monthly (MVP), excluding scheduled maintenance (announced ≥ 48h ahead, outside 07:00–20:00 WAT/EAT/SAST).
- NFR-REL-02: Data freshness SLO: **95% of active tenants have cost data < 24h old** (relative to the latest CUR delivery).
- NFR-REL-03: Jobs are idempotent and retry safely; failed jobs go to a dead-letter queue with alerting.
- NFR-REL-04: Backups: database point-in-time recovery ≥ 7 days; daily backups kept 30 days; raw CUR files retained so the analytical store can be fully rebuilt. **RPO ≤ 24h, RTO ≤ 8h** for MVP.

### 5.4 Security
- NFR-SEC-01: TLS 1.2+ everywhere; HSTS; encryption at rest for databases, object storage and backups.
- NFR-SEC-02: OWASP ASVS Level 2 as the baseline for the web app; security headers (CSP, frame-ancestors, etc.).
- NFR-SEC-03: Dependency scanning, SAST and secret scanning in CI; container image scanning; an external penetration test before paid launch.
- NFR-SEC-04: Principle of least privilege for all internal service accounts; production access limited to named engineers with MFA; access reviews quarterly.
- NFR-SEC-05: Customer-facing security documentation: IAM policy explanation, data flow, sub-processors, data location (DigitalOcean region), retention.

### 5.5 Compliance & privacy
- NFR-CMP-01: Data processed: billing data, resource metadata, tags, user account data. **No customer application data or payment-transaction data is accessed.** Tags may contain personal data; treat tags as potentially personal.
- NFR-CMP-02: Provide a **Data Processing Agreement** covering GDPR/UK GDPR, Nigeria NDPA 2023, Kenya DPA 2019, South Africa POPIA, Egypt PDPL; list sub-processors.
- NFR-CMP-03: Data hosting region: **DigitalOcean (no African region available — London/Amsterdam/Frankfurt candidates)**; cross-border transfer basis documented (Phase 10).
- NFR-CMP-04: Data retention: raw CUR 13 months (configurable up to 25); aggregates 25 months; audit logs 12 months minimum; deleted on tenant request within 30 days.
- NFR-CMP-05: SOC 2 readiness controls implemented from MVP (access control, change management, logging, incident response), with Type I targeted after v1.

### 5.6 Observability & monitoring
- NFR-OBS-01: Structured JSON logs with `tenant_id`, `request_id`/`job_id`; no secrets or customer tokens in logs.
- NFR-OBS-02: Metrics: API latency/error rate, job durations/success, queue depth, ingestion lag per tenant, AWS API throttling, per-tenant AWS API cost estimate, FX fetch status.
- NFR-OBS-03: Distributed tracing for API → job → external calls (OpenTelemetry).
- NFR-OBS-04: Alerting (on-call) for: API error rate > 2% for 5 min; ingestion lag > 12h for > 5% of tenants; job DLQ growth; reconciliation failure; FX fetch failure > 24h.
- NFR-OBS-05: Product analytics events (activation funnel, recommendation lifecycle) to a privacy-respecting analytics store; no PII in event payloads beyond internal IDs.

### 5.7 Usability
- NFR-USA-01: A new user can connect AWS and find their top recommendation without help in ≥ 80% of usability-test sessions.
- NFR-USA-02: All recommendation text readable by non-specialists (target plain-English, ≤ grade 10 readability for "Why this matters").

---

## 6. Integration Requirements

| ID | Integration | MVP scope | Pri |
|---|---|---|---|
| INT-01 | **AWS** (STS, BCM Data Exports/CUR 2.0, S3, Cost Explorer, Cost Optimization Hub, Compute Optimizer, CloudWatch, EC2/EBS/ELB/RDS/Lambda/S3 describe APIs, Organizations, Savings Plans, Price List) | As in FR-CON/FR-ANA | M |
| INT-02 | **Slack** | OAuth app install per tenant; post alerts/digests to channels; DMs to assignees (email match); interactive buttons (Acknowledge anomaly, Mark in progress) | M |
| INT-03 | **Email** (transactional provider) | Alerts, invites, reports, digests; bounce handling | M |
| INT-04 | **Google OIDC** | Login with Google | M |
| INT-05 | **FX rate provider(s)** | Daily rates + history, with fallback | M |
| INT-06 | **Product analytics** | Event tracking | M |
| INT-07 | **Jira Cloud** | Create an issue from a recommendation; status sync | v1 (C for MVP) |
| INT-08 | **Microsoft Teams** | Alerts and digests | v1 |
| INT-09 | **Terraform / IaC repos** (GitHub/GitLab) | Generate pull requests with fixes | v2 |
| INT-10 | **Payments** (Paystack/Flutterwave/Stripe) | Local-currency subscriptions | v1 |
| INT-11 | **Webhooks (outbound)** | Generic webhook for alerts (JSON) | S |

---

## 7. Analytics & Instrumentation (for success criteria)

| Event | Properties (no PII) |
|---|---|
| `signup_completed`, `tenant_created` | country, currency |
| `aws_connect_started` / `_succeeded` / `_failed` | method (single/org), failure reason, duration |
| `first_value_viewed` | minutes since connect |
| `recommendation_viewed` / `_assigned` / `_dismissed` / `_done` / `_verified` | type, risk, confidence, savings band, dismissal reason, engine version |
| `realised_savings_recorded` | amount band, type |
| `budget_created`, `alert_sent`, `alert_acknowledged` | type, channel |
| `report_generated`, `report_opened` | recipient role (if known) |
| `fx_scenario_used`, `currency_toggled` | currency |

Dashboards (internal) track every metric in Section 1.2.

---

## 8. Release Criteria (MVP beta)

- All **M** requirements implemented and tested; no open Critical/High security findings.
- Reconciliation within ±1% for all design-partner tenants for the last full month.
- Guided-fix snippets for R1–R8 validated against a test AWS account (incl. `af-south-1`).
- Runbooks for connector failure, data discrepancy and incident response exist (Phase 11g).
- Privacy policy, terms, DPA and security overview published.
- Usability test (5+ participants across personas A–C) completed with no blocking issues.

---

## 9. Open Questions & Assumptions

### 9.1 Open questions

| # | Question | Owner | Needed by |
|---|---|---|---|
| Q1 | **Hosting region** on DigitalOcean (LON1 vs AMS3 vs FRA1): latency from Lagos/Nairobi/Johannesburg vs regulatory perception. Is there demand for a later African data-location option (which would mean a different provider for data storage)? | Architect + Security | Phase 9/10 |
| Q2 | How does our **AWS principal** authenticate from DigitalOcean without long-lived keys (IAM Roles Anywhere with our own CA vs IAM user with rotated keys vs a small AWS-hosted "assume-role broker")? | Architect | Phase 9 |
| Q3 | ~~FX rate basis~~ **Resolved 22 Oct 2026:** Cloudoptics staff set rates by hand in the platform admin console (FR-FX-01, -06, -07). Still open: who owns daily rate updates (named person and a backup), and whether tenants may later set their own rate | PM | Before beta |
| Q4 | Should design partners without a CUR export get a "Cost Explorer only" mode (lower fidelity, higher API cost) rather than being required to create an export? | PM | Increment 1 |
| Q5 | Exact list of non-prod naming/tag conventions in target customers (to tune R7) | PM + design partners | Increment 2 |
| Q6 | Do we show recommendations for member accounts without the resource role (cost-only, from CUR) with lower confidence? | PM | Increment 2 |
| Q7 | Credits balance: no AWS API exposes the remaining Activate credit balance directly — confirm and design the manual-entry UX | PM + Eng | Increment 2 |
| Q8 | Pricing and packaging for the paid conversion (needed for O5) | GTM | Phase 10 |
| Q9 | Is the CBN June 2026 directive's scope confirmed, and should R9 "placement analysis" move into the MVP (Phase 5 §B5)? | PM + Legal | After V8 |
| Q10 | Our liability position if a customer follows a guided fix and has an incident (terms of service, disclaimers) | Legal | Before beta |
| Q11 | Confirm competitor price points quoted from third-party sources (doc 12) before they are used in sales material | GTM | Before launch |

### 9.2 Assumptions

| # | Assumption |
|---|---|
| AS1 | Most design partners can create (or already have) a CUR 2.0 / Data Exports export; first delivery may take up to 24h |
| AS2 | Compute Optimizer and Cost Optimization Hub are free to enable and query (paid enhanced metrics are optional) |
| AS3 | Typical design-partner scale: ≤ 20 accounts, ≤ 5,000 resources, ≤ 5M CUR rows/month |
| AS4 | English-only UI is acceptable for the MVP (French/Arabic later for North/West/Francophone Africa) |
| AS5 | Customer-side AWS charges from our access remain < USD 5/month per typical tenant with the polling plan in FR-CON-07 (to be measured in Increment 1) |
| AS6 | Slack is the dominant chat tool among the ICP; Teams follows in v1 |
| AS7 | Manual invoicing is acceptable for design-partner conversion; self-serve local-currency billing ships at v1 |
| AS8 | Engineering team and timeline per Phase 11 (target: ~12 weeks for Increments 1–2) |

---

## Appendix: Sources for technical facts

- DigitalOcean regional availability (no African region; 16 datacentres in 13 regions as of Aug 2026) — https://docs.digitalocean.com/products/platform/availability-matrix
- AWS Cost Optimization Hub (consolidates 18+ recommendation types; 24h initial population, daily refresh) — https://aws.amazon.com/aws-cost-management/cost-optimization-hub ; https://aws.amazon.com/blogs/aws/new-cost-optimization-hub-to-find-all-recommended-actions-in-one-place-for-saving-you-money ; https://prosperops.com/blog/aws-cost-optimization-hub
- Compute Optimizer data requirements and limitations — see Phase 2, Appendix A.
