# ADR-013: Staging on DigitalOcean App Platform, Kubernetes later

**Status:** Decided 22 Oct 2026 (stakeholder: "App Platform (recommended)"; domain `cloudoptics.cloudboosta.com`; WorkOS for sign-in)

## Context

ADR-012 put the demo on GitHub Pages and planned the move to DigitalOcean. ADR-008 chose DOKS + Argo CD + Terraform for the long term. With no customers yet, a Kubernetes cluster (about $75+/month and several days of setup) buys capabilities we do not need yet.

## Decision

Run staging on **App Platform** in London, as code in `infra/digitalocean/`:

| Component | What | Size / price (list, Oct 2026) |
|---|---|---|
| `api` | The GHCR image `cloudoptics-api:main`, health check `/readyz` (checks PostgreSQL) | apps-s-1vcpu-1gb-fixed, $10 |
| `worker` | Same image, `python -m cloudoptics.worker` (Procrastinate) | apps-s-1vcpu-0.5gb, $5 |
| `migrate` | Same image, `alembic upgrade head` as a pre-deploy job, as the database owner | billed per run |
| `site`, `web`, `demo` | Static builds from the public repository: the public site at `/` (with the templates under `/cfn`), the app at `/app`, the demo at `/demo` (SITE-13, ADR-016) | static (check App Platform static-site pricing: three static components instead of one) |
| Database | Managed PostgreSQL 16, 1 node, `cloudoptics-pg`; firewall admits only the app | db-s-1vcpu-1gb, $15 |
| Address | `https://cloudoptics.cloudboosta.com`, API under `/api` (same origin, no CORS) | — |

About **$35/month** in total. `provision.py` is idempotent and reads secrets (database passwords) from the DigitalOcean API at deploy time, so none are committed. The data key is generated once and kept on later deploys.

Staging runs with `environment=staging`, which the settings guard turns into hard requirements: OIDC sign-in (WorkOS AuthKit, ADR-006), queued jobs, and a non-development data key.

## Consequences

- Same image and settings as Codespaces and the future cluster: moving to DOKS is a deployment change.
- No fixed outgoing IP from App Platform, so customers cannot yet pin our role with `aws:SourceIp` (ADR-011). Acceptable for staging; DOKS + egress proxy when a customer requires it.
- The local AES data key is held as an App Platform secret. KMS envelope encryption (ADR-007 amendment) replaces it once Staging setup turns it on; the local key then only reads older ciphertexts.
- Until the WorkOS keys are configured, the API answers 503 "Sign-in is not configured yet".

## When to move to DOKS (ADR-008)

Any of: a customer requires IP pinning; worker load needs queue-based autoscaling; we need the Roles Anywhere sidecar (CC-1) rather than an environment credential.
