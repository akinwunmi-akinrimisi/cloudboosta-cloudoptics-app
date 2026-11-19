# ADR-015: Data deletion removes workspace data and keeps the audit log

**Status:** Decided 19 Nov 2026 (WBS ADM-7)

## Context

PRD US-8.3 and NFR-CMP-04 require that an admin can delete a disconnected AWS account's data, and that a workspace deletion removes all of its data within 30 days, "except billing/legal records". docs/10 A5 asks for a certificate of deletion; docs/08 §1.7 asks for a typed confirmation and a 7-day grace period for the workspace. The audit log (docs/09 §1.5) is append-only and hash-chained, and docs/09 §2.4 keeps it at least 12 months online and 3 years archived. Customers in Nigeria, Kenya and South Africa (NDPA, Kenya DPA, POPIA) can ask for erasure, and the same laws allow keeping records needed for legal obligations and security.

## Decision

- **Connection data** is deleted straight away when an admin asks, once the connection is disconnected and the admin has typed its AWS account ID. The connection row goes, and with it (by `ON DELETE CASCADE`) its accounts, sync runs, bills, cost tables, resources, metrics, commitments and setup links; its recommendations (with their history) and its accounts' anomalies are deleted explicitly, and tag sets no cost row uses any more are removed. Workspace-wide rows stay: members, budgets, settings, monthly reports and the email log.
- **A workspace** is deleted after a grace period (`CLOUDOPTICS_TENANT_DELETION_GRACE_DAYS`, 7 by default, at most 21 so the 30-day promise holds with a margin). Any admin can cancel until then. The worker job `privacy:deletion.run` deletes the `tenant` row, and every workspace table goes with it by cascade. Admins get an email when it is scheduled, cancelled and completed. There is no owner role, so admins can do this.
- **Verification:** every deletion counts the rows of each affected table, in the workspace's RLS scope, before and after. A count above zero afterwards rolls the whole transaction back and the job retries. The counts are stored in `data_deletion.verification` and written to the audit log (`connection.data_deleted`, `tenant.data_deleted`). That record is the certificate of deletion.
- **The audit log is kept, unchanged.** Its rows are not deleted or redacted: deleting breaks the tamper evidence that is its purpose, and redacting the hashed fields breaks the chain the same way. It is kept as a legal and security record for its retention period, then removed by the retention purge (docs/09 §2.4, not built yet). The workspace's deletion records (`data_deletion`) are kept too; they hold the AWS account ID or workspace name and the counts, and no personal data beyond the IDs of the users who asked and cancelled.
- **User accounts** (`app_user`) are not workspace data: a person may belong to other workspaces. Deleting a workspace removes their membership; deleting a person's own account is a separate request, not covered here.
- **The customer's AWS accounts are theirs.** Cloudoptics has read-only access and deletes nothing there. Both flows list the CloudFormation stacks each account has (and the export bucket AWS keeps), so the customer can remove them.
- A test compares `privacy.deletion.TABLES` with every table that has a `tenant_id` column after all migrations, and checks each one cascades from `tenant` (and, for tables with a `connection_id`, from `cloud_connection`). A new table fails CI until the deletion code covers it.

## Consequences

- The audit log of a deleted workspace still holds actor IDs, IP addresses, user agents and the before/after values of past changes (for example invited email addresses) until its retention ends. The privacy notice should say so.
- Backups keep deleted data until they age out (30 days, docs/09 §2.4). Restoring a backup would bring a deleted workspace back; the restore runbook must re-apply completed `data_deletion` records (docs/10 T13).
- Normalised Parquet in Spaces (not used on staging yet) is not deleted by this job; when object storage is in use, the job must also delete the workspace's `tenant=<id>/` prefix. Locally the `normalised_dir` folder is removed.
- Monthly reports keep the totals they were made with, including a deleted connection's costs, until the workspace itself is deleted.

## Alternatives

- **Redact personal fields in the audit log.** Rejected: it breaks the hash chain and the log's value as evidence, and the laws above allow keeping security records.
- **Soft delete (a `deleted` status).** Rejected: the data would still be there, which is not what the customer asked for.
- **Delete each table with its own statement instead of by cascade.** Rejected for now: the cascades already exist on every table, and the coverage test enforces them for new tables.
