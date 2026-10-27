# Cloudoptics setup step for cloudoptics-access.yaml. Runs in the customer's account as a
# CloudFormation custom resource; Cloudoptics never assumes its role. The template carries
# this file inline (tests check the two match). Actions:
#   cleanup: on create, remove the older Cloudoptics stacks that hold the same role names.
#   bucket:  create the bill export bucket, or reuse it when an earlier stack left it behind.
#   setup:   roll the resource role out to member accounts when this is an Organization's
#            management account, then tell Cloudoptics the stack is ready (or gone).
import contextlib
import json
import os
import time
import urllib.error
import urllib.request

import boto3
import cfnresponse
from botocore.exceptions import ClientError

STACKSET = "cloudoptics-resource-access"
# The StackSet stack first: if it cannot be removed, the billing role is still in place.
OLD_STACKS = (
    "cloudoptics-resource-stackset",
    "cloudoptics-resource-access",
    "cloudoptics-billing-access",
)
REGIONS = ["us-east-1"]  # IAM is global: one region is enough
PREFS = {
    "RegionConcurrencyType": "PARALLEL",
    "MaxConcurrentPercentage": 100,
    "FailureTolerancePercentage": 100,
}
DENIED = ("AccessDenied", "AccessDeniedException")
ORG_ACCESS_TRIES = 12  # about 6 minutes, inside the function's 15
# Kept back from the function's timeout for the callback and the reply to CloudFormation:
# without a reply the stack waits an hour and then fails with no reason.
RESERVE_SECONDS = 120
_deadline = None
# Taken from this stack's own parameters (the function's environment), never from the event:
# anyone allowed to invoke the function could otherwise choose the template and the trusted
# principal rolled out to every member account, or where the callback is sent.
FIXED = ("ExternalId", "CloudopticsPrincipalArn", "ResourceTemplateUrl", "CallbackUrl")


def time_left():
    return float("inf") if _deadline is None else _deadline - time.time()


class _Retrying:
    """A boto3 client whose calls retry AccessDenied for two minutes: a role created moments
    ago can be refused until IAM has propagated it."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr) or name in ("get_waiter", "get_paginator"):
            return attr

        def call(*args, **kwargs):
            for attempt in range(12):
                try:
                    return attr(*args, **kwargs)
                except ClientError as exc:
                    if _code(exc) not in DENIED or attempt == 11 or time_left() < 30:
                        raise
                    time.sleep(10)
            return None

        return call


def client(name):
    return _Retrying(boto3.client(name))


def _code(exc):
    return exc.response.get("Error", {}).get("Code")


def handler(event, context):
    global _deadline
    if context is not None:
        _deadline = time.time() + context.get_remaining_time_in_millis() / 1000 - RESERVE_SECONDS
    props = {**event["ResourceProperties"], **{k: os.environ[k] for k in FIXED}}
    physical = event.get("PhysicalResourceId") or "cloudoptics-" + props["Action"]
    try:
        data = ACTIONS[props["Action"]](event["RequestType"], props) or {}
        cfnresponse.send(event, context, cfnresponse.SUCCESS, data, physical)
    except Exception as exc:  # report every failure to CloudFormation
        reason = f"{type(exc).__name__}: {exc}"[:900]
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, physical, reason=reason)


def _missing(exc):
    return _code(exc) in ("ValidationError", "StackSetNotFoundException")


def cleanup(kind, props):
    if kind != "Create":
        return None
    cfn = client("cloudformation")
    removed = []
    for name in OLD_STACKS:
        try:
            [stack] = cfn.describe_stacks(StackName=name)["Stacks"]
        except ClientError as exc:
            if _missing(exc):
                continue
            raise
        if stack["StackStatus"] == "DELETE_COMPLETE":
            continue
        cfn.delete_stack(StackName=name)
        # Bounded by the time left, so CloudFormation always gets a reply.
        attempts = int(max(1, min(80, (time_left() - 30) // 10)))
        try:
            cfn.get_waiter("stack_delete_complete").wait(
                StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": attempts}
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not remove the old stack {name}. "
                "Delete it in CloudFormation, then try again."
            ) from exc
        removed.append(name)
    return {"Removed": ",".join(removed)}


def bucket_policy(bucket, account):
    arns = [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowBcmDataExportsDelivery",
                "Effect": "Allow",
                "Principal": {
                    "Service": ["bcm-data-exports.amazonaws.com", "billingreports.amazonaws.com"]
                },
                "Action": ["s3:PutObject", "s3:GetBucketPolicy"],
                "Resource": arns,
                "Condition": {
                    "StringLike": {
                        "aws:SourceAccount": account,
                        "aws:SourceArn": [
                            f"arn:aws:cur:us-east-1:{account}:definition/*",
                            f"arn:aws:bcm-data-exports:us-east-1:{account}:export/*",
                        ],
                    }
                },
            },
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": arns,
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
        ],
    }


def bucket(kind, props):
    name = props["BucketName"]
    if kind == "Delete":
        return None  # kept on purpose, like the bills in it (Delete replies carry no data)
    s3 = client("s3")
    try:
        s3.head_bucket(Bucket=name, ExpectedBucketOwner=props["AccountId"])
        reused = True
    except ClientError as exc:
        if _code(exc) not in ("404", "NoSuchBucket", "NotFound"):
            raise RuntimeError(
                f"The S3 bucket name {name} belongs to another AWS account."
            ) from exc
        s3.create_bucket(Bucket=name)
        reused = False
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "BlockPublicPolicy": True,
            "IgnorePublicAcls": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_ownership_controls(
        Bucket=name, OwnershipControls={"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]}
    )
    s3.put_bucket_encryption(
        Bucket=name,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-old-exports",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "Expiration": {"Days": 400},
                }
            ]
        },
    )
    s3.put_bucket_policy(Bucket=name, Policy=json.dumps(bucket_policy(name, props["AccountId"])))
    return {"BucketName": name, "Reused": str(reused).lower()}


def organization():
    try:
        return client("organizations").describe_organization()["Organization"]
    except ClientError as exc:
        if _code(exc) == "AWSOrganizationsNotInUseException":
            return None
        raise


def trusted_access():
    """Turn on StackSets trusted access in AWS Organizations.

    Always asked for, even when it reads as on: the status can say ENABLED before
    StackSets will create a service-managed stack set, and asking again is harmless.
    """
    cfn = client("cloudformation")
    # Only CloudFormation can turn it on (enabling the service in Organizations is not enough).
    # It needs iam:CreateServiceLinkedRole and iam:GetRole for the StackSets service-linked role.
    cfn.activate_organizations_access()
    for _ in range(12):
        if cfn.describe_organizations_access()["Status"] == "ENABLED" or time_left() < 60:
            break
        time.sleep(5)


def _no_org_access(exc):
    return _code(exc) == "ValidationError" and "organizations access" in str(exc).lower()


def with_org_access(call, tries=ORG_ACCESS_TRIES):
    """StackSets can take minutes to see newly enabled trusted access: ask again and wait."""
    for attempt in range(tries):
        try:
            return call()
        except ClientError as exc:
            if not _no_org_access(exc) or attempt == tries - 1 or time_left() < 120:
                raise
        trusted_access()
        time.sleep(30)
    return None


def root_id():
    trusted_access()
    return client("organizations").list_roots()["Roots"][0]["Id"]


def wait_operation(cfn, operation_id):
    """The operation's final status: SUCCEEDED, FAILED or STOPPED."""
    for _ in range(80):
        op = cfn.describe_stack_set_operation(StackSetName=STACKSET, OperationId=operation_id)
        status = op["StackSetOperation"]["Status"]
        if status not in ("RUNNING", "QUEUED", "STOPPING"):
            return status
        if time_left() < 60:
            raise TimeoutError(f"StackSet operation {operation_id} is still running")
        time.sleep(10)


def wait_idle(cfn):
    pages = cfn.get_paginator("list_stack_set_operations").paginate(StackSetName=STACKSET)
    for page in pages:
        for op in page["Summaries"]:
            if op["Status"] in ("RUNNING", "QUEUED", "STOPPING"):
                wait_operation(cfn, op["OperationId"])


def ensure_stackset(props, root):
    cfn = client("cloudformation")
    params = [
        {"ParameterKey": "ExternalId", "ParameterValue": props["ExternalId"]},
        {
            "ParameterKey": "CloudopticsPrincipalArn",
            "ParameterValue": props["CloudopticsPrincipalArn"],
        },
        {"ParameterKey": "RoleName", "ParameterValue": "CloudopticsResourceReader"},
    ]
    try:
        cfn.describe_stack_set(StackSetName=STACKSET)
        exists = True
    except ClientError as exc:
        if not _missing(exc):
            raise
        exists = False
    if exists:
        wait_idle(cfn)
        op = with_org_access(
            lambda: cfn.update_stack_set(
                StackSetName=STACKSET,
                TemplateURL=props["ResourceTemplateUrl"],
                Parameters=params,
                Capabilities=["CAPABILITY_NAMED_IAM"],
                OperationPreferences=PREFS,
            )
        )
        wait_operation(cfn, op["OperationId"])
        if cfn.list_stack_instances(StackSetName=STACKSET)["Summaries"]:
            return
    else:
        with_org_access(
            lambda: cfn.create_stack_set(
                StackSetName=STACKSET,
                Description="Read-only resource role for Cloudoptics in every member account.",
                TemplateURL=props["ResourceTemplateUrl"],
                Parameters=params,
                Capabilities=["CAPABILITY_NAMED_IAM"],
                PermissionModel="SERVICE_MANAGED",
                AutoDeployment={"Enabled": True, "RetainStacksOnAccountRemoval": False},
                ManagedExecution={"Active": True},
            )
        )
    # Not waited for: member accounts show up in Cloudoptics as the rollout reaches them.
    with_org_access(
        lambda: cfn.create_stack_instances(
            StackSetName=STACKSET,
            DeploymentTargets={"OrganizationalUnitIds": [root]},
            Regions=REGIONS,
            OperationPreferences=PREFS,
        )
    )


def remove_stackset():
    cfn = client("cloudformation")
    try:
        cfn.describe_stack_set(StackSetName=STACKSET)
    except ClientError as exc:
        if _missing(exc):
            return
        raise
    wait_idle(cfn)
    if cfn.list_stack_instances(StackSetName=STACKSET)["Summaries"]:
        root = client("organizations").list_roots()["Roots"][0]["Id"]
        op = cfn.delete_stack_instances(
            StackSetName=STACKSET,
            DeploymentTargets={"OrganizationalUnitIds": [root]},
            Regions=REGIONS,
            RetainStacks=False,
            OperationPreferences=PREFS,
        )
        status = wait_operation(cfn, op["OperationId"])
        if status != "SUCCEEDED":
            raise RuntimeError(f"removing the member accounts' roles ended {status}")
    cfn.delete_stack_set(StackSetName=STACKSET)


def callback(kind, props, management, rollout=None):
    body = json.dumps(
        {
            "requestType": kind,
            "ref": props["ConnectionRef"],
            "tenantId": props["TenantId"],
            "externalId": props["ExternalId"],
            "accountId": props["AccountId"],
            "management": management,
            "curBucket": props["BucketName"],
            "curPrefix": props["CurPrefix"],
            "stackName": props["StackName"],
            "templateVersion": props["TemplateVersion"],
            "memberRollout": rollout,
        }
    ).encode()
    error = "no answer"
    for delay in (0, 3, 10):
        time.sleep(delay)
        request = urllib.request.Request(  # noqa: S310 - our https URL
            props["CallbackUrl"],
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=20):  # noqa: S310 - our https URL
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            with contextlib.suppress(ValueError, AttributeError):
                detail = json.loads(detail).get("title") or detail
            error = f"HTTP {exc.code} {detail[:300]}"
            if exc.code < 500:
                break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    if kind == "Create":
        raise RuntimeError(f"Cloudoptics did not accept this account ({error}).")
    print(f"callback failed, ignored on {kind}: {error}")


def setup(kind, props):
    org = organization()
    management = org is not None and org["MasterAccountId"] == props["AccountId"]
    if kind == "Delete":
        left = None
        if management:
            # A rollout left behind is taken over by the next stack, so it never blocks a
            # delete; Cloudoptics is told, so the workspace's audit log says the member
            # accounts still have the role.
            try:
                remove_stackset()
            except Exception as exc:
                left = f"failed: member roles not removed: {type(exc).__name__}: {exc}"[:900]
                print(left)
        callback(kind, props, management, left)
        return None
    if not management:
        rollout = "not an Organization's management account"
    elif org.get("FeatureSet") != "ALL":
        # Service-managed StackSets need all features, not only consolidated billing.
        rollout = (
            "skipped: turn on all features in AWS Organizations, then update this stack "
            "to give member accounts their read-only role"
        )
    else:
        # Billing and this account's resource role work without it, so a failed rollout
        # does not undo the whole stack; updating the stack tries the rollout again.
        try:
            ensure_stackset(props, root_id())
            rollout = "started"
        except Exception as exc:
            # Long enough for AWS's whole AccessDenied message, which names the missing action.
            rollout = f"failed: {type(exc).__name__}: {exc}"[:900]
            print(f"member rollout {rollout}")
    callback(kind, props, management, rollout)
    return {"Management": str(management).lower(), "MemberRollout": rollout}


ACTIONS = {"cleanup": cleanup, "bucket": bucket, "setup": setup}
