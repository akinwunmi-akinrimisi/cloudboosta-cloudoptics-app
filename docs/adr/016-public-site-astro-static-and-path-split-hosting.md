# ADR-016: Public site as a static Astro app on the app's origin, with no third-party scripts

**Status:** Decided 19 Nov 2026 (founder approved docs/14). The hosting change is built in SITE-13 (docs/14 §7.2 "As built").

## Context

Cloudoptics has no public pages. Signed out, `/` redirects to the sign-in card, and the only public text is one meta description (docs/14 §1). Design-partner recruitment for the Increment 2 beta needs a landing page, a "how it works" page and a security page that a reviewer can read before anyone signs up. The Refraxion teardown (docs/13 §5.2, §7) shows what goes wrong when marketing pages live inside a single-page app: one `index.html` with one title and canonical for every route, content only after JavaScript, soft 404s, about 420 KB of render-blocking transfer, and a tag manager and consent tool that booted twice and leaked developer comments into production HTML.

The site and the app share decisions that must not drift: the design tokens, and the list of IAM permissions in `infra/cloudformation/cloudoptics-access.yaml`.

## Decision

1. **Option C of docs/14 §7.1: a separate static site in the monorepo, `apps/site`, built with Astro** (static output, no JavaScript by default, Tailwind v4). Each route is real HTML with its own title, description, canonical URL, Open Graph tags and JSON-LD; `404.html` is a real 404; the sitemap carries `lastmod` from git. Interactive pieces are small islands; the landing page's currency toggle is the only script and works as plain text without it.
2. **Shared sources, checked in CI.** The site and `apps/web` import the same tokens (`packages/design-tokens/tokens.css`). The permission reference is generated at build time from the CloudFormation template plus plain-language entries in `apps/site/src/data/permissions.yaml`; the build fails when the template gains or loses an action, role or managed policy without a matching entry, when a role marked read-only gains a non-read action or loses its ExternalId condition, and when the member-account template (`resource-role.yaml`) differs from the main resource role. The result is also published as `/permissions.json` for the app's onboarding panel. Public claims (setup time, step names, hosting location, pricing) live in one file, `apps/site/src/data/claims.json`.
3. **Hosting layout H1 of docs/14 §7.2: path split on one origin.** `cloudoptics.cloudboosta.com/` serves the site, `/app/` the web app, `/api` the API (unchanged), `/demo/` the demo, `/cfn/` the templates. The absolute URL comes from the `SITE_URL` build variable, so a domain change is configuration. Moving the app to `/app` is a separate change (SITE-13) with its own ordering constraints (docs/14 §7.2).
4. **No third-party scripts on this origin.** The site shares an origin with the app, whose AuthKit refresh token is in local storage. No page served from this origin may load a script, style, font or frame from another host: no tag manager, ad pixel, consent manager, chat widget or analytics SDK. Every page carries a Content-Security-Policy meta tag that allows `'self'` only (production builds), and CI fails on inline scripts or styles. Analytics, when added (SITE-15), is a first-party beacon with no cookies; if the PostHog EU ingestion host is used, it is added to `connect-src` and nothing else changes. The site sets no cookies and uses no device storage.
5. **No developer comments in shipped HTML.** An Astro integration strips HTML comments at build time, and CI fails if any remain.

## Consequences

- One more framework in the repository (Astro), with its own CI job ("Site": lint, typecheck, permission tests, build, link check, size budgets per docs/14 §8.3, comment check, axe and token-contrast checks).
- Marketing copy changes are pull requests reviewed by engineers. If the founder wants marketing to edit pages without engineers (docs/14 §11 Q2), revisit this ADR.
- If paid ads, tracking pixels or a consent manager ever become necessary, move to a subdomain split (H2) first, or keep those tags on cloudboosta.com. They never run on this origin.
- App Platform static sites have limited control over response headers; the CSP is a meta tag until Cloudflare (ADR-012 "later") or App Platform can send headers such as HSTS and `frame-ancestors`.

## Alternatives

- **A. Public routes inside the SPA.** Rejected: the Refraxion failure mode (one document, JavaScript-only content, soft 404s, the whole app shell before any text).
- **B. Prerendered SPA routes.** Rejected: still ships React and the router to hydrate, and ties marketing changes to app releases.
- **D. A hosted website builder (Webflow, Framer, a page on cloudboosta.com).** Rejected unless Q2 says otherwise: the tokens and the permission reference cannot be generated from this repository, and it adds a licence and a third-party origin.
- **H2. Subdomain split (`app.` for the app and API).** Deferred: it moves the API host, so stack callbacks of existing connections, WorkOS redirect URIs, CORS and email links would all change. Only needed if third-party tags are ever required.
