#!/usr/bin/env bash
# Cloudoptics' own Google Cloud side of the GCP connector (ADR-019). Run by the owner, once
# per environment, in Cloud Shell or with gcloud signed in as a project owner. Nothing here
# touches a customer's project. It is idempotent: re-running it brings what exists to the state
# below.
#
#   PROJECT_ID=cloudoptics-staging AWS_ACCOUNT_ID=<broker account> ./infra/gcp/setup.sh
#
# PROJECT_ID should be a project of Cloudoptics' own that holds nothing else: every service
# account in it can be impersonated by the broker and can run BigQuery jobs there (below).
#
# What it creates in PROJECT_ID:
#   1. The APIs: IAM, IAM Service Account Credentials, Security Token Service, Resource
#      Manager and BigQuery (Cloudoptics' queries run here, at our cost).
#   2. A workload identity pool "cloudoptics" with an AWS provider "aws-broker" that admits
#      only the broker role (CloudopticsBroker, ADR-007) of AWS_ACCOUNT_ID.
#   3. Project roles for the broker's federated identity (the pool's principal set):
#        - Create Service Accounts and Delete Service Accounts: one account per connection
#          (co-<12 hex>), made when a customer connects and deleted when they disconnect;
#        - Workload Identity User: impersonate those accounts. Granted on the project, not per
#          account, so Cloudoptics needs no right to change IAM policies (see ADR-019).
#   4. BigQuery Job User for every service account of the project (the principal set
#      principalSet://cloudresourcemanager.googleapis.com/projects/<number>/type/
#      ServiceAccount), so the per-connection accounts run queries here and customers grant
#      no Job User. Nothing is written per connection.
#   5. Prints the settings to put on the API and worker:
#        CLOUDOPTICS_GCP_WORKLOAD_PROVIDER and CLOUDOPTICS_GCP_SERVICE_ACCOUNT_PROJECT.
#
# Cost: the pool, provider and service accounts are free. Queries are billed to this project
# at BigQuery on-demand prices (the first TiB a month is free); cap them with the custom quotas
# printed at the end.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID (Cloudoptics' own Google Cloud project)}"
: "${AWS_ACCOUNT_ID:?set AWS_ACCOUNT_ID (the 12-digit broker account)}"
BROKER_ROLE="${BROKER_ROLE:-CloudopticsBroker}"
POOL="${POOL:-cloudoptics}"
PROVIDER="${PROVIDER:-aws-broker}"

if ! [[ "$AWS_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "AWS_ACCOUNT_ID must be 12 digits" >&2
  exit 1
fi

gcloud config set project "$PROJECT_ID" >/dev/null
NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"

echo "1/4 APIs"
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudresourcemanager.googleapis.com bigquery.googleapis.com

echo "2/4 Workload identity pool and AWS provider"
if ! gcloud iam workload-identity-pools describe "$POOL" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL" --location=global \
    --display-name="Cloudoptics" --description="Cloudoptics workers, from the AWS broker"
fi
# Google's documented AWS mapping (Workload Identity Federation with AWS): the subject is the
# caller's ARN and attribute.aws_role is the role *name* taken from the assumed-role ARN
# (arn:aws:sts::<account>:assumed-role/<role>/<session>). The provider only accepts callers
# from AWS_ACCOUNT_ID (--account-id), and the condition admits sessions of the broker role
# only (Roles Anywhere sessions of CloudopticsBroker), not any other identity in that account.
# If CLOUDOPTICS_GCP_FEDERATION_ROLE_ARN names a dedicated role, set BROKER_ROLE to its name.
MAPPING="google.subject=assertion.arn,attribute.account=assertion.account,attribute.aws_role=assertion.arn.extract('assumed-role/{role_name}/')"
CONDITION="assertion.arn.startsWith('arn:aws:sts::${AWS_ACCOUNT_ID}:assumed-role/${BROKER_ROLE}/')"
if ! gcloud iam workload-identity-pools providers describe "$PROVIDER" \
    --workload-identity-pool="$POOL" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-aws "$PROVIDER" \
    --workload-identity-pool="$POOL" --location=global --account-id="$AWS_ACCOUNT_ID" \
    --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION"
else
  # Re-running brings an existing provider to the current mapping and condition.
  gcloud iam workload-identity-pools providers update-aws "$PROVIDER" \
    --workload-identity-pool="$POOL" --location=global \
    --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION"
fi

echo "3/4 The broker creates, deletes and impersonates the per-connection accounts"
BROKER="principalSet://iam.googleapis.com/projects/${NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.aws_role/${BROKER_ROLE}"
for ROLE in roles/iam.serviceAccountCreator roles/iam.serviceAccountDeleter \
    roles/iam.workloadIdentityUser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="$BROKER" --role="$ROLE" \
    --condition=None >/dev/null
done

echo "4/4 Every service account of the project may run BigQuery jobs here"
ACCOUNTS="principalSet://cloudresourcemanager.googleapis.com/projects/${NUMBER}/type/ServiceAccount"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="$ACCOUNTS" \
  --role=roles/bigquery.jobUser --condition=None >/dev/null

cat <<OUT

Done. Set these on the API and worker (they are not secrets):
  CLOUDOPTICS_GCP_WORKLOAD_PROVIDER=//iam.googleapis.com/projects/${NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}
  CLOUDOPTICS_GCP_SERVICE_ACCOUNT_PROJECT=${PROJECT_ID}
  (CLOUDOPTICS_GCP_SERVICE_ACCOUNT is no longer read; remove it.)

Then, in the console, for ${PROJECT_ID} (docs/11g, Google Cloud runbook):
  - IAM & Admin, Quotas: check "Service Account Count" (one account per Google Cloud
    connection; deleted accounts do not count) and request more before it runs out.
  - IAM & Admin, Quotas: set the BigQuery custom quotas "Query usage per day" (all of
    Cloudoptics) and "Query usage per day per user" (one connection's account), so a large or
    hostile export cannot run up the bill.
  - Recommended: the organization policy iam.disableServiceAccountKeyCreation, so no key can
    ever be created for these accounts.
OUT
