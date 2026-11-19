# First-party analytics and the sign-up funnel (SITE-15)

Founder decision (19 Nov 2026): no ads, no pixels, first-party only. This replaces the PostHog
plan in docs/14 §8.4. Nothing on the public site, in the app or in the API sends data to an
analytics vendor; the numbers live in our own PostgreSQL (migration 0038).

## What is collected

**Public site (`apps/site`, `src/scripts/beacon.mjs`).** One small script (about 1.1 KB
gzipped) on every page posts to `POST /api/v1/site/visits`:

| Event | Fields |
|---|---|
| `page_view` | path (no query string), whether it is an entry page, the referring host on an entry page, `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`, `ref` |
| `cta_click` | the same campaign fields, the path, and the CTA: `start`, `sign_in`, `demo`, `template`, or a link's `data-cta` value |

It sets no cookies, uses no localStorage or sessionStorage, and sends no identifier or
fingerprint. It sends nothing when the browser has Do Not Track or Global Privacy Control on,
on any path under `/demo`, or when the page or an element on it has `data-no-analytics`. A
page can also leave the script out with `<Base noAnalytics>`, and `SITE_ANALYTICS=off` builds
the whole site without it.

**API (`cloudoptics.analytics.site`).** The route is public and limited per client address
(`CLOUDOPTICS_RATE_LIMIT_SITE_PER_MINUTE`, default 120). Bodies over 2 KB are refused before
parsing; every field is validated and capped. It stores one `site_event` row with the time
truncated to the hour. The client address is used only by the rate limiter (as a hash) and the
user agent only to drop crawlers; neither is stored. Requests with `DNT: 1` or `Sec-GPC: 1`,
crawlers, and paths under `/demo`, `/app` or `/api` get the same 204 and are not stored.
Campaign values that look like an email address are dropped. Rows older than 400 days are
deleted by the beacon route itself, now and then.

**Attribution through sign-up.** On an entry page with a campaign (or an outside referrer),
the beacon copies `utm_*`, `ref` (the referring host when there is no campaign) and `lp` (the
landing path) onto this site's own links, so they reach the app's sign-in in the URL. The app
(`apps/web/src/lib/attribution.ts`) keeps them in sessionStorage for that tab, so they survive
the WorkOS round trip, unless the browser asks not to be tracked. `HttpApi.createTenant` sends
them once as `attribution` with the first workspace and then forgets them. The API cleans them
and stores them on the `workspace_created` funnel row. First touch wins: a later campaign in
the same tab does not replace the first.

**Server funnel events (`cloudoptics.analytics.funnel`).** Each step is stored once, the first
time it happens, with internal IDs only:

| Event | Recorded when | Once per |
|---|---|---|
| `signup_completed` | a sign-in creates the user (`identity.deps.current_user`) | user |
| `workspace_created` | `POST /v1/tenants`, with the first-touch campaign | workspace |
| `connect_started` | an admin opens the connect link (`POST …/connections/intents`) | workspace |
| `connected` | a validation succeeds (healthy or degraded) | workspace |
| `first_costs` | the first costs are stored: `provisional` (Cost Explorer) or `bills` (CUR) | workspace |
| `first_recommendation` | an analysis run produces at least one recommendation | workspace |

Funnel rows go with the workspace when it is deleted (privacy.deletion `TABLES`).

## How to read the funnel

Staff (platform-admin allowlist, verified email, MFA session) can call:

- `GET /api/v1/admin/analytics/funnel?since=2026-11-01&until=2026-11-30`
- `GET /api/v1/admin/analytics/site?since=…&until=…` (top pages, entry referrers, campaigns,
  CTA clicks; 50 rows each)

Both default to the last 30 days (UTC) and allow at most 400 days.

The funnel answer has:

- `site`: page views, entry-page views (`landings`, a stand-in for visits) and Start-free
  clicks in the window;
- `signups`, and `signupsWithWorkspace` (of those, how many created a workspace);
- `steps`: workspaces **created in the window** and how many of them reached each later step,
  whenever that happened. Recent cohorts are still moving, so compare cohorts of the same age;
- `bySource`: the same per source, next to that source's landings and Start-free clicks.

The source is `utm_source`, else `ref` (on the site, else the referring host), else `direct`.
Sign-ups are not split by source: the campaign is sent with the workspace, not at sign-in.

Read the conversion of each step as the next step's count over the previous one, for example
`connected / connect_started` for "the stack worked once someone opened the link".

The same numbers in SQL, for a session with `SET app.analytics_read = 'on'` (or as the owner):

```sql
-- Workspaces created in November, by source, and how far each got.
WITH cohort AS (
  SELECT tenant_id, coalesce(source, ref, 'direct') AS source
  FROM funnel_event
  WHERE event = 'workspace_created'
    AND occurred_at >= '2026-11-01' AND occurred_at < '2026-12-01'
)
SELECT c.source,
       count(*) FILTER (WHERE f.event = 'workspace_created')    AS workspaces,
       count(*) FILTER (WHERE f.event = 'connect_started')      AS connect_started,
       count(*) FILTER (WHERE f.event = 'connected')            AS connected,
       count(*) FILTER (WHERE f.event = 'first_costs')          AS first_costs,
       count(*) FILTER (WHERE f.event = 'first_recommendation') AS first_recommendation
FROM cohort c JOIN funnel_event f ON f.tenant_id = c.tenant_id
GROUP BY c.source ORDER BY workspaces DESC;

-- Median time from workspace to first costs, in hours.
SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(epoch FROM c.occurred_at - w.occurred_at) / 3600)
FROM funnel_event w JOIN funnel_event c ON c.tenant_id = w.tenant_id AND c.event = 'first_costs'
WHERE w.event = 'workspace_created';
```

## Owner actions

1. **Search Console and Bing Webmaster Tools.** Add the property for the production domain in
   each tool, choose the HTML tag method, and set the token at site build time:
   `SITE_GOOGLE_VERIFICATION=<token>` and `SITE_BING_VERIFICATION=<token>` (App Platform build
   environment for the `site` component). The site then emits `google-site-verification` and
   `msvalidate.01` meta tags on every page; without them no tag is emitted. Then submit
   `https://<domain>/sitemap.xml` in both tools.
2. **Rich results and previews.** After deploy, run Google's Rich Results Test on `/`,
   `/how-it-works` and `/security` (FAQPage, SoftwareApplication) and paste a link into
   LinkedIn Post Inspector, Slack and WhatsApp to check the preview card.
3. **Privacy notice.** When counsel's privacy notice is written (SITE-12), describe the site
   analytics as above: first-party, no cookies, no IP address stored, Do Not Track and GPC
   respected. No analytics sub-processor needs listing.
4. **Staff access.** Funnel readers must be on `CLOUDOPTICS_PLATFORM_ADMIN_EMAILS` and sign in
   with MFA.
