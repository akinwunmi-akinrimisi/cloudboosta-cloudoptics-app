# 07 — UX Research Plan, User Journeys & Information Architecture

**Product:** Cloudoptics by Cloudboosta
**Phase:** 7 of 11 — UX Research Plan, Journeys & Information Architecture
**Role:** Senior UX Researcher / Product Designer (B2B SaaS, DevTools)
**Date:** 22 October 2026
**Inputs:** Phases 3 (personas, use cases), 4 (validation plan V1–V8), 5 (MVP), 6 (PRD)
**Status:** Draft for review

---

# Part 1 — UX Research Plan

## 1.1 Research goals

1. **Validate the problem and personas:** confirm the proto-personas A–D and the channel persona, their pains, triggers and current workflows (Phase 3, A1–A7).
2. **Shape the MVP experience:** learn what makes a recommendation trustworthy enough to act on, and what finance needs from FX-aware reporting.
3. **Remove onboarding friction:** find where AWS connection (IAM role, CUR export, Organizations) fails or causes hesitation.
4. **Test the information architecture and key flows** before high-fidelity design (Phase 8).
5. **Feed the business validation plan** (Phase 4 V1–V3, V8) with the same sessions, so research is efficient.

## 1.2 Key research questions

| Theme | Questions |
|---|---|
| **Problem & context** | How do teams find out about cost problems today? Who notices first? What happened the last time the AWS bill surprised them? How do FX movements show up in their work? |
| **Decision & trust** | What evidence does an engineer need before changing a production resource? What makes them reject a recommendation? Who must approve changes? |
| **Action** | How do changes actually get made (console, Terraform, tickets)? Where does cost work sit in the sprint? What would "done" look like? |
| **Finance** | How is cloud cost budgeted, forecast and reported today? Which exchange rate do they use, and who decides? What goes to the board? |
| **Onboarding & security** | Who approves third-party read access? What questions does security ask? Do they already have a CUR export? AWS Organizations? |
| **Regulation & placement** | (Nigerian fintechs) How is the CBN 2027 onshore rule affecting architecture and cost plans? |
| **Learning** | How do engineers learn AWS cost concepts today? Would in-product lessons be welcome or noise? |
| **Channel** | (Partners/MSPs) How do they run client cost reviews today? What would a multi-tenant tool need? |

## 1.3 Target participants & recruitment

| Segment | Persona | # (discovery) | # (usability) | Key criteria |
|---|---|---|---|---|
| CTO / Head of Engineering | A | 6–8 | 3 | Company 20–500 staff; AWS spend ≥ USD 5k/month; decision power over infrastructure tools |
| DevOps / Platform lead | B | 6–8 | 5 | Hands-on AWS (IaC); responsible for infrastructure; ≥ 2 AWS accounts |
| Finance manager / CFO | C | 5–6 | 3 | Owns budgeting; cloud is a material cost; local-currency revenue |
| FinOps lead / cost analyst | D | 3–4 | 2 | Formal cost responsibility; mid-market or regulated company |
| AWS partner / MSP lead | Channel | 3–5 | — | Manages ≥ 10 client AWS accounts |
| **Total** | | **23–31** | **13** | |

**Coverage quotas:** ≥ 3 countries (target split NG 35%, KE 25%, ZA 20%, EG/GH/other 20%); ≥ 3 industries (fintech ≥ 30%); mix of AWS Organizations users and single-account users; ≥ 3 participants with AWS credits running out within 6 months; ≥ 3 regulated companies (fintech/health/insurance).

**Recruitment channels:**
- Cloudboosta's alumni, community and course networks (screen out anyone with a conflict of interest).
- LinkedIn outreach to CTOs and DevOps leads in target companies.
- African tech communities: DevOps/AWS user groups in Lagos, Nairobi, Cape Town/Johannesburg and Cairo; startup hubs and accelerators.
- AWS partners in target markets (also channel interviews).
- The Phase 4 landing page waitlist (V4).

**Incentive:** a free cost assessment report (concierge V5) or a voucher (≈ USD 30–50 equivalent, paid locally), plus early access.

**Screener (summary):** role; company size and country; AWS monthly spend band; number of AWS accounts; current cost tools; who approves tools; revenue currency; consent to recording.

**Ethics & privacy:** informed consent (recording, anonymised quotes); no customer AWS data collected in discovery; concierge assessments use a signed data agreement and read-only role; data stored securely and deleted after the study; compliance with NDPA/Kenya DPA/POPIA/GDPR consent requirements.

## 1.4 Methods

| # | Method | Purpose | Participants | Format | Output |
|---|---|---|---|---|---|
| M1 | **Discovery interviews** (semi-structured, 45–60 min) | Pains, triggers, workflows, trust, FX handling | 23–31 across segments | Remote video (low-bandwidth friendly; audio-only fallback) | Interview notes, affinity map, persona updates |
| M2 | **Artefact review** | See real reports, spreadsheets, budgets, board slides, IAM review checklists | Subset of M1 (finance, FinOps, security) | Screen-share, anonymised copies | Document inventory, requirements for reports |
| M3 | **Survey** (10 min) | Quantify pain severity, tool use, FX importance, pricing perceptions (with V4) | 60–100 respondents | Online, shared via communities | Survey report; prioritisation inputs |
| M4 | **Concept tests** | Compare action modes (insight / guided / autonomous) and recommendation card designs (V3) | 10–12 (A, B, D) | Clickable mid-fidelity prototype | Preferences, trust drivers, wording |
| M5 | **Card sort (hybrid) + tree test** | Validate the navigation and labels (Part 3) | Card sort 15; tree test 30+ | Remote unmoderated tools | IA revisions |
| M6 | **Moderated usability tests** (2 rounds) | Onboarding, overview, recommendation detail/act, budgets, report | 13 (round 1: 7 on prototype; round 2: 6 on beta build) | Remote, think-aloud | Usability issues with severity; SUS score |
| M7 | **Concierge assessments** (V5) | Real recommendations on real accounts: test which evidence and fixes work | 8–10 design partners | Report + walkthrough call | Validated recommendation content and risk factors |
| M8 | **Beta diary + in-app feedback** | Ongoing use, triggers and frictions | Design partners (8–12 tenants) | Short weekly prompts (Slack/email) + in-app widget | Insight log; backlog items |
| M9 | **Product analytics review** | Funnel drop-offs, recommendation actions | All beta tenants | Event data (PRD §7) | Monthly UX metrics report |

## 1.5 Timeline & responsibilities

12 weeks, aligned with the Phase 4 validation plan and engineering Increments 1–2.

| Week | Activities | Lead | Support |
|---|---|---|---|
| 1 | Research plan sign-off; screener; recruitment starts; interview guides; consent forms | UX Researcher | PM |
| 2–4 | M1 discovery interviews (wave 1); M2 artefact review; M3 survey live | UX Researcher | PM, Founder/CPO (sits in on ≥ 5) |
| 4 | Synthesis workshop (affinity mapping); persona and journey updates; **gate G-A input** | UX Researcher | PM, Designer, Tech Lead |
| 4–5 | M5 card sort → IA v2; tree test | Product Designer | UX Researcher |
| 4–7 | M4 concept tests (action modes, recommendation card, FX views) | Product Designer | UX Researcher |
| 4–10 | M7 concierge assessments (design partners) | PM + Cloud/FinOps engineer | UX Researcher (observes walkthroughs) |
| 6–7 | M6 usability round 1 (prototype) | UX Researcher | Designer |
| 10–11 | M6 usability round 2 (Increment 1 build with real data) | UX Researcher | Designer, Engineers (observe) |
| 8–12+ | M8 diary and M9 analytics (beta onwards) | PM | UX Researcher |
| 12 | Research read-out; insights doc; updated journeys; backlog priorities | UX Researcher | All |

**Roles:** UX Researcher (plan, moderate, synthesise); Product Designer (prototypes, IA, concepts); PM (recruitment pipeline, design partners, decisions); Tech Lead (feasibility, observing); Founder/CPO (stakeholder, observes sessions); Cloud/FinOps engineer (concierge analysis).

## 1.6 Expected outputs

1. **Insights document** with key findings, quotes and implications, ranked by impact.
2. **Affinity map** (digital whiteboard) and evidence-linked themes.
3. **Validated personas** (updated Phase 3 proto-personas) with confidence ratings.
4. **Current-state journey maps** (as-is) for personas A, B and C.
5. **Future-state journeys** (Part 2, updated with findings).
6. **IA v2** with card sort and tree-test results.
7. **Usability findings** with severity (Critical/Major/Minor), recommended fixes and SUS scores (target ≥ 75 by round 2).
8. **Recommendation content guidelines:** evidence, risk wording and fix formats that build trust.
9. **Inputs for business gates** G-A/G-B (Phase 4): problem severity, FX need, action-mode preference, pricing signals.

## 1.7 UX success metrics (beta)

| Metric | Target |
|---|---|
| Task success: connect AWS without help | ≥ 80% |
| Time to connect (median, usability test) | ≤ 10 min |
| Task success: find and understand the top recommendation | ≥ 90% |
| Correctly explain a recommendation's risk after reading detail | ≥ 80% |
| Finance participants can produce next month's local-currency forecast | ≥ 80% |
| SUS score (round 2) | ≥ 75 |
| Tree test: correct first click on key tasks | ≥ 70% |

---

# Part 2 — Key User Journeys (future state, MVP)

Each journey lists the **trigger**, **steps**, **desired outcome**, and **pain points & opportunities** (from research hypotheses, to be validated). Step references to PRD stories are given in brackets.

## J1 — Onboarding & AWS account connection

**Primary persona:** B (DevOps lead), sometimes A (CTO).
**Trigger:** bill shock, credits ending, CTO asks for a cost review, landing page/webinar, partner referral.

| # | Step | System behaviour |
|---|---|---|
| 1 | Signs up with Google or email; verifies email | Creates tenant; user = Admin [US-1.1] |
| 2 | Chooses country and display currency | Default currency from country; shows a preview ("₦ and $") |
| 3 | Reads "What we access" panel | Plain-language permissions list; read-only; link to IAM policy JSON and security page |
| 4 | Clicks **Launch stack**; AWS console opens pre-filled; reviews and creates stack | Quick-create link with ExternalId; option to create a CUR 2.0 export [US-1.2] |
| 5 | Returns to Cloudoptics; connection auto-validates | Assume-role test; per-permission check list ✓/✗ [FR-CON-06] |
| 6 | (If Organization) sees linked accounts; optionally deploys StackSet for resource access | Coverage indicator "8 of 12 accounts" [US-1.3] |
| 7 | Sees first insights: MTD spend, top services, first quick wins (R1–R3) | "Full detail within 24h" banner [US-1.4] |
| 8 | Invites teammates (finance, CTO); connects Slack | Invite emails; Slack OAuth |
| 9 | Next day: receives "Your full cost analysis is ready" email | Triggered after first CUR load |

**Desired outcome:** connected in ≤ 15 min, first useful insight in ≤ 60 min, team invited, Slack connected.

**Pain points & opportunities:**

| Pain point | Opportunity |
|---|---|
| Fear of granting third-party access; security sign-off needed | "What we access" panel; downloadable security pack; read-only guarantee; sample IAM policy before sign-up |
| User lacks rights in the management account | "Send setup link to a colleague" (shareable instructions with the same ExternalId) |
| CUR export not set up; 24h wait feels like a dead end | Show quick API-based insights immediately; email when ready; show progress |
| Confusion over Organizations vs member accounts | Visual explanation; detect the account type and adapt instructions |
| CloudFormation errors (permissions, region) | Specific error mapping with fixes; support chat on African time zones |

## J2 — Viewing cost overview & drilling into anomalies

**Primary personas:** A (CTO), B (DevOps); C (Finance) for the overview.
**Trigger:** weekly check, anomaly alert in Slack, budget alert, end-of-month review, board question.

| # | Step | System behaviour |
|---|---|---|
| 1 | Receives Slack anomaly alert: "EC2-Other in eu-west-1 is $420 above normal today" | Alert with amount, % and top contributors; deep link [US-6.3] |
| 2 | Opens link → Costs view pre-filtered (account, service, date) | Daily chart with anomaly marked; breakdown by usage type [US-2.2] |
| 3 | Drills into usage type (e.g., NAT Gateway bytes) and resource | Table sorted by cost increase; resource IDs and tags |
| 4 | Identifies cause (e.g., new data pipeline) | — |
| 5 | Marks anomaly "Expected" or "Investigating"; optionally assigns a follow-up | Feeds suppression/tuning; activity log |
| 6 | Alternatively, from Overview, reads "What changed" summary in USD and local currency | Top 3 drivers in plain language; FX vs usage split [US-2.1, US-3.2] |

**Desired outcome:** understand the cause of any significant change in < 5 minutes; reduce time-to-detect from weeks to < 24h.

**Pain points & opportunities:**

| Pain point | Opportunity |
|---|---|
| Alert fatigue from minor anomalies | Minimum-impact thresholds; daily grouping; "Expected" learning |
| CUR delay means "today" data is incomplete | Clear "data as of" and "partial day" labels |
| Hard to tell FX-driven from usage-driven increases | FX decomposition in the overview and reports |
| Drill-down requires AWS knowledge (usage types are cryptic) | Friendly names for usage types + tooltips ("NatGateway-Bytes = data processed by NAT Gateway") |
| Cause is in an account without resource access | Prompt to extend access; still show CUR-level detail |

## J3 — Reviewing & acting on recommendations

**Primary personas:** B (DevOps) acts; A (CTO) and D (FinOps) prioritise and assign.
**Trigger:** "Top 5 this week" on Overview, weekly digest, new high-value recommendation, sprint planning, cost-reduction goal.

| # | Step | System behaviour |
|---|---|---|
| 1 | Opens Recommendations; sees totals (identified / in progress / realised) and a ranked list | Ranked by priority score [US-4.1, FR-REC-04] |
| 2 | Filters to Low risk + Small effort ("quick wins") | Saved filter suggestion |
| 3 | Opens a rightsizing recommendation | Detail: savings in both currencies, **risk reasons**, confidence, metric charts, resource facts [US-4.2] |
| 4 | Reviews evidence; checks memory note ("memory metrics unavailable — confidence Medium") | Link: "How to enable memory metrics" |
| 5 | Decides to act: assigns to self or a teammate with a due date | Slack DM to assignee [US-5.1] |
| 6 | Opens **Guided fix**: copies Terraform change; follows pre-steps and rollback notes | Snippets filled with resource IDs [US-4.3] |
| 7 | Applies change via the team's normal process (PR → pipeline) | — (outside Cloudoptics) |
| 8 | Marks "Done" | Status updated |
| 9 | Within 72h: Cloudoptics detects the change → **Verified**; after 7 days shows realised savings | Notification "₦1.2M/month saved — verified" [US-5.2] |
| 10 | Alternatively dismisses with reason ("Needed for DR") | Hidden; reason logged for learning [US-4.4] |

**Desired outcome:** engineers act on recommendations with confidence; savings are verified and visible to leadership.

**Pain points & opportunities:**

| Pain point | Opportunity |
|---|---|
| Distrust of generic recommendations | Evidence-first detail; explicit risk factors; confidence; conservative defaults |
| Cost work loses to feature work | Quick-win filter; weekly digest; effort labels; sprint-sized bundles ("5 quick wins = $X") |
| Applying changes is manual and error-prone | Ready-to-use Terraform/CLI; pre-steps and rollback |
| No recognition for savings | Verified savings feed with names (opt-in); monthly report credits teams |
| Duplicate/conflicting suggestions for one resource | Primary + alternatives; no double counting |

## J4 — Setting budgets & alerts

**Primary personas:** C (Finance), D (FinOps); A (CTO) approves.
**Trigger:** start of the budgeting cycle, a budget overrun, new team/project, FX shock, onboarding checklist.

| # | Step | System behaviour |
|---|---|---|
| 1 | Opens Budgets & Alerts → "New budget" | Form [US-6.1] |
| 2 | Chooses scope (whole company / account / tag e.g. `team=payments`) | Shows last 3 months' actual spend for that scope as a guide |
| 3 | Enters amount and currency (e.g., ₦45M / month) | Shows USD equivalent at today's rate and an FX-risk note |
| 4 | Keeps default thresholds (50/80/100% actual, 100% forecast); adds recipients and Slack channel | Preview of alert message |
| 5 | Saves; sees budget in list with current status (on track / at risk / over) | Status uses forecast |
| 6 | Later receives "Forecast to exceed by 12%" alert; opens budget → drill-down | Deep link to cost view for scope [US-6.2] |
| 7 | Configures anomaly sensitivity in Alerts settings (min USD/day) | Tenant setting [FR-REC-08] |

**Desired outcome:** budgets in place in < 5 minutes; overruns flagged early; finance and engineering share one view.

**Pain points & opportunities:**

| Pain point | Opportunity |
|---|---|
| Local-currency budget vs USD bill causes confusion | Show both, state the rate basis, highlight FX-driven variance separately |
| Unsure what amount to set | Suggested budget from history + growth; templates |
| Tags missing → can't budget by team | Show untagged share; link to tagging guidance (v1: allocation rules) |
| Alert spam | One alert per threshold per period; digest option |

## J5 — Admin & configuration

**Primary persona:** B or D as Admin.
**Trigger:** onboarding, team changes, security review, new AWS accounts, connection failure email.

| # | Flow | Key steps |
|---|---|---|
| 5a | **Users & roles** | Invite → assign Admin/Member/Viewer → resend/revoke invite → change role → remove user [US-8.1] |
| 5b | **Connections** | View accounts and health → fix degraded connection with guided steps → add account / StackSet → disconnect → request data deletion [US-1.5, US-8.3] |
| 5c | **Security** | Enforce MFA → view and export audit log → download security pack [US-8.2] |
| 5d | **Preferences** | Display currency, country/time zone, fiscal month start, non-prod patterns, ignore tags, anomaly thresholds [FR-REC-08] |
| 5e | **Notifications & integrations** | Connect Slack, choose channels per alert type, per-user preferences [FR-NOT-03] |
| 5f | **Reports** | Configure monthly executive summary recipients and logo [US-7.1] |

**Desired outcome:** Admin tasks are self-serve and quick; security reviewers find what they need; connection problems are fixed without support.

**Pain points & opportunities:**

| Pain point | Opportunity |
|---|---|
| Connection breaks silently (role deleted, export changed) | Proactive email + in-app banner + guided fix |
| Security questionnaire burden | Security pack (policy, data flow, sub-processors, hosting region, DPA) |
| Settings scattered | One Settings area with clear sections; search |

## J6 — Monthly reporting (supporting journey)

**Persona:** C (Finance), A (CTO).
**Trigger:** month end / board meeting.
**Steps:** email "Your October report is ready" → open PDF (both currencies, FX vs usage split, savings realised) → forward to board/CEO → (optional) export CSV for the finance close.
**Outcome:** board-ready report with zero manual work.
**Opportunity:** local formatting, company logo, rate basis disclosed, consistent month-on-month layout.

---

# Part 3 — Information Architecture (MVP)

## 3.1 Design principles

1. **Action first:** the home page answers "what should we do this week?" before "what did we spend?".
2. **Two currencies everywhere** with a persistent global toggle.
3. **Progressive disclosure:** simple summaries for CTO/finance; detail and evidence for engineers.
4. **Everything is a link:** alerts, reports and cards deep-link into pre-filtered views.
5. **Role-aware defaults:** the same IA, but default landing views and digests differ by role (finance lands on the Overview in local currency).
6. **Lightweight pages** for low-bandwidth networks.

## 3.2 Primary navigation

```
Cloudoptics
├── Overview (home)
├── Costs
│   ├── Explorer (group/filter/trend)
│   ├── Anomalies
│   └── Credits
├── Recommendations
│   ├── All recommendations (list + filters)
│   ├── My assignments
│   ├── Savings tracker (identified → in progress → realised)
│   └── Commitments (Savings Plan advisory)
├── Budgets & Alerts
│   ├── Budgets
│   └── Alert rules & sensitivity
├── Reports
│   ├── Monthly executive summary (history)
│   ├── Exports
│   └── Forecast & FX scenarios
├── Learn (FinOps guides; links to Cloudboosta courses)
└── Settings
    ├── Connections (AWS accounts, health, StackSet)
    ├── Users & roles
    ├── Notifications & integrations (Slack, email)
    ├── Preferences (currency, time zone, fiscal start, non-prod rules, ignore tags)
    ├── Security (MFA policy, audit log, security pack)
    └── Workspace (name, logo, data deletion)
```

**Global header:** workspace switcher · account filter · date range · currency toggle (USD / Local / Both) · "data as of" indicator · notifications bell · help · user menu (profile, MFA, sign out).

**Utility routes (outside nav):** onboarding wizard; invite acceptance; recommendation detail (`/recommendations/:id`); anomaly detail (`/costs/anomalies/:id`); budget detail (`/budgets/:id`); report viewer (`/reports/:month`).

## 3.3 Key screens and relationships

| Screen | Purpose | Main entry points | Links to |
|---|---|---|---|
| **Onboarding wizard** | Set up the workspace and connect AWS | Sign-up | Overview; Settings › Connections |
| **Overview** | Weekly snapshot: spend (both currencies), forecast, "what changed", top 5 actions, budgets at risk, credits runway, savings realised | Login, logo, digest emails | Costs Explorer (pre-filtered), Recommendation detail, Budget detail, Credits, Forecast & FX |
| **Costs › Explorer** | Analyse spend by dimension and time | Overview, alerts, nav | Resource-level table → Recommendation detail (if one exists); Export |
| **Costs › Anomalies** | List and triage anomalies | Slack/email alert, Overview | Explorer (pre-filtered); assign follow-up |
| **Costs › Credits** | Credit burn and runway | Overview card, digest | Forecast & FX |
| **Recommendations › List** | Prioritised actions | Overview "Top 5", digest, nav | Recommendation detail; bulk assign; export |
| **Recommendation detail** | Evidence, risk, guided fix, workflow | List, Slack DM, Overview | Costs Explorer (resource); Learn article; activity timeline |
| **Savings tracker** | Show value: identified → in progress → realised | Overview, Reports | List (filtered by status) |
| **Commitments** | Savings Plan advisory with FX and cash view | Recommendations list (R8), nav | Forecast & FX |
| **Budgets › List/Detail** | Create and monitor budgets | Nav, alerts, Overview | Explorer (scope); alert rules |
| **Reports › Monthly summary** | Board-ready monthly report | Email, nav | PDF download; Explorer |
| **Reports › Forecast & FX** | Forecast and FX scenarios | Overview, Commitments, Credits | Budgets |
| **Learn** | Contextual and general FinOps learning | Recommendation "Learn more", nav | External Cloudboosta content |
| **Settings › Connections** | Account health and access coverage | Failure emails, onboarding | Onboarding steps; StackSet |
| **Settings › Security** | MFA policy, audit log, security pack | Admin tasks, security reviewers | Audit export |

## 3.4 Screen relationship map (simplified)

```
                 ┌───────────── Alerts (Slack/Email) ─────────────┐
                 ▼                                                ▼
Onboarding ──► OVERVIEW ──► Costs Explorer ◄──► Anomalies     Budget detail
                 │  │            │                                ▲
                 │  │            ▼                                │
                 │  └──► Recommendations list ──► Recommendation detail ──► Learn
                 │              │                       │
                 │              ▼                       ▼
                 │        Savings tracker        My assignments / Slack DM
                 │
                 ├──► Forecast & FX ◄── Commitments (SP advisory)
                 ├──► Credits
                 └──► Reports (monthly summary) ──► PDF / Export
Settings (Connections · Users · Notifications · Preferences · Security · Workspace)
```

## 3.5 Role-based defaults

| Role / persona | Default landing | Overview emphasis | Digest content |
|---|---|---|---|
| CTO (A) | Overview | Spend, what changed, top 5 actions, savings realised | Weekly summary + monthly report |
| DevOps (B) | Overview → "My assignments" badge | Top 5 actions, anomalies | Assignments, new high-value recommendations, anomalies |
| Finance (C) | Overview in **local currency** | Forecast, budgets, FX impact, credits | Budget status, forecast, monthly report |
| FinOps (D) | Overview | All, plus savings tracker | Full digest |

## 3.6 Naming and labels (to validate in card sort)

| Label | Alternatives tested | Notes |
|---|---|---|
| Recommendations | "Savings", "Actions", "Opportunities" | Finance may prefer "Savings"; engineers "Recommendations" |
| Overview | "Home", "Dashboard" | — |
| Costs | "Spend", "Cost explorer" | — |
| Budgets & Alerts | "Guardrails", "Budgets" | — |
| Commitments | "Savings Plans", "Discounts" | Non-experts may not know "commitments" |
| Learn | "Academy", "Guides" | Link to the Cloudboosta brand |

## 3.7 Future IA extensions (post-MVP, placeholders)

- **Placement** (region and residency analysis) under Recommendations or a new "Architecture" section (v1).
- **Allocation** (virtual tags, showback) under Costs (v1).
- **Integrations** marketplace under Settings (Jira, Teams, IaC repos) (v1–v2).
- **Partner console** (client list, white-label) as a separate top-level workspace type (v1).
- **Unit economics** under Costs (v2); **Benchmarks** under Reports (v2).
