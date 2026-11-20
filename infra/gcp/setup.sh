#!/usr/bin/env bash
# Cloudoptics' own Google Cloud side of the GCP connector (ADR-019). Run by the owner, once
# per environment, in Cloud Shell or with gcloud signed in as a project owner. Nothing here
# touches a customer's project. It is idempotent: re-running it changes nothing that exists.
#
#   PROJECT_ID=cloudoptics-staging AWS_ACCOUNT_ID=<broker account> ./infra/gcp/setup.sh
#
# What it creates in PROJECT_ID (a project of Cloudoptics' own, with no customer data):
#   1. The APIs the token exchange needs: IAM, IAM Credentials, STS.
#   2. A workload identity pool "cloudoptics" with an AWS provider "aws-broker" that admits
#      only the broker role (CloudopticsBroker, ADR-007) of AWS_ACCOUNT_ID.
#   3. The service account "cloudoptics-reader" customers grant read roles to. It has no
#      roles in this project and no keys (key creation stays blocked: see step 5).
#   4. Lets identities from the pool that are the broker role impersonate that account.
#   5. Prints the two settings to put on the API and worker:
#        CLOUDOPTICS_GCP_WORKLOAD_PROVIDER and CLOUDOPTICS_GCP_SERVICE_ACCOUNT.
#
# Cost: none of these resources is billed. BigQuery queries run in each customer's project.
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID (Cloudoptics' own Google Cloud project)}"
: "${AWS_ACCOUNT_ID:?set AWS_ACCOUNT_ID (the 12-digit broker account)}"
BROKER_ROLE="${BROKER_ROLE:-CloudopticsBroker}"
POOL="${POOL:-cloudoptics}"
PROVIDER="${PROVIDER:-aws-broker}"
SA_NAME="${SA_NAME:-cloudoptics-reader}"

if ! [[ "$AWS_ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then
  echo "AWS_ACCOUNT_ID must be 12 digits" >&2
  exit 1
fi

gcloud config set project "$PROJECT_ID" >/dev/null
NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "1/4 APIs"
gcloud services enable iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudresourcemanager.googleapis.com

echo "2/4 Workload identity pool and AWS provider"
if ! gcloud iam workload-identity-pools describe "$POOL" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools create "$POOL" --location=global \
    --display-name="Cloudoptics" --description="Cloudoptics workers, from the AWS broker"
fi
# The condition admits the broker role's sessions only (Roles Anywhere sessions of
# CloudopticsBroker), not any other identity in the broker account.
CONDITION="attribute.aws_role == 'arn:aws:sts::${AWS_ACCOUNT_ID}:assumed-role/${BROKER_ROLE}'"
MAPPING="google.subject=assertion.arn,attribute.aws_account=assertion.account,attribute.aws_role=assertion.arn.extract('{account_arn}assumed-role/') + 'assumed-role/' + assertion.arn.extract('assumed-role/{role_name}/')"
if ! gcloud iam workload-identity-pools providers describe "$PROVIDER" \
    --workload-identity-pool="$POOL" --location=global >/dev/null 2>&1; then
  gcloud iam workload-identity-pools providers create-aws "$PROVIDER" \
    --workload-identity-pool="$POOL" --location=global --account-id="$AWS_ACCOUNT_ID" \
    --attribute-mapping="$MAPPING" --attribute-condition="$CONDITION"
fi

echo "3/4 Service account (no keys, no roles here)"
if ! gcloud iam service-accounts describe "$SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" --display-name="Cloudoptics (read-only)" \
    --description="Customers grant this account read roles on their billing export and projects"
fi

echo "4/4 The broker role may impersonate it"
MEMBER="principalSet://iam.googleapis.com/projects/${NUMBER}/locations/global/workloadIdentityPools/${POOL}/attribute.aws_role/arn:aws:sts::${AWS_ACCOUNT_ID}:assumed-role/${BROKER_ROLE}"
gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --role=roles/iam.workloadIdentityUser --member="$MEMBER" --condition=None >/dev/null

cat <<OUT

Done. Set these on the API and worker (they are not secrets):
  CLOUDOPTICS_GCP_WORKLOAD_PROVIDER=//iam.googleapis.com/projects/${NUMBER}/locations/global/workloadIdentityPools/${POOL}/providers/${PROVIDER}
  CLOUDOPTICS_GCP_SERVICE_ACCOUNT=${SA}

Recommended once, at the organization or project: the constraint
iam.disableServiceAccountKeyCreation, so nobody can ever create a key for ${SA}.
OUT
