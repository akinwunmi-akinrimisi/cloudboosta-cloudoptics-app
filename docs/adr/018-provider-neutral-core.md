# ADR-018: Provider-neutral core

**Status:** Decided 19 Nov 2026 (founder approved Google Cloud, Azure and DigitalOcean as the next providers; multi-cloud plan, Phase 1)

## Context

Cloudoptics reads AWS only. The multi-cloud readiness review (19 Nov 2026) found no provider abstraction in the code: `cloud_connection.provider` existed (default `aws`, migration 0001) but nothing read it; account columns were `char(12)` with a 12-digit CHECK; bills in any currency other than USD were rejected; recommendation fingerprints and resource keys had no provider; and every job called the AWS modules directly. docs/09 §1.3.1 and docs/11b §1.2 had planned a `CloudProvider` interface and FOCUS as the canonical cost model, but neither was built.

Phase 1 prepares the ground so each new provider is a plug-in, without changing what AWS customers see or what the AWS code does.

## Decision

### Providers

| Name | Status | Account noun | Account ID |
|---|---|---|---|
| `aws` | live | account | 12 digits (CHECKed in the database) |
| `gcp` | next (Phase 2) | project | project ID |
| `azure` | next (Phase 3) | subscription | subscription GUID |
| `digitalocean` | next (Phase 4) | account (team) | team UUID |

The `provider` CHECK on every table lists exactly these four. Adding a fifth (Huawei, Oracle) is a migration that widens the CHECK plus a plug-in.

### Shared and per provider

| Shared core (one implementation) | Per provider (`providers/<name>`) |
|---|---|
| Workspaces, roles, invites, audit log, data deletion, exports | `connect`: validation and health of a connection |
| Cost tables (FOCUS-shaped, native currency and USD side by side) | `credentials`: short-lived access (no stored long-lived secrets) |
| Budgets, anomalies, forecasts, trends, credits, FX, reports, digests, notifications | `accounts`: the accounts, projects or subscriptions a connection covers |
| Scoring, deduplication, the recommendation workflow, verification, realised savings | `billing_source`: loading the bill into the cost tables |
| The rule registry and the engine loop | `reconcile`: month-to-date snapshot and the check of loaded bills |
| The job queue and its schedules | `inventory`, `metrics`, `native_recs`, `prices`, `commitments`, `rules` |

Every capability is optional. A provider without one is skipped by the job that needs it (the job returns `unsupported`), so a provider can start with costs only, as the plan expects for DigitalOcean.

### Schema (migrations 0042 and 0043)

- **Provider everywhere a row belongs to a cloud.** `provider text NOT NULL DEFAULT 'aws'` with the four-value CHECK on `cloud_connection` (CHECK added), `cloud_account`, `cost_daily`, `cost_resource_daily`, `cost_snapshot_ce`, `resource`, `recommendation`, `anomaly`, `commitment` and `price_catalog` (part of its primary key). Tables keyed by a connection or a resource ID only (`sync_run`, `ingest_batch`, `billing_file`, `cost_reconciliation`, `commitment_snapshot`, `resource_metric`) take the provider from their connection or resource and do not repeat it. `tag_set` is keyed by a hash and is not provider-specific. `aws_api_usage` stays AWS's paid-API counter; other providers get their own counters with their connectors.
- **Neutral account IDs.** `cloud_connection.aws_account_id`, `cloud_account.aws_account_id` and the fact tables' `account_id` (all `char(12)`) become `provider_account_id text`. The 12-digit CHECK now applies only `WHERE provider = 'aws'`; every provider's ID must be 1 to 128 characters. On the fact tables, which had no CHECK before, the AWS rule is added `NOT VALID`: it binds every new row, and existing rows came from writers that already enforced it.
- **Keys include the provider** wherever two providers' accounts could collide: `cloud_account` (tenant, provider, account), `resource` (tenant, provider, account, region, resource ID), `anomaly` (tenant, day, provider, account, service) and `price_catalog` (provider, region, item). `cloud_connection`'s two account keys, one per workspace and the one-verified-workspace-per-account partial index (ADR-017), already had the provider, so the rule now holds per provider.
- **Native billing currency.** `cost_daily`, `cost_resource_daily` and `cost_snapshot_ce` gain `billing_currency` (ISO 4217, default `USD`) and a native amount beside each `*_usd` amount (`billed_cost`, `effective_cost`, `net_cost`, `list_cost`, `credits`). A row-level trigger sets native = USD whenever the row is billed in USD, so AWS writers needed no change and the two can never disagree. A row in another currency must bring its own native amounts (NOT NULL). The internal reporting base stays USD (docs/09 §2.3.5); converting native bills to USD is Phase 2/3 work.
- **Currency rule per provider.** `ingestion/quality.py` blocks a bill whose currency is not in the provider's `billing_currencies`. AWS stays USD-only, with the same message as before.

### Resource types: provider in the key, names kept

`resource.resource_type` keeps its values (`ec2_instance`, `ebs_volume`...) and `provider` joins the unique key. The CHECK on the AWS names binds AWS rows only; each provider declares its own types in its plug-in (`CloudProvider.resource_types`), for example `gce_instance` for Google Cloud.

Namespacing the values (`aws.ec2_instance`) was the riskier option. The names are read in many places: every rule, the metrics map, verification, the commitments SQL and the bill-only resources whose IDs are derived from the type (`uuid5` in `analysis/billing.py`). Renaming would rewrite every `resource` row, change those derived IDs and touch each reader, for nothing the provider column does not already give.

### Fingerprints

A recommendation's fingerprint is now `<provider>:<rule>:<account>:<region>:<resource>`. Migration 0043 prefixes every stored fingerprint with its row's provider (`aws:`), and the engine writes the same form, so its next run updates each row in place: statuses, assignees, history, verification and realised savings are kept.

### Code

- `cloudoptics/providers`: `base.py` (the `CloudProvider` value, the capability protocols and `ProviderName`), `registry.py` (registration, lookup, `capability_for(tenant, connection, name)`), and `aws/` (the AWS plug-in and `AWS_RULES`).
- **AWS is wrapped, not moved.** Each AWS capability calls the module that has always done the work, looked up at call time. Behaviour, audit records and job results are unchanged, and the AWS-only modules can move under `providers/aws` one at a time later without touching the jobs.
- **Jobs dispatch by `cloud_connection.provider`**: validate and health check, account discovery, bill ingest and reload, snapshot and reconciliation, inventory scan, metrics pull and commitments refresh. A connection that is gone resolves to AWS, so the AWS code reports it exactly as before.
- **Rule registry** (`analysis/registry.py`): each rule is a `RuleSpec` with `id`, `provider`, `rule_ids`, `resource_types`, `version` and its function, replacing the four plain lists. The engine runs each provider's rules over that provider's resources only, in the same order as before. R8 is listed for its metadata and still runs after the loop.

### API and web app

Responses that expose connections, accounts, recommendations, anomalies, commitments and the cost breakdown by account gain a `provider` field, after the existing fields; nothing else in them changes. The web app has neutral wording helpers (`accountNoun`, `accountLabel`: "AWS account 123…", "Google Cloud project …") and shows a provider badge only when a workspace has more than one provider, so AWS-only workspaces look exactly as before. There are no new connect wizards in this phase.

## Migrating existing AWS data

1. **0042**: adds `provider` (default `aws`; a constant default rewrites nothing) and its CHECKs; renames and widens the account columns (`char(12)` to `text` rewrites those tables once, under a lock, for the length of the copy); rebuilds the keys listed above.
2. **0043**: adds the currency columns and the trigger, fills native = USD on every existing cost row, then sets NOT NULL; prefixes the fingerprints. The data fixes use the NO FORCE / fix / FORCE pattern, as the migration role is bound by FORCE RLS.

Both downgrades are lossless for AWS data, and they refuse to run while any row belongs to another provider or another currency, since `char(12)` and the AWS-only keys could not hold it. A test seeds AWS data at 0041, upgrades, checks every row, status and fingerprint, downgrades and compares the rows byte for byte.

**Deploying.** The renamed columns make the migration incompatible with the previous release's code. Stop the worker while the migration runs and roll the API straight after; requests served by the old API in that window fail. The backfill and the type change take time in proportion to the cost tables' size, which is small today. Run 0042/0043 before cost tables grow to millions of rows, or split the backfill into batches then.

## Consequences

- Phase 2 (Google Cloud) adds `providers/gcp` with `connect` and `credentials` (Workload Identity Federation from our AWS identity), `billing_source` (the BigQuery FOCUS export, writing `provider = 'gcp'`, the project as `provider_account_id`, and native and USD amounts), `reconcile`, `inventory`, `metrics`, `native_recs` (Recommender) and its rules as `RuleSpec`s; then registers it. The jobs, cost tables, budgets, anomalies, forecasts, recommendation workflow and data deletion need no change to carry its data.
- Still AWS-shaped, for the provider phases: the cost queries group by account ID alone (fine while IDs differ in form; group by provider and account once two providers share a workspace); the engine loads one AWS price book; `cloud_connection` keeps AWS columns (`role_arn`, `cur_bucket`, `stack_name`) that other providers leave unused or will move to a per-provider settings column; and the connect wizard, onboarding and CloudFormation flows stay AWS-only.
- Every new table keyed by a cloud account must carry `provider` and include it in its keys. The RLS lint, the every-table cross-tenant test and the deletion guards still apply as before.

## Alternatives

- **Keep the old column names and add `provider_account_id` as a generated or trigger-synced copy.** Rejected: two names for one value in every cost row, and the neutral name would be the read-only one.
- **Views with the old names for compatibility.** Rejected: the app writes these tables, and RLS, triggers and `ON CONFLICT` keys all work on the tables themselves.
- **Separate tables per provider** (`gcp_cost_daily`...). Rejected: budgets, anomalies, forecasts and exports would each need a union, and FOCUS exists so one table can hold every provider's bill.
- **Namespaced resource types.** Rejected for Phase 1; see above.
