# Release checklist: MVP beta (PRD §8)

**Status as of 19 November 2026** (Sprint 6, release candidate). Each criterion from [PRD §8](06_mvp_prd.md#8-release-criteria-mvp-beta) links to where it is satisfied or is marked **open** with what is missing. The beta go/no-go is at the end of Sprint 7 ([docs/11d](11d_implementation_roadmap_and_sprint_plan.md)); the operational readiness checklist ([11g §3.3](11g_runbooks_and_operational_readiness.md#33-operational-readiness-checklist-before-beta-end-of-s6)) is signed off at the same time.

Legend: **Done**, **Partly** (some evidence, gaps listed), **Open**.

## 1. PRD §8 release criteria

| # | Criterion | Status | Where satisfied / what is missing |
|---|---|---|---|
| 1a | All **M** requirements implemented and tested | **Partly** | Built and tested per sprint: [README "What works today"](../README.md#what-works-today-18-nov-2026), sprint status in [docs/11d](11d_implementation_roadmap_and_sprint_plan.md); CI runs unit and integration tests with coverage ≥ 80 % ([ci.yml](../.github/workflows/ci.yml)). Missing: a requirement-by-requirement trace of the PRD's M items to tests; FR-SEC-07 staff break-glass (WBS ADM-6, being built); E2E suite and load tests (docs/11f §1.4, §1.5) |
| 1b | No open Critical/High security findings | **Partly** | Security audit run 1 (Sprint 6): no confirmed findings; leads fixed in #76, #77, #78 and #83 ([11g RB-19](11g_runbooks_and_operational_readiness.md#rb-19-security-audit-follow-ups)). CI blocks on Trivy HIGH/CRITICAL, Semgrep ERROR, gitleaks, pip-audit and pnpm audit ([ci.yml](../.github/workflows/ci.yml)). Images signed and verified before deploy (OPS-9, [11g §4.4](11g_runbooks_and_operational_readiness.md#44-image-signing-and-the-admission-check-ops-9)). Open: the owner checks listed in RB-19 (WorkOS MFA settings, GitHub fork approvals and environment reviewers, Variables tab, bucket ownership, branch protection); a second audit run is planned for Sprint 7 |
| 2 | Reconciliation within ±1 % for all design-partner tenants for the last full month | **Open** | The check exists (cost reconciliation against the invoice: `apps/api/src/cloudoptics/ingestion/reconcile.py`, table `cost_reconciliation`). Needs design partners connected for a full closed month, then a review per tenant (RB-02) |
| 3 | Guided-fix snippets for R1–R8 validated against a test AWS account (incl. `af-south-1`) | **Open** | Fix and rollback steps ship with each recommendation (README). Needs the test AWS accounts from [docs/spikes/sprint-0.md](spikes/sprint-0.md) and a recorded run of each snippet, including `af-south-1` |
| 4 | Runbooks for connector failure, data discrepancy and incident response exist (Phase 11g) | **Done** (staging adaptation partly) | Connector failure [RB-01](11g_runbooks_and_operational_readiness.md#rb-01--handling-connector-failures), data discrepancy [RB-02](11g_runbooks_and_operational_readiness.md#rb-02--investigating-data-discrepancies), incident response [RB-10](11g_runbooks_and_operational_readiness.md#rb-10-incident-response-staging-practical) with [RB-05](11g_runbooks_and_operational_readiness.md#rb-05--on-call-escalation-flow) and [RB-06](11g_runbooks_and_operational_readiness.md#rb-06--security-event-summary). RB-01 and RB-02 are written for the production design (Roles Anywhere, `coctl`); on staging the broker is an IAM user (RB-09, RB-18) and the checks run from the UI and SQL |
| 5 | Privacy policy, terms, DPA and security overview published | **Partly** | Security overview: the public site's security page ([apps/site/src/pages/security.astro](../apps/site/src/pages/security.astro), ADR-016; publishing it is SITE-13). Open: privacy notice ("being prepared"), terms of service, DPA with NDPA, Kenya DPA and POPIA clauses, sub-processor list (legal dependency in docs/11d Sprint 6). The privacy notice must say the audit log is kept after a deletion (ADR-015) |
| 6 | Usability test (5+ participants across personas A–C) completed with no blocking issues | **Open** | Usability round 2 is a Sprint 6 dependency (docs/11d); no recorded results in the repository yet |

## 2. Operations items for this release (Sprint 6, docs/11d OPS-7 to OPS-10)

| Item | Status | Where |
|---|---|---|
| OPS-7 alert rules | **Done in code; live after the next Staging setup run** | `alerts` in [app.yaml](../infra/digitalocean/app.yaml); recipients and Uptime checks by `provision.py` from the `ALERT_EMAILS` secret ([11g §4.3](11g_runbooks_and_operational_readiness.md#43-alerting-as-built-ops-7)) |
| OPS-7 on-call rota | **Open: names needed** | [11g §4.2](11g_runbooks_and_operational_readiness.md#42-on-call-rota-ops-7-replaces-the-grafana-irm-plan-in-rb-05-for-now) has hours (WAT/EAT/SAST) and escalation with TODO names |
| OPS-8 backups and restore test | **Done in code; first run needs owner approval** | Daily DigitalOcean backups (7 days); [restore_test.py](../infra/digitalocean/restore_test.py) and the manual [Restore test workflow](../.github/workflows/restore-test.yml); [RB-11](11g_runbooks_and_operational_readiness.md#rb-11-database-restore-and-disaster-recovery). Open: first passing run this month; decision on 30-day / second-region backups |
| OPS-9 image signing | **Done** | cosign keyless signing and SBOM attestation in [container.yml](../.github/workflows/container.yml); verify before deploy in [deploy-staging.yml](../.github/workflows/deploy-staging.yml) (the Kyverno equivalent); [11g §4.4](11g_runbooks_and_operational_readiness.md#44-image-signing-and-the-admission-check-ops-9) |
| OPS-10 scaling, limits, budget | **Done (documented; no autoscaling on App Platform shared sizes)** | [11g §4.5](11g_runbooks_and_operational_readiness.md#45-worker-scaling-resource-limits-and-our-cost-budget-ops-10-equivalent): sizing against `CLOUDOPTICS_WORKER_CONCURRENCY`, queue-depth queries, budget $50/month |
| Runbooks (11g) | **Done for staging** | RB-08 to RB-19 in [11g §4](11g_runbooks_and_operational_readiness.md#4-staging-on-app-platform-runbooks-as-built-sprint-6-19-nov-2026) |

## 3. Owner actions before the release candidate goes to the alpha partners

1. Name the primary, secondary and escalation contacts in 11g §4.2, and make each a DigitalOcean team member.
2. Add the `ALERT_EMAILS` secret to the GitHub environment Staging setup uses, then run **Actions → Staging setup** (applies the alert rules, recipients and Uptime checks; also rotates the AWS broker key).
3. In the DigitalOcean console, add database alerts (CPU > 80 %, disk > 75 %) and a billing alert at $40.
4. Approve and run the first restore test (**Actions → Restore test**, type `restore`; about an hour of a $15/month cluster).
5. Complete the RB-19 owner checks, including branch protection on `main`.
6. Legal documents (criterion 5) and the usability round (criterion 6).
