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
from botocore.config import Config

STACK_REGION = "us-east-1"  # BCM Data Exports (CUR 2.0) is created in us-east-1
BILLING_ROLE_NAME = "CloudopticsBillingReadOnly"
RESOURCE_ROLE_NAME = "CloudopticsResourceReadOnly"
STACK_NAME = "cloudoptics-billing-access"
RESOURCE_STACK_NAME = "cloudoptics-resource-access"
STACKSET_STACK_NAME = "cloudoptics-resource-stackset"
# The single stack (infra/cloudformation/cloudoptics-access.yaml) that replaces the three above.
ACCESS_STACK_NAME = "cloudoptics-access"
ACCESS_TEMPLATE_VERSION = "2.0.3"
LEGACY_STACK_NAMES = (STACK_NAME, RESOURCE_STACK_NAME, STACKSET_STACK_NAME)
# The single stack's roles have their own names: CloudFormation refuses to create a stack whose
# named roles already exist, and the older stacks' roles are only removed once it is running.
ACCESS_BILLING_ROLE_NAME = "CloudopticsBillingReader"
ACCESS_RESOURCE_ROLE_NAME = "CloudopticsResourceReader"


def new_external_id() -> str:
    """At least 32 characters (PRD FR-CON-01), from the set IAM allows in ExternalId."""
    return secrets.token_urlsafe(32)


def billing_role_arn(aws_account_id: str, stack_name: str | None = None) -> str:
    name = ACCESS_BILLING_ROLE_NAME if stack_name == ACCESS_STACK_NAME else BILLING_ROLE_NAME
    return f"arn:aws:iam::{aws_account_id}:role/{name}"


# The StackSet the single stack uses to give member accounts their resource role.
MEMBER_STACKSET_NAME = "cloudoptics-resource-access"


def member_rollout(session: AssumedSession) -> dict[str, str] | str:
    """Each member account's rollout of the resource role, in plain words, by account ID.

    Read with the payer's billing role (2.0.1 may read its own StackSet). When it cannot
    be read, the AWS error code instead, e.g. StackSetNotFoundException or AccessDenied.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    cfn = session.client("cloudformation", STACK_REGION)
    found: dict[str, str] = {}
    try:
        for page in cfn.get_paginator("list_stack_instances").paginate(
            StackSetName=MEMBER_STACKSET_NAME
        ):
            for item in page.get("Summaries", []):
                found[item["Account"]] = _rollout_words(item)
    except ClientError as exc:
        return str(exc.response.get("Error", {}).get("Code") or "Error")
    except BotoCoreError as exc:
        return type(exc).__name__
    return found


def _rollout_words(item: dict[str, Any]) -> str:
    detail = (item.get("StackInstanceStatus") or {}).get("DetailedStatus", "")
    reason = (item.get("StatusReason") or "").strip()
    if detail in ("PENDING", "RUNNING"):
        return "AWS is adding the read-only role to this account now."
    if detail == "SUCCEEDED" or item.get("Status") == "CURRENT":
        return "The role is in place. Check now in a minute; AWS can take that long to use it."
    if detail == "SKIPPED_SUSPENDED_ACCOUNT":
        return "AWS skipped this account because it is suspended."
    words = "AWS could not add the read-only role to this account"
    words += f": {reason}" if reason else "."
    return words


def resource_role_arn(aws_account_id: str, stack_name: str | None = None) -> str:
    """Member accounts get the role from the connection's stack, so it has the same name."""
    name = ACCESS_RESOURCE_ROLE_NAME if stack_name == ACCESS_STACK_NAME else RESOURCE_ROLE_NAME
    return f"arn:aws:iam::{aws_account_id}:role/{name}"


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


def access_quick_create_url(
    template_url: str,
    *,
    external_id: str,
    principal_arn: str,
    resource_template_url: str,
    callback_url: str,
    connection_ref: str,
    tenant_id: uuid.UUID,
) -> str:
    """The single stack. ``connection_ref`` is ``i:<intent>`` for a new account (the stack
    reports which account it is in) or ``c:<connection>`` to move or set up one again."""
    params = {
        "templateURL": template_url,
        "stackName": ACCESS_STACK_NAME,
        "param_ExternalId": external_id,
        "param_CloudopticsPrincipalArn": principal_arn,
        "param_ResourceTemplateUrl": resource_template_url,
        "param_CallbackUrl": callback_url,
        "param_ConnectionRef": connection_ref,
        "param_TenantId": str(tenant_id),
    }
    return (
        f"https://{STACK_REGION}.console.aws.amazon.com/cloudformation/home"
        f"?region={STACK_REGION}#/stacks/create/review?{urlencode(params)}"
    )


def stack_console_url(stack_name: str = ACCESS_STACK_NAME) -> str:
    """The stack's page in the AWS console, where Update and Delete are."""
    return (
        f"https://{STACK_REGION}.console.aws.amazon.com/cloudformation/home"
        f"?region={STACK_REGION}#/stacks/stackinfo?{urlencode({'stackId': stack_name})}"
    )


def stack_update_url(template_url: str, stack_name: str = ACCESS_STACK_NAME) -> str:
    """Opens AWS's Update stack page for the stack with the new template already chosen.

    AWS documents quick-create links for new stacks only; for updates the console takes the
    same templateURL on its update page. The app keeps the template address on screen too,
    in case AWS ever stops filling it in.
    """
    query = urlencode({"stackId": stack_name, "templateURL": template_url})
    return (
        f"https://{STACK_REGION}.console.aws.amazon.com/cloudformation/home"
        f"?region={STACK_REGION}#/stacks/update/template?{query}"
    )


def version_tuple(version: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in (version or "0").split("."))
    except ValueError:
        return (0,)


def session_name(tenant_id: uuid.UUID, job: str) -> str:
    """``cloudoptics-<tenant>-<job>`` (FR-CON-04), within the 64-character STS limit."""
    return f"cloudoptics-{tenant_id.hex[:20]}-{job}"[:64]


# Adaptive retries back off on throttling and rate-limit each client with a token bucket
# (WBS CC-13), so a busy account slows CloudOptics down instead of failing its scan.
CLIENT_CONFIG = Config(retries={"mode": "adaptive", "max_attempts": 8})


@dataclass(frozen=True)
class AssumedSession:
    session: Any  # boto3.Session

    def client(self, service: str, region: str = STACK_REGION) -> Any:
        return self.session.client(service, region_name=region, config=CLIENT_CONFIG)


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
