# 08 — Wireframe Descriptions & UI Design Brief

**Product:** Cloudoptics by Cloudboosta
**Phase:** 8 of 11 — Wireframes & UI Design Brief
**Role:** Senior Product Designer (B2B SaaS, data-heavy products)
**Date:** 22 October 2026
**Inputs:** Phase 6 PRD (stories US-x.x), Phase 7 journeys (J1–J6) and IA
**Status:** Draft for review. Textual low-fidelity wireframes; visual design follows in Figma.

---

# Part 1 — Low-Fidelity Wireframe Descriptions

## 1.0 App shell (all authenticated screens)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ [Logo] Workspace ▾ │ Accounts: All ▾ │ Oct 1–22 ▾ │ $ / ₦ / Both │ ⓘ Data as of 06:10 │ 🔔 ? 👤 │
├──────────┬───────────────────────────────────────────────────────────────────┤
│ Overview │                                                                   │
│ Costs    │                      PAGE CONTENT                                 │
│ Recomm.  │   Page title · short description · primary action (right)         │
│ Budgets  │                                                                   │
│ Reports  │                                                                   │
│ Learn    │                                                                   │
│ ──────── │                                                                   │
│ Settings │                                                                   │
└──────────┴───────────────────────────────────────────────────────────────────┘
```

- **Left nav:** collapsible to icons; on mobile becomes a bottom bar (Overview, Costs, Recommendations, Budgets, More).
- **Global header:** workspace switcher; account filter (multi-select with search); date range; **currency toggle** (persisted per user); "data as of" with tooltip per data source; notifications; help (docs, contact support with local hours); user menu.
- **Global banners** (dismissible, stacked): connection degraded; data reconciling; first CUR pending; credits ending.
- **Primary interactions:** global filters apply to all data views and are reflected in the URL (shareable); keyboard shortcut `/` for search (accounts, resources, recommendations).

---

## 1.1 Dashboard — Overview

**Purpose:** a 2-minute weekly answer to "What are we spending, why did it change, and what should we do?" (J2, J3, J6; US-2.1, US-2.4, US-4.1).

**Layout (desktop, 12-column grid):**

```
Row 1 — KPI tiles (4 × 3 cols)
┌─────────────────┬─────────────────┬──────────────────┬──────────────────┐
│ Month to date    │ Forecast (month) │ vs last month     │ Savings realised │
│ $18,420          │ $26,900          │ ▲ 12% (+$2,140)   │ $1,310 / month   │
│ ₦28.4M           │ ₦41.5M           │ usage +7% · FX +5%│ ₦2.0M · 9 actions│
└─────────────────┴─────────────────┴──────────────────┴──────────────────┘
Row 2 — Spend trend (8 cols) | What changed (4 cols)
┌──────────────────────────────────────────┬──────────────────────────────┐
│ Daily spend: this month vs last (lines)  │ What changed                  │
│ budget line (dashed); anomaly markers    │ 1. NAT Gateway (eu-west-1)    │
│                                          │    +$910 — data processing ↑  │
│                                          │ 2. RDS (af-south-1) +$620 …   │
│                                          │ 3. EC2 –$300 (rightsized)     │
│                                          │ [Explore costs →]             │
└──────────────────────────────────────────┴──────────────────────────────┘
Row 3 — Top 5 actions this week (8 cols) | Status panel (4 cols)
┌──────────────────────────────────────────┬──────────────────────────────┐
│ ▸ Delete 14 unattached EBS volumes        │ Budgets: 1 at risk, 3 on track│
│   $420/mo · ₦648k · Low risk · S effort   │ Anomalies: 2 open             │
│ ▸ Rightsize api-prod-3 (m5.2xl→m5.xl) …   │ Credits: $8.2k left · ~63 days│
│ ▸ …                                       │ Connections: ✓ 12/12 healthy  │
│ [See all 37 recommendations ($6.1k/mo) →] │                               │
└──────────────────────────────────────────┴──────────────────────────────┘
Row 4 — Spend by service (donut or bar, 6 cols) | Spend by account (bar, 6 cols)
```

**Key components:** KPI tile (two-currency value, delta chip, sub-label); line chart with budget and anomaly overlays; "What changed" list; action card list; status panel with linked counts; breakdown charts.

**Primary interactions:**
- Clicking a KPI opens the matching detail (MTD → Explorer; Forecast → Forecast & FX; Savings → Savings tracker).
- Hovering the chart shows a tooltip with the day's value in both currencies; clicking a day opens Explorer filtered to it.
- Action cards: click to open the recommendation; quick actions "Assign" and "Dismiss".
- Status items link to filtered lists.

**States:**
- **First-run:** skeleton plus "Analysing your account — first results in ~1h; full detail within 24h" with a progress checklist.
- **No data:** connect CTA.
- **Partial data:** banner.
- **Role default:** Finance sees local-currency-first ordering and the forecast tile emphasised.

**Mobile:** KPIs as a 2×2 grid; trend chart; Top 5 list; status panel collapsed into an accordion.

---

## 1.2 Cost breakdown views — Costs › Explorer (by service, tag, team, project, account, region)

**Purpose:** let engineers and FinOps answer any "where is the money going?" question (J2; US-2.2, US-2.3).

**Layout:**

```
┌ Toolbar ─────────────────────────────────────────────────────────────────────┐
│ Group by: [Service ▾]  Filters: [+ Add filter] (Account: Prod ×) (Tag team=payments ×)│
│ Granularity: Daily | Monthly   Metric: Unblended ▾   [Save view] [Export CSV] │
└──────────────────────────────────────────────────────────────────────────────┘
┌ Chart (stacked bar, top 8 groups + "Other") ──────────────────────────────────┐
│ legend toggles; hover = values both currencies                                │
└──────────────────────────────────────────────────────────────────────────────┘
┌ Table ────────────────────────────────────────────────────────────────────────┐
│ Group | This period | Previous | Δ | Δ% | Share | Trend (sparkline) | Recs    │
│ EC2   | $7,210 ₦11.1M| $6,900  |+310|+4% | 39%  | ▁▃▅▆ | 6 ($940/mo) →        │
│ ▸ expand row → next dimension (e.g., usage type, then resource)               │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Group-by options:** Service, Account, Region, Usage type (friendly names), Tag key (team, project, env, cost-centre…), Resource (top 500), Charge type (usage, tax, credit, SP fee).

**"Team" and "project" views:**
- In the MVP these come from **tags** (e.g., `team`, `project`) or simple **virtual-tag rules** (account → team mapping) set in Settings › Preferences.
- An "(untagged)" row is always visible, with % share and a link "Improve tagging".

**Primary interactions:**
- Add filter (searchable dimension values); drill down by expanding table rows.
- Click a chart segment to filter.
- Save view (named, private or shared).
- Export CSV (async for large files with a notification).
- Rows with recommendations show a count and link to them.

**Anomalies sub-view:**
- List: date, scope (account × service), actual vs expected, impact, status (Open / Investigating / Expected / Resolved), owner.
- Detail drawer: chart with expected band, top contributors, actions (mark status, assign, open in Explorer).

**Credits sub-view:**
- Credit burn chart.
- Remaining balance (editable input, with an explanation of why it is manual).
- Depletion date and post-credit forecast.

**States:**
- Empty filter result: "No costs match"; suggest clearing filters.
- Large query: progressive loading with cancel.
- Tag key not activated as a cost allocation tag in AWS: inline guidance.

**Mobile:** chart + simplified table (group, amount, Δ%); drill-down via tap; filters in a bottom sheet.

---

## 1.3 Recommendation list

**Purpose:** a prioritised, filterable work queue that builds trust and drives action (J3; US-4.1).

**Layout:**

```
┌ Summary bar ──────────────────────────────────────────────────────────────────┐
│ Open: $6,110/mo (₦9.4M) · 37 items │ In progress: $1,050 · 5 │ Realised: $1,310 · 9 ✓ │
└──────────────────────────────────────────────────────────────────────────────┘
┌ Quick filters ─┐ [Quick wins (Low risk + S)] [High value] [Assigned to me] [Non-prod]
│ Filters: Type ▾ Account ▾ Region ▾ Risk ▾ Effort ▾ Status ▾ Owner ▾ Env ▾   Sort: Priority ▾
└──────────────────────────────────────────────────────────────────────────────┘
┌ Table ────────────────────────────────────────────────────────────────────────┐
│ ☐ | Recommendation              | Resource / Acct / Region | Savings/mo  | Risk | Conf. | Effort | Status | Owner │
│ ☐ | Delete unattached volume     | vol-0a1… / prod / eu-w-1 | $38 ₦59k    | Low  | High  | S      | New    | —     │
│ ☐ | Rightsize m5.2xl → m5.xl     | api-prod-3 / prod / af-s-1| $142 ₦219k | Med  | Med   | M      | Assigned| Wanjiru│
│   ↳ 2 alternatives                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
Bulk bar (when selected): [Assign ▾] [Dismiss ▾] [Export]   "5 selected · $410/mo"
```

**Key components:** summary metrics; quick-filter chips; filter bar; data table with risk/confidence badges (colour + text + icon, never colour alone); alternatives indicator; bulk action bar; pagination (50/page) or virtual scroll.

**Primary interactions:**
- Click a row to open the detail (full page; or a side panel on wide screens, with "open full page").
- Multi-select for bulk assign/dismiss.
- Sort by priority, savings, risk or date.
- Quick filters persist per user.
- **Savings tracker** tab: a Kanban-style or funnel view (Identified → Assigned → In progress → Done → Verified) with totals per column.

**States:**
- No recommendations yet (analysis running).
- "All clear" (celebrate, suggest next steps, e.g., enable memory metrics or member-account access for more coverage).
- Coverage note: "Recommendations cover 8 of 12 accounts — extend access".

**Mobile:** card list (title, savings, risk, status); filters in a bottom sheet; no bulk actions.

---

## 1.4 Recommendation detail

**Purpose:** give enough evidence and guidance for an engineer to act safely, and a manager to decide (J3; US-4.2–4.5, US-5.1–5.2).

**Layout (two-column):**

```
┌ Header ───────────────────────────────────────────────────────────────────────┐
│ ← Back   Rightsize EC2 api-prod-3: m5.2xlarge → m5.xlarge                      │
│ [Risk: Medium] [Confidence: Medium] [Effort: M]  Status: Assigned → Wanjiru ▾  │
│ Savings: $142/mo · $1,704/yr  |  ₦219k/mo · ₦2.6M/yr     [Assign] [Dismiss ▾] [Mark done] │
└──────────────────────────────────────────────────────────────────────────────┘
┌ Left (8 cols) ─────────────────────────────────┬ Right (4 cols) ──────────────┐
│ Summary (plain language, 2–3 sentences)         │ Why this is Medium risk       │
│ Current vs recommended (table: vCPU, RAM, $/hr, │ • Production environment (+25)│
│   monthly cost, family)                         │ • Memory metrics missing (+10)│
│ Evidence (tabs: CPU | Network | Memory* | Credits)│ • Projected p95 CPU 48% (+8) │
│   30-day chart with p95 and max lines; projected │ Confidence: Medium            │
│   utilisation after change (shaded)             │ 29/30 days of data; no memory │
│ Resource facts: tags, launch date, ASG/LB, AZ   │ [How to improve confidence]   │
│ ─────────────────────────────────────────────── │ Alternatives (1)              │
│ Guided fix                                      │ • Graviton m7g.xlarge $171/mo │
│ Tabs: Console | AWS CLI | Terraform             │ Activity                      │
│ Pre-steps ▸  Steps ▸  Rollback ▸ [Copy]         │ • Assigned by Tunde · 2d ago  │
│ "Cloudoptics never changes your account"        │ • Comment …  [Add comment]    │
│ ─────────────────────────────────────────────── │ Learn: How EC2 rightsizing    │
│ Why this matters (≤150 words)                   │ works (5 min) →               │
└────────────────────────────────────────────────┴──────────────────────────────┘
```

**Variants:**
- **Delete-type (R1/R2):** emphasises the data-loss warning, a "create snapshot" pre-step and a snapshot cost estimate.
- **Schedule (R7):** a visual weekly schedule picker (preview only; produces the scheduler template with the chosen hours).
- **Savings Plan advisory (R8):** see below.

**Savings Plan advisory (Commitments page):**
- **Coverage gauge:** current %, target %.
- **Option cards:** Conservative / AWS-recommended; 1-yr no upfront / all upfront.
- For each option: hourly commitment, monthly savings, payback, total commitment in USD and local currency.
- **FX slider:** commitment cost if the currency moves ±X%.
- **Risk note:** usage decline sensitivity.
- Explicit "Advisory only" label.

**Primary interactions:**
- Switch evidence tabs; hover charts.
- Copy snippets (copy confirms with a toast).
- Assign / reassign; set due date; comment.
- Dismiss with a reason (modal with reason options and snooze).
- Mark done; view the verification status (Pending verification → Verified, with realised savings).

**States:**
- **Verified:** green banner with the realised-savings calculation and before/after cost chart.
- **Resource gone (externally resolved):** information banner.
- **Stale data:** warning that the evidence is older than 48h.

**Mobile:** single column; header summary; evidence chart; risk panel; guided fix collapsed by default (copy works).

---

## 1.5 Budgets & alerts configuration

**Purpose:** set up budgets and alert rules in minutes, in local currency (J4; US-6.1–6.4).

**Budgets list layout:**

```
┌ Budgets ─────────────────────────────────────────────────── [+ New budget] ┐
│ Name          | Scope             | Budget      | Actual MTD | Forecast | Status      │
│ Company total | All accounts      | ₦45M ($29.2k)| ₦28.4M 63% | ₦41.5M 92%| On track    │
│ Payments team | tag team=payments | $6,000      | $4,900 82% | $6,700   | ⚠ At risk   │
└────────────────────────────────────────────────────────────────────────────┘
```

**New/edit budget (side panel or page, 4 steps on one screen):**
1. **Scope:** whole company / accounts (multi-select) / tag key = value. Shows a "last 3 months" mini-chart for the scope and a suggested amount.
2. **Amount:** value + currency selector; live conversion at today's rate; FX-risk note ("A 10% currency move changes this by …").
3. **Thresholds:** default 50/80/100% actual + 100% forecast (editable chips).
4. **Notify:** users, external emails, Slack channel. Preview of the message.

Buttons: [Save] [Cancel].

**Budget detail:**
- Progress bar (actual + forecast segment); daily cumulative chart vs budget line.
- Alert history.
- Breakdown of the scope's spend (top services); link to Explorer.

**Alert rules & sensitivity:**
- Anomaly minimum impact (USD/day) and % threshold (sliders with recommended defaults).
- Channels per alert type: budget, anomaly, connection, assignments, digest, report. Matrix of checkboxes (Email / Slack).
- Slack connection status and channel picker.
- Quiet hours (optional) by time zone.

**Primary interactions:** create/edit/duplicate/delete budget; pause alerts; test alert ("Send test to Slack").

**States:** no budgets (empty state with templates: "Company monthly budget", "Per account", "Per team tag"); tag not found; Slack not connected.

---

## 1.6 Onboarding wizard

**Purpose:** connect AWS in ≤ 15 min and reach first value quickly (J1; US-1.1–1.5).

**Structure:** full-screen, minimal chrome; progress stepper at top; "Save and continue later"; help link + support chat.

| Step | Content | Key interactions |
|---|---|---|
| **1. Welcome & workspace** | Workspace name; country; display currency (preview "$1,000 ≈ ₦1.5M"); role selector ("I'm a CTO / DevOps / Finance / FinOps") to personalise defaults | Continue |
| **2. How access works** | Plain-language card: "Read-only. We can't change or delete anything." Permission groups (Billing, Resource metadata, Metrics) with expandable details; links: IAM policy JSON, security pack, data location | "Connect AWS"; "Send instructions to a colleague" (generates an email/link) |
| **3. Connect AWS** | Choice: **Single account** or **AWS Organization (management account)**. Checkbox "Create billing export for me" (default on if none detected later). [Launch CloudFormation stack ↗] Instructions with screenshots; ExternalId shown with copy | Launch stack (new tab); "I've created the stack" → validation spinner |
| **4. Validation** | Checklist with live ticks: Role assumed ✓ · Billing export found ✓/creating · Cost Explorer ✓ · Resources ✓ · Metrics ✓ · Organization accounts (n) ✓. Failures show cause + fix + retry | Retry; view troubleshooting; contact support |
| **5. (Org only) Member account access** | Coverage summary (0 of 12 accounts with resource access). Explanation + [Launch StackSet ↗]. Skip option ("cost data still works") | Launch StackSet; Skip |
| **6. Invite team & connect Slack** | Invite emails with role chips (suggest a finance colleague); [Add to Slack] | Send invites; Connect Slack; Skip |
| **7. You're set** | "First insights in ~1 hour; full detail within 24 hours." Shows what's already available. CTA: Go to Overview | Go to Overview |

**States:** stack not detected after 15 min (nudge with a troubleshooting checklist); account already connected to another workspace (block with an explanation); insufficient AWS permissions for the user (suggest the colleague flow).

**Mobile:** onboarding is supported, but the CloudFormation step recommends desktop; the "Send to colleague" option is prominent.

---

## 1.7 Admin / Settings screens

Common layout: a Settings sub-nav on the left (Connections, Users & roles, Notifications & integrations, Preferences, Security, Workspace) with content on the right.

| Screen | Purpose | Key components & layout | Primary interactions |
|---|---|---|---|
| **Connections** | Monitor and manage AWS access | Table: account name/ID, type (management/member), status badge, last sync per data type (cost/resources/metrics), resource coverage, region count. Row detail drawer: per-permission checks, errors with fix steps, external ID, CUR export details | Add account; launch StackSet; re-validate; disconnect (confirm with typed account ID); request data deletion |
| **Users & roles** | Manage team access | Table: name, email, role, MFA status, last active; pending invites | Invite; change role; resend/revoke invite; remove user (confirm) |
| **Notifications & integrations** | Configure channels | Slack card (connected workspace, default channel); email settings; alert-type × channel matrix; per-user overrides note | Connect/disconnect Slack; send test; save matrix |
| **Preferences** | Tenant-wide behaviour | Display currency; time zone; fiscal month start; cost metric default; non-prod patterns (tag keys/values, name regex with live preview of matches); ignore tag; idle thresholds (bounded sliders); virtual-tag rules (account → team) | Save; reset to defaults; preview affected resources |
| **Security** | Security controls and evidence | MFA enforcement toggle; session policy (read-only in MVP); audit log table (filter by actor/action/date, export CSV); security pack downloads | Enforce MFA; export audit log; download pack |
| **Workspace** | Identity and lifecycle | Name; logo (for reports); report recipients; danger zone: delete workspace | Upload logo; set recipients; delete (typed confirmation, 7-day grace period) |

---

## 1.8 Reports (supporting screens)

- **Monthly summary history:**
  - List by month, with View (in-app HTML) / Download PDF / Resend.
  - The report preview mirrors the PDF layout: cover KPIs (both currencies), FX vs usage split, top drivers, budgets, savings, top open actions, data-accuracy note.
- **Forecast & FX:**
  - Forecast chart (actual, forecast, range).
  - Currency scenario slider and table for the next 3 months: USD, local at current rate, local at scenario.
  - Rate basis disclosure.
- **Exports:** list of generated CSV files (status, expiry, download).

---

# Part 2 — UI Design Brief (High-Fidelity)

## 2.1 Design goals

1. **Trustworthy:** feels like a financial tool. Precise numbers, clear sources ("data as of", rate basis) and no hype.
2. **Calm and clear in a dense product:** lots of data, little noise; clear hierarchy, whitespace and restraint with colour.
3. **Action-oriented:** every screen makes the next step obvious.
4. **Respectful of local context:** dual currency is first-class; fast on slow networks; works on mid-range Android phones.
5. **Accessible by default:** WCAG 2.2 AA.

## 2.2 Visual style direction

| Aspect | Direction |
|---|---|
| **Personality** | Modern, confident, warm-professional. "A sharp finance analyst who is also a great engineer." |
| **Brand alignment** | Cloudoptics is a Cloudboosta product: use Cloudboosta's brand colours and logo system if they exist (designer to obtain brand guidelines). Cloudoptics needs a distinct product mark ("optics": lens/clarity motif) |
| **Colour** | Cloudboosta brand palette (§2.2.1) on a neutral base (near-white surfaces, cool greys). **Semantic colours reserved for meaning:** green = savings/realised/healthy, amber = at risk/medium, red = over budget/high risk/failure, blue = information. **Chart palette:** 8 categorical colours, colour-blind safe, distinct from semantic colours; sequential palette for heat/intensity |
| **Dark mode** | Supported via design tokens from the start (engineers expect it); light mode is the default |
| **Typography** | A highly legible sans-serif with **tabular numerals** (e.g., Inter, IBM Plex Sans; final choice with brand). Monospace for IDs, ARNs and code snippets. Scale: 12/14/16/20/24/32; body 14px in data views, 16px in reading content |
| **Iconography** | One consistent outline icon set (e.g., Lucide or Phosphor); AWS service icons used only in tables/legends (respect AWS trademark guidelines) |
| **Imagery** | Minimal; simple illustrations only for empty states and onboarding; diverse, Africa-relevant representation where people appear |
| **Density** | Two density modes for tables (comfortable / compact). Default comfortable |
| **Motion** | Subtle (150–250 ms) for transitions and feedback; respect `prefers-reduced-motion` |
| **Tone of voice** | Plain English, direct, specific ("Delete 14 unattached volumes to save $420/month"). Avoid jargon or explain it inline. Never blame users |

### 2.2.1 Brand palette (confirmed 22 Oct 2026)

Cloudboosta's two brand colours plus a complementary deep navy. Contrast ratios are WCAG 2.x values (AA needs ≥ 4.5:1 for normal text, ≥ 3:1 for large text and UI parts).

| Token | Hex | Role | Use / don't use |
|---|---|---|---|
| `brand.blue` (Boosta Blue) | **#2C68FC** | Primary: buttons, links, selected states, active nav, focus ring, primary chart series | White text on blue = 4.67:1 ✓. Blue text on white = 4.67:1 ✓ (normal text OK). Hover/pressed: `#1F54D6` (6.37:1 on white) |
| `brand.yellow` (Boosta Yellow) | **#FAC812** | Accent: "savings found" highlights, Top-5 badge, onboarding progress, marketing moments, KPI spotlight | **Never as text or icons on white** (1.57:1 ✗). Use as a fill with navy text (10.17:1 ✓). Not used for warnings, so it doesn't get confused with "at risk" |
| `brand.navy` (Optics Navy — the complement) | **#0B1F4B** | Anchor neutral: headings and body text, side navigation background, report covers, dark-mode base | White on navy = 16.01:1 ✓; yellow on navy = 10.17:1 ✓. Blue on navy is only 3.42:1, so use `#5B8CFF` for links/accents on dark surfaces |

**Why navy:** blue + yellow is a high-energy complementary pair. A deep, slightly desaturated navy from the same hue family as Boosta Blue calms it down, reads as "finance-grade trust", gives the strongest text contrast, and lets yellow work as a highlight without clashing.

**Supporting semantic colours** (chosen to pass AA on white and to stay distinct from the brand colours):

| Token | Light mode (on white) | Dark mode (on navy/near-black) |
|---|---|---|
| `success` (savings, verified, healthy) | `#0A7F5A` (5.0:1) | `#3DD598` (8.5:1 on navy) |
| `danger` (over budget, high risk, failed) | `#B42318` (6.6:1) | `#FF6B5B` (5.7:1 on navy) |
| `warning` (at risk, medium risk) | text `#B54708` (5.4:1) on tint `#FEF0C7` | `#FCD34D` text on navy (11.1:1) |
| `info` | `brand.blue` | `#5B8CFF` |

**Suggested proportions:** ~70% neutrals (white/greys), ~15% navy (text, nav), ~10% blue (actions, data), ≤ 5% yellow (highlights only). The chart palette starts with Boosta Blue, then colour-blind-safe hues (e.g., teal, orange, purple, pink, slate) that avoid the success/danger greens and reds.

**Currency display:** the global currency toggle switches between **USD**, **local currency** and **both** (USD + local side by side). It is persisted per user, and every money value (`MoneyValue` component) follows it.

## 2.3 Component library expectations

Build as a **design system** in Figma with tokens that map 1:1 to code (Phase 11 picks the frontend stack, e.g., a React component library with Tailwind/tokens).

| Category | Components & requirements |
|---|---|
| **Foundations** | Design tokens: colour (semantic + chart), typography, spacing (4-pt grid), radius, elevation, z-index, motion; light/dark themes |
| **Money & numbers** | **MoneyValue** component: primary + secondary currency, abbreviation rules (k/M/B), locale formatting (₦, KSh, R, E£, GH₵, £, €, $), tooltip with rate and date, negative/credit styling. **Delta chip** (▲▼ with semantic colour + sign + text). **Percentage** and **progress** displays |
| **Data display** | **DataTable:** sortable, resizable columns, sticky header, row expansion (drill-down), selection and bulk bar, column visibility, density, empty/loading/error rows, virtualisation for 10k+ rows, CSV export hook. Badges (risk, confidence, status, effort) with icon + text. KPI tile. Key-value list. Activity timeline. Code block with copy and tabs |
| **Charts** | Line/area (with budget line, anomaly markers, expected band), stacked bar, horizontal bar, donut (max 6 segments + Other), sparkline, gauge (coverage). Common requirements: tooltips in both currencies, legends with toggle, accessible table alternative, responsive, colour-blind safe palette, consistent axis formatting, "partial day" styling. Library chosen in Phase 11 (e.g., ECharts/Recharts/visx) |
| **Filters & inputs** | Filter bar with chips; multi-select with search; date-range picker with presets and fiscal months; currency toggle (segmented control); sliders with numeric input; tag key/value pickers; regex input with live preview |
| **Forms** | Text, number (with currency adornment), select, combobox, checkbox, radio, toggle, textarea; inline validation; helper text; stepper (onboarding) |
| **Navigation** | Side nav (collapsible), top bar, tabs, breadcrumbs (detail pages), settings sub-nav, bottom bar (mobile), command/search palette |
| **Feedback** | Toasts, inline alerts, page banners (stackable), modals/confirmation dialogs (typed confirmation for destructive actions), side panels/drawers, skeleton loaders, progress checklists, empty states with CTA |
| **Workflow** | Assignee picker; status selector; dismiss-reason modal; comment box; verification banner |
| **Reports** | Print/PDF layout templates (A4, both currencies, logo slot) that share tokens with the app |

**Standards:** every component documented with anatomy, variants, states (default/hover/focus/active/disabled/loading/error), accessibility notes and do/don't examples.

## 2.4 Accessibility & responsiveness

| Area | Requirement |
|---|---|
| **Standard** | WCAG 2.2 AA |
| **Contrast** | Text ≥ 4.5:1 (≥ 3:1 for large text); UI components and chart marks ≥ 3:1 against adjacent colours |
| **Colour independence** | Risk, status and deltas always use text and/or icon as well as colour; charts support pattern or direct labels where needed |
| **Keyboard** | All features usable by keyboard; visible focus rings; logical tab order; skip-to-content; table keyboard navigation; Escape closes overlays |
| **Screen readers** | Semantic HTML landmarks; ARIA only where needed; charts have summaries and table alternatives; money values read with currency names |
| **Targets** | Touch targets ≥ 44×44 px on mobile; ≥ 24×24 px on desktop |
| **Text** | Supports 200% zoom without loss; no text in images |
| **Motion** | Respect reduced motion |
| **Responsive breakpoints** | ≥ 1440 (wide), 1280 (desktop – primary), 1024 (laptop/tablet landscape), 768 (tablet), 375–414 (mobile) |
| **Mobile scope (MVP)** | Fully usable: Overview, Recommendation list/detail, Budgets (view + create), Alerts, Reports view. Desktop-recommended: Explorer deep analysis, onboarding CloudFormation step, Settings › Preferences |
| **Performance** | Design for low bandwidth: lazy-load charts below the fold, avoid heavy imagery and web fonts beyond 2 weights (subset), prefer system font fallback; skeletons instead of spinners. Target performance budgets in PRD FR-UI-10 / NFR-PERF-03 |
| **Localisation readiness** | Layouts tolerate +30% text expansion (French) and are RTL-ready (Arabic for Egypt/North Africa later); dates and numbers locale-formatted; no concatenated strings |

## 2.5 Handoff expectations (Figma structure & process)

**Figma file structure:**

```
Cloudoptics — Design System (library file)
  00 Cover & changelog
  01 Foundations (tokens: colour, type, spacing, radius, elevation, motion; light/dark)
  02 Components (each with variants & states, documented)
  03 Charts
  04 Patterns (filter bars, tables with drill-down, empty states, forms, onboarding stepper)
  05 Report templates (PDF)

Cloudoptics — MVP Product (product file)
  00 Cover, status legend (🟡 WIP · 🟢 Ready for dev · 🔵 In dev · ✅ Shipped)
  01 Flows (J1–J6 user flow diagrams linked to frames)
  02 Onboarding
  03 Overview
  04 Costs (Explorer, Anomalies, Credits)
  05 Recommendations (List, Detail variants R1–R8, Savings tracker, Commitments)
  06 Budgets & Alerts
  07 Reports
  08 Settings
  09 Mobile
  10 Edge states (empty, loading, error, permission denied, stale data)
  99 Archive / explorations
```

**Conventions:**
- Frames named `Screen / State / Breakpoint` (e.g., `Recommendation Detail / Verified / 1280`).
- Auto-layout everywhere; components from the library only (no detached instances in "Ready for dev" frames).
- Design tokens exported (e.g., Tokens Studio → JSON) and versioned in the code repo; token names identical in Figma and code.
- Every "Ready for dev" screen includes: annotations for behaviour, data sources (PRD requirement IDs, e.g., US-4.2 / FR-REC-03), validation rules, empty/loading/error states, responsive behaviour and analytics events (PRD §7).
- Prototypes for J1 (onboarding), J3 (recommendation → act → verified) and J4 (budget) for usability testing (Phase 7 M6).

**Process:**
- Weekly design–engineering sync; a design review before each sprint's "Ready for dev" cut-off.
- Dev Mode for specs; Storybook (or similar) for implemented components, with a visual regression check against the design.
- Definition of Ready (design): flows approved by the PM; usability-tested where in scope; accessibility checklist passed; copy reviewed (plain-language check); all states designed.
- Design QA on staging before release; issues logged with screenshots and severity.

## 2.6 Copy guidelines (summary)

- Lead with the **action and the value**: "Stop 6 dev instances overnight — save ₦310k/month".
- Explain risk honestly: "Medium risk: production server; we couldn't see memory use".
- Always show **sources and timing**: "Costs as of 06:10 WAT · converted at CBN rate 22 Oct".
- Use friendly names for AWS usage types, with the raw name in a tooltip.
- Avoid "FinOps" jargon in primary UI; define terms in "Learn".

## 2.7 Open design questions

1. Final label for the main section: "Recommendations" vs "Savings" (Phase 7 card sort).
2. Primary/secondary currency order by role: should Finance default to **local first**? (Proposed: yes.)
3. Chart library choice and its accessibility support (decision with engineering in Phase 11).
4. ~~Cloudboosta brand guidelines availability.~~ Resolved: brand colours #2C68FC and #FAC812, with #0B1F4B navy as the complement (§2.2.1). Still open: logo and typography guidelines, and how strongly Cloudoptics shares the parent identity.
5. Whether the Savings tracker should be a Kanban board or a funnel/list (test in round 1 usability).
