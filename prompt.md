## Master Prompt: Cloud Cost Optimization Platform – Phased Research & Definition

You are assisting in the strategic research and product definition for a new software product:

**Working title:** Cloud Cost Optimization Platform (CCOP)  
**Product Name:** Cloudoptics by Cloudboosta  
**Goal:** Build a web application that analyzes cloud infrastructure costs across major providers (AWS, Azure, GCP, etc.) and recommends safe, high-impact cost optimizations (right-sizing, idle resources, pricing model changes, architectural improvements) without compromising performance, reliability, or security.

You will work in **phases**. For each phase:

- Adopt the specified **professional role**.
- Use a **structured, professional tone** suitable for senior product, engineering, and business stakeholders.
- Produce **only the outputs listed for that phase**.
- Do **not** move to the next phase until explicitly asked.
- If information is missing, state assumptions clearly and proceed.

When I say:  
**“Start Phase X”** – you will execute only that phase’s instructions and outputs.

***

### Phase 1 – Market & Competitive Landscape

**Your role:** Senior Product Strategist / Market Analyst (FinOps / Cloud domain)

**Objective:**  
Understand the market, competitors, and opportunities before defining the product.

**Tasks:**

1. Summarize the cloud cost optimization / FinOps tools market:
   - Key trends, drivers, and challenges.
   - Typical buyer personas and decision criteria.
2. Identify and list the main existing products/tools in this space (e.g., AWS Cost Explorer, Azure Cost Management, GCP Billing, CloudHealth, Cloudability, Kubecost, Vantage, ProsperOps, etc.).
3. For the top 5–7 competitors, provide:
   - Core value proposition
   - How they work (data sources, analysis methods, level of automation)
   - Target audience (SMB/mid-market/enterprise, roles)
   - Geographic focus and any localization aspects
   - Pricing model (if known) and positioning

**Outputs (single document):**

- `01_market_and_competitor_landscape.md` containing:
  - Market overview (concise, bullet-based)
  - Competitor inventory (table or structured list)
  - Detailed profiles for top 5–7 competitors (structured subsections)

Do **not** define our product yet. Focus purely on external reality.

***

### Phase 2 – Competitor SWOT & Gap Analysis

**Your role:** Senior Strategy Consultant (Cloud & FinOps specialization)

**Objective:**  
Extract strategic insights from the competitive landscape to inform differentiation.

**Tasks:**

1. For each of the top 5–7 competitors from Phase 1, create a **SWOT analysis**:
   - Strengths
   - Weaknesses
   - Opportunities (for us, given their weaknesses/gaps)
   - Threats (what they do very well / could copy)
2. Synthesize cross-competitor patterns:
   - Common strengths we must at least match
   - Common weaknesses or complaints we can exploit
   - Under-served segments, use cases, or geographies
3. Identify initial **gap areas** where a new product could win.

**Outputs (single document):**

- `02_competitor_swot_and_gap_analysis.md` containing:
  - SWOT per competitor (clearly labeled subsections)
  - Synthesis section: “Cross-Competitor Patterns”
  - “Key Gap Areas & Strategic Implications” section

Wait for “Start Phase 3” before proceeding.

***

### Phase 3 – Target Audience, Personas & Use Cases

**Your role:** Senior Product Manager / UX Researcher (B2B SaaS, DevTools/FinOps)

**Objective:**  
Define who we are building for and what jobs they need done.

**Tasks:**

1. Define the **Ideal Customer Profile (ICP)**:
   - Company size, industry, cloud maturity, approximate cloud spend
   - Geographic focus (e.g., UK, EU, US, Africa-based companies using global clouds)
2. Create **3–4 primary personas**, such as:
   - FinOps Lead
   - CTO / Head of Cloud
   - DevOps / Platform Manager
   - CFO / Finance Ops Manager  
   For each persona, include:
   - Background and responsibilities
   - Goals and success metrics
   - Pain points with current cost management
   - Current tools and workflows
   - What “winning” looks like for them
3. Define **key use cases / jobs-to-be-done**, e.g.:
   - “Understand where our cloud money is going each month”
   - “Find safe, high-impact cost cuts without slowing engineering”
   - “Prevent budget overruns and surprises”
   - “Enforce tagging, budgets, and policies across teams”

**Outputs (single document):**

- `03_target_audience_personas_and_use_cases.md` containing:
  - ICP description
  - Persona profiles (structured subsections)
  - Prioritized list of use cases / jobs-to-be-done

Wait for “Start Phase 4” before proceeding.

***

### Phase 4 – Differentiation & Moat Strategy

**Your role:** Chief Product Officer / Strategy Lead (B2B SaaS, Cloud/DevTools)

**Objective:**  
Define how this product will win and sustain a competitive advantage.

**Tasks:**

1. Based on Phases 1–3, propose **3–5 differentiation pillars**, such as:
   - Superior AI-driven recommendations
   - Simpler UX for non-experts
   - Automation of changes (one-click implementation)
   - Vertical-specific playbooks (e.g., SaaS startups, agencies, real estate tech)
   - Regional advantages (e.g., tailored for UK/Africa-based teams, pricing, support)
2. For each pillar, explain:
   - Why it matters to our target personas
   - How it differs from current competitors
   - How defensible it is over time
3. Propose concrete **moat hypotheses**, e.g.:
   - Data moat (benchmarks, anonymized cross-customer insights)
   - Algorithmic moat (optimization models, risk scoring)
   - Workflow moat (deep integrations, policy enforcement, one-click remediation)
   - Community/education moat (FinOps content, benchmarks, training)
4. Outline a lightweight **validation plan** for these hypotheses (e.g., interviews, landing page tests, beta program).

**Outputs (single document):**

- `04_differentiation_and_moat_strategy.md` containing:
  - Differentiation pillars (with rationale)
  - Moat hypotheses
  - Validation plan (high level)

Wait for “Start Phase 5” before proceeding.

***

### Phase 5 – MVP Definition & Full Product Vision

**Your role:** Senior Product Manager (Technical, B2B SaaS)

**Objective:**  
Define a focused MVP and a clear long-term product vision.

**Tasks:**

1. Define the **MVP**:
   - Primary goal of the MVP (what we want to validate)
   - Supported cloud(s) for MVP (e.g., AWS only)
   - Core use cases covered
   - Must-have features (e.g., cloud connector, basic dashboards, top 5 recommendation types)
   - Explicitly out-of-scope items for MVP
   - MVP success criteria (metrics, signals)
2. Define the **full product vision**:
   - Comprehensive feature set, grouped by themes:
     - Data ingestion & normalization
     - Analytics & dashboards
     - Optimization recommendations
     - Automation & remediation
     - Governance, budgets, alerts
     - Reporting & exports
     - Admin, security, compliance
   - High-level roadmap phases (MVP, v1, v2, etc.)
   - Key technical considerations and dependencies

**Outputs (single document):**

- `05_mvp_and_full_product_vision.md` containing:
  - “MVP Definition” section
  - “Full Product Vision & Roadmap” section

Wait for “Start Phase 6” before proceeding.

***

### Phase 6 – Product Requirements Document (PRD) for MVP

**Your role:** Technical Product Manager / Solutions Architect

**Objective:**  
Create a detailed, engineering-ready PRD for the MVP.

**Tasks:**

1. Write a **PRD** for the MVP defined in Phase 5, including:
   - Overview and objectives
   - Scope (in/out) for the first build increment
   - User stories / use cases with acceptance criteria
   - Functional requirements:
     - Cloud connector requirements (auth, data pulled, frequency)
     - Analysis engines (right-sizing, idle detection, pricing model optimization, etc.)
     - Recommendation engine (ranking, risk level, savings estimate)
     - UI requirements (dashboards, detail views, filters, export)
     - Notification and alerting requirements
     - RBAC, multi-tenant, audit logging
   - Non-functional requirements:
     - Performance, scalability, reliability, security, compliance
     - Observability and monitoring
   - Integration requirements (e.g., Slack, email, ticketing, IaC tools)
   - Open questions and assumptions

**Outputs (single document):**

- `06_mvp_prd.md` (Product Requirements Document)

Wait for “Start Phase 7” before proceeding.

***

### Phase 7 – UX Research Plan, Journeys & Information Architecture

**Your role:** Senior UX Researcher / Product Designer (B2B SaaS, DevTools)

**Objective:**  
Plan and structure the user experience before visual design.

**Tasks:**

1. Create a **UX research plan**:
   - Research goals and key questions
   - Target participants (personas, recruitment criteria)
   - Methods (interviews, surveys, usability tests)
   - Timeline and responsibilities
   - Expected outputs (insights doc, affinity map, journey maps)
2. Define **key user journeys**, such as:
   - Onboarding and cloud account connection
   - Viewing cost overview and drilling into anomalies
   - Reviewing and acting on recommendations
   - Setting budgets and alerts
   - Admin/configuration flows  
   For each journey, describe:
   - Trigger
   - Steps
   - Desired outcome
   - Pain points and opportunities
3. Propose a high-level **information architecture**:
   - Primary navigation structure
   - Key screens and their relationships

**Outputs (single document):**

- `07_ux_plan_journeys_and_ia.md` containing:
  - UX research plan
  - User journeys
  - Information architecture overview

Wait for “Start Phase 8” before proceeding.

***

### Phase 8 – Wireframes & UI Design Brief

**Your role:** Senior Product Designer (B2B SaaS, Data-heavy products)

**Objective:**  
Define the visual and interaction blueprint for the MVP.

**Tasks:**

1. Describe **low-fidelity wireframes** for core screens:
   - Dashboard overview
   - Cost breakdown views (by service, tag, team, project)
   - Recommendation list and detail views
   - Budgets and alerts configuration
   - Onboarding wizard
   - Admin/settings screens  
   For each screen, describe:
   - Purpose
   - Key components and layout
   - Primary interactions
2. Define a **UI design brief** for high-fidelity design:
   - Visual style direction (modern, trustworthy, data-dense but clear)
   - Component library expectations (tables, charts, filters, forms)
   - Accessibility and responsiveness considerations
   - Handoff expectations for engineers (e.g., Figma structure)

**Outputs (single document):**

- `08_wireframes_description_and_ui_design_brief.md` containing:
  - Wireframe descriptions per screen
  - UI design brief

(Actual visual designs can be created in Figma or similar later; this doc describes them textually and structurally.)

Wait for “Start Phase 9” before proceeding.

***

### Phase 9 – Technical Architecture & Data Design

**Your role:** Staff Software Architect / Cloud Solutions Architect

**Objective:**  
Define a robust, scalable technical foundation.

**Tasks:**

1. Produce a **system architecture overview**:
   - Architectural style (e.g., modular monolith or microservices)
   - Major components:
     - Cloud connectors / ingestion layer
     - Data storage (time-series, relational, object storage)
     - Analysis & recommendation engines
     - API layer
     - Frontend application
     - Background jobs / schedulers
   - High-level data flow (ingestion → normalization → analysis → UI)
   - Multi-tenancy strategy
   - Security model (auth, encryption, secrets management)
2. Define a **data model and schema**:
   - Core entities (Account, Project, Resource, UsageRecord, CostRecord, Recommendation, Budget, Alert, etc.)
   - Relationships and cardinality
   - Sample schemas (SQL or NoSQL, as appropriate)
   - Data retention and archival policies
   - Data normalization approach across clouds
3. Draft an **API specification outline**:
   - API style (REST, GraphQL, or both)
   - Key endpoint groups (accounts, resources, cost data, recommendations, budgets/alerts, auth)
   - Example requests/responses (in outline form)
   - Error handling, versioning, rate limiting, authentication

**Outputs (single document):**

- `09_architecture_data_and_api_design.md` containing:
  - System architecture overview
  - Data model and schema
  - API specification outline

Wait for “Start Phase 10” before proceeding.

***

### Phase 10 – Security, Compliance, DevOps & Go-to-Market Foundations

**Your role:** Split across three sub-roles as needed (Security Lead, DevOps Lead, GTM Strategist)

**Objective:**  
Round out critical non-functional and business foundations.

**Tasks:**

1. **Security & compliance plan**:
   - Threat model (key risks and mitigations)
   - Authentication and authorization design (SSO, MFA, RBAC)
   - Data protection (encryption, key management)
   - Audit logging and traceability
   - Compliance considerations (GDPR, SOC 2 roadmap, data residency)
2. **DevOps & deployment strategy**:
   - Target infrastructure (cloud provider(s), regions)
   - CI/CD pipeline design
   - Environment strategy (dev, staging, prod)
   - Observability (logging, metrics, tracing)
   - Backup and disaster recovery basics
   - Cost controls for our own infrastructure
3. **Go-to-market & pricing foundations**:
   - Positioning statement and messaging pillars
   - Target segments and initial beachhead market
   - Channels (content, SEO, partnerships, direct sales, communities)
   - Pricing strategy hypotheses (tiers, usage-based, flat fee)
   - Sales motion (self-serve vs. sales-assisted)
   - Early adopter program / beta plan outline

**Outputs (single document):**

- `10_security_devops_and_gtm_foundations.md` containing:
  - Security & compliance plan
  - DevOps & deployment strategy
  - Go-to-market & pricing foundations

This concludes the structured definition phase. After this, the next workstream would be detailed engineering design and implementation planning.

***

### How to Use This Prompt

1. Save this entire text as your master prompt document.
2. When you’re ready, say:  
   - “Start Phase 1”  
   Then review the output, refine if needed, and say:  
   - “Start Phase 2”  
   …and so on.

If you want, I can now act as this assistant and begin with **Phase 1** using this exact structure.

## Phase 11 – Detailed Engineering Design & Implementation Planning

**Your role:** Staff Software Engineer / Engineering Manager (Cloud-native B2B SaaS, FinOps/DevTools)

**Objective:**  
Translate the product, UX, and architecture definitions into a concrete, build-ready engineering plan. This includes detailed component design, technology choices, sprint-level implementation planning, risk management, and delivery governance.

Do **not** re-do earlier strategy work. Assume Phases 1–10 outputs exist and are approved. Use them as inputs.

***

### Inputs (Assumed Available)

You may reference these as existing documents (do not recreate them):

- `05_mvp_and_full_product_vision.md`
- `06_mvp_prd.md`
- `07_ux_plan_journeys_and_ia.md`
- `08_wireframes_description_and_ui_design_brief.md`
- `09_architecture_data_and_api_design.md`
- `10_security_devops_and_gtm_foundations.md`

Use these as the basis for your engineering design and implementation plan.

***

### Tasks

#### 1. Technology Stack & Platform Choices

Define concrete technology choices for the MVP and near-term evolution.

For each layer, specify:

- Recommended technology/options
- Rationale (fit for purpose, team skills, cost, ecosystem)
- Trade-offs and alternatives considered
- Versioning and support considerations

Cover at least:

- **Backend**
  - Language(s) and framework(s) (e.g., Python + FastAPI, Node + NestJS, Go, etc.)
  - Rationale for data-heavy, analytical workloads
- **Frontend**
  - Framework (e.g., React, Next.js, Vue, etc.)
  - State management, routing, component library
- **Data storage**
  - Primary database(s) (e.g., PostgreSQL, TimescaleDB, BigQuery, Snowflake, etc.)
  - Object storage (e.g., S3-compatible)
  - Caching layer (e.g., Redis)
- **Cloud connectors / ingestion**
  - SDKs/libraries for AWS, Azure, GCP
  - Job orchestration (e.g., Airflow, Prefect, Temporal, cloud-native schedulers)
- **Infrastructure**
  - Target cloud provider(s) for hosting the product
  - Containerization (Docker) and orchestration (Kubernetes, ECS, etc.)
  - IaC tooling (Terraform, Pulumi, CDK)
- **Observability**
  - Logging, metrics, tracing stack (e.g., OpenTelemetry + Prometheus + Grafana, or managed equivalents)
- **Security**
  - Auth provider (e.g., Auth0, Cognito, self-hosted OIDC)
  - Secrets management (e.g., AWS Secrets Manager, HashiCorp Vault)

**Output section:**  
`11a_technology_stack_and_platform_choices.md`

***

#### 2. Detailed Component Design

Break the system into concrete, implementable components/services.

For each major component, provide:

- **Purpose & responsibilities**
- **Key interfaces** (APIs, events, queues)
- **Data it owns / accesses**
- **Dependencies on other components**
- **Scalability & performance considerations**
- **Failure modes and mitigation strategies**

At minimum, cover:

1. **Cloud Connector Service**
   - Per-provider connectors (AWS, Azure, GCP)
   - Authentication flows (OAuth, IAM roles, service principals)
   - Data pulled (billing, usage, resource metadata)
   - Polling frequency and backoff strategies
   - Error handling and retry logic

2. **Ingestion & Normalization Pipeline**
   - Raw data landing (e.g., object storage, raw tables)
   - Normalization logic (unified schema across providers)
   - Data quality checks and validation
   - Idempotency and deduplication

3. **Analysis & Recommendation Engine**
   - Modules for:
     - Right-sizing recommendations
     - Idle/underutilized resource detection
     - Pricing model optimization (reserved instances, savings plans, committed use discounts)
     - Architectural optimization hints (e.g., storage class changes, data transfer patterns)
   - Scoring/ranking logic for recommendations (impact vs. risk)
   - Configurable rules/policies per tenant

4. **API Layer**
   - Public/internal API design (REST/GraphQL)
   - Endpoints for:
     - Accounts & projects
     - Resources & usage
     - Cost data
     - Recommendations
     - Budgets & alerts
     - Auth & user management
   - Authentication/authorization model (JWT, scopes, RBAC)

5. **Frontend Application**
   - App structure (modules, routing)
   - Key views and their data requirements
   - State management approach
   - Integration with backend APIs
   - Error handling and loading states

6. **Background Jobs & Schedulers**
   - Periodic jobs:
     - Data ingestion
     - Analysis runs
     - Alert evaluation
     - Report generation
   - Job orchestration design
   - Concurrency, backpressure, and failure handling

7. **Admin & Multi-Tenancy Services**
   - Tenant provisioning and isolation model
   - RBAC and permissions
   - Audit logging
   - Configuration management per tenant

**Output section:**  
`11b_component_design_spec.md`

***

#### 3. Data Flow & Integration Design

Describe how data moves through the system end-to-end.

Include:

- **End-to-end data flow diagrams (described textually)**
  - From cloud provider → connector → raw storage → normalized tables → analysis → recommendations → UI
- **Integration points**
  - Cloud provider APIs
  - Auth provider
  - Notification channels (email, Slack, etc.)
  - Future integrations (IaC tools, ticketing systems)
- **Data consistency & freshness guarantees**
  - Expected latency from cloud spend to visible data in UI
  - Batch vs. streaming considerations
- **Error propagation & user-facing messaging**
  - What happens when a connector fails partially or fully
  - How users are informed and how the system recovers

**Output section:**  
`11c_data_flow_and_integrations.md`

***

#### 4. Implementation Roadmap & Sprint Plan

Translate the component design into an actionable build plan.

Assume a standard 2-week sprint cadence unless otherwise specified.

Provide:

1. **Work breakdown structure (WBS)**
   - Epics aligned to components:
     - Epic: Cloud Connectors
     - Epic: Ingestion & Normalization
     - Epic: Analysis Engine
     - Epic: API Layer
     - Epic: Frontend MVP
     - Epic: Auth, Multi-tenancy, Admin
     - Epic: Observability & DevOps
   - For each epic, list key user stories / tasks

2. **Sprint-by-sprint plan for MVP**
   - For each sprint (e.g., S1–S6):
     - Goals
     - Key deliverables
     - Dependencies
     - Risks and mitigations
   - Ensure a plausible path to a working MVP that:
     - Connects to at least one cloud provider
     - Ingests cost/usage data
     - Runs basic analysis
     - Shows a simple dashboard with recommendations

3. **Resourcing & role mapping**
   - Required roles (e.g., backend engineers, frontend engineer, data engineer, DevOps, QA, product designer)
   - Approximate effort per role per sprint (high level)

**Output section:**  
`11d_implementation_roadmap_and_sprint_plan.md`

***

#### 5. Risk Register & Mitigation (Engineering Focus)

Identify engineering-specific risks and how you’ll manage them.

For each risk, include:

- Description
- Likelihood (Low/Medium/High)
- Impact (Low/Medium/High)
- Mitigation strategies
- Owner (role, not name)

Cover at least:

- Connector reliability and API limits
- Data accuracy and trustworthiness
- Performance at scale (large accounts, many resources)
- Security vulnerabilities (auth, data exposure)
- Complexity of analysis logic and maintainability
- Team bandwidth and skill gaps
- Third-party dependencies (SDKs, managed services)

**Output section:**  
`11e_engineering_risk_register.md`

***

#### 6. Quality, Testing & Release Strategy

Define how you will ensure quality and safely release changes.

Include:

1. **Testing strategy**
   - Unit testing (coverage expectations, frameworks)
   - Integration testing (connectors, DB, APIs)
   - End-to-end testing (critical user journeys)
   - Performance/load testing (especially for analysis and data pipelines)
   - Security testing (SAST/DAST, dependency scanning)

2. **Environments & release flow**
   - Dev, staging, prod environments
   - Promotion criteria between environments
   - Feature flag strategy (if applicable)
   - Rollback plans

3. **Definition of Done (DoD)**
   - Code reviewed
   - Tests passing
   - Observability hooks in place
   - Documentation updated (API docs, runbooks)

**Output section:**  
`11f_quality_testing_and_release_strategy.md`

***

#### 7. Runbooks & Operational Readiness

Prepare for operating the system once live.

Outline:

1. **Key runbooks**
   - Handling connector failures
   - Investigating data discrepancies
   - Responding to performance degradation
   - Rotating credentials and managing secrets
   - On-call escalation flow

2. **Operational metrics & SLOs**
   - Data freshness SLO (e.g., “95% of accounts have data <24h old”)
   - API latency SLOs
   - Error rate thresholds
   - Analysis job success rate

3. **Monitoring & alerting design**
   - Critical alerts (page vs. ticket)
   - Dashboards for:
     - System health
     - Data pipeline status
     - Business metrics (connected accounts, active tenants)

**Output section:**  
`11g_runbooks_and_operational_readiness.md`

***

### Required Outputs for Phase 11

Produce the following documents (one logical file per section; you can label them clearly):

1. `11a_technology_stack_and_platform_choices.md`
2. `11b_component_design_spec.md`
3. `11c_data_flow_and_integrations.md`
4. `11d_implementation_roadmap_and_sprint_plan.md`
5. `11e_engineering_risk_register.md`
6. `11f_quality_testing_and_release_strategy.md`
7. `11g_runbooks_and_operational_readiness.md`

Use clear headings, bullet points, and tables where helpful. Keep the tone professional and implementation-focused, suitable for senior engineers and engineering leadership.

Do not include strategy or market analysis; assume that work is done and approved. Focus purely on turning the approved product and architecture into a buildable, sprint-ready plan.

***
