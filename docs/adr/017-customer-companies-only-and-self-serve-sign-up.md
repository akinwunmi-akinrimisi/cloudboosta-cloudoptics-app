# ADR-017: Customer companies only, self-serve sign-up, one verified workspace per cloud account

**Status:** Decided 19 Nov 2026 (founder; multi-cloud plan, Phase 0)

## Context

The multi-cloud readiness review (19 Nov 2026) confirmed Cloudoptics is a self-serve SaaS with isolated workspaces, and raised three open points: whether resellers, agencies and MSPs (one company managing many clients' clouds) are customers; whether sign-up is open to everyone or starts with design-partner applications (docs/14 Q4); and whether the rule from migration 0017, one verified workspace per AWS account, should be relaxed so an MSP and its client can both monitor the same account. `tenant.parent_tenant_id` (docs/09 §2.3, "partners/MSPs later") exists but is unused.

## Decision

- **Customers are individual companies.** Each company has its own workspace (or several, for separate business units), with its own members, AWS accounts and data. Resellers, agencies and MSPs managing client clouds are out of scope: there are no partner consoles, client hierarchies or cross-workspace views. `parent_tenant_id` stays unused.
- **One verified workspace per cloud account stays.** The unique index from 0017 keeps a second workspace from verifying an account another workspace already monitors. A company that needs two views of the same account uses members and roles in one workspace.
- **Sign-up is self-serve** (docs/14 Q4, decided). Anyone can sign up with WorkOS AuthKit and create a workspace; there is no allowlist or application step. A signed-in member can create another workspace from the menu ("New workspace") or Settings → Workspace.

## Consequences

- Isolation stays strictly per workspace. The checks added with this decision (RLS lint, every-table cross-tenant test, route membership test, `app_user` row-level security; docs/09 §2.3) assume no workspace may read another's data, with the listed exceptions only.
- Open sign-up depends on the WorkOS dashboard allowing sign-ups in each environment; that is an environment setting, not code.
- Abuse controls rest on the existing rate limits, the email-verification rules for invites and staff, and the one-account-per-workspace rule. If spam workspaces appear, add limits per user rather than applications.
- Serving MSPs later would need a new ADR: delegated access across workspaces, a billing relationship, and a change to the one-workspace-per-account rule.

## Alternatives

- **Design-partner applications first** (doc 10 C7). Rejected: the onboarding is self-serve end to end, and a waitlist slows the feedback the beta needs.
- **Partner/MSP hierarchy now.** Rejected: it widens the isolation model (cross-tenant reads) before the single-company product is proven.
