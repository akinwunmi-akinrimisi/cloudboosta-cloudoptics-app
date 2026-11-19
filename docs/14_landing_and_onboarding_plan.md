# 14 — Public Landing Page and "How It Works" Onboarding Page: Implementation Plan

**Product:** Cloudoptics by Cloudboosta
**For:** the Cloudoptics team (engineers, designer, PM, founder)
**Date:** 18 November 2026
**Inputs:** doc 13 (Refraxion teardown), README, docs 03, 04, 06 (FR-UI-*, US-1.x, FR-CON-*), 07 (J1, IA), 08 (wireframes, brand palette, accessibility), 10 (Part C GTM, A7 compliance), 11a, 11d (sprint plan), 11f, ADR-012/013, `apps/web`, `infra/cloudformation/cloudoptics-access.yaml`, `infra/digitalocean/app.yaml`
**Status:** Plan for review. §1 states facts about today's code; §§2–11 are recommendations. Nothing here is built yet.

---

## 1. Where we are today (facts)

| Area | Today | Source |
|---|---|---|
| Public pages | None. `/` in the app redirects to `/sign-in` when signed out. The only public text is the sign-in card and `index.html`'s one meta description | `apps/web/src/app/router.tsx`, `apps/web/index.html` |
| Web app | React 19 + Vite SPA, TanStack Router (browser history; hash history in demo mode), TanStack Query, Tailwind v4 with design tokens in `src/styles.css`. All pages are imported eagerly in `router.tsx` | `apps/web` |
| Sign-in | `SignIn.tsx`: WorkOS AuthKit on staging ("Continue to sign in"), a development form locally, a no-password form in the demo | `pages/SignIn.tsx`, `app/AuthGate.tsx` |
| In-app onboarding | `Onboarding.tsx`, three steps: **Workspace** (name, country) → **Connect AWS** (one quick-create link, four numbered instructions, a "by hand" fallback, polling every 5 s, a slow-path hint after 10 min) → **Check access** (per-permission checklist, then "Go to my workspace") | `pages/Onboarding.tsx` |
| Not in the wizard yet | The "What we access" panel (J1 step 3; FR-CON-13 asks the onboarding screen to list every permission and which recommendation types need it), a colleague hand-off (J1 pain point), an invite step (FR-UI-03) | docs 06, 07, 08 §1.6 |
| Access template | One stack, `cloudoptics-access.yaml` v2.0.3, us-east-1 only: two read roles, a bill-export bucket, a CUR 2.0 export, a setup function, and a StackSet for member accounts | `infra/cloudformation/cloudoptics-access.yaml` |
| Hosting | Staging at `https://cloudoptics.cloudboosta.com` on App Platform (London): static `web` at `/` with an `index.html` catch-all, API at `/api`, templates at `/cfn`. Demo on GitHub Pages | `infra/digitalocean/app.yaml`, ADR-012/013 |
| URL settings | The API builds email links from `CLOUDOPTICS_APP_URL` and, when `CLOUDOPTICS_API_PUBLIC_URL` is empty, the stack callback from `APP_URL + "/api"` | `apps/api/src/cloudoptics/platform/settings.py` |
| Analytics | PostHog EU is the chosen tool (doc 11a) but nothing is wired into the web app yet | `apps/web/src` (no analytics code) |
| Budgets | Initial JS ≤ 300 KB gzipped for the app shell, enforced by `scripts/check-bundle-size.mjs` | PRD FR-UI-10 |
| Plan of record | Marketing site in Sprint 13 (v1 launch); "landing page + waitlist" in GTM months 0–3 | docs 11d, 10 C8 |

**The gap.** Design-partner recruitment for the Increment 2 beta (end of Sprint 7) needs a public page that explains the product, the read-only access and the setup, and that a security reviewer can read before anyone signs up. Sprint 13 is too late for that.

---

## 2. Goals and success metrics

### 2.1 Goals

1. Explain Cloudoptics in one screen to an African CTO, DevOps lead or finance manager: costs in USD and local currency, what is safe to cut, verified savings, read-only.
2. Remove the access objection before sign-up: publish exactly what the stack creates and every permission, in plain language.
3. Shorten time to first value: people arrive at the wizard already knowing what they will do in AWS and who in their team must do it.
4. Be findable and shareable: each public page has its own HTML, title and social card (Refraxion's single `index.html` is the counter-example, doc 13 §5.2).

### 2.2 Metrics

| Metric | Definition | Target (beta) | Source |
|---|---|---|---|
| Visitor → sign-up | Unique visitors on `/` and `/how-it-works` who complete WorkOS sign-up within 7 days ÷ unique visitors | ≥ 4% overall; ≥ 8% for visitors who viewed `/how-it-works` | Site events + server `signup_completed` with attribution |
| Sign-up → connected | Sign-ups with a healthy connection within 7 days | ≥ 60% (PRD O1) | Server events |
| Onboarding completion | Workspace created → stack callback received → connection healthy, each step's conversion | ≥ 85% workspace → link opened; ≥ 75% link opened → healthy | Server events (§10.3) |
| Time to connect | Median sign-up → connection healthy | ≤ 15 min (PRD O1); ≤ 10 min in usability tests (doc 07 §1.7) | Server events |
| Time to first value | Median connection healthy → first recommendation viewed; and → first cost data shown | ≤ 60 min to first recommendation (US-1.4); ≤ 24 h to first costs | Server events |
| Colleague hand-off | Share of connections completed through a shared link | Tracked, no target | `connect_link_copied` |
| Security-page reach | Share of sign-ups that viewed `/security` first | Tracked; a high share means the page is doing its job | Site events |
| Quality gates | Lighthouse mobile performance ≥ 90, accessibility 100, SEO 100; axe: 0 serious issues | Every release | CI (§7.3, §7.4) |

Targets for visitor conversion are starting assumptions for a B2B free-tier product; the PM resets them after four weeks of data.

---

## 3. Audience and messaging hierarchy

### 3.1 Who lands on these pages

| Persona (doc 03) | Arrives from | What they need to read | Decides |
|---|---|---|---|
| Tunde, CTO | Founder outreach, webinar, partner referral | What it is, why it's safe, what it costs, who's behind it | Whether to try it |
| Wanjiru, DevOps lead | Link from the CTO, AWS user group | Exactly what the stack creates, permissions, time, how to remove it | Whether she will deploy it |
| Aisha, finance manager | Link from the CTO | Local currency, forecasts, reports | Whether she will use it monthly |
| A security reviewer | Link from Wanjiru | Permissions, data location, retention, sub-processors, DPA | Sign-off |
| Ngozi, partner/MSP | Cloudboosta network | Multi-account, partner plans later | A pilot |

### 3.2 Message hierarchy

| Level | Message (our words) | Proof we can show at beta |
|---|---|---|
| Promise | See your AWS bill in your currency, and know what is safe to cut. | Product screenshots on sample data, clearly labelled |
| Pillar 1, safe savings | Every saving comes with a risk score, the evidence behind it and the steps to undo it. | Recommendation detail screenshot; list of recommendation types (README) |
| Pillar 2, your currency | Every figure in dollars and naira, shillings, rand or cedis, with "what if the rate moves" forecasts. | Currency toggle, FX scenario, credits runway |
| Pillar 3, read-only and quick | One CloudFormation stack, about 15 minutes, and Cloudoptics can only read. | The published permission list (§5.5), generated from the template |
| Pillar 4, proven | We count a saving only when it shows up in your bill. | Verified-savings tracker screenshot; no customer numbers until partners agree |
| Pillar 5, built for African teams | Local pricing, local support hours, Cloudboosta's training community. | Founder-provided facts only (§11 Q5) |

**Rules for copy.** Plain English, specific numbers, no FinOps jargon in headlines (doc 08 §2.6). No savings percentages, customer logos or testimonials until design partners agree in writing. Every time, location and permission claim comes from one claims file (§7.6) so pages cannot contradict each other (doc 13 §3.3).

---

## 4. Information architecture and URLs

### 4.1 Site map (public)

| URL | Page | Indexed | Owner |
|---|---|---|---|
| `/` | Landing | yes | PM |
| `/how-it-works` | Descriptive onboarding page | yes | PM + Tech Lead |
| `/security` | Security, data handling and the full permission reference (`#permissions`) | yes | Tech Lead |
| `/pricing` | Pricing (or a landing section until prices are decided, §11 Q3) | yes | Founder |
| `/about` | Cloudboosta, team, contact | yes | Founder |
| `/legal/privacy`, `/legal/terms`, `/legal/cookies`, `/legal/subprocessors`, `/legal/dpa` | Legal | yes | Founder + counsel |
| `/demo/` | The existing demo build (sample data) | **no** (`noindex`, robots disallow) | Frontend |
| `/app/…` | The product (sign-in, onboarding, workspace) | **no** | Frontend |
| `/api/…` | API (unchanged) | no | Backend |
| `/cfn/…` | Downloadable CloudFormation templates (paths unchanged) | no | Tech Lead |
| `/404` | Real 404 page (served with status 404) | no | Frontend |
| `/.well-known/security.txt` | Vulnerability reporting contact | n/a | Tech Lead |

Later, without changing the architecture: `/guides/…` for the SEO content in doc 10 C4 ("AWS bill in naira", FX explainers, CBN 2027).

### 4.2 Navigation

Header: logo ("Cloudoptics by Cloudboosta") · How it works · Security · Pricing · Demo · **Sign in** · **Start free** (primary). Footer: the same links plus About, legal pages, contact, support hours, and the AWS trademark notice ("not affiliated with Amazon Web Services").

### 4.3 Entry points into the app

| From | To | Carries |
|---|---|---|
| "Start free" | `/app/sign-in?intent=signup` | `utm_*`, `ref`, landing page path (§7.5) |
| "Sign in" | `/app/sign-in` | — |
| "Try the demo" | `/demo/` (opens the sample workspace, §6.4) | nothing; the demo loads no analytics |
| Wizard "What will this create?" | `/how-it-works#what-the-stack-creates` (new tab) | — |
| Wizard "Read every permission" | `/security#permissions` (new tab) | — |

---

## 5. Content briefs

Draft headlines are ours and are starting points for the copy deck (task SITE-04). They do not reuse Refraxion's wording.

### 5.1 Landing page (`/`), section by section

| # | Section | Must say | Must show | Draft copy direction |
|---|---|---|---|---|
| 1 | Header | Where to learn, where to start | Logo, nav, two CTAs | — |
| 2 | Hero | The promise, who it is for, read-only | Overview screenshot with the currency toggle; a small toggle that switches the hero figure between USD and NGN/KES/ZAR/GHS (the only script on the page, ≤ 5 KB) | H1: "Your AWS bill in dollars and naira. And what's safe to cut." Alt: "Cut your AWS bill without breaking production." Sub: "Cloudoptics reads your AWS bill and resources, shows every figure in USD and your local currency, and ranks each saving by how safe it is. Read-only, set up in about 15 minutes." CTAs: "Start free" / "Try the demo". Trust line: "Read-only access · No card needed · Remove it any time" |
| 3 | The problem | Three pains our ICP names | Three short cards | "The bill is in dollars, the budget is in naira." · "Credits run out on a date nobody is watching." · "Nobody on the team has time to be a FinOps analyst." |
| 4 | How it works (summary) | Three steps that match the wizard's names | Three numbered steps, each with one concrete artefact (stack name, a permission check tick, a first recommendation) | "1. Connect read-only: one stack in AWS, about 3 minutes." "2. See costs in both currencies: first figures within a day of AWS delivering your bill." "3. Act safely, then see it in the bill." Link: "See every step →" `/how-it-works` |
| 5 | Safe savings | Risk score, evidence, rollback, guided fix, open in console | One recommendation card (sample data): "Delete unattached volume, Low risk, saves $38 / ₦58k a month, snapshot first" | H2: "Savings you can make on a Friday afternoon." List the recommendation types from the README in one line each |
| 6 | Your currency | Dual currency, FX scenario, forecast, credits runway | Forecast with a rate slider (screenshot) | H2: "Know next quarter's bill before the rate moves." |
| 7 | Verified savings | Counted from the bills, not estimated | Savings tracker screenshot | H2: "We only count savings that show up in your bill." |
| 8 | Read-only by design | What the stack creates, what it cannot do, how to remove it | Two short lists, generated from the permission data (§5.5); link to the template file | H2: "It can read. It can't change anything." Points: "Two read roles, each limited to a named list of actions." "No access to your files, databases, secrets or logs." "Delete one stack and the access is gone." Link: "Read every permission →" |
| 9 | For your team | CTO / DevOps / Finance each get something | Three columns | "For the CTO: one number for what can safely go." "For DevOps: fixes with rollback steps, assigned to an owner." "For finance: the monthly report in your currency." |
| 10 | Pricing or beta offer | Free entry, what is free, what is paid later | Per §11 Q3; until decided: "Free during the beta" card and what happens after | Never "TBD" without a date; state what the free tier includes in numbers |
| 11 | Who's behind it | Cloudboosta and its training community | Founder-approved facts only | H2: "Built by Cloudboosta, who train cloud engineers across Africa." (subject to Q5) |
| 12 | FAQ | Top objections (§5.3) | Native `<details>` list, also emitted as `FAQPage` JSON-LD | 8–10 questions |
| 13 | Final CTA | Start, or read how it works | Two buttons | "Connect your first account in about 15 minutes." |
| 14 | Footer | §4.2 | — | — |

The page works fully without JavaScript; the currency toggle is an enhancement. No autoplay video.

### 5.2 "How it works" page (`/how-it-works`), section by section

This page describes the onboarding for the person who will do it, and is the page the DevOps lead forwards to a security reviewer.

| # | Section | Content |
|---|---|---|
| 1 | Intro | "What happens when you connect AWS, step by step. About 15 minutes, most of it waiting for AWS." Jump links to each section |
| 2 | Before you start | Checklist: sign in to the AWS account that receives the bill (the Organization's management account if you have one); permission to create CloudFormation stacks and IAM roles; the stack runs in **US East (N. Virginia), us-east-1**, because AWS delivers bill exports there; for member-account coverage, AWS Organizations with "all features" turned on. If you are not that person: "Copy the link for a colleague" (§6.2) |
| 3 | Step 1: Create your workspace | Company name and country; the country sets the local currency and time zone (wording matches `Onboarding.tsx`). Time: under a minute |
| 4 | Step 2: Open AWS and create the stack | Press "Open AWS and connect". AWS opens the `cloudoptics-access` stack with everything filled in. Tick the "I acknowledge" box and choose **Create stack**. Time: about 3 minutes. Annotated screenshot of the AWS review page with the acknowledgement box and the pre-filled parameters (labelled "pre-filled by Cloudoptics") |
| 5 | What the stack creates | Table from the template (§5.4) |
| 6 | Step 3: Cloudoptics checks access | The wizard moves on when the stack reports in. It then tests each permission and shows a tick or a fix per line ("Assume the Cloudoptics role", "Read Cost Explorer", "Read the export bucket", "Check the account type", "Read Savings Plans", …, the labels from `connectors/validation.py`). Failure messages are specific (FR-CON-14) |
| 7 | What you see afterwards | Minutes: account list and the first recommendations after the first resource scan. Within a day: costs, once AWS delivers the first bill export. Following weeks: verified savings as changes show up in the bills. For an Organization: member accounts appear as the rollout reaches them |
| 8 | Permissions, in plain language | The billing role and the resource role, grouped (§5.5), with "what we cannot do". Link to `/security#permissions` for the action-by-action list and to the raw template |
| 9 | What it costs you in AWS | Bill-export storage in your S3 bucket and a small number of paid AWS API calls (for example Cost Explorer at USD 0.01 per request). Our design target is under USD 5 a month for a typical account (PRD FR-CON-09, AS5; to be confirmed with beta data before we publish a number) |
| 10 | Removing access | Delete the `cloudoptics-access` stack in CloudFormation (us-east-1). This removes both roles, the export and, for an Organization, the member-account roles. The bill-export bucket and its files are **kept on purpose**, so you keep your bills; delete the bucket yourself if you want them gone. What Cloudoptics deletes on its side and when (from the privacy notice) |
| 11 | Updating access | When we publish a new template version, the Connections page shows "Update CloudOptics access", a one-click in-place stack update; existing data is kept (README) |
| 12 | Security FAQ | §5.3 subset |
| 13 | CTA | "Start free" and "Read the security page" |

### 5.3 FAQ (shared by landing, how-it-works and security)

| Question | Answer direction |
|---|---|
| Can Cloudoptics change anything in my account? | No. Both roles have only read actions; the setup step that creates the bucket and export runs in your account during stack creation, and Cloudoptics never uses its role |
| Can it read my files or databases? | No. It reads bills from the one bucket the stack creates, plus resource settings and usage metrics. No object contents elsewhere, no database contents, secrets, parameters, log contents, Lambda environment variables or instance user data |
| Do you store AWS keys? | No. Cloudoptics assumes the role for up to an hour at a time with an ID unique to your workspace (ExternalId) |
| Where is my data stored? | London (DigitalOcean) for staging today; the production statement waits for §11 Q6. Encrypted in transit and at rest |
| How long does setup take? | About 15 minutes; about 3 of them in AWS |
| When do I see results? | First recommendations in minutes; costs within a day |
| Does it work with AWS Organizations? | Yes; one stack in the management account covers member accounts, including ones added later |
| What does it cost? | Per §11 Q3 |
| How do I remove it? | Delete one stack (§5.2 row 10) |
| Is Cloudoptics an AWS product? | No; it is built by Cloudboosta |

### 5.4 "What the stack creates" (from `cloudoptics-access.yaml` v2.0.3)

| Resource | Name | Purpose | Who uses it |
|---|---|---|---|
| IAM role | `CloudopticsBillingReader` | Read costs, the bill export, Savings Plans, budgets, prices, account list | Cloudoptics, with your ExternalId, sessions ≤ 1 hour |
| IAM role | `CloudopticsResourceReader` (this account; and every member account through a StackSet) | Read resource settings and usage metrics | Cloudoptics, same conditions |
| S3 bucket | `cloudoptics-cur-<account id>` | Holds the bill export. Public access blocked, encrypted, files expire after 400 days; only AWS's billing services may write; HTTPS only | AWS writes; Cloudoptics reads |
| Bill export | `cloudoptics-cur2` (CUR 2.0, hourly, Parquet) | The detailed bill | AWS |
| Setup function and its role | Lambda (Python 3.12) | Runs only while the stack is created, updated or deleted: makes the bucket, rolls out member roles, removes older Cloudoptics stacks; its write actions are limited to Cloudoptics-named resources | CloudFormation; never Cloudoptics |
| StackSet (management accounts only) | `cloudoptics-resource-access` | Gives each member account the resource role, including accounts added later | AWS |

### 5.5 Permissions in plain language

Generated at build time from the template (SITE-08); the wording below is the starting point for the data file.

**Billing role (`CloudopticsBillingReader`)**

| Actions | Plain language | Used for |
|---|---|---|
| `ce:Get*`, `ce:Describe*`, `ce:List*` | Read Cost Explorer: costs, forecasts and AWS's own savings figures | Month-to-date costs before the first export; forecasts |
| `savingsplans:Describe*` | List your Savings Plans | Commitments page, Savings Plan advice |
| `budgets:ViewBudget` | Read your AWS Budgets | Budgets |
| `pricing:GetProducts`, `pricing:DescribeServices` | Read AWS's public price list | Savings estimates by region, incl. af-south-1 |
| `bcm-data-exports:GetExport`, `ListExports`, `GetExecution`, `ListExecutions`; `cur:DescribeReportDefinitions` | Check that the bill export exists and when AWS delivered it | Connection health |
| `organizations:DescribeOrganization`, `organizations:ListAccounts` | List the accounts in your Organization, with names | Account filter, coverage |
| `cloudformation:DescribeStackSet`, `ListStackInstances`, `DescribeStackInstance` (only the `cloudoptics-resource-access` stack set) | See which member accounts have the resource role yet | "8 of 12 accounts covered" |
| `s3:GetObject`, `s3:ListBucket`, `s3:GetBucketLocation` (only `cloudoptics-cur-<account id>`) | Read the bill files in the bucket this stack creates; no other bucket | All cost views |

**Resource role (`CloudopticsResourceReader`)**

| Actions | Plain language | Used for |
|---|---|---|
| A named list of 14 `ec2:Describe…` actions (regions, zones, instances and their status and types, volumes, snapshots, images, addresses, NAT gateways, reserved instances, tags) | List servers, disks, snapshots, IP addresses, NAT gateways and their tags. Not instance user data or launch templates, which can hold secrets | Unattached volumes, old snapshots, idle IPs, rightsizing, NAT gateways |
| `rds:Describe*`, `rds:ListTagsForResource` | List databases and their settings, not their data | Idle databases, gp2 storage, Extended Support |
| `elasticloadbalancing:Describe*` | List load balancers | Idle load balancers |
| `eks:ListClusters`, `eks:DescribeCluster` | List Kubernetes clusters and versions | Extended Support charges |
| `logs:DescribeLogGroups` | List log groups and their sizes, not log contents | Storage costs |
| `s3:ListAllMyBuckets`, `GetBucketLocation`, `GetBucketTagging`, `GetLifecycleConfiguration`, `GetIntelligentTieringConfiguration` | List bucket names, regions, tags and storage rules, never the files | Storage recommendations |
| `tag:GetResources` | Read tags | Owners and environments |
| `cloudwatch:GetMetricData`, `cloudwatch:ListMetrics` | Read usage metrics such as CPU and network | Idle and rightsizing evidence |
| `compute-optimizer:Get*`, `Describe*`; `cost-optimization-hub:Get*`, `List*`; `ecs:ListClusters`, `ecs:ListServices` | Read AWS's own recommendations | Cross-checking our recommendations |

**What Cloudoptics cannot do** (true of both roles): create, change, stop or delete anything; buy Savings Plans or Reserved Instances; read files outside the bill-export bucket; read database contents, secrets, Systems Manager parameters, log contents, Lambda configuration, instance user data or DNS records; open support cases.

The build fails if the template gains an action that has no plain-language entry, or if the data file lists an action the template no longer has (SITE-08). The same data feeds the in-app "What Cloudoptics can read" panel (§6.1).

### 5.6 Security page (`/security`)

Sections: how access works (assume-role, ExternalId, ≤ 1 h sessions, no stored keys); the permission reference (§5.5 in full, with the "used for" column); data location and retention; encryption; sign-in and MFA (WorkOS); sub-processors (link); our own security practice (CI scanners, pinned dependencies, audit log; from README and doc 10); how to report a vulnerability (`security.txt`); "Download the template" (the exact file in `/cfn/`); and a contact for the security pack (doc 07 J1).

---

## 6. How the pages tie into the in-app wizard

| # | Change | Where | Why |
|---|---|---|---|
| 6.1 | A collapsible "What Cloudoptics can read" panel on the Connect AWS step, rendered from the same permission data as `/security`, grouped by role, with "used for" | `Onboarding.tsx` (`ConnectNew`, `ConnectExisting`) | FR-CON-13 and J1 step 3; today the step shows only the four AWS instructions |
| 6.2 | "Copy link for a colleague": copies the quick-create URL plus a short message and a link to `/how-it-works`; tracked as `connect_link_copied` | `ConnectNew` | J1 pain point "user lacks rights in the management account". Tech Lead to confirm the per-intent link is safe to share (it carries the ExternalId, which is not a secret in AWS's model, but our template marks it `NoEcho`) |
| 6.3 | Links "What will this create?" and "Read every permission" open the public pages in a new tab | `ConnectNew`, `AwsSteps` | Same text everywhere |
| 6.4 | Same step names and times as the site, from the shared claims file: "Workspace · Connect AWS · Check access", "about 3 minutes" in AWS, "within a day" for costs | `STEPS`, `AwsSteps`, `CheckAccess` | Avoid the contradiction seen in doc 13 §3.3 |
| 6.5 | `SignIn.tsx` reads `intent=signup` and shows "Create your account" wording; keeps attribution parameters through the WorkOS round trip (AuthKit `state`, to verify) | `SignIn.tsx`, `AuthGate.tsx` | Conversion attribution without cookies (§7.5) |
| 6.6 | Demo: an "Explore the sample workspace" button signs into the demo with a fixed sample user and opens Overview; a persistent "Sample data" banner with "Start free" | demo build, `SignIn.tsx` demo branch | One click from the site to the product, as Refraxion does well (doc 13 §6) |
| 6.7 | Later (not in this plan): the optional "Invite your team" step from FR-UI-03 | wizard | Alignment item |

---

## 7. Architecture decision

### 7.1 Options for where the public pages live

| Option | How | SEO and sharing | First load | Isolation from the app | Effort | Verdict |
|---|---|---|---|---|---|---|
| A. Inside the SPA | New TanStack routes `/`, `/how-it-works`, … in `apps/web` | Poor: one `index.html`, content only after JS, one set of meta tags, soft 404s (exactly Refraxion's problem, doc 13 §5.2) | Whole app shell (React, Router, Query, AuthKit) before any text | None: marketing and app share a bundle and a document | Low | No |
| B. Prerender SPA routes | A Vite prerender step or TanStack Start static prerendering for the public routes | Good HTML per route | Still ships React and the router to hydrate | Partial; same build and release cadence as the app | Medium; couples marketing changes to app releases | No |
| C. Separate static site in the monorepo | `apps/site`, built with **Astro** (static output, no JS by default, Tailwind v4, optional React islands) | Good: real HTML, per-page metadata, real 404s, sitemap | Near zero JS; CSS shared with the app's tokens | Full: different build, no app code or auth on public pages | Medium | **Recommended** |
| D. Hosted website builder | Webflow, Framer, or a page on cloudboosta.com | Good | Varies, usually heavy | Full | Low to build, ongoing licence; tokens and permission data cannot be generated from our repo | No, unless the founder wants marketing to edit without engineers (§11 Q2) |

**Recommendation: C.** Doc 11a already says "marketing site is separate". Astro is chosen over a hand-written prerender script because it gives routing, layouts, Markdown content collections (for `/legal` and later `/guides`), image optimisation and a sitemap integration without a server, and it can import our Tailwind tokens and, if ever needed, a React component from the app. The cost is one more framework in the repo; the alternative (a 200-line custom React-to-HTML script) is cheaper to start and costlier to maintain. The Tech Lead records this as **ADR-016** (task SITE-01).

### 7.2 Hosting and URLs

| Option | Layout | For | Against |
|---|---|---|---|
| H1. Path split on one origin | `cloudoptics.cloudboosta.com/` site · `/app/` web app · `/api` API (unchanged) · `/demo/` demo · `/cfn/` templates | One domain builds search authority; no change to the API or the stack callback host; one App Platform app; same-origin sign-in keeps working | The site shares an origin with the app (the AuthKit refresh token is in local storage, `AuthGate.tsx`), so no third-party scripts may run on the site |
| H2. Subdomain split | `cloudoptics.cloudboosta.com` site · `app.cloudoptics.cloudboosta.com` app and API | Clean isolation for future ad tags | Moves the API host: stack callbacks of existing connections, WorkOS redirect URIs, CORS and email links all change; two App Platform apps |
| H3. On the parent site | `cloudboosta.com/cloudoptics` | Parent brand | Outside this repo's control; templates and permission data cannot be generated; splits authority |

**Recommendation: H1 now**, with one rule: no third-party script tags on any page served from this origin (the site's analytics is a first-party beacon, §7.5). If paid-ads pixels or a consent manager become necessary, first move to H2 or keep those tags on cloudboosta.com. `cloudboosta.com` should link to the Cloudoptics home page rather than host a second copy (§11 Q2). The production domain is a founder decision (§11 Q1); the site takes its absolute URL from a build variable, so a domain change is configuration.

**Changes H1 needs (task SITE-13):**

1. `infra/digitalocean/app.yaml`: add a `site` static component at `/` with `error_document: 404.html` (real 404s); move `web` to the `/app` prefix (keep its `index.html` catch-all) and build it with `VITE_BASE=/app/`; add a `demo` static component at `/demo` built with `VITE_API_MODE=demo VITE_BASE=/demo/`. Verify App Platform's prefix stripping for static components.
2. `apps/web`: TanStack Router `basepath: '/app'`; AuthKit `redirectUri` and `onRedirectCallback` move from `/` to `/app/`; WorkOS dashboard redirect URIs updated (docs/handoff/staging-next-steps.md).
3. API settings: set `CLOUDOPTICS_API_PUBLIC_URL=https://cloudoptics.cloudboosta.com/api` **before** changing `CLOUDOPTICS_APP_URL` to `…/app`; otherwise the derived callback URL becomes `…/app/api` and new stacks report to the wrong place. Existing stacks keep calling `/api/v1/cfn/callback`, which does not move.
4. `/cfn/*`: today `copy-templates.mjs` copies templates into the web build; with the app under `/app` they would move to `/app/cfn`. Copy them into the site build instead so `CLOUDOPTICS_CFN_TEMPLATE_DOWNLOAD_BASE` (`…/cfn`) keeps working, and add `cloudoptics-access.yaml` to the copied set (the security page links to it).
5. Old app paths (`/sign-in`, `/onboarding`, `/overview`, `/costs`, `/recommendations/…`, `/settings/…`, …) appear in sent emails and bookmarks. The site's 404 page carries a small inline redirect map for those prefixes to `/app/…`, so they keep working.
6. Demo: keep the GitHub Pages build as a mirror until `/demo/` is live, then point the README and `SignIn.tsx` "public demo" link at `/demo/`.

### 7.3 Security headers

App Platform static sites have limited control over response headers (to verify in SITE-13). Minimum: a `<meta http-equiv="Content-Security-Policy">` on every site page allowing scripts and connections only to `'self'` and the PostHog EU ingestion host; `<meta name="referrer" content="strict-origin-when-cross-origin">`. When Cloudflare fronts the site (ADR-012 "later" column), set HSTS with a long max-age, `frame-ancestors 'none'` and `X-Content-Type-Options` there. Do not ship developer comments in HTML: the build minifies HTML and strips comments (doc 13 §5.1).

---

## 8. Supporting pieces

### 8.1 Design system and brand

- Extract the CSS custom properties in `apps/web/src/styles.css` into `packages/design-tokens/tokens.css`; both `apps/web` and `apps/site` import it, so the site cannot drift from the app (SITE-02).
- Palette per doc 08 §2.2.1: Boosta Blue `#2C68FC` for actions, navy `#0B1F4B` for text and dark sections, yellow `#FAC812` only as a fill behind navy text ("savings found" moments), never as text on white.
- Light mode by default with the existing `prefers-color-scheme` dark tokens.
- Typography: the system stack with Inter if the brand confirms it (§11 Q11), self-hosted WOFF2, two weights, subset, `font-display: swap`. No base64-inlined fonts (doc 13 §5.2.7).
- Lucide icons (doc 08 §2.8). Imagery: product screenshots from the demo's "Kola Pay" sample tenant, labelled "Sample data"; any people shown are African and real (founder-approved) or none.

### 8.2 Accessibility (WCAG 2.2 AA)

| Requirement | How |
|---|---|
| Structure | Landmarks (`header`, `nav`, `main`, `footer`), one `h1`, ordered headings, skip link, `lang="en"` |
| Keyboard and focus | All controls reachable; visible focus ring from the tokens (`:focus-visible`); focus not hidden by the sticky header (2.4.11) |
| Targets | ≥ 24×24 px (2.5.8); ≥ 44×44 px on mobile nav |
| Contrast | Token pairs from doc 08 §2.2.1; checked in CI |
| Motion | No autoplay; respect `prefers-reduced-motion` |
| Images | Screenshots have alt text that states what the screen shows; key numbers are also in text, not only inside images |
| FAQ | Native `<details>`/`<summary>` |
| Consistent help | Contact and support link in the same place on every page (3.2.6) |
| Forms | None on the site at launch; the app's sign-in is WorkOS (check 3.3.8 accessible authentication with WorkOS) |
| Testing | axe (via Playwright) on every page in CI; a manual keyboard and screen-reader pass (NVDA and VoiceOver) before launch; 200% zoom |

### 8.3 Performance budgets

Our users are often on mobile data (FR-UI-10, NFR-PERF-03).

| Budget (per page, gzipped) | Limit |
|---|---|
| HTML + CSS on the critical path | ≤ 60 KB |
| JavaScript | ≤ 10 KB (the currency toggle); 0 KB on legal pages |
| Hero image | ≤ 100 KB (AVIF with WebP fallback, responsive `srcset`) |
| Total landing page weight | ≤ 500 KB |
| LCP / CLS / INP (Lighthouse mobile, slow 4G) | ≤ 2.5 s / ≤ 0.1 / ≤ 200 ms |

A `size` check for `apps/site` like `apps/web/scripts/check-bundle-size.mjs`, plus Lighthouse CI with these budgets, runs on every pull request.

### 8.4 Analytics and consent (UK GDPR, EU GDPR, NDPA and others)

| Decision | Detail |
|---|---|
| No cookies or device storage on the public site | So no consent banner is needed for analytics under PECR-style rules; counsel to confirm for each market in doc 10 A7 |
| First-party beacon, not a third-party SDK | A small inline script (< 1 KB) posts anonymous events to PostHog EU's capture endpoint: `site_page_viewed`, `site_cta_clicked` {cta, page}, `site_demo_opened`. No distinct ID kept between pages; PostHog project set to discard IP addresses |
| Funnel events from the server | The API emits `signup_completed`, `workspace_created`, `connect_link_opened`, `connect_link_copied`, `stack_ready` (callback received), `connection_healthy`, `first_recommendation_viewed`, `first_costs_shown`, with internal IDs only (NFR-OBS-05) |
| Attribution | `utm_*`, `ref` and the landing path travel in the URL to `/app/sign-in`, survive the WorkOS round trip (to verify), and are sent once with workspace creation, then stored on the workspace |
| No analytics in `/demo/` | The demo build has no beacon; the same mistake cost Refraxion double pageviews (doc 13 §5.1) |
| If ads or a consent manager are ever needed | One consent manager, one loader, Consent Mode defaults set to denied, only on the marketing origin, never on `/app` or `/demo`, and only after moving to H2 (§7.2) |
| Disclosure | The privacy notice lists every processor, checked against the tag inventory at each release (SITE-19 checklist) |

### 8.5 SEO and social cards

- Per page: `<title>` (≤ 60 characters), meta description (≤ 155), absolute canonical, Open Graph and Twitter tags with a per-page 1200×630 image (designer exports four: landing, how it works, security, pricing).
- JSON-LD: `Organization` (Cloudboosta), `SoftwareApplication` (Cloudoptics, with an `Offer` for the free tier once decided), `FAQPage` on `/`, `/how-it-works` and `/security`.
- `sitemap.xml` with `lastmod` from git; `robots.txt` disallowing `/app/`, `/demo/` and `/api/`; `noindex` meta in the app and demo `index.html`.
- Real 404s. Google Search Console and Bing Webmaster verification. Check previews in LinkedIn, Slack and WhatsApp, which do not run JavaScript.

### 8.6 Legal pages needed

| Page | Contents | Owner |
|---|---|---|
| Privacy notice | Controller vs processor roles (doc 10 A7.1), what we collect on the site (anonymous events, no cookies) and in the product, lawful bases, retention, transfers, rights, NDPA/Kenya DPA/POPIA/UK GDPR sections | Counsel + founder |
| Terms of service (beta) | Beta terms, read-only access, customer's revocation right, liability for guided fixes (doc 10 flags this for counsel) | Counsel |
| Cookie statement | "The public site sets no cookies"; the product uses only what sign-in needs | PM, counsel review |
| Sub-processors | DigitalOcean, WorkOS, Postmark, PostHog, Sentry, Grafana Cloud (doc 11a), GitHub for code hosting; purpose and location for each | Tech Lead |
| DPA | Summary page and downloadable DPA with local transfer clauses (11d Sprint 6 dependency) | Counsel |
| AWS trademark notice | Footer line | PM |

Sprint 6 already depends on ToS, privacy policy and DPA drafts (11d); this plan reuses them rather than adding new legal work.

### 8.7 Content ownership

| Content | Owner | Reviewer | Source of truth |
|---|---|---|---|
| Landing and about copy | PM | Founder | `apps/site/src/content/*.md` |
| How it works, security | PM | Tech Lead (accuracy) | Same, plus the generated permission data |
| Claims (setup time, data location, retention, supported currencies, sessions ≤ 1 h) | Tech Lead | PM | `packages/site-claims/claims.json`, imported by the site and the app |
| Permissions | Tech Lead | Security owner | `cloudoptics-access.yaml` + `permissions.yaml` (CI-checked) |
| Pricing | Founder | PM | `apps/site/src/content/pricing.md` |
| Legal | Founder | Counsel | `apps/site/src/content/legal/*.md` with a "last updated" date |
| Screenshots | Designer | PM | Exported from the demo build; regenerated when the UI changes |

Any change to a claim, permission or price is a pull request; the reviewer column is a required review (CODEOWNERS).

---

## 9. Work breakdown

Estimates are person-days (pd). Roles as in doc 11d §4.1: TL Tech Lead, FE senior frontend, FS full-stack, BE backend, DevOps, Des designer, PM, QA, Founder.

| ID | Task | Role (pd) | Sprint | Depends on | Acceptance criteria |
|---|---|---|---|---|---|
| SITE-01 | ADR-016: public site architecture and URL plan (§7) | TL 1 | S6 | — | ADR merged with the decision, options and the H1/H2 rule |
| SITE-02 | Shared design tokens package; `apps/web` imports it with no visual change | FE 1.5 | S6 | 01 | App screenshots unchanged in tests; site and app read one file |
| SITE-03 | Scaffold `apps/site` (Astro static, Tailwind v4, base layout with SEO head, header, footer, 404, robots, sitemap); workspace and CI job (build, typecheck, lint, link check, size, HTML comment stripping) | FS 3 | S6 | 01, 02 | `pnpm --filter @cloudoptics/site build` produces static HTML per route; CI green; 404 page served with status 404 locally |
| SITE-04 | Message house and copy deck v1 for every page, FAQ, claims file | PM 3, Founder 1 | S6 | — | Founder sign-off; every claim in the claims file; no unverified numbers |
| SITE-05 | Wireframes and visual design: landing, how it works, security; mobile and desktop | Des 5 | S6 | 04 (draft) | Reviewed in the design system; contrast pairs from doc 08 only |
| SITE-06 | Product imagery and social cards from the demo (sample data labels, AVIF/WebP, alt text, four OG images) | Des 1.5, FE 0.5 | S7 | 05 | Hero image ≤ 100 KB; every image has alt text |
| SITE-07 | Build the landing page (§5.1), including the currency-toggle island | FS 4 | S6–S7 | 03, 05 | Matches design; works with JS off; budgets in §8.3 met |
| SITE-08 | Permission data: `permissions.yaml` plain-language entries, build script that reads `cloudoptics-access.yaml`, JSON export for the app, CI test both ways | TL 1, FE 1 | S6 | 01 | Adding an action to the template without an entry fails CI; removing one fails CI |
| SITE-09 | Build `/how-it-works` (§5.2) | FS 3 | S7 | 03, 05, 08 | All 13 sections; step names and times from the claims file |
| SITE-10 | Build `/security` (§5.6) with the full permission reference | FE 1.5, TL 0.5 | S7 | 08 | TL confirms every statement against the template and code |
| SITE-11 | Pricing section or page per the founder's decision | FS 1 | S7 | Q3 | Free tier contents stated in numbers, or "free during the beta" with what happens after |
| SITE-12 | Legal pages from counsel's drafts (§8.6) and `security.txt` | PM 1, FS 1 | S7 | Counsel drafts (11d S6 dependency) | Each page has an owner and a "last updated" date; sub-processor list matches doc 11a and the tag inventory |
| SITE-13 | Hosting change (§7.2 items 1–6): site at `/`, app at `/app`, demo at `/demo`, `/cfn` from the site, explicit `API_PUBLIC_URL`, WorkOS redirect URIs, old-path redirects | DevOps 2, FE 1.5 | S7 | 03 | On staging: sign-in, onboarding and a new stack callback work end to end; an old `/overview` link lands on `/app/overview`; `/cfn/cloudoptics-access.yaml` downloads |
| SITE-14 | Demo at `/demo/`: `noindex`, no analytics, "Explore the sample workspace", sample-data banner with "Start free" | FE 1.5 | S7 | 13 | One click from the site to the demo Overview; no beacon requests from `/demo/` |
| SITE-15 | Analytics: site beacon, attribution through sign-up, server funnel events (§8.4), PostHog funnel dashboard | FE 1.5, BE 1.5 | S7 | 13 | A test sign-up from a UTM-tagged link shows up in the funnel with its source; no cookies set by the site (checked in the browser) |
| SITE-16 | Wizard tie-ins (§6.1–6.5) in `Onboarding.tsx` and `SignIn.tsx` | FE 3 | S7 | 04, 08 | Permission panel lists every action from the template; "Copy link for a colleague" works; App tests updated |
| SITE-17 | SEO: metadata, JSON-LD, sitemap `lastmod`, Search Console and Bing, social preview checks | FS 1, PM 0.5 | S7 | 07, 09 | Rich-results test passes for `FAQPage`; previews correct in LinkedIn, Slack, WhatsApp |
| SITE-18 | QA: WCAG 2.2 AA (axe in CI plus manual), Lighthouse CI budgets, low-end Android on throttled network, copy proofread | QA 3 | S7 | 07, 09, 10 | 0 serious axe issues; Lighthouse thresholds in §2.2 met |
| SITE-19 | Launch: checklist (processor list vs tags, claims vs app, redirects), uptime checks on `/` and `/how-it-works`, CODEOWNERS for content | PM 1, DevOps 0.5 | End of S7 | all | Checklist signed; site live with the Increment 2 beta |

### 9.1 Totals

| Role | S6 | S7 | Total |
|---|---|---|---|
| Tech Lead | 2 | 0.5 | 2.5 |
| Frontend (senior) | 2.5 | 9.5 | 12 |
| Full-stack | 5 | 8 | 13 |
| Backend | 0 | 1.5 | 1.5 |
| DevOps | 0 | 2.5 | 2.5 |
| Designer | 5 | 1.5 | 6.5 |
| PM | 3 | 2.5 | 5.5 |
| Founder | 1 | 0 | 1 (plus reviews) |
| QA | 0 | 3 | 3 |
| **Total** | **18.5** | **29** | **47.5 pd** |

With 15% contingency: **about 55 person-days**, excluding counsel's time. Elapsed: two sprints (Weeks 12–15), launching with the Increment 2 beta at the end of Sprint 7.

### 9.2 Placement against doc 11d

- **Sprint 6 (next):** SITE-01 to SITE-05, SITE-08 and the first half of SITE-07. This fits beside Sprint 6's own work only if the full-stack engineer gives about half the sprint to the site and the designer's 0.6 FTE (6 pd) goes almost entirely to it; the PM confirms this at sprint planning.
- **Sprint 7:** the rest. Sprint 7 is the buffer sprint, so the site competes with carry-over. If capacity is short, ship the **minimum launch set** at the beta date: landing, `/how-it-works`, `/security`, privacy and terms, 404, sitemap and robots, the hosting change, and the beacon. Move SITE-11, SITE-14, SITE-16's colleague link and SITE-17's verification work to Sprint 8.
- Update doc 11d: replace "marketing site" in Sprint 13 with "content and guides (`/guides`)", since the site itself ships in Sprints 6–7.

### 9.3 Risks

| Risk | Likelihood / impact | Mitigation |
|---|---|---|
| Moving the app to `/app` breaks sign-in, email links or stack callbacks | Med / High | Order in §7.2 item 3; staging end-to-end test in SITE-13; old-path redirects; do it before any design partner connects |
| Sprint 7 carry-over squeezes the site | Med / Med | Minimum launch set (§9.2) |
| Claims on the site outrun the product (savings numbers, data location, setup time) | Med / High | Claims file with TL review; no customer numbers before written consent |
| Legal drafts late | Med / High | Already a Sprint 6 dependency; the site cannot launch without privacy and terms |
| Brand assets not final (logo, typography, doc 08 §2.7 Q4) | Med / Low | Build with tokens; swap assets later |
| Staging domain used publicly while it holds test data | Med / Med | Founder decides the production domain (Q1); the site is domain-agnostic |
| App Platform static sites cannot set security headers | Med / Low | Meta CSP now; Cloudflare in front later (§7.3) |
| Permission page drifts from the template | Low / High | Generated data with a two-way CI check (SITE-08) |
| A competitor ships a free bill-savings tool first (Refraxion's flagged "Cost check", doc 13 §1.8) | Med / Med | Lead with what a PDF estimate cannot give: connected, local-currency, verified savings |

---

## 10. Out of scope

Blog or `/guides` content, localisation (French, Arabic, Portuguese), paid-ads landing pages, A/B testing tooling, a waitlist form (the CTA goes straight to sign-up unless the founder chooses applications, Q4), an MCP server or agent setup docs (doc 12 defers these), and the "Invite your team" wizard step.

---

## 11. Open questions for the founder

| # | Question | Needed by | Blocks |
|---|---|---|---|
| Q1 | What is the production domain? Keep `cloudoptics.cloudboosta.com`, or a dedicated domain? Does staging stay on that host or move? | Start of S7 | SITE-13, SEO |
| Q2 | Is cloudboosta.com in our control, and should it link to (not duplicate) the Cloudoptics site? Should marketing be able to edit pages without engineers (which would favour option D)? | S6 planning | SITE-01 |
| Q3 | What do we show for pricing at the beta: the doc 10 C5 tiers in local currency, "free during the beta", or the free tier only? Which local prices? | Mid S6 | SITE-11, landing §10 |
| Q4 | Self-serve sign-up for everyone, or a design-partner application (doc 10 C7 criteria) first? | S6 planning | CTAs, SITE-04 |
| Q5 | What proof can we publish: Cloudboosta community and training numbers, team names and photos, any AWS partner status? | Mid S6 | Landing §11, About |
| Q6 | Where will production data live (London today; doc 10 mentions backups in AMS3/FRA1)? What retention do we promise after deletion? | Mid S6 | FAQ, security page, privacy notice |
| Q7 | Will we run paid ads or tracking pixels in the next six months? (If yes, plan H2 and one consent manager now.) | S6 planning | §7.2, §8.4 |
| Q8 | Who signs off security claims and legal pages, and is counsel engaged for NDPA, Kenya DPA and POPIA wording? | S6 | SITE-12 |
| Q9 | Do we offer a human "cost review" from Cloudboosta engineers on the site (doc 10 C5 expert review add-on)? | S7 | Landing, pricing |
| Q10 | Which support channel and hours do we publish (WAT/EAT/SAST)? | S7 | Footer, FAQ |
| Q11 | Is the brand final: logo lock-up, Inter or the system font, how strongly Cloudoptics shows the Cloudboosta parent? | Mid S6 | SITE-05 |
