# 13 — Refraxion Teardown: Public Site, Onboarding and Stack

**Product:** Cloudoptics by Cloudboosta
**Subject:** https://refraxion.io/ ("Refraxion – AWS posture review"), by Cloudvisor
**Date of capture:** 18 November 2026 (release `v0.3.0+2570343`, footer string in the main bundle)
**Inputs:** the public HTML, JS/CSS bundles and lazy chunks, robots.txt, sitemap.xml and the published CloudFormation template; our README, docs 04, 06, 10 and 12
**Status:** Research note. Facts are separated from recommendations; recommendations are in §9 and in doc 14.

---

## 0. How this teardown was done

| Step | What we did |
|---|---|
| Capture | Downloaded `index.html`, the main bundle `assets/index-DMvJBcRz.js` (938 KB), the main CSS `assets/index-Bw3BK6Cl.css` (536 KB), and every lazy chunk named in the bundle's `__vite__mapDeps` list (21 more JS/CSS files, 4.5 MB in total). Also `robots.txt`, `sitemap.xml`, `favicon.svg`, `og-image.png`, and the CloudFormation template at the public S3 URL the bundle uses for onboarding (the Terms say the template "is published at a public URL" so customers can review it). One request per file, a generic user agent, a pause between requests. |
| Not done | No sign-up, sign-in, form submission, API call (`api.refraxion.io`), authenticated route or runtime capture. Nothing downloaded was executed. |
| Static analysis | REA (github.com/morluto/rea), CLI fallback `npx -y rea-agents@latest analyze-javascript-application <capture dir> --json`, run from the scratchpad root with absolute paths. Result: see §0.1. Then manual reading of the minified bundles: route table, JSX text, string tables, analytics event names, endpoint paths and CSS custom properties. |
| Identity | `index.html` SHA-256 `29abc395edc1d101…`; main bundle `f9d9058e102fe4b4…`; template `77204f221fd0e5ff…`. No source maps are published (`index-DMvJBcRz.js.map` returns 404 and no file carries `sourceMappingURL`). |

**Evidence labels.** Every finding is marked:

- **[O] Observation**: seen in the HTML, JS, CSS, template or HTTP headers. Evidence gives the file and a short string, or a byte offset in the main bundle.
- **[I] Inference**: a conclusion drawn from observations, with the reasoning.
- **[U] Unknown**: what static files cannot show (runtime behaviour, backend, what actually loads after consent).

Files are named relative to the capture folder (`refraxion-site/`); captures are working files and are not committed. Quotes are kept to short phrases.

### 0.1 REA result

REA ran (`rea-agents` 6.9.0 on Node 22.23.5; the first attempt failed because the system Node 22.14.0 is below REA's minimum). Evidence `ev_ebf268f5…338f9df`, 218 MB of JSON, covering the capture folder (SHA-256 `795b8258…`).

| REA output | Value |
|---|---|
| Files read / JavaScript parsed / skipped | 21 / 19 / 1; 3.2 MB of text; 613,717 AST nodes; no parse failures |
| Application graph | 1,432 nodes, 2,553 edges, coverage **partial** |
| Semantic graph | Truncated at its 100,000-node cap; about 48,600 unknown call and object flows omitted (dynamic calls, ambiguous targets) |
| Route inventory | 6 candidate routes from 20 sources, all **low** confidence: `/auth/sign-in`, `/auth/sign-up`, `/auth/reset`, and three agent documents `/agents/setup.md`, `/agents/rules.md`, `/agents/setup-troubleshooting.md`. Application route coverage reported as "unknown" |
| Requests | 5 static request sites (4 in the WAF captcha chunk, 1 HubSpot form post in `PaidBeta`) |
| Electron/IPC surfaces | none, as expected for a website |

**Limitations REA reported, which bound what it proves:**

- The main bundle `assets/index-DMvJBcRz.js` exhausted the analyser's 1 GiB heap, so its semantic analysis is partial. That file holds the landing page, the router and the legal pages; REA's route inventory therefore misses `/`, `/about`, `/privacy`, `/terms`, `/demo`, `/paid-beta` and `/cost-check`, because Refraxion routes with `pathname ===` comparisons rather than a route table.
- The two inline scripts in `index.html` (Consent Mode defaults and the GTM loader) were not analysed.
- Everything is static: imports, endpoints and routes "do not prove runtime execution".

**What we used it for.** REA gave the file inventory, the request sites and three routes we had not found by hand (the `/agents/*.md` documents, which we then fetched and read as data). Everything in §§1–5 about the landing page, router, copy, analytics and template comes from reading the bundles and files directly, and cites them. Where REA and manual reading overlap (auth routes, the HubSpot form post) they agree. A future pass could raise `--max-heap-mb` and select only the main bundle with `--source-path` to get its semantic graph.

---

## 1. Positioning, audience and promise

| # | Finding | Evidence |
|---|---|---|
| 1.1 | **[O]** Refraxion is Cloudvisor's self-service product. The brand object is `{name:"Refraxion",parentName:"Cloudvisor",accent:"#5b8cff"}` and the nav shows "by Cloudvisor". The legal entity is Cloudvisor, UAB (Lithuania). | main bundle @356069; privacy page subtitle "a product of Cloudvisor, UAB"; Lithuanian supervisory authority named in "Your rights" |
| 1.2 | **[O]** The promise is a free, read-only AWS posture review: findings ranked by severity across three pillars, Security, Reliability and Cost Optimization (the AWS Well-Architected names). | `<meta name="description">` in index.html; landing "Audit pillars" strip |
| 1.3 | **[O]** Headline pattern: a category claim ("The AWS service that should have existed") plus a one-line sub-claim ("One place to see your entire AWS posture"). | main bundle, landing component `Hb` @371542 |
| 1.4 | **[O]** Proof leans on the parent: "AWS Advanced Tier" partner badge, "2,000+" startups audited, a team page with four named people, and a Cloudvisor company photo. | landing "Why Refraxion" section; About page `Ab` @387616 |
| 1.5 | **[I]** Audience: startup founders and small engineering teams on AWS without a dedicated security or cloud architect. Copy repeatedly says you need not be an expert ("Do I need to be an AWS expert?" FAQ; "Plain language" rule). The sign-up form blocks personal email domains, so the target is companies, not hobbyists. | FAQ array `Eb` @368640; `freeEmailDomains-*.js` (50 domains incl. typo variants) |
| 1.6 | **[I]** Refraxion is also a lead source for Cloudvisor's services. Every results page offers "Get expert help" (a HubSpot meetings link for Cloudvisor); a sign-up that answers "No, …" to "Do you use AWS?" is sent to Cloudvisor's migration-assessment page with UTM tags; a `brand=cloudvisor` parameter switches the sign-up copy to "Cloudvisor's free AWS scan". | `ExpertHelpModal-*.js` (`meetings.hubspot.com/meet-with-cloudvisor/expert-help`); `formOptions-*.js`; `signupBrand-*.js` |
| 1.7 | **[O]** Cost is one of three pillars, not the focus. The sample findings on the landing page are mostly security (S3 public read, root without MFA, SSH open to the world). Cost appears as one `COST06` item. | landing "Built for action" list; `DemoArea-*.js` sample findings |
| 1.8 | **[O]** An unreleased "Cost check" moves towards our space: upload up to three AWS bill PDFs, see "What you could save", answer questions to "Sharpen the result", then "Talk to Cloudvisor". It parses PDFs in the browser with pdf.js. A `/cost-check/sales` variant asks for a "Sales password". It is behind a flag that returns false (`function _f(){return!1}`), and its lazy import is never used. | `CostCheck-*.js` (520 KB; `pdfjs-dist`, `GlobalWorkerOptions`); main bundle @367402 and router @431845 |

## 2. Pricing and free tier

| # | Finding | Evidence |
|---|---|---|
| 2.1 | **[O]** Two cards in a `#pricing` section of the landing page (there is no pricing page): "Free · forever" at $0, and "Premium · coming later" at "TBD". | landing component, `id:"pricing"` @382766 |
| 2.2 | **[O]** The free card lists a full three-pillar scan, ranked findings with fixes, a prioritised plan and "a generous monthly scan allowance". The allowance is not quantified anywhere in the public bundles. | same |
| 2.3 | **[O]** The FAQ commits to the free tier as "the plan, not a beta promotion", and promises notice and opt-in before any charge. | FAQ "What will it cost?" |
| 2.4 | **[O]** A separate `/paid-beta` campaign page (not linked from the nav or sitemap) offers a 30-minute call with an engineer to companies spending $1,000+/month on AWS, in return for "a Trustpilot review" and "a short LinkedIn post". It mentions a reward ("We send … either way") whose value is injected at runtime. | `PaidBeta-*.js` |
| 2.5 | **[I]** Asking for public reviews in exchange for a reward is a disclosure risk under consumer-protection rules on incentivised reviews, and Trustpilot's own guidelines restrict it. We should not copy this. | 2.4 |
| 2.6 | **[U]** What "Premium" will contain, and when. The only public hints are "Deeper coverage" and "Shaped by what beta users actually ask for". | pricing card |

## 3. Public routes, page by page

### 3.1 Route table

The SPA serves one `index.html` for every path. The router is a hand-written chain of `pathname ===` checks in the main bundle (`function Xb`, around @431845), not a router library's route table. Two hosts share one bundle: `refraxion.io` (marketing) and `app.refraxion.io` (product), chosen by `hostname.startsWith("app.")` (@359421).

| Path | Host | What it renders | In sitemap | Notes |
|---|---|---|---|---|
| `/` | marketing | Landing page (`Hb`) | yes | If framed, redirects to `/demo` |
| `/about` | marketing | About: origin story, team, "A few rules we don't break", CTA | yes | |
| `/privacy`, `/terms` | both | Legal pages rendered from JS arrays (headings + numbered clauses) | yes | Privacy last updated 28 July 2026 |
| `/demo`, `/demo/scans/01DEM0PR0DACME0000000000A3` | both | Interactive demo on sample data (`DemoArea`) | no | Header and footer "Demo" links go to the deep scan URL |
| `/paid-beta` | marketing | Paid feedback-call campaign with a HubSpot form | no | Unlisted |
| `/cost-check`, `/cost-check/sales` | marketing | Nothing yet (flag off) | no | See 1.8 |
| `/auth/sign-up`, `/auth/sign-in`, `/auth/reset`, `/auth/confirm`, `/auth/code`, `/invite` | app | Auth screens (`AuthArea`) | no | Marketing links are rewritten to `app.` with UTM/click IDs carried over |
| `/app/*` | marketing | Redirect to the same path on `app.` | no | |
| `/agents/setup.md`, `/agents/rules.md`, `/agents/setup-troubleshooting.md` | app | Plain markdown written for AI agents: how to add Refraxion's MCP server to Claude Code, Codex, Gemini CLI, Cursor, VS Code, Kiro, Claude/ChatGPT apps and others, sign in, and verify | no | Found by REA's route inventory; served as `text/markdown` |
| `/dashboard`, `/assessments`, `/findings`, `/settings` | marketing | Redirect to `/demo` | no | Old paths; disallowed in robots.txt |
| anything else | both | `NotFound` component, served with HTTP 200 | no | `curl https://refraxion.io/pricing` returns the same 9,220-byte `index.html` with status 200 |

**[O]** Apart from the agent documents, there is no "how it works", getting-started, docs, security or pricing page. "How it works" is a landing section; onboarding is explained inside the app (§4). **[I]** A prospect's security reviewer has nothing to link to except the landing FAQ, the Terms (clause 4) and the template itself.

### 3.2 Landing page, section by section

Order as rendered by `Hb` (main bundle @371542–385700):

| # | Section | Content pattern | CTA |
|---|---|---|---|
| 1 | Nav | Logo with "by Cloudvisor", About, Pricing (anchor), Demo, light/dark toggle | "Get started" |
| 2 | Hero | Category headline, one-line promise, a trust line under the buttons ("Read-only IAM role. We never store your AWS credentials."), then a product video in a fake browser frame labelled "product demo". The video autoplays muted and loops; with `prefers-reduced-motion` it shows controls instead | "Get started" (primary), "See sample report →" (secondary, opens the demo) |
| 3 | Pillars strip | Three words: Security, Reliability, Cost Optimization | — |
| 4 | How it works | "Three steps." Each step has a short title, one sentence and a monospace "terminal" detail: an example role ARN; `scan status: RUNNING · 38 services checked · eta ~1 min`; `12 findings · 1 critical` | — |
| 5 | Built for action | Three claims (exact resource ARNs, a ranked weekly plan, plain-language fixes) beside a mock list of four findings tagged with Well-Architected IDs (SEC07, SEC02, REL10, COST06) | "See a sample report →" |
| 6 | Fix it your way | Three routes to a fix: open the resource in the AWS console, hand it to an AI agent (read-only), or book Cloudvisor | — |
| 7 | MCP server | Connect Claude, Cursor or any MCP client to your findings; "Get the fix as code" (Terraform); a second video, loaded only when scrolled into view | — |
| 8 | Why Refraxion | Parent credibility: AWS Advanced Tier badge image, "2,000+" audits | "Meet Cloudvisor →" (external, short link) |
| 9 | Pricing | Free forever vs Premium TBD (§2) | "Get free beta access" |
| 10 | Beta CTA (`#waitlist`) | "Get beta access" (the anchor is still called waitlist, but the button goes to sign-up) | "Get started" |
| 11 | FAQ | Nine questions in native `<details>`/`<summary>` elements (§3.3) | — |
| 12 | Footer | About, Demo, Get started, Privacy, Terms, `mailto:` contact; a "Built with … by" line; a full "not affiliated with … Amazon" trademark disclaimer; version string | — |

**[O]** The primary CTA appears five times (nav, hero, pricing, beta block, footer). The demo link appears four times. **[O]** Theme: dark by default with a light toggle; the hero image swaps between `/hero-dark-mountains.webp` and `/hero-light.webp`.

### 3.3 Objection handling and trust messaging

The FAQ and the About page's six "rules" carry most of the trust story. Paraphrased:

| Objection | Their answer (paraphrase) | Where |
|---|---|---|
| Is this AWS? | No, independent, built by Cloudvisor | FAQ 1, footer |
| What does it cost? | Free tier stays free; notice and opt-in before any charge | FAQ 2, pricing |
| What access? | A read-only IAM role deployed with CloudFormation; no write access; reads configuration and metadata only; revocable | FAQ 3, How it works step 1, About rule 1 |
| Where is my data? | No stored AWS credentials, temporary sessions; findings stored in eu-central-1 (Frankfurt), encrypted; deletable | FAQ 4, privacy §§9–10 |
| Will you scan when I'm not looking? | Never; a scan only runs when you start one | FAQ 5, About rule 5 |
| How long? | "About a minute", larger accounts a few | FAQ 6 |
| Do I need expertise? | No; plain language, ranked, with exact changes; human help available | FAQ 7 |
| Can I leave? | Delete findings or the account any time; delete the role to cut access; data removed within 30 days | FAQ 8, About rule 6 |
| Is it ready? | Live now in beta; sign up with a work email and scan straight away | FAQ 9 |
| Will you scare me into buying? | "No fear-mongering"; nothing to upgrade to yet | About rule 2 |
| Is it a black box? | Every check maps to a public Well-Architected best practice | About rule 4 |

**[O]** The `/paid-beta` FAQ gives a different scan time, "Typically less than ten minutes", against "about a minute" on the landing page. **[I]** Two pages disagree on a number buyers remember; a single source of truth for public claims would prevent it.

### 3.4 Sign-up and first-run questions

| # | Finding | Evidence |
|---|---|---|
| 3.4.1 | **[O]** Sign-up fields: first name (required), last name, work email (required), password (required), "Do you use AWS at your company?" (four options), and acceptance of the Terms and Privacy Policy. Personal mailboxes are refused with a friendly message. A captcha step exists. | `AuthArea-*.js`; `WafCaptcha-*.js`; `freeEmailDomains-*.js` |
| 3.4.2 | **[I]** Auth is AWS Cognito through Amplify (the bundle carries Amplify's `CONFIRM_SIGN_IN_WITH_TOTP_CODE` challenge names), with TOTP MFA and an AWS WAF captcha. | `AuthArea-*.js` |
| 3.4.3 | **[O]** After sign-up, an "About you" step asks for AWS comfort level, AWS spend band (USD: $0 to >$10,000), country, workspace name and goals (Security worries, Cost surprises, Reliability incidents, Compliance / audit prep, General posture check). The dashboard opens only after the answers save. Events: `onboarding_started`, `onboarding_step_completed`, `onboarding_completed`, `onboarding_skipped`. | `AppArea-*.js` near `onboarding_started`; `formOptions-*.js` |
| 3.4.4 | **[I]** The survey is for qualification and CRM, not personalisation: answers post to `/v1/me/onboarding` and there is a `/v1/me/crm-capture` endpoint; the HubSpot visitor cookie (`hubspotutk`) is read in the browser. | `analytics-*.js` endpoint list; `hubspotCookie-*.js` |

## 4. The onboarding (account connection) flow

The public bundle contains the in-app "Account linking steps" wizard. We read its copy; we did not run it.

| Step | What the customer sees (paraphrase) | Mechanics seen in code |
|---|---|---|
| 1. Account details | Enter the 12-digit account ID and an optional nickname; help on finding the ID | Client-side validation of the 12 digits |
| 2. Deploy the read-only role | "Everything is filled in – you confirm it in AWS." A button opens CloudFormation; a note warns that AWS opens in whichever account the browser is signed in to; a "send them the link" option for a colleague with AWS access; a skip for people who already deployed | Quick-create URL `https://eu-central-1.console.aws.amazon.com/cloudformation/home?region=eu-central-1#/stacks/create/review?templateURL=…` with the workspace's ExternalId; the page polls "every 15 seconds" for the role; events `connect_template_launched` (with `source:"copy_link"` for the colleague path), `connect_autocheck_connected/failed/timed_out` |
| — | Inline explanations: no write permissions; only their scanner can use the role, because it trusts an ID unique to the workspace; what is read ("how your account is set up – buckets, instances, policies, network settings"); only while a scan you started is running; how to remove it (CloudFormation → delete the stack) | Plain-language blocks next to the button |
| 3. Verify the connection | "We check that the role answers before any scan runs." Specific outcomes: role not there yet (check the account ID, wait for the stack), a problem on their side ("That's on our side, not yours"), or a check that did not complete | `POST /v1/verify-connection`; events `connect_verify_attempted/failed`, `aws_account_connected` |

### 4.1 What the customer deploys

**[O]** The template (`refraxion-readonly-role-v2.yaml`, 3.2 KB) creates **one IAM role**, `Refraxion-ReadOnly-v2`, with:

- a trust policy for one Refraxion AWS account, conditioned on `sts:ExternalId` (a parameter the link pre-fills, labelled "do not change");
- `MaxSessionDuration: 3600`;
- **AWS's managed `ReadOnlyAccess` policy** as its only permission ("AWS maintains it, we do not define it");
- tags `refraxion:tenant` and `ManagedBy: Refraxion`, and outputs for the role ARN, ExternalId and account ID.

**[O]** The template's comments are written for the customer: a header says it is a "CUSTOMER-FACING FILE" and that internal history and ticket numbers are kept out of it. **[O]** Region is fixed to eu-central-1 in the link (`yc="eu-central-1"`).

**[I] Gap between the promise and the policy.** AWS's managed `ReadOnlyAccess` includes data-plane reads such as `s3:GetObject` and DynamoDB item reads (this is the policy's documented content; we did not fetch the policy here). So "never your files or database contents" (FAQ 3) is a promise about how the scanner behaves, not a limit the role enforces. A cautious security reviewer will notice. Our template is an allow-list that cannot read objects outside the bill-export bucket (doc 12, Moat 4; §8 below).

**[O]** A role migration is under way: the bundle knows the old `Refraxion-ReadOnly` / `Refraxion-ReadOnly-Access` names and the v2 names, and shows a banner "Scans of … stopped – update the role" with a "two clicks in AWS" update path (events `role_update_*`). This is the same problem our "Update CloudOptics access" flow solves.

### 4.2 After connection (seen in strings only)

**[O]** Scan profiles, findings with feedback and state, a cost-analysis view per scan (`/v1/scans/{id}/cost-analysis`), help requests to Cloudvisor, workspace members and invites, data export and account deletion, agent (MCP) tokens with an approval flow, and a public feature-request board with votes (`/v1/features/{id}/vote`). **[U]** How findings are computed, scan cadence, and whether "Premium" will add scheduled scans.

## 5. Tech and marketing stack

| Layer | Finding | Evidence |
|---|---|---|
| Build | **[O]** Vite SPA (`__vite__mapDeps`, hashed `assets/*`), React (JSX runtime calls), route-level `React.lazy` chunks | main bundle |
| UI kit | **[O]** AWS's Cloudscape design system in the product (`awsui_*` class names; Cloudscape's `Alert` is even in the landing bundle). Marketing pages use hand-written `rfx-*` CSS with a dark "spectrum" theme (`--rfx-bg:#0a0b12`, gradient `--rfx-spectrum`). Fonts: Manrope, Sora, JetBrains Mono, Open Sans | `index-*.css`, `AgentsNotice-*.css` |
| Hosting | **[O]** S3 behind CloudFront (`Server: AmazonS3`, `X-Cache: Hit from cloudfront`); API at `https://api.refraxion.io`; onboarding templates in an S3 bucket in eu-central-1 | response headers; bundle strings |
| Security headers | **[O]** `Content-Security-Policy` limited to `frame-ancestors 'self'; object-src 'none'; base-uri 'self'` (no `script-src`); `Strict-Transport-Security: max-age=86400` (one day, no preload); `X-Frame-Options: SAMEORIGIN`; `nosniff`; `strict-origin-when-cross-origin` | `curl -I https://refraxion.io/` |
| Tags and consent | **[O]** Google Tag Manager `GTM-KCS5VWS2`, preceded by Google Consent Mode v2 defaults (everything denied except security storage, `wait_for_update: 500`, `ads_data_redaction`). Cookiebot runs as a tag inside GTM. The GTM loader skips framed documents | index.html head |
| Product analytics | **[O]** PostHog (EU ingestion host `eu.i.posthog.com` in the SDK), with about 35 named events for onboarding, connection, agents, invites and settings | `analytics-*.js` |
| CRM and forms | **[O]** HubSpot: forms API for `/paid-beta`, the `hubspotutk` cookie, HubSpot Meetings for "expert help" | `PaidBeta-*.js`, `hubspotCookie-*.js`, `ExpertHelpModal-*.js` |
| Session replay | **[O]** Microsoft Clarity is named in the head comment ("4 clarity.ms" requests measured) and chunks use a `clarity-mask` class to hide fields. **[I]** Clarity is loaded through GTM | index.html comment; 4 chunks |
| Calendly | **[O]** The head comment says Calendly's widget loads from `src/lib/calendly.ts` when someone books a call. **[O]** No chunk we downloaded contains the string "calendly"; booking uses HubSpot Meetings. **[I]** The comment is out of date | grep over all 23 chunks |
| Attribution | **[O]** UTM parameters and click IDs (`gclid`, `gbraid`, `wbraid`, `fbclid`, `ttclid`, `hsa_*`) plus `promo` and `brand` are copied from marketing URLs onto sign-up links on the `app.` host | parameter set near @356069 |
| Social | **[O]** Instagram, LinkedIn, YouTube; "Building in public" on About | `ga` object in main bundle |

### 5.1 The consent story, in their own head comment

**[O]** The production `index.html` ships a long developer comment (with internal ticket IDs) explaining two incidents:

1. **Two Cookiebot loaders.** One was hard-coded in the head with auto-blocking and one ran inside GTM. Visitors saw two stacked banners, and auto-blocking rewrote the GTM snippet to `type="text/plain"`, so GTM never loaded: per the comment, "googletagmanager.com was never requested, GA never fired, no ad pixel fired" for anyone who had not yet consented, which is all paid traffic. The fix kept one Cookiebot, inside GTM.
2. **The whole stack booting twice.** The landing page embedded `/demo?theme=dark` in an iframe, and because every route serves the same `index.html`, the iframe loaded GTM, Cookiebot and Clarity again: double pageviews, double ad tags, and a nested consent banner. The fix skips the GTM loader when `window.self !== window.top`.

**[O]** Today's landing hero is a `<video>` (`/landing/demo-2026-10-28.mp4`), not an iframe, while the router still redirects a framed `/` to `/demo`. **[I]** The iframe that caused incident 2 is gone; the guard and the comment remain.

**[I] Lessons for us.** (a) One consent manager, one loader, in one place. (b) Never put the marketing tag stack on a document that can be embedded or that renders sample data. (c) Strip HTML comments at build time; Refraxion's tell competitors about internal tickets, file paths and an analytics outage. (d) The privacy policy's processor table lists AWS, HubSpot, Google Analytics and PostHog, but not Clarity or Cookiebot, which the same file says are loaded. Keep the processor list generated from, or checked against, the tag inventory.

### 5.2 SEO approach and its weaknesses

| # | Finding |
|---|---|
| 5.2.1 | **[O]** One `<title>`, description, canonical (`https://refraxion.io/`), Open Graph and Twitter card set for every route. The comment admits it: "one tag set covers every route". So `/about`, `/privacy` and `/terms` all declare `/` as canonical and share the landing title. |
| 5.2.2 | **[O]** `<div id="root"></div>` is empty in the HTML; all copy, headings and FAQ text exist only after JavaScript runs. The `<noscript>` block contains only the GTM iframe. |
| 5.2.3 | **[O]** Unknown paths return HTTP 200 with the app shell (soft 404s). |
| 5.2.4 | **[O]** The sitemap lists four URLs with no `lastmod`. robots.txt says demo routes are "not for indexing" but does not disallow `/demo`; it disallows four old paths that only redirect. |
| 5.2.5 | **[O]** No structured data (no `application/ld+json`), even though the FAQ is a natural `FAQPage`. |
| 5.2.6 | **[I]** Google renders JavaScript, so the landing page can be indexed, but late and with less certainty; LinkedIn, Slack and WhatsApp previews do not run JavaScript and will show the landing card for every shared URL. Non-landing pages are effectively invisible as separate results because their canonical points home. For a product that wants inbound search traffic ("AWS posture review", "AWS security scan free"), this throws away most of the site. |
| 5.2.7 | **[O]** Payload before first paint of the landing page: 201 KB gzipped JS plus 217 KB gzipped CSS. The CSS carries 18 base64-inlined WOFF2 fonts and Cloudscape component styles the landing page does not use. **[I]** About 420 KB of render-blocking transfer before any image or video, heavy for 3G-class African networks (our FR-UI-10 budget is 300 KB of JS for the whole app shell). |

## 6. What works

| What | Why it works | Evidence |
|---|---|---|
| Free forever, stated plainly | Removes the first objection; the FAQ says it is not a beta promotion | §2 |
| Sample report one click from the hero | Shows the product before any sign-up; "See sample report" is the secondary CTA everywhere | §3.2 |
| "How it works" with concrete artefacts | A role ARN, a scan status line, a findings count: tangible and short | §3.2 row 4 |
| Trust copy written as rules | Six short rules ("Read-only, always", "We never scan uninvited") are easier to believe than a security page full of adjectives | About page |
| Plain-language permission explanation at the moment of deployment | What the role can read, who can use it, how to remove it, beside the button | §4 |
| Colleague path | "Not the person with AWS access? Send them the link", tracked as its own event | §4 |
| Specific error states | "No role there yet" vs "That's on our side, not yours" | §4 |
| Customer-readable template | Comments speak to the reviewer; fixed role name to audit later | §4.1 |
| Role-update banner | Treats permission changes as a guided two-click task | §4.1 |
| Reduced-motion and lazy video | Video controls instead of autoplay when the user asks for less motion; second video `preload:"none"` | §3.2 |
| Attribution carried across hosts | UTM and click IDs survive the hop from marketing to app | §5 |

## 7. What does not work

| What | Why it hurts | Evidence |
|---|---|---|
| One HTML document for every route | No per-page titles, canonicals or social cards; JS-only content; soft 404s | §5.2 |
| Heavy first load | ~420 KB gzipped before first paint; fonts inlined in CSS | §5.2.7 |
| Security review has nowhere to go | No security, permissions or "how it works" page; the template link is only inside the app | §3.1 |
| `ReadOnlyAccess` | The role technically allows reading object and table data, which contradicts the spirit of "configuration and metadata only" | §4.1 |
| Inconsistent claims | Scan time "about a minute" vs "less than ten minutes" | §3.3 |
| Unquantified free allowance | "Generous" invites the question "how many?" | §2.2 |
| Incentivised public reviews | Disclosure and platform-policy risk | §2.4–2.5 |
| Internal notes in production HTML | Leaks ticket numbers, file paths and incident history | §5.1 |
| Processor list vs tags | Clarity and Cookiebot not in the privacy policy's processor table | §5.1 |
| Weak HSTS and CSP | One-day HSTS, no `script-src` | §5 |
| Cost is a side pillar | Fine for them; it leaves cost-led buyers wanting depth | §1.7 |

## 8. Where Cloudoptics is different

| Theme | Refraxion (observed) | Cloudoptics (from README, docs 04, 06, 12 and the template) |
|---|---|---|
| Job | Posture review across three pillars; cost is one third | Cost optimisation only: what is safe to cut, how, and what it saved |
| Audience | Generic "AWS builder", EU-hosted, USD spend bands | African growth companies; country and local currency chosen at sign-up |
| Money | No currency handling seen | Every figure in USD and local currency; FX scenarios; credits runway |
| Permissions | One role with AWS `ReadOnlyAccess` | An allow-list per role (`CloudopticsBillingReader`, `CloudopticsResourceReader`): no `ec2:Describe*` wildcard, no Lambda configuration, no DNS records; `s3:GetObject` only on the bill-export bucket (`infra/cloudformation/cloudoptics-access.yaml`) |
| Data | Findings in Frankfurt | Hosted in London (ADR-013); data-residency notes for NDPA, Kenya DPA, POPIA (doc 10 A7) |
| Fix path | Console link, AI agent, or book Cloudvisor | Risk score, evidence, guided fix with rollback steps, "Open in AWS console", assignment, then **verified** savings from the bills (README) |
| Proof | Parent partner badge and audit count | Verified savings in local currency once design partners exist; Cloudboosta's training community (doc 04 Pillar 5) |
| Organisations | One role per account, account ID typed in | One stack in the management account rolls the resource role out to every member account by StackSet (README; template) |
| Pricing | Free forever; premium TBD | Free tier plus flat local-currency tiers by spend band (doc 10 C5, hypotheses) |

**[I]** Refraxion is not a direct competitor today, but `CostCheck` shows Cloudvisor wants a cost-savings lead magnet. If it ships, it will compete with our "Free AWS savings report" hook (doc 10 C4). Our answer is depth: a connected, verified, local-currency view instead of a one-off PDF estimate.

## 9. Recommendations for our public pages (summary; detail in doc 14)

1. **Copy the patterns, not the words.** Free entry stated plainly; sample product one click away; a three-step "how it works" with concrete artefacts; trust written as short rules; plain-language permissions beside the deploy button; a colleague path; specific error states.
2. **Do better on SEO.** Prerendered HTML per public route, each with its own title, description, canonical and social card; real 404s; a sitemap with `lastmod`; `FAQPage` and `Organization`/`SoftwareApplication` structured data.
3. **Publish the permissions.** A public page that lists every action in plain language, grouped by role, generated from `cloudoptics-access.yaml` so it cannot drift, with a link to the raw template.
4. **One consent manager, loaded once,** never on embedded or demo documents; the processor list in the privacy notice checked against the tag inventory; HTML comments stripped at build.
5. **Keep the first load light:** a JS-free or near-JS-free public page under 100 KB gzipped of critical transfer, system-font fallback, no inlined font files.
6. **One source for public claims** (setup time, data location, retention) shared by every page.
7. **Lead with local currency and verified savings,** the two things Refraxion cannot say.

## 10. Checklist of the questions this teardown set out to answer

| Question | Status |
|---|---|
| Positioning, audience, promise | Answered (§1) |
| Pricing and free tier | Answered; premium contents unknown (§2) |
| Every public route | Answered from the router (§3.1); runtime reachability not tested |
| Landing section order and content | Answered (§3.2) |
| Onboarding page | There is no public onboarding page; in-app wizard answered from code (§4) |
| What the customer deploys and why | Answered from the published template (§4.1) |
| Tech, marketing stack, SEO | Answered; what GTM actually loads after consent is unknown (§5) |
| What works, what doesn't, how we differ | §§6–8 |
