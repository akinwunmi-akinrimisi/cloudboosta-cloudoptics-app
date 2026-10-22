# ADR-012: Host on GitHub first, move to DigitalOcean when the platform is proven

**Status:** Decided 22 Oct 2026 (stakeholder request)

## Context

The stakeholder asked to deploy on GitHub first and move to DigitalOcean once everything works. GitHub can host static sites (Pages), container images (GHCR) and development environments (Codespaces). It cannot run a long-lived API, a worker or a database.

## Decision

| Piece | Where now | Where later (ADR-008) |
|---|---|---|
| Web app, public demo | **GitHub Pages**, built with `VITE_API_MODE=demo`: in-browser demo data, hash routing, no AWS calls, no customer data | Stays on Pages or Cloudflare as the marketing demo |
| Web app, real | Codespaces or local, pointed at the API | DigitalOcean (Spaces + Cloudflare) |
| API and worker image | **GHCR** `ghcr.io/<owner>/cloudoptics-api`, built on every PR and published from `main` | Same image, pulled by DOKS |
| API + PostgreSQL for development | **Codespaces** (dev container + `compose.yaml`) | DOKS + DO Managed PostgreSQL |

The move to DigitalOcean happens when the Sprint 0 spikes pass (CC-1 Roles Anywhere, ADM-1 auth, OPS-5 egress) and before any design partner connects a real AWS account.

## Consequences

- The public URL is a demo. It says so (a "Demo" badge in the app) and never collects real AWS account data; anything typed stays in the visitor's browser.
- No customer connects a real account until the DigitalOcean environment exists: a Codespace is a personal development machine, not a hosting environment.
- The image, migrations and settings are the same in both places, so the move is a deployment change, not a code change.
- GitHub Pages on a private repository needs a paid GitHub plan; on a public repository it is free.

## Alternatives considered

Render, Fly.io or Railway for the API now (adds a third provider before DigitalOcean); going straight to DigitalOcean (blocked on the Sprint 0 spikes).
