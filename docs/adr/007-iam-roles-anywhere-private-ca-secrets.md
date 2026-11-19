# ADR-007: IAM Roles Anywhere with AWS Private CA; Secrets Manager and KMS

**Status:** Accepted for staging with the amendment below (19 Nov 2026). The original
decision (Sprint 0 spike) is kept as written; the amendment replaces AWS Private CA with a
self-managed CA on staging.

## Context

Workers on DigitalOcean must assume customer roles without long-lived AWS keys (threat T2).

## Decision

A broker AWS account. Workloads get short-lived X.509 certificates from AWS Private CA (short-lived certificate mode) and exchange them through IAM Roles Anywhere for ≤ 1 h credentials, then call `sts:AssumeRole` with the tenant's External ID. Secrets sit in AWS Secrets Manager (`eu-west-2`), synced by External Secrets Operator; envelope encryption with KMS.

## Consequences

No static keys on DigitalOcean. If the spike fails, fall back to an IAM user with keys rotated every ≤ 90 days and tight IP conditions.

## Alternatives considered

IAM user with rotated keys; a small AWS-hosted assume-role broker.

## Amendment (19 Nov 2026): self-managed CA on staging, KMS envelope encryption built

### What was built

**Broker credentials.** `connectors/rolesanywhere.py` signs `CreateSession` with the
workload's certificate and key, following the Roles Anywhere signing process
(`AWS4-X509-RSA-SHA256` / `AWS4-X509-ECDSA-SHA256`). boto3 has no client for it and nothing in
the lockfile does it, so it is implemented in the app; tests check it against botocore's own
SigV4 canonical request and a fixed key, certificate and time. `SessionFactory` fetches
1-hour broker credentials, renews them five minutes before they expire, and assumes customer
roles with their External ID as before. The profile maps straight to `CloudopticsBroker` (the
principal customers trust), so there is no extra role hop. The IAM user key remains as an
explicit fallback: `CLOUDOPTICS_BROKER_CREDENTIALS=default_chain`. With `auto` (the default)
Roles Anywhere is used as soon as a certificate is configured.

**Envelope encryption.** `connectors/crypto.py` has `KmsEnvelopeEncryptor`: per-tenant
256-bit data keys from `kms:GenerateDataKey` on a symmetric key in the broker account
(`eu-west-2`), AES-256-GCM, the encrypted data key stored with each ciphertext (format
version `0x02`). The KMS encryption context is `{tenant_id, purpose}` and the AES associated
data binds the header and the tenant, so a ciphertext copied to another tenant's row fails at
KMS and at AES. Data keys are cached in memory for 5 minutes, at most 1,000 records per data
key and 1,024 keys. Version `0x01` ciphertexts (the old local key) are still read; they are
rewritten under KMS when a connection is read inside a transaction (validation, health
checks, setup page, scans), by the hourly job `connectors:secrets.reencrypt`, and by
`python -m cloudoptics.connectors.reencrypt` (`--check` counts only). The key is used through
the broker credentials; its policy lets only `CloudopticsBroker` call `GenerateDataKey` and
`Decrypt`, and only with both context keys present.

Secrets Manager and External Secrets Operator stay as written for the Kubernetes phase
(ADR-008); App Platform keeps its own encrypted environment variables.

### CA choice: self-managed, not AWS Private CA

| Option | Cost | Notes |
|---|---|---|
| AWS Private CA, general-purpose mode | $400 a month per CA, plus $0.75 a certificate | Any validity |
| AWS Private CA, short-lived mode | $50 a month per CA, plus $0.058 a certificate | Certificates ≤ 7 days, needs an issuer running beside the app |
| **Self-managed CA (chosen for staging)** | $0 | Roles Anywhere accepts any CA certificate as a trust anchor |

Roles Anywhere itself has no charge. Staging cannot run a certificate issuer next to the app
(App Platform has no sidecars), so a 7-day Private CA certificate would need a redeploy at
least weekly anyway; the cost buys nothing here.

How the self-managed CA works (`infra/aws/bootstrap.py`, run by Staging setup):

- Two trust anchors, `cloudoptics-staging-a` and `-b`. Each run generates a new CA key pair,
  puts the CA certificate into the anchor that was updated least recently, issues one leaf
  certificate (EC P-256, `CN=cloudoptics-staging-broker`, digital signature, client auth,
  valid `certificate_days`, default 30), and **discards the CA private key**. No CA key is
  stored anywhere, so there is nothing to steal or rotate; revoking means replacing or
  disabling an anchor.
- The other anchor still holds the CA of the previous run's certificate, so the running app
  keeps working until the new deployment is live (no gap, unlike the IAM user key rotation).
- `CloudopticsBroker` trusts `rolesanywhere.amazonaws.com` only when `aws:SourceArn` is one
  of the two anchors, `aws:SourceAccount` is ours, and the certificate subject CN matches.
- The leaf key is an App Platform secret (`CLOUDOPTICS_ROLES_ANYWHERE_PRIVATE_KEY`, base64
  PEM) and is masked in the workflow log. It is a credential for at most `certificate_days`
  that only yields 1-hour sessions, against the IAM user key that stayed valid until the next
  manual run.

Trade-off: certificates live up to 30 days, not ≤ 24 h as threat T2 in docs/10 asks for
production. Production (DOKS, ADR-008) should issue ≤ 24 h certificates from a sidecar or init
job; the app code is the same.

### Owner steps, in order (nothing here has been applied)

Each step is one run of **Actions → Staging setup** from `main`, with the inputs shown. The AWS
admin key in that environment needs `kms:*` and `rolesanywhere:*` in addition to what it has.

KMS:

1. **Create the key, KMS optional.** `kms=optional`, `broker_credentials=iam-user`. Creates
   `alias/cloudoptics-staging-secrets` (yearly rotation) and deploys with
   `CLOUDOPTICS_KMS_KEY_ID`. New secrets are encrypted under KMS; old ones are still read.
   Cost: $1 a month for the key (rising to at most $3 after two yearly rotations); requests
   stay inside the 20,000-a-month free tier at staging volumes.
2. **Re-encrypt.** The hourly job does it on its own. To do it at once and see the count, in
   the App Platform console of the `worker` component:
   `python -m cloudoptics.connectors.reencrypt`, then `python -m cloudoptics.connectors.reencrypt --check`
   until it prints `"connections": 0, "links": 0` and exits 0. Connection links of a
   workspace that has no connection yet are re-encrypted when used, and expire after 7 days.
3. **Require KMS.** `kms=required`. The API and worker now refuse to start without the key.
   `CLOUDOPTICS_LOCAL_DATA_KEY` stays on the app to read anything missed; removing it from
   `app.yaml` is a later, separate change once `--check` has stayed at 0 for 7 days.

Broker credentials:

4. **Create the trust anchors and deploy a certificate.** `broker_credentials=roles-anywhere`
   (keep `kms` at its current value). Creates both anchors and the profile
   `cloudoptics-staging-broker`, adds Roles Anywhere to the broker role's trust policy
   (beside the user), issues the certificate and deploys with
   `CLOUDOPTICS_BROKER_CREDENTIALS=roles_anywhere`. The user's key stays on the app, unused.
5. **Check it.** On staging, Check a connection; in the broker account's CloudTrail, look for
   `CreateSession` from `rolesanywhere.amazonaws.com` and `AssumeRole` by
   `CloudopticsBroker` sessions named after the certificate serial. If it fails, run with
   `broker_credentials=iam-user` to switch back (a fresh user key is issued).
6. **Delete the IAM user.** `broker_credentials=roles-anywhere`, `delete_broker_user=true`.
   Removes the user from the trust policy, deletes its keys and the user, and takes
   `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` off the app. After this, `iam-user` recreates
   the user if ever needed.
7. **Keep rotating.** Re-run step 4's inputs before `certificate_days` runs out (every 3–4
   weeks with the default 30). The worker logs `roles_anywhere.certificate_expiring` in the
   last 7 days, and every AWS call fails once it has expired.

## References

PRD FR-CON-05, docs/10, docs/spikes/sprint-0.md (CC-1), docs/11g RB-09 and RB-20,
https://docs.aws.amazon.com/rolesanywhere/latest/userguide/authentication-sign-process.html,
https://docs.aws.amazon.com/rolesanywhere/latest/userguide/trust-model.html
