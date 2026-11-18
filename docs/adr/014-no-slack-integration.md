# ADR-014: No Slack integration

**Status:** Decided 18 Nov 2026 (stakeholder: "slack not needed, remove it from the plan")

## Context

The PRD planned a customer-facing Slack app (FR-NOT-01, INT-02): an OAuth install per workspace, alerts and digests posted to chosen channels, DMs to assignees, and interactive buttons. It was scheduled for Sprint 4, moved to Sprint 5, and never started. It needs a Slack app registered and approved for distribution, token storage, rate limiting and its own tests, and it repeats what email already delivers.

## Decision

Cloudoptics does not build a Slack integration. Email (Postmark) is the only notification channel: connection failures, budget and anomaly alerts, assignment notices, commitment expiries, the weekly digest and the monthly report.

## Consequences

- Removed from the plan: INT-02, the Slack part of FR-NOT-01/04 and FR-SEC-08, the "Slack connect" onboarding step (FR-UI-03), the Slack recipient on budgets, and the Slack app from ADM-8 and Sprint 5 (docs/06, docs/11d).
- The UX, architecture, operations and test documents (docs/07–11g) still describe Slack in places. This ADR supersedes those passages; they are not rewritten.
- The market research (docs/01–04, docs/12) is unchanged: it describes competitors and buyers, not our scope.
- No Slack secrets, tables or endpoints exist, so nothing has to be removed from the code.

## Alternatives

- **Build the Slack app (the original plan).** Rejected for now: no customer need yet, and it adds a distribution approval and a secret store to the critical path.
- **Generic outgoing webhooks** (a customer pastes a Slack or Teams incoming-webhook URL). A small, cheap option if a design partner asks for chat alerts later; it would need its own ADR.
