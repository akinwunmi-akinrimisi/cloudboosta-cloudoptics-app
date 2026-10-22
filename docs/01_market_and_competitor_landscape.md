# 01 — Market & Competitive Landscape

**Product:** Cloudoptics by Cloudboosta (working title: Cloud Cost Optimization Platform, CCOP)
**Phase:** 1 of 11 — Market & Competitive Landscape
**Role:** Senior Product Strategist / Market Analyst (FinOps / Cloud)
**Date:** 22 October 2026
**Status:** Draft for review

---

## 0. Scope, Inputs & Assumptions

This document covers the market and competitors only. It does not define our product.

**Decisions already made by stakeholders (used to frame the research):**

| Decision | Value | Effect on this document |
|---|---|---|
| Primary customer cloud | AWS first; Azure/GCP later | Competitors are judged mainly on their AWS depth |
| Primary market | Africa | An Africa lens is added to every profile |
| Competitor depth | 5–7 profiled in detail | 7 profiled (Section 3) |
| Our hosting | DigitalOcean | Not relevant to Phase 1; noted for Phase 9/10 |

**Assumptions:**
- "Africa" means mainly the large digital markets (Nigeria, South Africa, Kenya, Egypt), plus Ghana, Rwanda and Morocco as secondary markets. Customers are African companies that spend on AWS, whether they run in `af-south-1` (Cape Town), the Lagos Local Zone, or EU/US regions.
- Vendor pricing is rarely published. Figures here come from vendor pages, AWS Marketplace listings and third-party pricing sites. Many of those third-party sites are run by competing vendors, so treat **all prices as indicative** and confirm them before relying on them (see Appendix B).
- Market-size figures differ a lot between analyst firms because they define the market differently. We give ranges, not one number.

---

## 1. Market Overview

### 1.1 Size & growth

- **Global cloud FinOps market:** about **USD 15B in 2025**, growing at roughly **10–14% CAGR**.
  - MarketsandMarkets: USD 14.9B (2025) → USD 26.9B (2030), 12.6% CAGR.
  - Straits Research: USD 15.0B (2025) → USD 34.9B (2034), 9.8% CAGR.
  - Technavio: +USD 14.8B over 2025–2030, 14.4% CAGR.
- These totals include services and native tools. The market for **standalone software** is much smaller. CloudZero, one of the fastest-growing independents, was at an estimated **~USD 42M ARR in March 2026** (Sacra).
- **Kubernetes FinOps** is a sub-segment of about **USD 1.7B (2026)**.

### 1.2 Key trends

1. **AI spend now leads the agenda.** State of FinOps 2026 (FinOps Foundation): **98%** of practitioners actively manage AI costs, up from 63% in 2025 and 31% in 2024. AI/GPU, LLM API and data-platform costs (Snowflake, Databricks) are now treated as "cloud".
2. **The easy savings are mostly gone for mature teams.** Optimisation is still the top day-to-day task, but practitioners report "diminishing returns". Savings now come from many small, harder actions. This favours **automation** over dashboards.
3. **Tools are moving from insight to execution.** Buyers want tools that *do* the work (commitment purchasing, scheduling, rightsizing), not just recommend it. Consolidation reflects this:
   - IBM bought Apptio/Cloudability (2023) and Kubecost (2024).
   - Flexera bought Spot + CloudCheckr from NetApp (Mar 2025, reportedly about USD 100M against the USD 450M NetApp paid), then ProsperOps + Chaos Genius (Jan 2026).
4. **FinOps scope is widening.** 90% of FinOps teams now also manage SaaS spend, 64% licensing, 57% private cloud and 48% data centre (State of FinOps 2026).
5. **Shift-left.** Teams want cost guidance **before deployment** (architecture review, IaC cost estimates in pull requests). This is one of the most-requested tool capabilities.
6. **Unit economics and forecasting.** Buyers increasingly expect cost per customer / transaction / feature and forecast accuracy, not just total spend. "Cloud unit economics" rose five places in the 2026 priority ranking.
7. **Native tools keep improving, raising the floor.** AWS has added Cost Optimization Hub, broader Compute Optimizer idle detection (DynamoDB, ElastiCache, SageMaker endpoints and more), Amazon Q in Cost Explorer, Savings Plans target-coverage analysis, and CUR 2.0. Paid tools must offer clearly more than this free baseline.
8. **FinOps is moving up the org chart.** 78% of FinOps teams report to the CTO/CIO. The FinOps Foundation now frames its mission around "value of technology", not cost cutting alone.

### 1.3 Market drivers

- Cloud bills grow faster than revenue, and AI workloads make them less predictable.
- CFO pressure for forecastable, allocatable spend (chargeback/showback).
- Most cloud cost is engineer-controlled, and engineers lack cost context when they make decisions.
- Commitment discounts (Savings Plans, Reserved Instances) offer 30–70% off but are complex and risky to manage by hand.
- Maturing practice: the FinOps Foundation framework, certifications and the FOCUS billing data standard give buyers a shared vocabulary.

### 1.4 Market challenges

- **Trust in recommendations.** Engineers ignore advice that is risky or naive (e.g., rightsizing without checking memory or peak load).
- **Data quality.** Poor tagging and allocation leave much spend "unallocated" and undermine every downstream report.
- **Free native tools** set the price anchor for basic visibility.
- **Pricing friction.** Percent-of-spend and percent-of-savings models feel costly as spend grows. Buyers increasingly ask for flat or transparent pricing.
- **Access and security concerns.** Buyers hesitate to grant cross-account IAM access, and especially write access for automation.
- **Consolidation risk.** Acquired tools (Cloudability, Kubecost, Spot, CloudCheckr) face roadmap uncertainty and price rises (Forrester's view on Flexera/Spot).

### 1.5 Africa market lens (our primary market)

| Factor | Observation | Implication for the landscape |
|---|---|---|
| **AWS footprint** | Cape Town region (`af-south-1`, 3 AZs, since April 2020). Local Zones in **Lagos** (Jan 2023, the first in Africa) and Nairobi. AWS reports >USD 819M invested in Africa since 2018 and has committed a further USD 1.5B through 2029. | AWS is the established hyperscaler. Our AWS-first scope matches demand. |
| **Currency shock** | Cloud is billed in USD and revenue is in local currency. The naira went from ~₦471/$ (early 2023) to ~₦1,530/$ (mid-2025), so the same USD 1,000 bill cost about 225% more in naira. Cloud and salaries are typically a startup's two largest costs. | Cost pressure is **existential**, not merely an efficiency goal. FX-aware reporting is not offered by any global vendor. |
| **Local alternatives** | MTN and Airtel are investing ~USD 400M in naira-priced cloud. Some startups have moved workloads to local providers (e.g., Nobus) to escape USD billing. | Optimising AWS spend is also a retention tool against local-currency clouds. Multi-cloud later may need to include African providers. |
| **Maturity** | Kenya (PwC 2026 outlook): only **13.3%** of organisations rate themselves highly mature in cloud use. Most expect budgets to rise, and few have cost management fully in place. | Buyers need **guidance and simplicity** more than enterprise FinOps depth. |
| **Regulation** | Nigeria Data Protection Act 2023, Kenya Data Protection Act 2019, South Africa's POPIA. Data residency matters most in finance and health. Data security is the top barrier for 56.7% of Kenyan organisations. | Vendors with no African presence, residency story or local support are at a disadvantage. To be validated in Phase 10. |
| **Talent** | FinOps roles appear in Johannesburg, Cape Town and Lagos (banking). Few dedicated African FinOps consultancies were found. | FinOps is underserved locally: there is a service gap as well as a tool gap. |
| **Vendor presence** | No profiled global vendor shows African billing currencies, local pricing or Africa-focused go-to-market. DoiT, for example, bills only in USD/EUR/GBP/CAD/AUD/DKK/NOK/SEK. | Africa is served by global tools sold in USD at US/EU price points, or not served at all. |

### 1.6 Buyer personas (market view)

| Persona | Typical title | Primary concern | Decision criteria |
|---|---|---|---|
| **Economic buyer: technical** | CTO, VP Engineering, Head of Cloud/Platform | Cut spend without slowing delivery or risking reliability | Safety of recommendations, engineering effort needed, integrations, security of access |
| **Economic buyer: finance** | CFO, Finance Director, FP&A lead | Predictability, allocation, budget control | Forecast accuracy, showback/chargeback, reporting, ROI and payback, pricing model |
| **Champion / daily user** | FinOps lead/analyst (larger orgs), Cloud/DevOps/Platform engineer (SMB) | Find and act on savings, explain variances | Recommendation quality, time to value, alerting, drill-down, automation |
| **Influencer / gatekeeper** | Security / compliance lead | Third-party access to the cloud account | Read-only vs write access, SOC 2/ISO, data residency, IAM scope |
| **Channel buyer** | AWS partner / MSP / reseller | Margin and multi-tenant management of client accounts | Multi-tenant views, white-labelling, billing/rebilling |

**Typical decision criteria, in rough order:**
1. Time to value: connect in minutes and see savings in a day.
2. Clear savings ROI, with a fee well below the savings.
3. Recommendations that are safe and trusted.
4. Allocation accuracy (tags, accounts, teams).
5. Level of automation, and how much control the buyer keeps.
6. Security posture: IAM scope, certifications, residency.
7. Pricing model fit and predictability.
8. Integrations: Slack/Teams, Jira, IaC, BI.

---

## 2. Competitor Inventory

Categories: **N** = native cloud tool; **P** = third-party FinOps platform (visibility + recommendations); **A** = automation-first (commitments, compute); **K** = Kubernetes-focused; **M** = MSP/reseller-led.

| # | Product (owner) | Cat. | Clouds | Primary focus | Target segment | Pricing model (indicative) | Profiled |
|---|---|---|---|---|---|---|---|
| 1 | **AWS Cost Explorer / Cost Optimization Hub / Compute Optimizer / Budgets** (AWS) | N | AWS | Native visibility, recommendations, budgets | All AWS customers | Mostly free; small API charges | ✅ |
| 2 | **Vantage** | P | AWS, Azure, GCP, K8s, ~20 SaaS/data providers | Developer-friendly multi-source visibility and reports | Startup → mid-market | Free → tiers by tracked spend | ✅ |
| 3 | **CloudZero** | P | AWS, Azure, GCP, K8s, SaaS | Unit cost / cost intelligence | Mid-market → enterprise SaaS | Custom; % of spend | ✅ |
| 4 | **IBM Cloudability** (+ Kubecost, Turbonomic) | P/K | AWS, Azure, GCP, OCI | Enterprise FinOps suite | Enterprise | Custom annual; by spend under management | ✅ |
| 5 | **Flexera One FinOps** (+ Spot, CloudCheckr, ProsperOps) | P/A/M | AWS, Azure, GCP | Unified IT asset + FinOps suite, plus automation | Enterprise; MSPs (via CloudCheckr) | Custom; ProsperOps on share of savings | ✅ |
| 6 | **nOps** | A/P | AWS-centric (+ K8s) | Automated AWS commitment management and visibility | SMB → mid-market AWS shops | Share of savings + fixed visibility fee | ✅ |
| 7 | **DoiT (Cloud Navigator / Flexsave)** | M/P | AWS, GCP, Azure | Reseller + platform + expert support | Startups → mid-market | Platform tiers; cloud resale | ✅ |
| 8 | Azure Cost Management + Advisor (Microsoft) | N | Azure (+ limited AWS) | Native Azure visibility | Azure customers | Free | — |
| 9 | Google Cloud Billing / Recommender (Google) | N | GCP | Native GCP visibility | GCP customers | Free | — |
| 10 | ProsperOps (Flexera since Jan 2026) | A | AWS, Azure, GCP | Autonomous commitment management | Mid-market → enterprise | % of savings | Within #5 |
| 11 | Spot (Flexera; Eco, Elastigroup, Ocean) | A/K | AWS, Azure, GCP | Spot instances, K8s infrastructure automation | Mid-market → enterprise | % of savings / usage | Within #5 |
| 12 | Kubecost (IBM) | K | K8s on any cloud | Kubernetes cost allocation and optimisation | Platform teams | Free ≤250 cores; per-vCPU enterprise | Within #4 |
| 13 | Finout | P | Multi-cloud + SaaS/data | "MegaBill" allocation, virtual tagging | Mid-market → enterprise | Flat fee by spend tier (custom) | — |
| 14 | Harness Cloud Cost Management | P/A | AWS, Azure, GCP, K8s | CCM inside a DevOps platform; AutoStopping | Engineering orgs on Harness | Module pricing | — |
| 15 | Ternary | P | GCP-first, multi-cloud | FinOps for GCP-heavy orgs | Mid-market → enterprise | Custom | — |
| 16 | Datadog Cloud Cost Management | P | AWS, Azure, GCP | Cost next to observability | Datadog customers | Add-on to Datadog | — |
| 17 | Zesty / CAST AI / Usage.ai / Archera | A/K | AWS (+ others) | Commitment, storage and K8s autoscaling automation | SMB → mid-market | % of savings / insurance-style | — |
| 18 | Infracost | Shift-left | Terraform (multi-cloud) | Cost estimates in pull requests | Engineering teams | Open source + paid cloud | — |
| 19 | Local/African MSPs and AWS partners | M | Mostly AWS/Azure | Consulting-led cost reviews | African SMB/mid-market | Services fees | — |

**Why these seven were profiled:** they best represent the options an **AWS-first, Africa-based SMB or mid-market buyer** will actually compare: the free native baseline (#1), the modern self-serve platform (#2), the unit-economics leader (#3), the two consolidating enterprise suites (#4, #5), the AWS automation specialist (#6) and the reseller/MSP model (#7), which is how many African companies already buy AWS.

---

## 3. Detailed Competitor Profiles

### 3.1 AWS native tools: Cost Explorer, Cost Optimization Hub, Compute Optimizer, Budgets, Trusted Advisor

**Core value proposition**
Free, built-in visibility and recommendations for every AWS account. No onboarding or data sharing with a third party.

**How it works**
- **Data sources:** first-party billing (CUR 2.0 / Data Exports, FOCUS-format exports), CloudWatch metrics, resource inventory.
- **Analysis:**
  - Cost Explorer: trends, grouping, forecasting, anomaly detection; Amazon Q explanations (no extra charge).
  - Compute Optimizer: ML-based rightsizing for EC2, ASG, EBS, Lambda, ECS on Fargate and RDS, plus idle detection now covering DynamoDB, ElastiCache, MemoryDB, DocumentDB, WorkSpaces and SageMaker endpoints. Lookback extended to 32 days for some resources.
  - Cost Optimization Hub: consolidates recommendations across accounts and Organizations, deduplicates them, estimates savings, exports to CSV.
  - Savings Plans/RI purchase recommendations with target-coverage analysis.
- **Automation:** low. Recommendations only. Acting on them requires the console, scripts or third-party tools. Budgets Actions can apply IAM/SCP restrictions.

**Target audience**
Every AWS customer. In practice used by engineers and finance in SMBs, and as a data source in larger orgs.

**Geographic focus / localisation**
Global. Covers `af-south-1` and Local Zones. The console is localised in several languages. Some local-currency invoicing exists (e.g., ZAR; reports suggest NGN — **to verify**), but pricing is still USD-based. No FX analysis.

**Pricing & positioning**
Mostly free. Cost Explorer API calls are about USD 0.01 each; hourly granularity is charged. Positioned as "good enough" and the default.

**Known gaps (factual):** AWS-only. Recommendations are not ranked by business risk. No team-level workflow (ownership, tickets, approvals). Shallow allocation beyond tags/cost categories. Unit economics need manual work. Limited automated execution. Steep learning curve for non-experts.

---

### 3.2 Vantage

**Core value proposition**
Fast, developer-friendly cost visibility across clouds **and** SaaS/data providers, with a generous free tier and transparent self-serve pricing.

**How it works**
- **Data sources:** AWS CUR via a cross-account read-only IAM role (CloudFormation one-click). Azure, GCP, Kubernetes (agent), and integrations with Datadog, Snowflake, MongoDB, OpenAI, Fastly and others.
- **Analysis:** cost reports and dashboards, virtual tagging and segments, unit costs, anomaly detection, budgets, forecasting, Kubernetes allocation. Rightsizing and idle recommendations, plus "Autopilot" for automated AWS Savings Plan purchases.
- **Automation:** medium. Mainly reporting plus selected automated commitment purchases. Terraform provider and API/FinOps-as-code.

**Target audience**
Startups through mid-market. Engineering-led buyers (CTO, platform, DevOps). Self-serve PLG motion with an Enterprise sales tier.

**Geographic focus / localisation**
US-based, global self-serve. English only. USD pricing. No Africa-specific presence or local currency.

**Pricing & positioning (indicative; sources disagree)**
| Tier | Tracked spend / month | Users | Price |
|---|---|---|---|
| Starter | ≤ USD 2,500 | 3 | Free (6-month retention) |
| Pro | ≤ USD 7,500 | 5 | ~USD 30–50 / month |
| Business | ≤ USD 20,000 | 10 | ~USD 200–250 / month |
| Enterprise | Custom | Custom | Quote |

Positioned as "modern, transparent, developer-friendly FinOps", in contrast with legacy enterprise suites.

---

### 3.3 CloudZero

**Core value proposition**
"Cost intelligence": allocate 100% of spend, including untagged, to the business dimensions engineers and finance care about (product, feature, customer), and track **unit cost**.

**How it works**
- **Data sources:** AWS CUR, Azure, GCP, Kubernetes, Snowflake, Datadog, New Relic and others. Custom telemetry streams (e.g., per-customer usage) for unit-cost maths.
- **Analysis:** CostFormation, a code-based allocation language that allocates without perfect tags. Unit-cost metrics, anomaly detection with engineering alerts, dashboards by team and product. Optimisation recommendations, increasingly supported by AI assistants.
- **Automation:** low to medium. Focus is insight and accountability, with limited direct remediation. FinOps advisory services are included in higher tiers.

**Target audience**
Mid-market to enterprise, cloud-native **SaaS** companies (typically ≥ USD 1M/yr cloud spend). Buyers: CTO, VP Engineering, CFO, FinOps lead.

**Geographic focus / localisation**
US (Boston). Customers mainly North America and Europe. No Africa presence or local pricing.

**Pricing & positioning (indicative)**
- No public price list. Custom contracts with unlimited users.
- AWS Marketplace listing reported at about **USD 19 per USD 1,000** monitored monthly spend (~1.9%). Sacra estimates a blended take rate of ~0.5% of spend.
- Rapid growth: an estimated ~USD 42M ARR in March 2026.
- Positioned as **premium** and business-outcome-focused ("unit economics for engineering-led companies").

---

### 3.4 IBM Cloudability (with Kubecost and Turbonomic)

**Core value proposition**
Enterprise-grade, multi-cloud FinOps: allocation, chargeback, forecasting and commitment planning at very large scale. Part of IBM's broader IT Financial Management stack (Apptio).

**How it works**
- **Data sources:** billing exports from AWS, Azure, GCP and OCI; container data via Kubecost; Apptio TBM data.
- **Analysis:** business mappings and allocation, showback/chargeback, budgets and forecasts, rightsizing, RI/SP planning, anomaly detection.
  - **Kubecost:** Kubernetes cost allocation and optimisation (free up to 250 cores; enterprise priced per vCPU).
  - **Turbonomic:** automated resource actions, mainly in on-prem/hybrid and Kubernetes contexts.
- **Automation:** low within Cloudability itself; higher when combined with Turbonomic. Commitment purchasing is mostly guided, not autonomous.

**Target audience**
Large enterprises and global organisations with central FinOps teams. Buyers: CIO, CFO, FinOps/IT finance leaders.

**Geographic focus / localisation**
Global through IBM's sales and partner network, **including Africa**: IBM has a long-standing presence in South Africa, Kenya, Nigeria and Egypt. Multi-currency reporting. Listed on the UK G-Cloud framework.

**Pricing & positioning (indicative)**
- Quote-based annual contracts.
- AWS Marketplace tiers reported at about **USD 30k/yr** for up to USD 1M managed spend, ~USD 77k for up to USD 3M and ~USD 132k for up to USD 6M, with overage fees.
- Positioned as the **enterprise standard** in IBM's ITFM portfolio. Reviewers note it is lagging on AI features, and some report price rises after acquisitions.

---

### 3.5 Flexera One FinOps (with Spot, CloudCheckr and ProsperOps)

**Core value proposition**
A single vendor for **all technology spend**: software licensing (ITAM/SAM), SaaS and cloud. Since 2025–26 it is also an execution layer that automates savings.

**How it works**
- **Data sources:** cloud billing (AWS, Azure, GCP), software and SaaS usage, IT asset inventory, Snowflake/Databricks (via Chaos Genius).
- **Analysis:** Flexera One cost reporting, allocation, policies (large policy catalogue), budgets and recommendations. CloudCheckr adds cost and compliance checks, with strong **MSP multi-tenant billing and re-billing**.
- **Automation (high, through acquisitions):**
  - **ProsperOps** (acquired Jan 2026, still run under its own brand): autonomous AWS/Azure/GCP commitment management, continuously buying and rebalancing Savings Plans/RIs. About USD 6B/yr of cloud usage under management.
  - **Spot** (acquired Mar 2025): Eco (commitment), Elastigroup (spot instances), Ocean (Kubernetes infrastructure automation).
- **Integration status:** a portfolio assembled through M&A. Forrester expects price rises and slower innovation during integration.

**Target audience**
Enterprise IT, procurement and FinOps teams. **MSPs and resellers** (CloudCheckr). Mid-market reached through ProsperOps' self-serve onboarding.

**Geographic focus / localisation**
Global (US HQ, strong EMEA). Africa is covered indirectly through partners and resellers. No Africa-specific pricing found.

**Pricing & positioning (indicative)**
- Flexera One: quote-based enterprise subscriptions.
- ProsperOps: **share of realised savings**. The rate is not published; third-party estimates range from ~25% to 30–35%. Some contract terms reportedly carry savings fees on future commitments after termination.
- Positioned as **"from dashboards to execution"**: the broadest technology-spend suite.

---

### 3.6 nOps

**Core value proposition**
An AWS-focused platform that **automatically** lowers the rate you pay (commitment management, spot) and gives visibility, priced so "we only get paid when you save".

**How it works**
- **Data sources:** AWS CUR and cost data; AWS APIs; Kubernetes/EKS cluster data.
- **Analysis:** cost visibility and allocation, showback, rightsizing and idle recommendations, Kubernetes (EKS) cost insights, AI cost visibility.
- **Automation:** high for rates and compute.
  - Autonomous Savings Plan/RI management, with incremental purchases and rebalancing as often as hourly. Covers EC2, Fargate, Lambda, RDS and ElastiCache.
  - Spot and Kubernetes compute automation (Compute Copilot / Karpenter-based).
  - Credit-back protection for unused commitments, with conditions.
- **Requires delegated purchasing authority:** reportedly, nOps decides the quantity and term of commitments under its agreements.

**Target audience**
SMB to mid-market AWS-centric companies; DevOps/platform teams and CTOs. Strong AWS partnership and co-sell motion.

**Geographic focus / localisation**
US-based, global through AWS Marketplace. No African localisation found.

**Pricing & positioning (indicative)**
- **Rate optimisation:** share of savings (rate not public).
- **Visibility/allocation:** fixed fee based on spend. A free tier is reported for small accounts (~USD 2,500/mo tracked spend; to verify). Free savings analysis offered.
- Positioned as the **AWS automation specialist** with low upfront cost.

---

### 3.7 DoiT (Cloud Navigator, Flexsave, expert services)

**Core value proposition**
Buy your cloud through DoiT and get a FinOps/CloudOps platform plus **unlimited access to senior cloud engineers**. Savings and expertise come bundled with resale.

**How it works**
- **Data sources:** billing data from accounts DoiT resells or connects (AWS, GCP, Azure).
- **Analysis:** Cloud Navigator / Cloud Intelligence reports, allocation, budgets, anomalies, recommendations. Flexsave automates commitment-style discounts without the customer making commitments.
- **Automation:** medium (Flexsave, some automated actions), backed by **human experts** for architecture and optimisation reviews.

**Target audience**
Startups to mid-market (especially VC-backed scale-ups), plus enterprises. Buyers: CTO, Head of Cloud, Finance. A strong fit for teams without in-house FinOps skills.

**Geographic focus / localisation**
Global (offices in North America, Europe, Israel, APAC). Billing currencies: USD, EUR, GBP, CAD, AUD, DKK, NOK and SEK — **no African currency**. No Africa-specific presence found; it competes with local AWS/GCP resellers.

**Pricing & positioning (indicative)**
- Cloud resale with "no markup" claimed; DoiT is paid through partner margins.
- Cloud Navigator list tiers: Standard ~USD 200, Enhanced ~USD 1,500, Premium ~USD 3,000 (billing period not stated). An Essentials tier is reported at USD 0 base. Bespoke enterprise pricing.
- Vendr median buyer spend is about **USD 82k/yr**.
- Positioned as **"expertise + platform + resale"**: a partner, not just a tool.

---

## 4. Landscape Summary (comparative snapshot)

| Dimension | AWS native | Vantage | CloudZero | IBM Cloudability | Flexera (+ProsperOps/Spot) | nOps | DoiT |
|---|---|---|---|---|---|---|---|
| AWS depth | ●●● | ●●○ | ●●○ | ●●○ | ●●● | ●●● | ●●○ |
| Multi-cloud | ○○○ | ●●● | ●●● | ●●● | ●●● | ●○○ | ●●○ |
| Allocation / unit cost | ●○○ | ●●○ | ●●● | ●●● | ●●○ | ●●○ | ●●○ |
| Recommendation depth | ●●○ | ●●○ | ●●○ | ●●○ | ●●○ | ●●○ | ●●○ |
| Automated execution | ●○○ | ●●○ | ●○○ | ●○○ (more with Turbonomic) | ●●● | ●●● | ●●○ |
| Ease for non-experts | ●○○ | ●●● | ●●○ | ●○○ | ●○○ | ●●○ | ●●○ (human help) |
| Self-serve / free entry | ●●● | ●●● | ○○○ | ○○○ | ●○○ | ●●○ | ●●○ |
| SMB affordability | ●●● | ●●● | ●○○ | ○○○ | ●○○ | ●●○ | ●●○ |
| Africa presence / localisation | ●●○ (infrastructure) | ○○○ | ○○○ | ●○○ (IBM network) | ●○○ (partners) | ○○○ | ○○○ |
| Local currency / FX insight | ●○○ (invoicing only) | ○○○ | ○○○ | ●○○ (multi-currency) | ○○○ | ○○○ | ○○○ |

`●●● strong · ●●○ moderate · ●○○ limited · ○○○ none/not found` — qualitative analyst judgement from public information; to be validated in Phase 2.

**Observed pricing archetypes:**
1. **Free / native** (AWS).
2. **Tier by tracked spend**, self-serve (Vantage).
3. **% of spend under management**, custom (CloudZero, Cloudability, Flexera).
4. **% of realised savings** (ProsperOps, nOps rate optimisation, Spot).
5. **Resale + platform tier + services** (DoiT, African MSPs).

---

## Appendix A: Competitor Research Using REA (from `extra.md`)

The resource in `extra.md` ([morluto/rea](https://github.com/morluto/rea), "Reverse Engineer Anything") is an MCP server and CLI that inspects native binaries, JavaScript/Electron apps, .NET assemblies and **websites (network capture)**, and explains how a feature works, with evidence.

**How it applies to us:**
- **Phase 1 (this phase):** limited value. Market structure, pricing and positioning come from public sources, not code.
- **Phases 2, 7–8 (SWOT, UX, wireframes):** useful for examining **publicly available front ends**: Vantage's free tier, AWS console flows we own, public calculators and onboarding pages (e.g., the CloudFormation one-click role templates competitors publish). This shows UX patterns, onboarding steps and requested IAM permissions.
- **Phase 9/11 (architecture):** studying competitors' **published IAM templates and Terraform providers** shows what data they pull and how they scope access.

**Guardrails (required):**
- Use only accounts we legitimately hold (free tiers, trials) and public assets.
- Respect each vendor's Terms of Service. Many SaaS terms prohibit reverse engineering, scraping or benchmarking. Legal review is needed before any analysis beyond observing the UI and public pages.
- Study patterns and evidence only; never copy proprietary code, text or design assets.
- Keep the evidence logs REA produces for traceability.

---

## Appendix B: Sources & Confidence

**Confidence key:** H = official/primary source; M = reputable secondary (analyst, trade press); L = vendor-competitor blog or aggregator. All pricing should be confirmed with vendors before use in Phase 10 pricing work.

**Market & trends**
- MarketsandMarkets, Cloud FinOps Market to USD 26.91B by 2030 (Jan 2026) — M — https://tools.prnewswire.com/en-us/live/20813/release/20260106EN57079
- Technavio, Cloud FinOps market analysis — M — https://www.technavio.com/report/cloud-finops-market-industry-analysis
- Straits Research forecast (syndicated) — L — https://groups.google.com/g/straits-research-latest-trends/c/pB5lstRSw8Y
- FinOps for Kubernetes market — L — https://natlawreview.com/press-releases/finops-kubernetes-market-projected-grow-444-billion-2030
- State of FinOps 2026 summaries — M — https://sdtimes.com/softwaredev/report-finops-priorities-are-shifting-left-and-expanding/ ; https://finout.io/blog/state-of-finops-2026-report-key-trends-insights-and-what-comes-next ; https://www.usu.com/en/blog/6-takeaways-from-the-state-of-finops-report-2026 ; https://kion.io/finops-foundation-state-of-finops-2026-report-key-takeaways/
- AWS FinOps feature updates 2026 — M — https://finopsweekly.com/news/aws-updates-2026-06-13/ ; https://finopsweekly.com/news/aws-updates-2026-april-january/
- AWS Cost Optimization Hub launch — H — https://aws.amazon.com/blogs/aws/new-cost-optimization-hub-to-find-all-recommended-actions-in-one-place-for-saving-you-money

**Consolidation**
- Flexera completes Spot acquisition — H/M — https://www.thomabravo.com/press-releases/flexera-completes-acquisition-of-netapps-spot-finops-portfolio ; https://siliconangle.com/2025/01/15/netapp-sells-spot-cloudcheckr-cloud-cost-control-company-flexera/
- Forrester on NetApp exiting FinOps — M — https://www.forrester.com/blogs/netapp-focuses-on-storage-and-exits-finops
- Flexera acquires ProsperOps & Chaos Genius — H/M — https://www.thomabravo.com/press-releases/flexera-acquires-prosperops-and-chaos-genius-to-expand-its-finops-solution ; https://itassetmanagement.net/2026/01/07/flexera-buys-prosperops-and-chaos-genius-from-dashboards-to-execution/
- IBM acquires Kubecost — M — https://techcrunch.com/2024/09/17/ibm-acquires-kubernetes-cost-optimization-startup-kubecost/ ; https://www.cfodive.com/news/ibm-ups-bet-finops-market-kubecost-buy-cloudsoftware-apptio/727853/

**Competitor pricing & capabilities**
- Vantage pricing — L — https://www.costbench.com/software/cloud-cost-management/vantage/ ; https://pricingsaas.com/companies/vantage ; https://toolradar.com/tools/vantage/pricing
- CloudZero — M/L — https://sacra.com/research/cloudzero-at-42m-arr-growing-757-yoy/ ; https://www.usage.ai/blogs/finops/tools/cloudzero-pricing/ ; https://costbench.com/software/cloud-cost-management/cloudzero/
- IBM Cloudability — L — https://www.finout.io/blog/ibm-cloudability-key-features-limitations-alternatives-2026 ; https://www.nops.io/blog/cloudability-pricing/ ; https://www.trustradius.com/compare-products/ibm-cloudability-vs-yuki
- Kubecost — L — https://www.finout.io/blog/kubecost-pros/cons-pricing-tutorial-alternatives-2026-guide ; https://www.nops.io/blog/kubecost-pricing/
- ProsperOps — H/L — https://prosperops.com/pricing ; https://www.nops.io/blog/prosperops-pricing/ ; https://spendark.com/blog/prosperops-alternative/ ; https://www.usage.ai/blogs/finops/tools/prosperops-pricing/
- nOps — H/L — https://www.nops.io/sharesave-works ; https://www.usage.ai/blogs/finops/tools/nops-pricing/
- DoiT — H/L — https://help.doit.com/docs/pricing ; https://spendark.com/blog/doit-international-alternative/ ; https://www.vendr.com/marketplace/doit-international
- Finout / tool comparisons — L — https://spendark.com/blog/best-finops-tools-compared-2026/ ; https://www.finout.io/top-finops-tools

**Africa**
- AWS Africa investment (ITWeb, Sept 2026) — M — https://www.itweb.co.za/article/aws-puts-23bn-behind-africas-cloud-ai-future/PmxVE7KEYWxqQY85
- AWS Lagos Local Zone — M — https://connectingafrica.com/cloud-networking/lagos-launches-first-aws-local-zone-in-africa ; https://thisdaylive.com/index.php/2023/01/27/aws-launches-new-aws-local-zones-location-in-lagos-nigeria
- AWS Nairobi Local Zone — M — https://theexchange.africa/amazon-web-services-announces-the-launch-of-new-aws-local-zone-cloud-infrastructure-in-kenya/
- PwC Kenya Cloud 2026 outlook — M/H — https://www.pwc.com/ke/en/assets/pdf/kenya-cloud-at-crossroads-from-momentum-to-maturity-2026-outlook.pdf
- McKinsey, Africa's leap ahead into cloud (older study) — M — https://www.mckinsey.com/za/our-insights/africas-leap-ahead-into-cloud-opportunities-and-barriers
- Naira devaluation and cloud costs — M — https://businessday.ng/news/article/nairas-fall-drives-startups-to-embrace-local-services/ ; https://techcabal.com/2025/08/15/mtn-airtels-billion-naira-cloud/ ; https://businessday.ng/technology/article/mtn-challenges-aws-google-with-naira-priced-cloud-services/ ; https://techpoint.africa/insight/nigerian-cloud-hosting-startups-are-ready/
- FinOps services in Africa — L — https://www.n-ix.com/best-companies-for-finops-consulting-services/

**Open items to verify before Phase 2/10:**
1. Current Vantage list prices (vantage.sh/pricing).
2. ~~Whether AWS invoices in NGN, KES and EGP as well as ZAR.~~ Resolved in Phase 2: ZAR invoicing; NGN/EGP card payment only (since Jan 2025); KES unconfirmed.
3. Exact data-residency and cross-border transfer rules under Nigeria's NDPA, Kenya's DPA and South Africa's POPIA.
4. A list of African AWS partners/MSPs offering cost optimisation (AWS Partner Finder), as the likely local competitors and channel partners.
5. The primary State of FinOps 2026 report (data.finops.org) for exact figures.
