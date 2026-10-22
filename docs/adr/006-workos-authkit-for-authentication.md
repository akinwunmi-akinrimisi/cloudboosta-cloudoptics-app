# ADR-006: WorkOS AuthKit for authentication

**Status:** Proposed — Sprint 0 spike vs Keycloak

## Context

We need email/password, Google, MFA now and SAML/OIDC SSO with SCIM for v1 enterprise buyers, without building auth.

## Decision

WorkOS AuthKit hosted login. We keep our own user and tenant tables and store only `auth_subject`, so we can leave.

## Consequences

Fast to integrate; cost scales with SSO connections. Final choice depends on the spike (docs/spikes/sprint-0.md, ADM-1).

## Alternatives considered

Keycloak or Zitadel self-hosted (no per-user cost, more operations); Auth0 (higher cost); Clerk; Cognito.

## References

docs/11a §8
