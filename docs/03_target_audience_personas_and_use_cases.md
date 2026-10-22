# 03 — Target Audience, Personas & Use Cases

**Product:** Cloudoptics by Cloudboosta
**Phase:** 3 of 11 — Target Audience, Personas & Use Cases
**Role:** Senior Product Manager / UX Researcher (B2B SaaS, DevTools/FinOps)
**Date:** 22 October 2026
**Inputs:** `01_market_and_competitor_landscape.md`, `02_competitor_swot_and_gap_analysis.md`
**Status:** Draft for review. Personas are **proto-personas** built from desk research and must be validated through interviews (Phase 7 research plan).

---

## 0. Context Carried Forward

- AWS is the first cloud; Africa is the primary market. Gaps G1–G9 and hypotheses H1–H5 come from Phase 2.
- **New regulatory signal:** in June 2026 the Central Bank of Nigeria (CBN) directed banks, fintechs, mobile-money operators and payment providers to store **payment transaction data generated in Nigeria onshore from 1 January 2027**. Press estimates suggest >90% of regulated Nigerian firms host on foreign clouds. This creates an urgent **cost-of-migration and placement** job for Nigerian fintechs (Section 3, UC-09). It is based on press coverage; the circular text needs to be confirmed.
- **Funding context (2025):** African tech funding was about USD 3.5–4.1B depending on the source (Partech: USD 4.1B, +25% YoY). Kenya, South Africa, Egypt and Nigeria took about 85–90% of it. Fintech took about 45–51% of disclosed funding. These are the companies most likely to run on AWS and to feel cloud costs in local currency.

---

## 1. Ideal Customer Profile (ICP)

### 1.1 Primary ICP: "Cloud-native African growth company"

| Attribute | Definition |
|---|---|
| **Company type** | Digital-first company whose product runs on AWS: tech startup/scale-up, or the digital unit of a traditional business |
| **Headquarters / operations** | **Tier 1:** Nigeria, Kenya, South Africa, Egypt. **Tier 2:** Ghana, Rwanda, Morocco, Côte d'Ivoire, Uganda. Also diaspora-founded companies (UK/US HQ) whose main operations and revenue are in Africa |
| **Company size** | 20–500 employees; engineering team of 5–80 |
| **Funding / stage** | Seed+ to Series C, or bootstrapped and profitable; also subsidiaries of larger groups |
| **Industries (priority order)** | 1. Fintech and payments, 2. E-commerce, logistics and mobility, 3. SaaS and B2B software, 4. Health tech and insurance tech, 5. Edtech, media and streaming, 6. Telco/bank digital units |
| **Cloud maturity** | 1–6 years on AWS. Mostly EC2/ECS/EKS, RDS, S3, Lambda, CloudFront. Uses AWS Organizations or several accounts. Tagging is partial or inconsistent. No dedicated FinOps function |
| **AWS spend** | **Sweet spot: USD 10k–100k per month** (≈ USD 120k–1.2M/yr). Serviceable range: USD 3k–250k per month |
| **Regions used** | Mix of `eu-west-1/2`, `us-east-1`, `af-south-1` (Cape Town), plus the Lagos/Nairobi Local Zones in some cases |
| **Revenue currency** | Mostly local (NGN, KES, ZAR, EGP, GHS); cloud costs in USD. This FX exposure is the defining pain |
| **Who buys** | CTO / Head of Engineering (decision) with the CFO / Finance lead (budget approval). DevOps / Platform lead is the daily user and champion |
| **Buying behaviour** | Prefers self-serve trial → monthly plan. Needs a security review for read-only access. Card or local bank transfer; local-currency invoicing strongly preferred |

**Trigger events (when they buy):**
1. An AWS bill spike or surprise (often during a currency fall).
2. AWS credits (Activate) running out within 3–6 months.
3. A fundraising or board push to reach profitability or extend runway.
4. Budget planning season or an FX-driven budget overrun.
5. Regulatory changes forcing re-architecture (e.g., CBN 2027 data localisation; Kenya/South Africa residency requirements).
6. A new CTO or Head of Platform taking over an untidy AWS estate.

**Signals of fit (qualification):** AWS spend ≥ USD 3k/month; ≥ 2 AWS accounts; spend growth > 20% YoY; local-currency revenue; no current FinOps tool, or only AWS Cost Explorer.

### 1.2 Secondary ICP: "African AWS partner / MSP" (channel)

| Attribute | Definition |
|---|---|
| **Profile** | AWS Select/Advanced partners, resellers and managed service providers in Nigeria, Kenya, South Africa and Egypt managing 10–300 client AWS accounts |
| **Need** | Multi-tenant cost visibility and optimisation reports for clients; FinOps as a value-added service; protecting margin and client retention |
| **Why it matters** | Many African SMBs buy AWS through partners; this is a distribution channel (Phase 2, G7) |
| **Buyer** | Partner CEO / Cloud Practice Lead |

### 1.3 Tertiary ICP (later): "Regulated African mid-market and enterprise digital units"

Bank, insurance and telco digital units, and large fintechs, spending USD 100k–1M+ per month on AWS. They need SSO, audit, data residency and approval workflows. They are attractive but have longer sales cycles and IBM/Flexera competition. Target them **after** product–market fit in the primary ICP.

### 1.4 Not a fit (anti-ICP) for now

- AWS spend below ~USD 1k/month: AWS native tools and Vantage's free tier are enough.
- Azure/GCP-only companies, until multi-cloud support exists.
- Large global enterprises with mature FinOps teams already on Cloudability/Flexera.
- Companies mostly on local/on-prem clouds with minimal AWS use.

### 1.5 Market sizing (directional, to be refined)

Rough bottom-up placeholder for later validation:
- Assume **~2,000–4,000** AWS-using companies across Tier 1/2 countries with AWS spend ≥ USD 3k/month. This is an assumption: no reliable public count exists. Validate with AWS partner data and LinkedIn/BuiltWith-style tech-stack sources.
- At an assumed **USD 150–600/month** average contract value, the serviceable market is about **USD 4–29M ARR**. This is modest, and supports a plan to expand beyond Africa (e.g., other emerging markets and diaspora companies) and into adjacent services later. Figures to be finalised in Phase 10 pricing.

---

## 2. Personas

Four primary personas plus one channel persona. Names are illustrative.

### 2.1 Persona A — "Tunde", CTO / Head of Engineering (primary buyer)

| Field | Detail |
|---|---|
| **Profile** | CTO at a 120-person Lagos-based payments fintech (Series A/B). 12 years' experience, ex-big tech or bank. Reports to the CEO and board |
| **Responsibilities** | Platform reliability, engineering delivery, security/compliance, infrastructure budget, hiring |
| **Goals** | Keep the platform reliable and compliant; extend runway; ship features fast; show the board infrastructure cost is under control |
| **Success metrics** | Uptime/SLOs; cloud cost as % of revenue; cost per transaction; delivery speed; audit findings closed |
| **Pain points** | • AWS bill in USD while revenue is in naira; FX swings blow up the budget. • No time to analyse Cost Explorer; doesn't trust raw recommendations ("will it break production?"). • AWS credits ending soon. • **CBN 2027 onshore data requirement**: must decide what moves to the Lagos Local Zone or a local provider, and at what cost. • Board asks "why did infrastructure costs rise 40%?" and they have no clear answer |
| **Current tools / workflow** | AWS Cost Explorer (occasional), spreadsheets, AWS account manager reviews, ad-hoc DevOps investigations, Slack |
| **What "winning" looks like** | A monthly 10-minute view: "costs are X in ₦ and $, up/down because Y; here are 5 safe actions worth Z", and a confident answer for the board and regulators |
| **Buying role** | Decision-maker; requires security sign-off and CFO budget approval |
| **Quote (hypothetical)** | "I don't need another dashboard. I need to know what's safe to cut and what it'll cost us in naira next quarter." |

### 2.2 Persona B — "Wanjiru", DevOps / Platform Lead (daily user and champion)

| Field | Detail |
|---|---|
| **Profile** | Senior DevOps/Platform engineer leading a 3–6 person team at a Nairobi logistics/e-commerce scale-up. Hands-on with Terraform, EKS/ECS, CI/CD |
| **Responsibilities** | Infrastructure provisioning, scaling, reliability, on-call, cost "when there's time" |
| **Goals** | Reduce waste without risking incidents; automate the boring work; cut on-call noise; get credit for savings |
| **Success metrics** | Incidents/MTTR; deployment frequency; monthly savings realised; % resources tagged |
| **Pain points** | • Cost work competes with features and incidents. • Generic recommendations ignore memory, peaks and dependencies, so she distrusts them. • No ownership data: can't tell which team owns what. • Applying changes is manual (console clicks, Terraform edits). • Tagging enforcement is hard |
| **Current tools / workflow** | AWS console, Compute Optimizer, CloudWatch/Grafana, Terraform, GitHub/GitLab, Jira/Linear, Slack; occasional scripts to find unused EBS volumes and Elastic IPs |
| **What "winning" looks like** | A prioritised list ranked by savings and risk, with evidence (metrics graphs), a ready-to-use Terraform/CLI fix, approvals in Slack and a record of realised savings |
| **Buying role** | Evaluator and champion; runs the trial and the security review of the IAM role |
| **Quote (hypothetical)** | "Show me the CPU **and** memory over 30 days, tell me the risk, and give me the Terraform change. Then I'll do it." |

### 2.3 Persona C — "Aisha", Finance Manager / CFO (economic co-buyer)

| Field | Detail |
|---|---|
| **Profile** | Finance Manager (or CFO at smaller firms) at a Cairo- or Johannesburg-based SaaS or health-tech company. Owns budgeting, FP&A, vendor payments and FX management |
| **Responsibilities** | Budgets and forecasts, monthly close, board reporting, cash/FX management, vendor contracts |
| **Goals** | Predictable spend; accurate forecasts in local currency; allocate costs to products and customers; no surprises |
| **Success metrics** | Budget variance; forecast accuracy; gross margin; days of runway; cost per customer |
| **Pain points** | • Cloud invoice is a single USD line she can't explain or allocate. • FX movements make budgets wrong within weeks. • Depends on engineers for explanations. • Paying in USD needs FX access (card limits, bank allocation), so commitments (Savings Plans) need careful cash planning. • Can't model "what if we commit for 1 year?" |
| **Current tools / workflow** | Excel/Google Sheets, accounting software (Xero, QuickBooks, Sage, Zoho, Odoo), AWS invoice PDFs, monthly emails with engineering |
| **What "winning" looks like** | A monthly local-currency and USD cost report by product/team, a forecast with FX scenarios, budget alerts before overruns, and clear payback on commitment purchases |
| **Buying role** | Approves budget; key influencer on pricing model (prefers flat local-currency pricing) |
| **Quote (hypothetical)** | "Tell me what the cloud bill will be in rand next month, and which product it belongs to." |

### 2.4 Persona D — "Kofi", Emerging FinOps Lead / Cloud Cost Analyst (mid-market and regulated)

| Field | Detail |
|---|---|
| **Profile** | Cloud cost analyst or newly appointed "FinOps lead" (often an ex-cloud engineer or IT finance analyst) at a larger fintech, insurer or telco digital unit in Accra, Lagos or Johannesburg. Recently completed or is studying for FinOps certification |
| **Responsibilities** | Cost allocation and showback, tagging policy, budgets, optimisation programmes, reporting to CTO/CFO, coordinating engineering teams |
| **Goals** | Build a FinOps practice from scratch; show measurable savings; get engineering teams to act; meet audit and residency requirements |
| **Success metrics** | % spend allocated; savings realised vs identified; commitment coverage/utilisation; tagging compliance; forecast accuracy |
| **Pain points** | • Starts with poor tagging and no process. • Lacks executive-ready reporting. • Recommendations are ignored without ownership or workflow. • Enterprise tools are unaffordable or too complex; native tools are fragmented. • Needs evidence and audit trails for regulators and auditors |
| **Current tools / workflow** | AWS Cost Explorer, CUR in Athena/QuickSight, spreadsheets, Jira tickets to teams, FinOps Foundation resources |
| **What "winning" looks like** | Automatic allocation to teams, a tagging policy that is enforced, a recommendations pipeline with owners and status, monthly executive reports, and a FinOps maturity score that improves each quarter |
| **Buying role** | Champion and power user; drives evaluation; may hold a small budget |
| **Quote (hypothetical)** | "I don't need someone to find savings — I need engineers to actually implement them, and proof that they did." |

### 2.5 Channel persona — "Ngozi", Cloud Practice Lead at an AWS partner / MSP

| Field | Detail |
|---|---|
| **Profile** | Leads the cloud practice at an AWS Advanced partner in Lagos/Nairobi managing ~80 client accounts |
| **Goals** | Differentiate the practice; retain clients; earn recurring FinOps service revenue; meet AWS partner programme requirements |
| **Pain points** | Manual monthly cost reviews per client; no multi-tenant tool priced for Africa; clients leave for "cheaper" local clouds when bills spike |
| **Winning** | One console across all clients, white-label reports, savings proven to clients, margin on the resold tool |
| **Buying role** | Channel buyer/reseller; can bring many end customers at once |

### 2.6 Persona priority

| Persona | Role in buying | Priority for MVP design |
|---|---|---|
| A — CTO | Decision-maker | **P0** (value proposition) |
| B — DevOps/Platform Lead | Daily user, champion | **P0** (core workflows) |
| C — Finance/CFO | Budget approver | **P1** (reports, FX, budgets) |
| D — Emerging FinOps Lead | Power user (larger accounts) | **P1–P2** |
| Channel — Partner/MSP | Distribution | **P2** (multi-tenant later) |

---

## 3. Use Cases / Jobs-to-be-Done (prioritised)

**Priority key:** P0 = essential to the core value proposition; P1 = high value, needed soon after; P2 = later / segment-specific. "Gap" refers to Phase 2 gap areas (G1–G9).

| ID | Job-to-be-done ("When…, I want to…, so I can…") | Personas | Priority | Gap |
|---|---|---|---|---|
| **UC-01** | When our AWS bill arrives, I want to **understand where the money goes** (by account, service, region, team/tag) and why it changed, so I can explain it confidently | A, B, C, D | **P0** | Table stakes |
| **UC-02** | When I look for savings, I want a **prioritised list of safe, high-impact actions** with savings, risk level and evidence, so I can cut cost without breaking production | A, B, D | **P0** | G2 |
| **UC-03** | When I decide to act, I want **guided fixes** (step-by-step, CLI/Terraform snippet, approval and rollback notes), so I can implement quickly and safely | B, D | **P0** | G3 |
| **UC-04** | When spend behaves unusually, I want **anomaly alerts in Slack/email** with probable cause, so I can stop overruns within hours, not at month end | A, B, D | **P0** | Table stakes |
| **UC-05** | When I plan and report, I want **costs and forecasts in local currency and USD**, with FX scenarios, so budgets survive currency moves | A, C | **P0** (Africa wedge) | G1 |
| **UC-06** | When I set budgets, I want **budgets by account/team/project with threshold alerts**, so we prevent overruns rather than discover them | A, C, D | **P1** | Table stakes |
| **UC-07** | When I report to leadership, I want a **monthly executive summary** (savings found vs realised, trend, top drivers), so I can show results to the board | A, C, D | **P1** | G4 |
| **UC-08** | When our AWS credits are running out, I want to see **credit burn rate and post-credit spend forecasts**, so I can plan runway and optimise before credits end | A, C | **P1** | G9 |
| **UC-09** | When regulation or latency requires moving workloads (e.g., CBN 2027 onshore data), I want to **compare the cost of placement options** (`af-south-1`, Lagos/Nairobi Local Zones, EU regions) including data transfer, so I can choose a compliant, cost-efficient design | A, D | **P1** (fintech-urgent) | G8 |
| **UC-10** | When commitments make sense, I want **Savings Plan / RI recommendations with payback, cash-flow and FX impact**, and the option to approve purchases, so I can save on rates without losing control | A, C, D | **P1** | G3, G1 |
| **UC-11** | When I allocate costs, I want **automatic allocation and tag-gap detection** (virtual tags, ownership mapping), so every dollar has an owner | C, D | **P1** | Table stakes |
| **UC-12** | When teams create resources, I want **tagging and budget policies enforced** (alerts or guardrails on non-compliance), so governance scales without policing | D, B | **P2** | — |
| **UC-13** | When I want accountability, I want recommendations **assigned to owners with status tracking** (Jira/Slack), so savings actually get implemented and verified | B, D | **P1** | G2, G3 |
| **UC-14** | When I'm new to FinOps, I want **in-product learning** (explanations, playbooks, maturity score), so my team builds capability while saving money | A, B, D | **P1** | G6 |
| **UC-15** | When I measure efficiency, I want **unit costs** (cost per customer, transaction or order), so I can link cloud spend to business value and pricing | A, C, D | **P2** | — |
| **UC-16** | When I manage many clients, I want a **multi-tenant view and white-label reports**, so I can deliver FinOps as a service | Channel | **P2** | G7 |
| **UC-17** | When we adopt AI services, I want **visibility of Bedrock/SageMaker/GPU spend**, so AI costs don't spiral | A, B, D | **P2** (rising) | Trend |
| **UC-18** | When I change infrastructure code, I want **cost impact before deployment** (pull-request cost estimate), so we avoid costly designs | B | **P2** | Shift-left trend |

### 3.1 Top jobs by persona

| Persona | Top 3 jobs |
|---|---|
| A — CTO | UC-02 safe savings · UC-05 local-currency forecast · UC-01 understand the bill |
| B — DevOps Lead | UC-02 trusted recommendations · UC-03 guided fixes · UC-04 anomaly alerts |
| C — Finance | UC-05 FX-aware forecasts · UC-06 budgets · UC-07 executive report |
| D — FinOps Lead | UC-11 allocation · UC-13 ownership and tracking · UC-07 reporting |
| Channel | UC-16 multi-tenant · UC-07 reports · UC-02 recommendations |

### 3.2 Use-case clusters (input for Phase 4–5)

1. **See it:** UC-01, UC-04, UC-11, UC-17. Visibility and allocation (table stakes).
2. **Cut it safely:** UC-02, UC-03, UC-10, UC-13. Trusted recommendations and guided action (core differentiator).
3. **Plan it in local currency:** UC-05, UC-06, UC-08, UC-07. FX-aware budgeting and reporting (Africa wedge).
4. **Govern and grow:** UC-12, UC-14, UC-15, UC-18, UC-09. Policy, education, unit economics, placement.
5. **Scale through partners:** UC-16.

---

## 4. Key Assumptions & Validation Needs

| # | Assumption | Risk if wrong | How to validate (detailed in Phase 7) |
|---|---|---|---|
| A1 | The USD 10k–100k/month AWS spend band in Tier-1 African markets has enough companies to sustain the business | Market too small → expand scope earlier | AWS partner data, tech-stack lookups, interviews with 3–5 AWS partners |
| A2 | CTOs, not finance, initiate the purchase | Wrong messaging and channel | 15–20 interviews across personas A–C |
| A3 | FX-aware reporting is a top-3 need for finance (H3) | Weaker wedge | Finance interviews; landing page test |
| A4 | DevOps leads will grant read-only cross-account access after a short security review | Slow onboarding | Trial funnel metrics; security questionnaire feedback |
| A5 | The CBN 2027 directive drives near-term placement and cost-analysis demand among Nigerian fintechs | UC-09 deprioritised | Confirm circular text; 5 fintech interviews |
| A6 | A partner/MSP channel will adopt a multi-tenant tool (H4) | Slower distribution | 3–5 partner interviews |
| A7 | Built-in education raises activation and retention (H5) | G6 weaker | Beta cohort comparison |

---

## Appendix A: Sources

- African tech funding 2025 (Partech, Techpoint, Disrupt Africa summaries) — https://engineeringnews.co.za/article/funding-for-tech-startups-in-africa-up-46-in-2025-after-two-years-of-declines-2026-02-03 ; https://allafrica.com/stories/202601250064.html ; https://intelpoint.co/report/funding-for-african-startups-in-2025/
- Fintech share of funding — https://businessday.ng/technology/article/fintech-grabs-45-of-africas-startup-funding-in-h1-2025/
- CBN payment data localisation (June 2026 directive, effective 1 Jan 2027) — https://businessday.ng/technology/article/over-90-of-nigerian-banks-fintechs-face-foreign-cloud-exit-as-cbn-forces-data-home-by-2027/ ; https://businessday.ng/technology/article/cbn-ends-offshore-payment-processing-as-banks-fintechs-face-2027-data-localisation-deadline/ ; https://tech.africa/?p=88017
- Nigeria data localisation and NDPA analysis — https://www.mondaq.com/nigeria/fin-tech/1722826/how-nigerias-data-localization-regime-shapes-fintechs-handling-of-financial-identity-and-transaction-data ; https://www.banwo-ighodalo.com/media-files/2025/12/data-localization.pdf
- Carried from Phases 1–2: see the appendices of `01_…` and `02_…`.

**Note:** current FX rates could not be confirmed from search (only outdated figures were returned). Live rates should come from central bank or market-data APIs when FX features are designed (Phase 9).
