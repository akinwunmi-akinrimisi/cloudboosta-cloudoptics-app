"""Set up Cloudoptics' own AWS account for staging (ADR-007, ADR-013). Idempotent.

Creates:
  * S3 bucket ``cloudoptics-public-<account>`` (eu-west-2): public read of ``cfn/v1/*`` only,
    TLS-only, versioned; uploads the customer CloudFormation templates there.
  * IAM role ``CloudopticsBroker``: the principal in customers' trust policies. It may only
    assume the two Cloudoptics read-only roles in customer accounts.
  * IAM user ``cloudoptics-staging-broker``: may only assume ``CloudopticsBroker``. Its access
    key is rotated on every run (new key created, older keys deleted) and handed to the
    caller; IAM Roles Anywhere replaces the user on Kubernetes (spike CC-1).

With inputs (environment variables the Staging setup workflow sets from its inputs):

  * ``KMS_MODE=optional|required``: KMS key ``alias/cloudoptics-staging-secrets`` (symmetric,
    yearly rotation). Its policy lets only the broker role generate and decrypt data keys,
    and only with a tenant in the encryption context. ``required`` makes the API and worker
    refuse to start without it: use it once the re-encryption job has finished.
  * ``BROKER_CREDENTIALS=roles-anywhere``: IAM Roles Anywhere instead of the user's key. Two
    trust anchors (``cloudoptics-staging-a``/``-b``) hold self-managed CA certificates. Each
    run puts a new CA into the anchor updated least recently, issues a leaf certificate valid
    for ``CERTIFICATE_DAYS`` (default 30) and discards the CA key, so no CA key is stored
    anywhere, and the certificate the previous run deployed (on the other anchor) keeps
    working until this run's deployment replaces it. A profile maps to the broker role,
    whose trust policy accepts Roles Anywhere only through the two anchors, for the leaf's
    subject.
  * ``DELETE_BROKER_USER=true`` (with ``roles-anywhere``): deletes the IAM user and its keys
    and drops it from the broker role's trust policy. Only once Roles Anywhere works.

Runs in GitHub Actions with an admin key (``staging-setup.yml``). Outputs go to
``$GITHUB_ENV`` (secrets masked) for the next step, or are printed without secrets locally.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

REGION = "eu-west-2"
ROLE = "CloudopticsBroker"
USER = "cloudoptics-staging-broker"
TEMPLATES = Path(__file__).resolve().parents[1] / "cloudformation"
TEMPLATE_PREFIX = "cfn/v1"
CUSTOMER_ROLES = (
    "CloudopticsBillingReadOnly",
    "CloudopticsResourceReadOnly",
    # The single onboarding stack (cloudoptics-access.yaml).
    "CloudopticsBillingReader",
    "CloudopticsResourceReader",
)
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


KMS_ALIAS = "alias/cloudoptics-staging-secrets"
TRUST_ANCHORS = ("cloudoptics-staging-a", "cloudoptics-staging-b")
PROFILE = "cloudoptics-staging-broker"
LEAF_CN = "cloudoptics-staging-broker"
RA_TAGS = [{"key": t["Key"], "value": t["Value"]} for t in TAGS]


def broker_trust_policy(
    account: str, *, user: bool = True, anchor_arns: list[str] | None = None
) -> dict[str, Any]:
    """Who may become the broker: the IAM user (the fallback) and/or Roles Anywhere."""
    statements: list[dict[str, Any]] = []
    if user:
        statements.append(
            {
                "Sid": "BrokerUser",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account}:user/{USER}"},
                "Action": "sts:AssumeRole",
            }
        )
    if anchor_arns:
        statements.append(
            {
                "Sid": "RolesAnywhere",
                "Effect": "Allow",
                "Principal": {"Service": "rolesanywhere.amazonaws.com"},
                "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
                "Condition": {
                    "StringEquals": {
                        "aws:PrincipalTag/x509Subject/CN": LEAF_CN,
                        "aws:SourceAccount": account,
                    },
                    "ArnEquals": {"aws:SourceArn": sorted(anchor_arns)},
                },
            }
        )
    if not statements:
        raise ValueError("the broker role must trust the user or Roles Anywhere")
    return {"Version": "2012-10-17", "Statement": statements}


def kms_key_policy(account: str, broker_role_arn: str) -> dict[str, Any]:
    """The account administers the key; only the broker role uses it, and only per tenant."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AccountAdministers",
                "Effect": "Allow",
                "Principal": {"AWS": f"arn:aws:iam::{account}:root"},
                "Action": "kms:*",
                "Resource": "*",
            },
            {
                "Sid": "BrokerUsesTenantDataKeys",
                "Effect": "Allow",
                "Principal": {"AWS": broker_role_arn},
                "Action": ["kms:GenerateDataKey", "kms:Decrypt"],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"kms:EncryptionContext:purpose": "connection-secret"},
                    "Null": {"kms:EncryptionContext:tenant_id": "false"},
                },
            },
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
    for name in (
        "billing-role.yaml",
        "resource-role.yaml",
        "resource-stackset.yaml",
        "cloudoptics-access.yaml",
    ):
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


# IAM says MalformedPolicyDocument, KMS MalformedPolicyDocumentException.
PROPAGATING = {"MalformedPolicyDocument", "MalformedPolicyDocumentException"}


def retry_while_propagating(call: Any, attempts: int = 12, delay: float = 5.0) -> Any:
    """IAM is eventually consistent: a new user can be rejected as a principal for a while."""
    for attempt in range(attempts):
        try:
            return call()
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code not in PROPAGATING or attempt == attempts - 1:
                raise
            time.sleep(delay)
    return None


def ensure_role(
    iam: Any, account: str, *, user: bool = True, anchor_arns: list[str] | None = None
) -> str:
    trust = json.dumps(broker_trust_policy(account, user=user, anchor_arns=anchor_arns))
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


def delete_user(iam: Any) -> bool:
    """Remove the broker user, its keys and its inline policies. False if already gone."""
    try:
        iam.get_user(UserName=USER)
    except iam.exceptions.NoSuchEntityException:
        return False
    for meta in iam.list_access_keys(UserName=USER)["AccessKeyMetadata"]:
        iam.delete_access_key(UserName=USER, AccessKeyId=meta["AccessKeyId"])
    for name in iam.list_user_policies(UserName=USER)["PolicyNames"]:
        iam.delete_user_policy(UserName=USER, PolicyName=name)
    iam.delete_user(UserName=USER)
    print(f"deleted user {USER} and its access keys")
    return True


def ensure_kms_key(kms: Any, account: str, broker_role_arn: str) -> str:
    """The secrets key, created once and found by alias; its policy is re-applied each run."""
    policy = json.dumps(kms_key_policy(account, broker_role_arn))
    try:
        key_arn = str(kms.describe_key(KeyId=KMS_ALIAS)["KeyMetadata"]["Arn"])
        kms.put_key_policy(KeyId=key_arn, PolicyName="default", Policy=policy)
    except kms.exceptions.NotFoundException:
        created = retry_while_propagating(
            lambda: kms.create_key(
                Policy=policy,
                Description="Cloudoptics staging: envelope encryption of connection secrets",
                KeySpec="SYMMETRIC_DEFAULT",
                KeyUsage="ENCRYPT_DECRYPT",
                Tags=[{"TagKey": t["Key"], "TagValue": t["Value"]} for t in TAGS],
            )
        )
        key_arn = str(created["KeyMetadata"]["Arn"])
        kms.create_alias(AliasName=KMS_ALIAS, TargetKeyId=key_arn)
        print(f"created KMS key {KMS_ALIAS}")
    kms.enable_key_rotation(KeyId=key_arn)
    return key_arn


# Roles Anywhere with a self-managed CA (ADR-007, amendment of 19 Nov 2026).


def _pem(cert: Any) -> str:
    from cryptography.hazmat.primitives import serialization

    return str(cert.public_bytes(serialization.Encoding.PEM).decode())


def _key_usage(*, sign: bool, ca: bool) -> Any:
    from cryptography import x509

    return x509.KeyUsage(
        digital_signature=sign,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=ca,
        crl_sign=ca,
        encipher_only=False,
        decipher_only=False,
    )


def new_ca(now: datetime, days: int) -> tuple[Any, Any]:
    """A throwaway CA: its key signs one leaf certificate and is then discarded."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cloudoptics"),
            x509.NameAttribute(
                NameOID.COMMON_NAME, f"Cloudoptics staging broker CA {now:%Y%m%dT%H%M%SZ}"
            ),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days, hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(sign=False, ca=True), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def new_leaf(ca_cert: Any, ca_key: Any, now: datetime, days: int) -> tuple[Any, Any]:
    """The broker's end-entity certificate: EC P-256, digital signature, client auth."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cloudoptics"),
            x509.NameAttribute(NameOID.COMMON_NAME, LEAF_CN),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(sign=True, ca=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return cert, key


def _anchor_source(ca_pem: str) -> dict[str, Any]:
    return {"sourceType": "CERTIFICATE_BUNDLE", "sourceData": {"x509CertificateData": ca_pem}}


def ensure_trust_anchors(ra: Any, now: datetime) -> list[dict[str, Any]]:
    """Both anchors, created with a placeholder CA when missing; least recently updated first."""
    found = {
        a["name"]: a
        for page in ra.get_paginator("list_trust_anchors").paginate()
        for a in page.get("trustAnchors", [])
        if a["name"] in TRUST_ANCHORS
    }
    for name in TRUST_ANCHORS:
        if name not in found:
            placeholder, _ = new_ca(now, 1)  # its key is discarded: it signs nothing
            found[name] = ra.create_trust_anchor(
                name=name, source=_anchor_source(_pem(placeholder)), enabled=True, tags=RA_TAGS
            )["trustAnchor"]
            print(f"created Roles Anywhere trust anchor {name}")
    return sorted(found.values(), key=lambda a: (a.get("updatedAt") or now, a["name"]))


def ensure_profile(ra: Any, role_arn: str) -> str:
    for page in ra.get_paginator("list_profiles").paginate():
        for prof in page.get("profiles", []):
            if prof["name"] == PROFILE:
                ra.update_profile(
                    profileId=prof["profileId"], roleArns=[role_arn], durationSeconds=3600
                )
                if not prof.get("enabled"):
                    ra.enable_profile(profileId=prof["profileId"])
                return str(prof["profileArn"])
    prof = ra.create_profile(
        name=PROFILE, roleArns=[role_arn], durationSeconds=3600, enabled=True, tags=RA_TAGS
    )["profile"]
    print(f"created Roles Anywhere profile {PROFILE}")
    return str(prof["profileArn"])


def rotate_certificate(ra: Any, anchor: dict[str, Any], now: datetime, days: int) -> dict[str, str]:
    """A new CA into ``anchor``, a leaf certificate from it; the CA key is not kept."""
    from cryptography.hazmat.primitives import serialization

    ca_cert, ca_key = new_ca(now, days)
    leaf, leaf_key = new_leaf(ca_cert, ca_key, now, days)
    del ca_key
    ra.update_trust_anchor(
        trustAnchorId=anchor["trustAnchorId"], source=_anchor_source(_pem(ca_cert))
    )
    if not anchor.get("enabled", True):
        ra.enable_trust_anchor(trustAnchorId=anchor["trustAnchorId"])
    key_pem = leaf_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    print(f"rotated the broker certificate on {anchor['name']}, valid {days} days")
    return {
        "anchor_arn": str(anchor["trustAnchorArn"]),
        "certificate": base64.b64encode(_pem(leaf).encode()).decode(),
        "private_key": base64.b64encode(key_pem).decode(),
    }


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


def read_inputs() -> dict[str, Any]:
    kms_mode = os.environ.get("KMS_MODE") or "off"
    broker = os.environ.get("BROKER_CREDENTIALS") or "iam-user"
    delete = (os.environ.get("DELETE_BROKER_USER") or "false").lower() == "true"
    days = int(os.environ.get("CERTIFICATE_DAYS") or "30")
    if kms_mode not in ("off", "optional", "required"):
        sys.exit(f"KMS_MODE must be off, optional or required, not {kms_mode!r}")
    if broker not in ("iam-user", "roles-anywhere"):
        sys.exit(f"BROKER_CREDENTIALS must be iam-user or roles-anywhere, not {broker!r}")
    if delete and broker != "roles-anywhere":
        sys.exit("DELETE_BROKER_USER needs BROKER_CREDENTIALS=roles-anywhere")
    if not 1 <= days <= 90:
        sys.exit("CERTIFICATE_DAYS must be between 1 and 90")
    return {"kms": kms_mode, "broker": broker, "delete_user": delete, "days": days}


def setup_broker(
    session: Any, iam: Any, account: str, inputs: dict[str, Any]
) -> tuple[dict[str, str], set[str]]:
    """The broker role and the credentials the app uses to become it."""
    if inputs["broker"] == "iam-user":
        ensure_user(iam)
        role_arn = ensure_role(iam, account)
        key = rotate_user_key(iam, account)
        outputs = {
            "CLOUDOPTICS_AWS_BROKER_ROLE_ARN": role_arn,
            "CLOUDOPTICS_BROKER_CREDENTIALS": "default_chain",
            "BROKER_AWS_ACCESS_KEY_ID": key["id"],
            "BROKER_AWS_SECRET_ACCESS_KEY": key["secret"],
        }
        return outputs, {"BROKER_AWS_SECRET_ACCESS_KEY"}
    ra = session.client("rolesanywhere")
    now = datetime.now(UTC)
    anchors = ensure_trust_anchors(ra, now)
    keep_user = not inputs["delete_user"]
    if keep_user:
        ensure_user(iam)
    role_arn = ensure_role(
        iam, account, user=keep_user, anchor_arns=[a["trustAnchorArn"] for a in anchors]
    )
    profile_arn = ensure_profile(ra, role_arn)
    cert = rotate_certificate(ra, anchors[0], now, inputs["days"])
    outputs = {
        "CLOUDOPTICS_AWS_BROKER_ROLE_ARN": role_arn,
        "CLOUDOPTICS_BROKER_CREDENTIALS": "roles_anywhere",
        "CLOUDOPTICS_ROLES_ANYWHERE_TRUST_ANCHOR_ARN": cert["anchor_arn"],
        "CLOUDOPTICS_ROLES_ANYWHERE_PROFILE_ARN": profile_arn,
        "CLOUDOPTICS_ROLES_ANYWHERE_ROLE_ARN": role_arn,
        "CLOUDOPTICS_ROLES_ANYWHERE_CERTIFICATE": cert["certificate"],
        "CLOUDOPTICS_ROLES_ANYWHERE_PRIVATE_KEY": cert["private_key"],
    }
    if not keep_user:
        delete_user(iam)
        outputs["BROKER_USER_DELETED"] = "true"
    return outputs, {"CLOUDOPTICS_ROLES_ANYWHERE_PRIVATE_KEY"}


def main() -> int:
    inputs = read_inputs()
    session = boto3.Session(region_name=REGION)
    account = session.client("sts").get_caller_identity()["Account"]
    print(f"AWS account {account}")
    s3, iam = session.client("s3"), session.client("iam")
    bucket = f"cloudoptics-public-{account}"
    ensure_bucket(s3, bucket)
    urls = upload_templates(s3, bucket)
    outputs, secret_keys = setup_broker(session, iam, account, inputs)
    outputs["CLOUDOPTICS_CFN_BILLING_TEMPLATE_URL"] = urls["billing-role.yaml"]
    outputs["CLOUDOPTICS_CFN_RESOURCE_TEMPLATE_URL"] = urls["resource-role.yaml"]
    if inputs["kms"] != "off":
        role_arn = outputs["CLOUDOPTICS_AWS_BROKER_ROLE_ARN"]
        outputs["CLOUDOPTICS_KMS_KEY_ID"] = ensure_kms_key(session.client("kms"), account, role_arn)
        outputs["CLOUDOPTICS_KMS_REQUIRED"] = "true" if inputs["kms"] == "required" else "false"
    emit(outputs, secret_keys=secret_keys)
    return 0


if __name__ == "__main__":
    sys.exit(main())
