# 05 — MVP Definition & Full Product Vision

**Product:** Cloudoptics by Cloudboosta
**Phase:** 5 of 11 — MVP Definition & Full Product Vision
**Role:** Senior Product Manager (Technical, B2B SaaS)
**Date:** 22 October 2026
**Inputs:** Phases 1–4
**Status:** Draft for review

**Fixed decisions:**
- Customers connect **AWS** first; Azure/GCP come later.
- The primary market is **Africa**.
- Cloudoptics itself is hosted on **DigitalOcean**.
- The MVP is **read-only** in customer accounts.
- The MVP supports **Guided** and **Assigned** action modes only (Phase 4 §5).

---

# Part A — MVP Definition

## A1. Primary goal of the MVP

> **Validate that African AWS-based growth companies will connect their accounts, trust and act on risk-scored, dual-currency savings recommendations, and pay for them in local currency.**

Specifically, the MVP must prove four things:
1. **Activation:** teams connect an AWS account read-only in under 15 minutes and see value within 24 hours.
2. **Trust:** recommendations with risk scores and evidence get **acted on** (not just viewed).
3. **Realised value:** savings are actually achieved and measurable from billing data.
4. **Willingness to pay:** design partners convert to paid at local-currency prices.

## A2. Supported clouds and scope boundaries

| Item | MVP decision |
|---|---|
| Cloud | **AWS only.** Both standalone accounts and AWS Organizations (management/payer account plus linked accounts) |
| Access | **Read-only** cross-account IAM role via a CloudFormation one-click stack (customer-owned ExternalId). No write permissions |
| Data sources | CUR 2.0 / AWS Data Exports (customer S3 bucket); Cost Optimization Hub and Compute Optimizer APIs; CloudWatch metrics (selected); resource inventory (EC2, EBS, RDS, S3, ELB, EIP, NAT GW, Lambda); Savings Plans/RI APIs; AWS credits from the CUR |
| Currencies | USD (source) plus **one display currency per tenant** from NGN, KES, ZAR, EGP, GHS (plus GBP/EUR for diaspora companies) |
| Users | Small teams: up to ~10 users per tenant; roles Admin / Member / Viewer |
| Hosting | Cloudoptics runs on DigitalOcean (details in Phases 9–10) |

## A3. Core use cases covered (from Phase 3)

| Use case | MVP coverage |
|---|---|
| UC-01 Understand the bill | ✅ Full |
| UC-02 Prioritised safe savings | ✅ Full (8 recommendation types) |
| UC-03 Guided fixes | ✅ Full (instructions + CLI + Terraform snippets) |
| UC-04 Anomaly alerts | ✅ Basic (daily detection; email + Slack) |
| UC-05 Local-currency costs and forecast | ✅ Core (dual currency, 3-month forecast, simple FX scenario) |
| UC-06 Budgets | ✅ Basic (account/tag budgets, threshold alerts) |
| UC-07 Executive summary | ✅ Basic (monthly PDF/email) |
| UC-08 Credits runway | ✅ Basic (credit burn and estimated depletion date) |
| UC-13 Ownership and tracking | ✅ Partial (assign to a user, status, verified savings; Slack notification; no Jira yet) |
| UC-10 Commitment advice | ⚠️ Advisory only (Savings Plan coverage and recommendation with payback); no purchasing |
| UC-11 Allocation | ⚠️ Tags, accounts and simple virtual tags (rules); no advanced allocation |
| UC-14 In-product learning | ⚠️ "Learn more" explanations linked per recommendation type |
| UC-09, UC-12, UC-15–18 | ❌ Out of scope (see A5) |

## A4. Must-have features

### F1. Onboarding and AWS connector
- Sign-up (email + Google login), tenant creation, choice of display currency and country.
- **One-click CloudFormation** stack: creates a read-only IAM role (with ExternalId) and, if needed, a CUR 2.0 data export to an S3 bucket in the customer account.
- Connection health check with clear error messages (permissions, missing export, Organizations access).
- **First insights within ~1 hour** from API-based data (Cost Explorer summaries, Cost Optimization Hub, inventory), and full CUR-based analytics within 24 hours (the CUR's first delivery can take up to 24h).
- Multiple accounts per tenant; Organizations auto-discovery of linked accounts.

### F2. Cost overview dashboard
- Month-to-date spend, forecast for month end, change vs last month — **in USD and local currency**.
- Breakdowns by service, account, region and tag; top cost drivers; daily trend.
- **"What changed" explanation:** the top 3 drivers of change, in plain language.
- Credits: amount used, remaining and estimated depletion date.

### F3. Recommendations engine (MVP types)

| # | Type | Signal | Typical risk |
|---|---|---|---|
| R1 | **Unattached EBS volumes** and **old EBS snapshots** | Volume state, age, last attachment; snapshot age and whether its source still exists | Low–Med (data-loss check: suggest snapshot before delete) |
| R2 | **Idle / unused public IPv4 and Elastic IPs**, idle load balancers | Unassociated EIPs; ELBs with no healthy targets or traffic | Low |
| R3 | **EBS gp2 → gp3** migration | Volume type, size, IOPS | Low |
| R4 | **Idle EC2 and RDS** instances | CPU, network and connections below thresholds over 14+ days | Med |
| R5 | **EC2 rightsizing** (and RDS where available) | Compute Optimizer findings + CloudWatch CPU/network (+ memory if the agent is present) + peak analysis | Med–High (scored) |
| R6 | **Previous-generation instance families** (e.g., m4 → m7g/m6i; Graviton options) | Instance family mapping + price comparison | Med |
| R7 | **Non-production scheduling** (stop nights/weekends) | Environment tag/name heuristics, utilisation pattern | Low–Med |
| R8 | **Savings Plan coverage** (advisory) | On-demand usage stability; AWS SP recommendations; payback, cash and FX impact | Financial risk (commitment) |

Each recommendation includes:
- savings (USD and local currency, monthly and annual) and effort (S/M/L);
- **risk score** (Low/Med/High) with reasons, and **confidence** (data coverage);
- **evidence** (metric charts, resource facts);
- **guided fix** (console steps, AWS CLI, Terraform snippet) plus **rollback notes**;
- status lifecycle: *New → Assigned → In progress → Done / Dismissed (with reason) → Verified (savings confirmed from billing)*.

Ranking: by **expected net savings × confidence, adjusted for risk**. The default view shows "Top 5 this week".

### F4. Action workflow (Guided + Assigned modes)
- Assign to a team member; due date; comments.
- Slack notification on assignment and status change.
- Dismiss with a reason (e.g., "needed for DR", "false positive"). The reason feeds the risk model (Moat M2).
- **Automatic verification:** detect from resource state and billing data that the change happened and calculate realised savings.

### F5. Budgets and alerts
- Budgets by tenant, account or tag (monthly), in USD or local currency.
- Thresholds (e.g., 50/80/100% actual and forecast) → email + Slack.
- **Daily anomaly detection** (spend vs a rolling baseline per service/account) with top contributing resources.

### F6. Currency and FX
- Daily FX rates from a trusted market-data provider (official and, where relevant, parallel-market indicator rates are a decision for Phase 6). Historical rates are stored.
- All views toggle between USD, local currency and both.
- Simple **FX scenario** on the forecast ("what if the {currency} moves ±X%").

### F7. Reports and export
- Monthly **executive summary** (PDF + email): spend, trend, top drivers, savings identified vs realised, budget status, in both currencies.
- CSV export of cost tables and recommendations.

### F8. Accounts, security and admin
- Email/password + Google SSO; **MFA (TOTP)**.
- Roles: Admin / Member / Viewer.
- Audit log of user actions (connections, assignments, settings).
- Tenant isolation; encryption in transit and at rest; customer can delete data and disconnect.

### F9. Learning layer (lightweight)
- Each recommendation type has a short "Why this matters / how AWS charges for this" explanation, linked to Cloudboosta learning content.

### F10. Product analytics (internal)
- Track the activation funnel, recommendation views, accepts, dismissals with reasons, verified savings, and weekly active users. This is needed for the success criteria and Moat M2.

## A5. Explicitly out of scope for MVP

| Out of scope | Why / when |
|---|---|
| Azure, GCP, SaaS (Snowflake, Datadog, OpenAI) cost ingestion | v2: multi-cloud after AWS PMF |
| **Write access / one-click execution / automated scheduling** | v1 (opt-in, separate role) after trust is validated |
| Automated Savings Plan/RI purchasing | v2 at the earliest; advisory only in MVP |
| Jira / Teams / ServiceNow integrations | v1 (Jira, Teams first) |
| Kubernetes (EKS) cost allocation | v1–v2 depending on demand |
| Region/placement analysis (CBN 2027 use case) | v1 (high priority for fintechs) |
| Advanced allocation, showback/chargeback, shared-cost splitting | v1 |
| Unit economics (cost per customer/transaction) | v2 |
| Partner/MSP multi-tenant console and white-label | v1 (architecture-ready in MVP) |
| Policy/tag enforcement | v2 |
| AI assistant (natural-language Q&A) | v1 (MVP has deterministic plain-language explanations) |
| Enterprise SSO (SAML/OIDC), SCIM | v1 |
| Pull-request cost estimates (shift-left) | v3 |
| Benchmarks across customers | v2 (needs data volume and consent) |
| Native mobile app | Not planned; responsive web only |
| Billing/payments self-serve | Manual invoicing for design partners; self-serve billing at v1 launch |

## A6. MVP success criteria

Measured over a **3-month beta** with 8–12 design partners (Phase 4 §4.1), then the first 60 days of paid availability.

| Category | Metric | Target |
|---|---|---|
| **Activation** | Sign-up → AWS connected | ≥ 60% |
| | Time from sign-up to connected account (median) | ≤ 15 min |
| | Connected → first recommendation viewed within 24h | ≥ 80% |
| **Value** | Identified savings as % of monthly AWS spend (median) | ≥ 15% |
| | **Realised (verified) savings** as % of AWS spend after 60 days (median) | ≥ 8–10% |
| | Realised ÷ identified savings | ≥ 40% |
| **Trust** | Recommendation action rate (assigned/done ÷ viewed) | ≥ 35% |
| | Dismissed as "wrong/unsafe" | ≤ 15% |
| | Production incidents caused by following a recommendation | 0 |
| **Engagement** | Weekly active tenants | ≥ 70% of tenants by month 3 |
| | Finance persona active (opened report/dashboard monthly) | ≥ 50% of tenants |
| **Commercial** | Design partner → paid conversion | ≥ 50% |
| | Fee as % of realised savings (customer ROI) | ≤ 20% (i.e., ≥ 5× ROI) |
| | NPS | ≥ 40 |
| **Quality** | Cost data accuracy vs AWS invoice | within ±1% |
| | Data freshness | 95% of tenants with data < 24h old |

**Qualitative signals:** customers ask for more recommendation types (not fewer); finance personas forward the monthly report to their board; at least one partner asks to resell.

---

# Part B — Full Product Vision & Roadmap

## B1. Vision statement

> **Every growth company in emerging markets can run its cloud efficiently and predictably — knowing what it spends, in its own currency, and acting safely on what to change — without needing a FinOps team.**

Long term, Cloudoptics becomes the **cost and efficiency layer** for cloud, AI and SaaS spend for companies in Africa, then other emerging markets. It combines trusted automation, local intelligence and an education community.

## B2. Feature themes (full vision)

### Theme 1 — Data ingestion & normalisation
- AWS (CUR 2.0, FOCUS export), then **Azure** (Cost Management exports, FOCUS) and **GCP** (BigQuery billing export, FOCUS).
- **FOCUS-native** internal schema (FinOps Open Cost & Usage Specification) for cross-cloud normalisation.
- SaaS and AI spend: OpenAI/Anthropic/Bedrock usage, Snowflake, Datadog, MongoDB Atlas, GitHub.
- Kubernetes cost allocation (EKS, then AKS/GKE) via OpenCost-compatible collectors.
- **Local/African cloud providers** (e.g., local data-centre providers relevant to data-residency moves), as a v3 exploration.
- Data quality: invoice reconciliation, late-arriving data handling, amortisation of upfront fees, credit/refund handling.

### Theme 2 — Analytics & dashboards
- Role-based dashboards (CTO, DevOps, Finance, FinOps).
- Cost explorer with flexible grouping and filters; saved views.
- **Dual-currency and FX analytics:** FX impact decomposition ("of the 18% rise, 11% was FX and 7% usage").
- Forecasting with seasonality and FX scenarios.
- Unit economics (cost per customer/transaction/API call) from business metrics.
- **Regional benchmarks** (anonymised, opt-in) by industry and size.
- AI assistant: natural-language questions about costs, grounded in tenant data.

### Theme 3 — Optimisation recommendations
- Expanded catalogue: S3 storage classes/lifecycle/Intelligent-Tiering, NAT Gateway and data-transfer optimisation, CloudFront, Lambda memory tuning, RDS/Aurora (incl. I/O-Optimized), ElastiCache, OpenSearch, DynamoDB capacity modes, log retention, ECR images, Graviton migration, spot suitability.
- **Region/placement optimisation:** compare `af-south-1`, Local Zones and other regions on cost, latency and residency (CBN 2027, NDPA, Kenya DPA, POPIA guidance).
- Commitment portfolio optimisation (Savings Plans/RIs), FX- and cash-flow-aware.
- AI/GPU optimisation (Bedrock provisioned vs on-demand, SageMaker endpoint idle, GPU utilisation).
- **Outcome-trained risk scoring** (Moat M2); confidence calibration; per-tenant tuning.

### Theme 4 — Automation & remediation
- Action modes: Guided → Assigned → **Approved one-click** → **Policy automation** (Pillar 2).
- Separate opt-in **write role** with least-privilege, per-action permissions; approvals (single/dual); automatic rollback where possible; change windows.
- Schedules for non-prod; cleanup policies (orphaned volumes/snapshots/IPs).
- IaC-first remediation: generate **pull requests** to the customer's Terraform/CloudFormation repo (GitHub/GitLab/Bitbucket).
- Optional automated commitment management with caps, guardrails and transparent reporting.

### Theme 5 — Governance, budgets & alerts
- Hierarchical budgets (org → BU → team → project), in any currency.
- Anomaly detection (ML-based) with root-cause hints; alert routing by owner.
- Tagging policies: detection, scoring, auto-suggestions, enforcement (SCP/tag policy guidance).
- Showback/chargeback; shared-cost allocation rules.
- FinOps maturity scoring aligned to the FinOps Foundation framework.

### Theme 6 — Reporting & exports
- Scheduled reports (exec summary, team reports, board pack) in PDF/email/Slack/Teams.
- Exports: CSV, Excel, API, **FOCUS-format** export, BI connectors (Looker Studio, Power BI, Metabase).
- Accounting integrations for finance (Xero, QuickBooks, Sage, Zoho) for accruals and allocation.
- White-label reports for partners.

### Theme 7 — Admin, security & compliance
- SSO (SAML/OIDC: Google Workspace, Microsoft Entra ID, Okta), SCIM, MFA enforcement.
- Fine-grained RBAC (by account/team scope), custom roles.
- Audit logs (exportable); data retention controls; customer-managed deletion.
- **Partner/MSP multi-tenancy:** parent-child tenants, delegated admin, white-labelling, client billing.
- Compliance: GDPR, NDPA (Nigeria), Kenya DPA, POPIA (South Africa), Egypt PDPL; **SOC 2 Type II** and/or **ISO 27001**; data-residency options for stored data.

### Theme 8 — Education & community (Pillar 5)
- In-product lessons and labs per recommendation; FinOps learning paths; certification ("Cloudoptics Certified").
- Industry playbooks (fintech, logistics, SaaS, health); community benchmarks; events.
- Expert review add-on (quarterly architecture and cost review by Cloudboosta experts or certified partners).

## B3. Roadmap phases

Indicative timing; depends on team size (Phase 11) and validation gates (Phase 4 §4.2).

| Release | Timing (indicative) | Theme focus | Key deliverables | Exit / success gate |
|---|---|---|---|---|
| **MVP (beta)** | Build ~12 weeks; beta 3 months | Prove trust + value + willingness to pay on AWS | Part A features F1–F10 | A6 success criteria; gate G-C |
| **v1 — Paid launch** | MVP + 3–6 months | Action and accountability; Africa depth | Self-serve billing in local currency; **Jira + Teams**; **region/placement analysis (CBN 2027)**; 8–10 more recommendation types (S3, NAT/data transfer, Lambda, RDS deeper); **opt-in one-click actions + non-prod schedules** (write role); advanced allocation and showback; **AI assistant**; partner multi-tenant console (beta); SAML SSO; basic EKS visibility | ≥ 50 paying tenants; ≥ 3 partners; churn < 3%/mo |
| **v2 — Platform** | v1 + 6–9 months | Multi-cloud, scale and intelligence | **Azure + GCP** (FOCUS-based); SaaS/AI spend ingestion; unit economics; ML anomaly detection; **regional benchmarks**; policy automation and tag enforcement; IaC pull-request remediation; commitment management with guardrails; accounting integrations; SOC 2 Type II | ≥ 200 paying tenants; NRR ≥ 110% |
| **v3 — Ecosystem** | v2 + 9–12 months | Expansion and moats | Other emerging markets (e.g., Middle East, South Asia, LatAm) with local currency; shift-left pull-request cost estimates; local cloud providers; certification programme at scale; marketplace listings (AWS Marketplace); public API/SDK | Market expansion targets set in Phase 10 |

## B4. Key technical considerations & dependencies

| # | Consideration | Implication |
|---|---|---|
| T1 | **Customer-side costs of our data access.** Cost Explorer API calls cost ~USD 0.01 each; CloudWatch `GetMetricData` is billed per metric; S3 data transfer out of the customer's CUR bucket to the internet (DigitalOcean) is billed to the bucket owner | Prefer CUR files and free APIs (Cost Optimization Hub, Compute Optimizer), cache aggressively, keep per-tenant API budgets, and **report to customers what our access costs them** (it should be well under USD 5/month for typical tenants; to be confirmed in Phase 9) |
| T2 | **Hosting on DigitalOcean while customers' data is on AWS** | Cross-cloud transfer, credentials for AWS STS from outside AWS (an IAM user or OIDC trust for our principal), latency and egress. Region choice: DO has no African region (nearest are Frankfurt, Amsterdam and London; to confirm), which affects residency claims. Design in Phase 9; compliance in Phase 10 |
| T3 | **Data volume** | CUR line items can be millions of rows/month per mid-size tenant. Need a columnar/analytical store (e.g., ClickHouse or DuckDB/Parquet on object storage) alongside a relational database (PostgreSQL) for application data |
| T4 | **Data freshness** | The CUR updates up to ~3 times/day with up to 24h delay; month-end restatements; credits/refunds post later. UI must show "data as of" timestamps |
| T5 | **FX data** | Needs a reliable FX source with history; a decision on official vs market rates per currency (NGN in particular); store the rate used for each report for auditability |
| T6 | **Security of cross-account access** | ExternalId, least-privilege read-only policy, short-lived STS credentials, secrets management, tenant isolation, audit logging. A future write role must be separate and opt-in |
| T7 | **Pricing data** | AWS Price List API (bulk files) for rightsizing/region comparisons, including `af-south-1` and Local Zone pricing; refreshed regularly |
| T8 | **Recommendation risk model** | Starts rule-based plus heuristics (explainable); collects outcome labels from day one for later ML (M2). Requires CloudWatch metrics history (14–30 days) |
| T9 | **Multi-tenancy designed for partners** | Tenant hierarchy (partner → client) in the data model from the MVP, even before the partner UI |
| T10 | **FOCUS alignment** | Model normalised cost data on FOCUS from the MVP to make Azure/GCP additions cheaper |
| T11 | **Third-party dependencies** | AWS SDKs, FX provider, email (e.g., Postmark/SES), Slack API, auth provider, payments provider supporting African currencies (e.g., Paystack/Flutterwave alongside Stripe) — choices in Phase 9/11 |
| T12 | **Regulatory** | We store billing and resource metadata, not customers' payment data. Even so, regulated customers' vendor reviews will ask about data location (DO region), sub-processors and DPAs. Prepare early (Phase 10) |

## B5. Dependencies on earlier validation

- MVP scope assumes Phase 4 gate **G-A** confirms Pillars 1–3 and the FX need. If FX is weaker than expected (V2), F6 is reduced to display-only and the effort moves to more recommendation types.
- Region/placement analysis moves **into the MVP** if V8 shows strong, urgent CBN-driven demand.
- If partners show strong intent (V6), the partner console moves earlier in v1.
