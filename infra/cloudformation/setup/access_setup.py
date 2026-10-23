# Cloudoptics setup step for cloudoptics-access.yaml. Runs in the customer's account as a
# CloudFormation custom resource; Cloudoptics never assumes its role. The template carries
# this file inline (tests check the two match). Actions:
#   cleanup: on create, remove the older Cloudoptics stacks that hold the same role names.
#   bucket:  create the bill export bucket, or reuse it when an earlier stack left it behind.
#   setup:   roll the resource role out to member accounts when this is an Organization's
#            management account, then tell Cloudoptics the stack is ready (or gone).
import json
import time
import urllib.error
import urllib.request

import boto3
import cfnresponse
from botocore.exceptions import ClientError

STACKSET = "cloudoptics-resource-access"
# The StackSet stack first: if it cannot be removed, the billing role is still in place.
OLD_STACKS = ("cloudoptics-resource-stackset", "cloudoptics-resource-access", "cloudoptics-billing-access")
REGIONS = ["us-east-1"]  # IAM is global: one region is enough
PREFS = {"RegionConcurrencyType": "PARALLEL", "MaxConcurrentPercentage": 100, "FailureTolerancePercentage": 100}
DENIED = ("AccessDenied", "AccessDeniedException")


class _Retrying:
    """A boto3 client whose calls retry AccessDenied for two minutes: a role created moments
    ago can be refused until IAM has propagated it."""

    def __init__(self, client):
        self._client = client

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr) or name == "get_waiter":
            return attr

        def call(*args, **kwargs):
            for attempt in range(12):
                try:
                    return attr(*args, **kwargs)
                except ClientError as exc:
                    if _code(exc) not in DENIED or attempt == 11:
                        raise
                    time.sleep(10)
            return None

        return call


def client(name):
    return _Retrying(boto3.client(name))


def _code(exc):
    return exc.response.get("Error", {}).get("Code")


def handler(event, context):
    props = event["ResourceProperties"]
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
        try:
            cfn.get_waiter("stack_delete_complete").wait(
                StackName=name, WaiterConfig={"Delay": 10, "MaxAttempts": 80}
            )
        except Exception as exc:
            raise RuntimeError(
                f"Could not remove the old stack {name}. Delete it in CloudFormation, then try again."
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
                "Principal": {"Service": ["bcm-data-exports.amazonaws.com", "billingreports.amazonaws.com"]},
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
        return {"BucketName": name}  # kept on purpose, like the bills in it
    s3 = client("s3")
    try:
        s3.head_bucket(Bucket=name, ExpectedBucketOwner=props["AccountId"])
        reused = True
    except ClientError as exc:
        if _code(exc) not in ("404", "NoSuchBucket", "NotFound"):
            raise RuntimeError(f"The S3 bucket name {name} belongs to another AWS account.") from exc
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
        ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
    )
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={
            "Rules": [{"ID": "expire-old-exports", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 400}}]
        },
    )
    s3.put_bucket_policy(Bucket=name, Policy=json.dumps(bucket_policy(name, props["AccountId"])))
    return {"BucketName": name, "Reused": str(reused).lower()}


def is_management(account):
    try:
        org = client("organizations").describe_organization()["Organization"]
    except ClientError as exc:
        if _code(exc) == "AWSOrganizationsNotInUseException":
            return False
        raise
    return org["MasterAccountId"] == account


def trusted_access():
    """Turn on StackSets trusted access in AWS Organizations, if it is off."""
    cfn = client("cloudformation")
    if cfn.describe_organizations_access()["Status"] == "ENABLED":
        return
    try:
        cfn.activate_organizations_access()
        for _ in range(30):
            if cfn.describe_organizations_access()["Status"] == "ENABLED":
                break
            time.sleep(5)
    except ClientError as exc:
        if _code(exc) not in DENIED:
            raise
        # The same result by hand: the StackSets service-linked role, then trusted access.
        try:
            client("iam").create_service_linked_role(AWSServiceName="stacksets.cloudformation.amazonaws.com")
        except ClientError as iam_exc:
            if _code(iam_exc) != "InvalidInput":  # the role already exists
                raise
        client("organizations").enable_aws_service_access(
            ServicePrincipal="member.org.stacksets.cloudformation.amazonaws.com"
        )
    time.sleep(15)  # let the new access reach the StackSets service


def root_id():
    trusted_access()
    return client("organizations").list_roots()["Roots"][0]["Id"]


def wait_operation(cfn, operation_id):
    for _ in range(80):
        op = cfn.describe_stack_set_operation(StackSetName=STACKSET, OperationId=operation_id)
        if op["StackSetOperation"]["Status"] not in ("RUNNING", "QUEUED", "STOPPING"):
            return
        time.sleep(10)


def wait_idle(cfn):
    for op in cfn.list_stack_set_operations(StackSetName=STACKSET)["Summaries"]:
        if op["Status"] in ("RUNNING", "QUEUED", "STOPPING"):
            wait_operation(cfn, op["OperationId"])


def ensure_stackset(props, root):
    cfn = client("cloudformation")
    params = [
        {"ParameterKey": "ExternalId", "ParameterValue": props["ExternalId"]},
        {"ParameterKey": "CloudopticsPrincipalArn", "ParameterValue": props["CloudopticsPrincipalArn"]},
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
        op = cfn.update_stack_set(
            StackSetName=STACKSET,
            TemplateURL=props["ResourceTemplateUrl"],
            Parameters=params,
            Capabilities=["CAPABILITY_NAMED_IAM"],
            OperationPreferences=PREFS,
        )
        wait_operation(cfn, op["OperationId"])
        if cfn.list_stack_instances(StackSetName=STACKSET)["Summaries"]:
            return
    else:
        cfn.create_stack_set(
            StackSetName=STACKSET,
            Description="Read-only resource role for Cloudoptics in every member account.",
            TemplateURL=props["ResourceTemplateUrl"],
            Parameters=params,
            Capabilities=["CAPABILITY_NAMED_IAM"],
            PermissionModel="SERVICE_MANAGED",
            AutoDeployment={"Enabled": True, "RetainStacksOnAccountRemoval": False},
            ManagedExecution={"Active": True},
        )
    # Not waited for: member accounts show up in Cloudoptics as the rollout reaches them.
    cfn.create_stack_instances(
        StackSetName=STACKSET,
        DeploymentTargets={"OrganizationalUnitIds": [root]},
        Regions=REGIONS,
        OperationPreferences=PREFS,
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
        wait_operation(cfn, op["OperationId"])
    cfn.delete_stack_set(StackSetName=STACKSET)


def callback(kind, props, management):
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
        }
    ).encode()
    error = "no answer"
    for delay in (0, 3, 10):
        time.sleep(delay)
        request = urllib.request.Request(
            props["CallbackUrl"], data=body, method="POST", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=20):  # noqa: S310 - our https URL
                return
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("title") or detail
            except (ValueError, AttributeError):
                pass
            error = f"HTTP {exc.code} {detail[:300]}"
            if exc.code < 500:
                break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
    if kind == "Create":
        raise RuntimeError(f"Cloudoptics did not accept this account ({error}).")
    print(f"callback failed, ignored on {kind}: {error}")


def setup(kind, props):
    management = is_management(props["AccountId"])
    if kind == "Delete":
        if management:
            remove_stackset()
        callback(kind, props, management)
        return None
    if management:
        ensure_stackset(props, root_id())
    callback(kind, props, management)
    return {"Management": str(management).lower()}


ACTIONS = {"cleanup": cleanup, "bucket": bucket, "setup": setup}
