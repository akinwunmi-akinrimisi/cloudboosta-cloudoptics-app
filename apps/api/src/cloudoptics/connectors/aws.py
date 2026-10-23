"""AWS connection helpers: External IDs, quick-create links and assumed-role sessions."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import boto3

STACK_REGION = "us-east-1"  # BCM Data Exports (CUR 2.0) is created in us-east-1
BILLING_ROLE_NAME = "CloudopticsBillingReadOnly"
RESOURCE_ROLE_NAME = "CloudopticsResourceReadOnly"
STACK_NAME = "cloudoptics-billing-access"
RESOURCE_STACK_NAME = "cloudoptics-resource-access"
STACKSET_STACK_NAME = "cloudoptics-resource-stackset"


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


def resource_quick_create_url(template_url: str, *, external_id: str, principal_arn: str) -> str:
    """The resource role is IAM only (global), so the billing stack's region works too."""
    params = {
        "templateURL": template_url,
        "stackName": RESOURCE_STACK_NAME,
        "param_ExternalId": external_id,
        "param_CloudopticsPrincipalArn": principal_arn,
    }
    return (
        f"https://{STACK_REGION}.console.aws.amazon.com/cloudformation/home"
        f"?region={STACK_REGION}#/stacks/create/review?{urlencode(params)}"
    )


def stackset_quick_create_url(
    template_url: str, *, external_id: str, principal_arn: str, resource_template_url: str
) -> str:
    """One stack in the management account that rolls the resource role out to members."""
    params = {
        "templateURL": template_url,
        "stackName": STACKSET_STACK_NAME,
        "param_ExternalId": external_id,
        "param_CloudopticsPrincipalArn": principal_arn,
        "param_ResourceTemplateUrl": resource_template_url,
    }
    return (
        f"https://{STACK_REGION}.console.aws.amazon.com/cloudformation/home"
        f"?region={STACK_REGION}#/stacks/create/review?{urlencode(params)}"
    )


def session_name(tenant_id: uuid.UUID, job: str) -> str:
    """``cloudoptics-<tenant>-<job>`` (FR-CON-04), within the 64-character STS limit."""
    return f"cloudoptics-{tenant_id.hex[:20]}-{job}"[:64]


@dataclass(frozen=True)
class AssumedSession:
    session: Any  # boto3.Session

    def client(self, service: str, region: str = STACK_REGION) -> Any:
        return self.session.client(service, region_name=region)


class SessionFactory:
    """Assumes customer roles from the broker identity.

    The process credentials come from the default chain: an IAM user key on App Platform
    staging, IAM Roles Anywhere via ``credential_process`` on DOKS later (ADR-007). With
    ``broker_role_arn`` set, they are only allowed to assume Cloudoptics' broker role; that
    role (the principal customers trust) then assumes the customer's role. The broker session
    is reused until five minutes before it expires.
    """

    def __init__(
        self,
        base: Any = None,
        broker_role_arn: str | None = None,
        make_session: Callable[..., Any] = boto3.Session,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._base = base or make_session()
        self._broker_role_arn = broker_role_arn
        self._make_session = make_session
        self._clock = clock
        self._broker: tuple[Any, datetime] | None = None

    def _from_credentials(self, creds: dict[str, Any]) -> Any:
        return self._make_session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
        )

    def _source(self) -> Any:
        if not self._broker_role_arn:
            return self._base
        if self._broker and self._broker[1] - self._clock() > timedelta(minutes=5):
            return self._broker[0]
        creds = self._base.client("sts", region_name=STACK_REGION).assume_role(
            RoleArn=self._broker_role_arn,
            RoleSessionName="cloudoptics-broker",
            DurationSeconds=3600,
        )["Credentials"]
        self._broker = (self._from_credentials(creds), creds["Expiration"])
        return self._broker[0]

    def assume(self, role_arn: str, external_id: str, name: str) -> AssumedSession:
        sts = self._source().client("sts", region_name=STACK_REGION)
        creds = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName=name,
            ExternalId=external_id,
            DurationSeconds=3600,
        )["Credentials"]
        return AssumedSession(self._from_credentials(creds))
