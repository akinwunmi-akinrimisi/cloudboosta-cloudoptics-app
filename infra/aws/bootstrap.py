"""Set up Cloudoptics' own AWS account for staging (ADR-007, ADR-013). Idempotent.

Creates:
  * S3 bucket ``cloudoptics-public-<account>`` (eu-west-2): public read of ``cfn/v1/*`` only,
    TLS-only, versioned; uploads the customer CloudFormation templates there.
  * IAM role ``CloudopticsBroker``: the principal in customers' trust policies. It may only
    assume the two Cloudoptics read-only roles in customer accounts.
  * IAM user ``cloudoptics-staging-broker``: may only assume ``CloudopticsBroker``. Its access
    key is rotated on every run (new key created, older keys deleted) and handed to the
    caller; IAM Roles Anywhere replaces the user on Kubernetes (spike CC-1).

Runs in GitHub Actions with an admin key (``staging-setup.yml``). Outputs go to
``$GITHUB_ENV`` (secrets masked) for the next step, or are printed without secrets locally.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

REGION = "eu-west-2"
ROLE = "CloudopticsBroker"
USER = "cloudoptics-staging-broker"
TEMPLATES = Path(__file__).resolve().parents[1] / "cloudformation"
TEMPLATE_PREFIX = "cfn/v1"
CUSTOMER_ROLES = ("CloudopticsBillingReadOnly", "CloudopticsResourceReadOnly")
TAGS = [{"Key": "app", "Value": "cloudoptics"}, {"Key": "env", "Value": "staging"}]


def bucket_policy(bucket: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadTemplatesOnly",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{bucket}/{TEMPLATE_PREFIX}/*",
            },
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
        ],
    }


def broker_trust_policy(account: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account}:user/{USER}"},
                "Action": "sts:AssumeRole",
            }
        ],
    }


def broker_permissions() -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AssumeCustomerReadOnlyRolesOnly",
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Resource": [f"arn:aws:iam::*:role/{r}" for r in CUSTOMER_ROLES],
            }
        ],
    }


def user_permissions(account: str) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AssumeBrokerOnly",
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Resource": f"arn:aws:iam::{account}:role/{ROLE}",
            }
        ],
    }


def ensure_bucket(s3: Any, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError:
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
        print(f"created bucket {bucket}")
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": TAGS})
    # Allow a public *policy* (templates only); keep ACLs blocked.
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": False,
            "RestrictPublicBuckets": False,
        },
    )
    s3.put_bucket_policy(Bucket=bucket, Policy=json.dumps(bucket_policy(bucket)))


def upload_templates(s3: Any, bucket: str) -> dict[str, str]:
    urls = {}
    for name in ("billing-role.yaml", "resource-role.yaml", "resource-stackset.yaml"):
        key = f"{TEMPLATE_PREFIX}/{name}"
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=(TEMPLATES / name).read_bytes(),
            ContentType="text/yaml",
            CacheControl="max-age=300",
        )
        urls[name] = f"https://{bucket}.s3.{REGION}.amazonaws.com/{key}"
    print(f"uploaded templates to s3://{bucket}/{TEMPLATE_PREFIX}/")
    return urls


def ensure_user(iam: Any) -> None:
    # The broker role's trust policy names this user, so it must exist first.
    try:
        iam.get_user(UserName=USER)
    except iam.exceptions.NoSuchEntityException:
        iam.create_user(UserName=USER, Tags=TAGS)
        print(f"created user {USER}")


def retry_while_propagating(call: Any, attempts: int = 12, delay: float = 5.0) -> Any:
    """IAM is eventually consistent: a new user can be rejected as a principal for a while."""
    for attempt in range(attempts):
        try:
            return call()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code != "MalformedPolicyDocument" or attempt == attempts - 1:
                raise
            time.sleep(delay)
    return None


def ensure_role(iam: Any, account: str) -> str:
    trust = json.dumps(broker_trust_policy(account))
    try:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
        retry_while_propagating(
            lambda: iam.update_assume_role_policy(RoleName=ROLE, PolicyDocument=trust)
        )
    except iam.exceptions.NoSuchEntityException:
        arn = retry_while_propagating(
            lambda: iam.create_role(
                RoleName=ROLE,
                AssumeRolePolicyDocument=trust,
                Description="Cloudoptics broker: assumes customers' read-only roles only",
                MaxSessionDuration=3600,
                Tags=TAGS,
            )
        )["Role"]["Arn"]
        print(f"created role {ROLE}")
    iam.put_role_policy(
        RoleName=ROLE,
        PolicyName="assume-customer-read-only-roles",
        PolicyDocument=json.dumps(broker_permissions()),
    )
    return str(arn)


def rotate_user_key(iam: Any, account: str) -> dict[str, str]:
    iam.put_user_policy(
        UserName=USER,
        PolicyName="assume-broker-only",
        PolicyDocument=json.dumps(user_permissions(account)),
    )
    existing = iam.list_access_keys(UserName=USER)["AccessKeyMetadata"]
    # IAM allows two keys; make room before creating the new one.
    for meta in sorted(existing, key=lambda m: m["CreateDate"])[: max(0, len(existing) - 1)]:
        iam.delete_access_key(UserName=USER, AccessKeyId=meta["AccessKeyId"])
    key = iam.create_access_key(UserName=USER)["AccessKey"]
    for meta in iam.list_access_keys(UserName=USER)["AccessKeyMetadata"]:
        if meta["AccessKeyId"] != key["AccessKeyId"]:
            iam.delete_access_key(UserName=USER, AccessKeyId=meta["AccessKeyId"])
    print(f"rotated access key for {USER}")
    return {"id": key["AccessKeyId"], "secret": key["SecretAccessKey"]}


def emit(outputs: dict[str, str], secret_keys: set[str]) -> None:
    env_file = os.environ.get("GITHUB_ENV")
    if not env_file:
        for k, v in outputs.items():
            print(f"{k}={'***' if k in secret_keys else v}")
        return
    with open(env_file, "a") as fh:
        for k, v in outputs.items():
            if k in secret_keys:
                print(f"::add-mask::{v}")
            fh.write(f"{k}={v}\n")


def main() -> int:
    session = boto3.Session(region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    print(f"AWS account {account}")
    s3, iam = session.client("s3"), session.client("iam")
    bucket = f"cloudoptics-public-{account}"
    ensure_bucket(s3, bucket)
    urls = upload_templates(s3, bucket)
    ensure_user(iam)
    role_arn = ensure_role(iam, account)
    key = rotate_user_key(iam, account)
    emit(
        {
            "CLOUDOPTICS_AWS_BROKER_ROLE_ARN": role_arn,
            "CLOUDOPTICS_CFN_BILLING_TEMPLATE_URL": urls["billing-role.yaml"],
            "CLOUDOPTICS_CFN_RESOURCE_TEMPLATE_URL": urls["resource-role.yaml"],
            "BROKER_AWS_ACCESS_KEY_ID": key["id"],
            "BROKER_AWS_SECRET_ACCESS_KEY": key["secret"],
        },
        secret_keys={"BROKER_AWS_SECRET_ACCESS_KEY"},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
