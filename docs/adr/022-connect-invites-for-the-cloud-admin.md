# ADR-022: "Send this to my cloud admin": connect invites with a token-only guest session

**Status:** Decided 20 Nov 2026 (frictionless-onboarding plan, "For everyone: I'm not the admin"; founder approval 20 Nov 2026).

## Context

The person who signs up for Cloudoptics is often a founder or a finance lead. They can create the workspace but usually do not hold the cloud role that the connect step needs: creating an IAM role in AWS, sharing a BigQuery dataset in Google Cloud, assigning an Azure role, approving an app in DigitalOcean. Until now the only help was "Copy link for a colleague", which copied the AWS one-click link with a short note. It did nothing for the other clouds, and the sender could not see whether anything had happened.

The goal: the sender types their cloud admin's email. The admin gets a link that opens only that cloud's connect step, finishes it without joining the workspace and without seeing any cost data, and the sender sees the status (sent, opened, connected, expired).

The design question is who the admin is to Cloudoptics while they do this.

## Options

1. **A WorkOS account.** The admin signs up or signs in, and the link gives that user a narrow grant. The OAuth sign-in flows (DigitalOcean today, Azure next) already bind their `state` to a user, and the audit log already names users. But the admin has to create an account with a product they have never used, for a five-minute favour. That is the friction the hand-off exists to remove. It also leaves an account and a "member-like" grant behind that need their own lifecycle and their own place in the membership model.
2. **A token-only session, with no identity.** Lowest friction, but the audit log would have no actor and an OAuth `state` would have nothing to bind to.
3. **A token-only "guest connector" identity (chosen).** No account. The link's token proves the person read the email sent to that address, the same proof a password reset or a magic link gives, so the invite itself is the identity: audit rows are recorded as actor type `connect_invite` with the invite's ID, and shown as "connect invite \<email\>". Sign-in states bind to the invite's ID.

## Decision

Option 3.

### The invite

- `POST /v1/tenants/{t}/connect-invites` `{email, provider}`, **admins only** (`AdminAccess`). The table is `connect_invite` (migration 0051): RLS ENABLE and FORCE with the tenant policy, in the deletion code (`TABLES`, going with the connection it set up) and in the cross-tenant and data-version guard lists.
- **The link token** is 256 random bits (`secrets.token_urlsafe(32)`). Only `sha256("cloudoptics/connect-invite:" + token)` is stored. The token appears once, in the email, and is never logged or returned.
- **Lifetime.** 7 days, like member invites and one-click links. It can be revoked. Sending the same cloud to the same address again revokes the open request. It stops working when its sender is no longer an admin of the workspace (the rule one-click links already follow, security audit run 1). It is **single use**: once it has connected an account, the link no longer opens.
- **Rate limit.** At most `connect_invites_per_day` (10) sends per workspace in 24 hours, counted in the table under a per-workspace advisory lock, so parallel requests cannot race past it. The 11th send gets 429 with `Retry-After`. The per-minute limiter in `platform/ratelimit.py` is the wrong shape for a daily cap.
- **The email** goes through the existing Postmark path (`notifications.email`), like member invites. It says who asked and for which company, what the step grants (read-only), what it does not (no changes; no files, databases or secrets; no workspace and no costs), who can do it, and when the link stops working. There is no marketing.

### The session

- `POST /v1/connect-invite/open {token}` trades the link's token for a **session token**. That token is also 256 random bits and stored only as a hash. Opening the link again issues a new session and ends the previous one, so only one browser at a time holds the invite. The web page then removes the token from the address bar.
- The session token is sent as `Authorization: ConnectInvite <token>`. **Only one dependency accepts it**, `connectors.handoff.invite_session`, and only the routes under `/v1/connect-invite/session` use it. Every workspace route depends on `require_role`, which needs a signed-in user's bearer token. Sent there, the session token is refused with 401, whatever scheme is used. The check is server-side and structural, not a UI rule.
- The session can do this and nothing else:
  - read its view: who asked, the workspace's name, the cloud, and the connection it set up, with that connection's access checks (`GET /session`);
  - its own cloud's connect actions, with the same handlers the in-app steps use: AWS (`/session/aws/link`, the one-click link and its status), Google Cloud (`/session/gcp`, create and the steps with the connection's own service account), Azure (`/session/azure`, create and its consent and role steps), DigitalOcean (`/session/digitalocean` token paste, `/session/digitalocean/oauth` sign-in);
  - Check access on its own connection (`/session/check`).

  Another cloud's routes return 403 `invite_provider`. A second connection while the first is being set up returns 409 `invite_in_progress`. Any connect action after the link has connected returns 409 `invite_used`. The session can still read its result and run Check access again.
- The handlers run with a `TenantAccess` whose `user` is the **sender**, so connections record the sender as `created_by`, and whose `connect_invite_id` is set. `user_actor()` returns the invite as the actor for the whole request, so every audit row a shared handler writes names the invite. The DigitalOcean sign-in state is bound to `TenantAccess.binding_id` (the invite in a session, the user otherwise). The Azure sign-in being built in parallel should bind its state the same way.
- **MFA.** A workspace's MFA policy governs its members' sessions. The invite session reaches no workspace data, and its authority comes from an admin who passed that policy when sending it, so it is not asked for MFA.

### Status and notifications

`settle()` moves an invite on. It picks up the connection an AWS stack reported through the invite's one-click link. It marks the invite **connected** once that connection's check passes (healthy or degraded). It notes an **expired** invite that was never used. Each outcome is audited (`connect_invite.connected` as the invite, `connect_invite.expired` as the system) and emailed to the sender once, de-duplicated through `notification`. It runs when the session is read or checked, when the sender lists invites (the onboarding step and the Connections page poll every 5 seconds while a request is open), and every 15 minutes in `connectors:connect_invites.sweep`. In the app, the status chip appears on the onboarding step and on the Connections page ("Sent to cloud admins"), with a line when an admin has connected a cloud. The onboarding step moves on to Check access by itself.

Audit actions: `connect_invite.sent`, `.opened`, `.revoked`, `.connected`, `.expired`, plus the usual `connection.*` rows written by the shared handlers.

### Role pre-checks

Before each cloud's connect step, the onboarding page shows one line on who can do it, then "Not you? Send this to your admin". The role names come from each provider's own documentation (read 20 Nov 2026):

| Cloud | The line | Source |
|---|---|---|
| AWS | An IAM user or role that can create IAM roles and CloudFormation stacks. | Creating a stack needs `cloudformation:CreateStack` plus the permissions for the template's own resources. A template with IAM resources needs the `CAPABILITY_IAM` / `CAPABILITY_NAMED_IAM` acknowledgement. [Control CloudFormation access with IAM](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/control-access-with-iam.html) |
| Google Cloud | Billing Account Administrator (or Billing Account Costs Manager) to turn on the export, BigQuery Data Owner / BigQuery Admin to share the dataset, Project IAM Admin to grant the viewer role. | [Set up Cloud Billing data export to BigQuery](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-setup); [Control access to resources with IAM (BigQuery)](https://docs.cloud.google.com/bigquery/docs/control-access-to-resources-iam) |
| Azure | Owner, or Contributor plus User Access Administrator (or Role Based Access Control Administrator), to assign roles (`Microsoft.Authorization/roleAssignments/write`). Privileged Role Administrator for tenant-wide admin consent to an app that asks for any permission. | [Assign Azure roles using the portal](https://learn.microsoft.com/en-us/azure/role-based-access-control/role-assignments-portal); [Grant tenant-wide admin consent](https://learn.microsoft.com/en-us/entra/identity/enterprise-apps/grant-admin-consent) |
| DigitalOcean | A team Owner, or a Member who can approve apps. Owner and Member have full access to "API-authorized applications"; Biller and Billing Viewer have none. | [Predefined team roles](https://docs.digitalocean.com/platform/teams/roles/predefined/) |

The Google Cloud line keeps the brief's "Billing Account Administrator, or Project IAM Admin and BigQuery Admin". The docs also accept Billing Account Costs Manager for the standard export and BigQuery Data Owner for sharing a dataset. Both of those are narrower, so the line names the roles an admin most often holds.

"Copy link for a colleague" stays on the AWS step as the secondary option, renamed "Or copy the AWS link", for teams that would rather pass it on in their own chat.

## Consequences

- An admin connects without an account, and a forwarded link is only as good as its newest opener. Anyone the email reaches can use it within its 7 days. That is the same exposure as the one-click link people copied by hand before, but this link is single use, revocable, tied to its sender staying an admin, and audited.
- Every new route under `/v1/connect-invite` must use `invite_session` (except `/open`), and no route elsewhere may. `tests/test_connect_invites.py` reads the app's route table, as the membership test from #100 does. It fails on a session route without the dependency and on any other route that accepts the session (401 or 403 required). It also fails on a new route that needs no signed-in user unless that route is listed with a reason.
- The audit log gains an actor type (`connect_invite`), and the audit page shows "connect invite \<email\>".
- The Azure sign-in being built in parallel adds new connect routes. The invite session does not reach them until they are mounted under `/v1/connect-invite/session/azure/...` with `invite_session`. The route-table test will show which ones are missing.
