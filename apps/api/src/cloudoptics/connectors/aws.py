"""AWS connection helpers: External IDs, quick-create links and assumed-role sessions."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import boto3

STACK_REGION = "us-east-1"  # BCM Data Exports (CUR 2.0) is created in us-east-1
BILLING_ROLE_NAME = "CloudopticsBillingReadOnly"
RESOURCE_ROLE_NAME = "CloudopticsResourceReadOnly"
STACK_NAME = "cloudoptics-billing-access"


def new_external_id() -> str:
    """At least 32 characters (PRD FR-CON-01), from the set IAM allows in ExternalId."""
    return secrets.token_urlsafe(32)


def billing_role_arn(aws_account_id: str) -> str:
    return f"arn:aws:iam::{aws_account_id}:role/{BILLING_ROLE_NAME}"


def quick_create_url(
    template_url: str,
    *,
    external_id: str,
    principal_arn: str,
    create_cur_export: bool,
    existing_bucket: str | None = None,
    stack_name: str = STACK_NAME,
) -> str:
    params = {
        "templateURL": template_url,
        "stackName": stack_name,
        "param_ExternalId": external_id,
        "param_CloudopticsPrincipalArn": principal_arn,
        "param_CreateCurExport": "true" if create_cur_export else "false",
    }
    if existing_bucket:
        params["param_ExistingCurBucketName"] = existing_bucket
    return (
        f"https://{STACK_REGION}.console.aws.amazon.com/cloudformation/home"
        f"?region={STACK_REGION}#/stacks/create/review?{urlencode(params)}"
    )


def session_name(tenant_id: uuid.UUID, job: str) -> str:
    """``cloudoptics-<tenant>-<job>`` (FR-CON-04), within the 64-character STS limit."""
    return f"cloudoptics-{tenant_id.hex[:20]}-{job}"[:64]


@dataclass(frozen=True)
class AssumedSession:
    session: boto3.Session

    def client(self, service: str, region: str = STACK_REGION) -> Any:
        return self.session.client(service, region_name=region)  # type: ignore[call-overload]


class SessionFactory:
    """Assumes customer roles from the broker identity.

    The broker's own credentials come from the default chain: IAM Roles Anywhere via
    ``credential_process`` on DigitalOcean (ADR-007), environment variables elsewhere.
    """

    def __init__(self, base: boto3.Session | None = None) -> None:
        self._base = base or boto3.Session()

    def assume(self, role_arn: str, external_id: str, name: str) -> AssumedSession:
        sts = self._base.client("sts", region_name=STACK_REGION)
        creds = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=name,
            ExternalId=external_id,
            DurationSeconds=3600,
        )["Credentials"]
        return AssumedSession(
            boto3.Session(
                aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"],
                aws_session_token=creds["SessionToken"],
            )
        )
