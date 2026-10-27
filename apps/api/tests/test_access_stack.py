"""The single Cloudoptics stack (cloudoptics-access.yaml) and its setup step."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from fake_aws import client_error
from test_cloudformation import TEMPLATES, WRITE_VERBS, load

from cloudoptics.connectors import aws

SETUP_SOURCE = TEMPLATES / "setup" / "access_setup.py"


def _actions(stmt: dict[str, Any]) -> list[str]:
    acts = stmt["Action"]
    return [acts] if isinstance(acts, str) else acts


def test_template_carries_the_setup_source() -> None:
    code = load("cloudoptics-access.yaml")["Resources"]["SetupFunction"]["Properties"]["Code"]
    assert code["ZipFile"] == SETUP_SOURCE.read_text(), "run infra/cloudformation/setup/embed.py"


def test_versions_agree() -> None:
    template = load("cloudoptics-access.yaml")
    version = template["Metadata"]["cloudoptics"]["templateVersion"]
    assert version == aws.ACCESS_TEMPLATE_VERSION
    assert template["Resources"]["Setup"]["Properties"]["TemplateVersion"] == version
    assert template["Outputs"]["TemplateVersion"]["Value"] == version


def test_cloudoptics_roles_are_read_only_and_need_the_external_id() -> None:
    template = load("cloudoptics-access.yaml")
    resources = template["Resources"]
    assert template["Parameters"]["ExternalId"]["NoEcho"] is True
    for name, role_name in (
        ("BillingRole", aws.ACCESS_BILLING_ROLE_NAME),
        ("ResourceRole", aws.ACCESS_RESOURCE_ROLE_NAME),
    ):
        role = resources[name]
        props = role["Properties"]
        assert props["RoleName"] == role_name
        assert role["DependsOn"] == "CleanUp"
        assert props["MaxSessionDuration"] == 3600
        [trust] = props["AssumeRolePolicyDocument"]["Statement"]
        assert trust["Condition"]["StringEquals"]["sts:ExternalId"] == {"Ref": "ExternalId"}
        for policy in props["Policies"]:
            for stmt in policy["PolicyDocument"]["Statement"]:
                assert stmt["Effect"] == "Allow"
                for action in _actions(stmt):
                    service, verb = action.split(":")
                    assert service not in {"iam", "sts", "secretsmanager", "ssm", "kms"}, action
                    assert not verb.startswith(WRITE_VERBS), action
                    assert verb != "*", action


def test_roles_do_not_clash_with_the_older_stacks() -> None:
    """CloudFormation checks role names before any step runs, so the old roles still exist."""
    new = {
        load("cloudoptics-access.yaml")["Resources"][r]["Properties"]["RoleName"]
        for r in ("BillingRole", "ResourceRole")
    }
    old = {
        load("billing-role.yaml")["Resources"]["BillingRole"]["Properties"]["RoleName"],
        load("resource-role.yaml")["Parameters"]["RoleName"]["Default"],
    }
    assert new.isdisjoint(old)
    assert (
        aws.ACCESS_RESOURCE_ROLE_NAME
        in load("resource-role.yaml")["Parameters"]["RoleName"]["AllowedValues"]
    )
    assert aws.billing_role_arn("1" * 12, aws.ACCESS_STACK_NAME).endswith(
        "/CloudopticsBillingReader"
    )
    assert aws.resource_role_arn("1" * 12, aws.STACK_NAME).endswith("/CloudopticsResourceReadOnly")


def test_setup_role_writes_only_to_cloudoptics_resources() -> None:
    props = load("cloudoptics-access.yaml")["Resources"]["SetupRole"]["Properties"]
    assert props["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {
        "Service": "lambda.amazonaws.com"
    }
    unscoped_writes = {
        # No resource-level scope exists for these.
        "cloudformation:ActivateOrganizationsAccess",
        "organizations:EnableAWSServiceAccess",
        "iam:CreateServiceLinkedRole",  # limited by path to the StackSets role
        "cloudformation:CreateStackSet",  # supports no resource-level permissions
        "bcm-data-exports:DeleteExport",
    }
    [policy] = props["Policies"]
    for stmt in policy["PolicyDocument"]["Statement"]:
        resources = stmt["Resource"] if isinstance(stmt["Resource"], list) else [stmt["Resource"]]
        for action in _actions(stmt):
            verb = action.split(":")[1]
            if not verb.startswith(WRITE_VERBS) or action in unscoped_writes:
                continue
            for resource in resources:
                text = json.dumps(resource).lower()
                scoped = ("cloudoptics", "type/resource", "stackset-target")
                assert any(s in text for s in scoped), (action, resource)


def test_setup_runs_after_the_roles_and_export() -> None:
    resources = load("cloudoptics-access.yaml")["Resources"]
    assert set(resources["Setup"]["DependsOn"]) == {"BillingRole", "ResourceRole", "CurExport"}
    assert resources["CurBucket"]["DependsOn"] == "CleanUp"
    bucket = resources["CurExport"]["Properties"]["Export"]["DestinationConfigurations"]
    assert bucket["S3Destination"]["S3Bucket"] == {"GetAtt": "CurBucket.BucketName"}


# The setup step, with fake AWS clients.


class Fake:
    """Records calls; ``answers`` maps method names to a value, an exception or a callable."""

    def __init__(self, **answers: Any) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Any:
        if name == "get_paginator":
            # One page: the answer for the paginated method.
            return lambda method: types.SimpleNamespace(
                paginate=lambda **kw: [getattr(self, method)(**kw)]
            )

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, kwargs))
            answer = self.answers.get(name, {})
            if isinstance(answer, Exception):
                raise answer
            return answer(*args, **kwargs) if callable(answer) else answer

        return call

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


class Waiter:
    def wait(self, **_: Any) -> None:
        return None


@pytest.fixture
def setup_step(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    sent: list[dict[str, Any]] = []
    fake_cfnresponse = types.ModuleType("cfnresponse")
    fake_cfnresponse.SUCCESS = "SUCCESS"  # type: ignore[attr-defined]
    fake_cfnresponse.FAILED = "FAILED"  # type: ignore[attr-defined]

    def send(event, context, status, data, physical, reason=None):  # type: ignore[no-untyped-def]
        sent.append({"status": status, "data": data, "physical": physical, "reason": reason})

    fake_cfnresponse.send = send  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cfnresponse", fake_cfnresponse)
    spec = importlib.util.spec_from_file_location("access_setup", SETUP_SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    clients: dict[str, Fake] = {}
    monkeypatch.setattr(module.boto3, "client", lambda name, **_: clients[name])
    posted: list[dict[str, Any]] = []
    monkeypatch.setattr(module.urllib.request, "urlopen", _accepting(posted))
    for name in module.FIXED:  # the template sets these from the stack's parameters
        monkeypatch.setenv(name, PROPS[name])
    return types.SimpleNamespace(
        module=module, clients=clients, sent=sent, posted=posted, monkeypatch=monkeypatch
    )


def _accepting(posted: list[dict[str, Any]]):  # type: ignore[no-untyped-def]
    class Answer(io.BytesIO):
        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_: Any) -> None:
            return None

    def urlopen(request, timeout):  # type: ignore[no-untyped-def]
        posted.append({"url": request.full_url, **json.loads(request.data)})
        return Answer(b"{}")

    return urlopen


PROPS = {
    "AccountId": "202951752028",
    "ExternalId": "x" * 40,
    "CloudopticsPrincipalArn": "arn:aws:iam::000000000000:role/CloudopticsBroker",
    "ResourceTemplateUrl": "https://b.s3.eu-west-2.amazonaws.com/cfn/v1/resource-role.yaml",
    "CallbackUrl": "https://cloudoptics.example/api/v1/cfn/callback",
    "ConnectionRef": "c:018f0000-0000-7000-8000-000000000001",
    "TenantId": "018f0000-0000-7000-8000-000000000002",
    "BucketName": "cloudoptics-cur-202951752028",
    "CurPrefix": "cloudoptics",
    "StackName": "cloudoptics-access",
    "TemplateVersion": "2.0.0",
}


def run(step: Any, action: str, kind: str = "Create", **extra: Any) -> dict[str, Any]:
    event = {"RequestType": kind, "ResourceProperties": {**PROPS, **extra, "Action": action}}
    step.module.handler(event, None)
    return step.sent[-1]


def test_cleanup_removes_the_old_stacks_stackset_first(setup_step: Any) -> None:
    present = {"cloudoptics-resource-stackset", "cloudoptics-billing-access"}

    def describe(**kwargs: str) -> Any:
        if kwargs["StackName"] not in present:
            raise client_error("ValidationError")
        return {"Stacks": [{"StackStatus": "CREATE_COMPLETE"}]}

    cfn = Fake(describe_stacks=describe, get_waiter=lambda name: Waiter())
    setup_step.clients["cloudformation"] = cfn
    reply = run(setup_step, "cleanup")
    assert reply["status"] == "SUCCESS"
    deleted = [kw["StackName"] for name, kw in cfn.calls if name == "delete_stack"]
    assert deleted == ["cloudoptics-resource-stackset", "cloudoptics-billing-access"]


def test_cleanup_does_nothing_on_update(setup_step: Any) -> None:
    cfn = Fake()
    setup_step.clients["cloudformation"] = cfn
    assert run(setup_step, "cleanup", kind="Update")["status"] == "SUCCESS"
    assert cfn.calls == []


def test_bucket_is_reused_when_an_earlier_stack_left_it(setup_step: Any) -> None:
    s3 = Fake()
    setup_step.clients["s3"] = s3
    reply = run(setup_step, "bucket")
    assert reply["data"] == {"BucketName": PROPS["BucketName"], "Reused": "true"}
    assert "create_bucket" not in s3.names()
    assert "put_bucket_policy" in s3.names()


def test_bucket_is_created_when_missing(setup_step: Any) -> None:
    s3 = Fake(head_bucket=client_error("404"))
    setup_step.clients["s3"] = s3
    assert run(setup_step, "bucket")["data"]["Reused"] == "false"
    assert s3.names()[:2] == ["head_bucket", "create_bucket"]


def test_bucket_owned_by_someone_else_fails_clearly(setup_step: Any) -> None:
    setup_step.clients["s3"] = Fake(head_bucket=client_error("403"))
    reply = run(setup_step, "bucket")
    assert reply["status"] == "FAILED"
    assert "another AWS account" in reply["reason"]


def test_standalone_account_reports_back_without_a_rollout(setup_step: Any) -> None:
    setup_step.clients["organizations"] = Fake(
        describe_organization=client_error("AWSOrganizationsNotInUseException")
    )
    cfn = Fake()
    setup_step.clients["cloudformation"] = cfn
    reply = run(setup_step, "setup")
    assert reply == {
        "status": "SUCCESS",
        "data": {
            "Management": "false",
            "MemberRollout": "not an Organization's management account",
        },
        "physical": "cloudoptics-setup",
        "reason": None,
    }
    assert cfn.calls == []
    [posted] = setup_step.posted
    assert posted["url"] == PROPS["CallbackUrl"]
    assert posted["requestType"] == "Create"
    assert posted["accountId"] == PROPS["AccountId"]
    assert posted["management"] is False
    assert posted["templateVersion"] == "2.0.0"


def test_management_account_rolls_the_role_out_to_members(setup_step: Any) -> None:
    setup_step.clients["organizations"] = Fake(
        describe_organization={
            "Organization": {"MasterAccountId": PROPS["AccountId"], "FeatureSet": "ALL"}
        },
        list_roots={"Roots": [{"Id": "r-ab12"}]},
    )
    cfn = Fake(
        describe_organizations_access={"Status": "ENABLED"},
        describe_stack_set=client_error("StackSetNotFoundException"),
    )
    setup_step.clients["cloudformation"] = cfn
    reply = run(setup_step, "setup")
    assert reply["data"] == {"Management": "true", "MemberRollout": "started"}
    calls = dict(cfn.calls)
    assert calls["create_stack_set"]["PermissionModel"] == "SERVICE_MANAGED"
    assert calls["create_stack_set"]["AutoDeployment"]["Enabled"] is True
    created = calls["create_stack_set"]["Parameters"]
    params = {p["ParameterKey"]: p["ParameterValue"] for p in created}
    assert params == {
        "ExternalId": PROPS["ExternalId"],
        "CloudopticsPrincipalArn": PROPS["CloudopticsPrincipalArn"],
        "RoleName": aws.ACCESS_RESOURCE_ROLE_NAME,
    }
    assert calls["create_stack_instances"]["DeploymentTargets"] == {
        "OrganizationalUnitIds": ["r-ab12"]
    }
    assert setup_step.posted[0]["management"] is True


def test_rollout_and_callback_ignore_values_from_the_invoking_event(setup_step: Any) -> None:
    """Someone allowed to invoke the function must not choose what member accounts get."""
    setup_step.clients["organizations"] = Fake(
        describe_organization={
            "Organization": {"MasterAccountId": PROPS["AccountId"], "FeatureSet": "ALL"}
        },
        list_roots={"Roots": [{"Id": "r-ab12"}]},
    )
    cfn = Fake(
        describe_organizations_access={"Status": "ENABLED"},
        describe_stack_set=client_error("StackSetNotFoundException"),
    )
    setup_step.clients["cloudformation"] = cfn
    forged = {
        "ResourceTemplateUrl": "https://evil.example/resource-role.yaml",
        "CloudopticsPrincipalArn": "arn:aws:iam::666666666666:role/Attacker",
        "ExternalId": "attacker-chosen",
        "CallbackUrl": "https://evil.example/v1/cfn/callback",
    }
    run(setup_step, "setup", **forged)
    created = dict(cfn.calls)["create_stack_set"]
    assert created["TemplateURL"] == PROPS["ResourceTemplateUrl"]
    params = {p["ParameterKey"]: p["ParameterValue"] for p in created["Parameters"]}
    assert params["CloudopticsPrincipalArn"] == PROPS["CloudopticsPrincipalArn"]
    assert params["ExternalId"] == PROPS["ExternalId"]
    [posted] = setup_step.posted
    assert posted["url"] == PROPS["CallbackUrl"]


def test_the_setup_function_gets_the_fixed_values_from_the_stack(setup_step: Any) -> None:
    function = load("cloudoptics-access.yaml")["Resources"]["SetupFunction"]["Properties"]
    assert function["Environment"]["Variables"] == {
        name: {"Ref": name} for name in setup_step.module.FIXED
    }


def test_read_roles_leave_out_calls_that_can_return_secrets() -> None:
    for name, policies in (
        ("cloudoptics-access.yaml", ["ResourceRole"]),
        ("resource-role.yaml", ["ResourceRole"]),
    ):
        resources = load(name)["Resources"]
        for resource in policies:
            for policy in resources[resource]["Properties"]["Policies"]:
                for stmt in policy["PolicyDocument"]["Statement"]:
                    for action in _actions(stmt):
                        service = action.split(":")[0]
                        assert service not in {"lambda", "route53", "autoscaling"}, action
                        assert action != "ec2:Describe*", action


def test_a_failed_member_cleanup_on_delete_is_reported(setup_step: Any) -> None:
    setup_step.clients["organizations"] = Fake(
        describe_organization={
            "Organization": {"MasterAccountId": PROPS["AccountId"], "FeatureSet": "ALL"}
        },
        list_roots={"Roots": [{"Id": "r-ab12"}]},
    )
    setup_step.clients["cloudformation"] = Fake(
        list_stack_set_operations={"Summaries": []},
        list_stack_instances={"Summaries": [{"Account": "172630973115"}]},
        delete_stack_instances={"OperationId": "op2"},
        describe_stack_set_operation={"StackSetOperation": {"Status": "FAILED"}},
    )
    assert run(setup_step, "setup", kind="Delete")["status"] == "SUCCESS"  # never blocks
    [posted] = setup_step.posted
    assert posted["memberRollout"].startswith("failed: member roles not removed")


def test_update_refreshes_the_existing_rollout(setup_step: Any) -> None:
    setup_step.clients["organizations"] = Fake(
        describe_organization={
            "Organization": {"MasterAccountId": PROPS["AccountId"], "FeatureSet": "ALL"}
        },
        list_roots={"Roots": [{"Id": "r-ab12"}]},
    )
    cfn = Fake(
        describe_organizations_access={"Status": "ENABLED"},
        list_stack_set_operations={"Summaries": []},
        update_stack_set={"OperationId": "op1"},
        describe_stack_set_operation={"StackSetOperation": {"Status": "SUCCEEDED"}},
        list_stack_instances={"Summaries": [{"Account": "172630973115"}]},
    )
    setup_step.clients["cloudformation"] = cfn
    assert run(setup_step, "setup", kind="Update")["status"] == "SUCCESS"
    assert "update_stack_set" in cfn.names()
    assert "create_stack_set" not in cfn.names()
    assert "create_stack_instances" not in cfn.names()


def test_delete_removes_the_rollout_and_tells_cloudoptics(setup_step: Any) -> None:
    setup_step.clients["organizations"] = Fake(
        describe_organization={
            "Organization": {"MasterAccountId": PROPS["AccountId"], "FeatureSet": "ALL"}
        },
        list_roots={"Roots": [{"Id": "r-ab12"}]},
    )
    cfn = Fake(
        list_stack_set_operations={"Summaries": []},
        list_stack_instances={"Summaries": [{"Account": "172630973115"}]},
        delete_stack_instances={"OperationId": "op2"},
        describe_stack_set_operation={"StackSetOperation": {"Status": "SUCCEEDED"}},
    )
    setup_step.clients["cloudformation"] = cfn
    assert run(setup_step, "setup", kind="Delete")["status"] == "SUCCESS"
    assert cfn.names()[-1] == "delete_stack_set"
    assert setup_step.posted[0]["requestType"] == "Delete"


def test_a_refused_callback_fails_the_new_stack_with_the_reason(setup_step: Any) -> None:
    setup_step.clients["organizations"] = Fake(
        describe_organization=client_error("AWSOrganizationsNotInUseException")
    )

    def refuse(request, timeout):  # type: ignore[no-untyped-def]
        raise urllib.error.HTTPError(
            request.full_url, 409, "Conflict", {}, io.BytesIO(b"already connected")
        )

    setup_step.monkeypatch.setattr(setup_step.module.urllib.request, "urlopen", refuse)
    reply = run(setup_step, "setup")
    assert reply["status"] == "FAILED"
    assert "HTTP 409 already connected" in reply["reason"]


def test_a_failed_callback_does_not_block_a_delete(setup_step: Any) -> None:
    setup_step.clients["organizations"] = Fake(
        describe_organization=client_error("AWSOrganizationsNotInUseException")
    )

    def down(request, timeout):  # type: ignore[no-untyped-def]
        raise OSError("unreachable")

    setup_step.monkeypatch.setattr(setup_step.module.urllib.request, "urlopen", down)
    assert run(setup_step, "setup", kind="Delete")["status"] == "SUCCESS"


def test_setup_source_is_plain_python() -> None:
    compile(Path(SETUP_SOURCE).read_text(), str(SETUP_SOURCE), "exec")


def test_access_denied_is_retried_while_iam_catches_up(setup_step: Any) -> None:
    tries = iter([client_error("AccessDenied"), client_error("AccessDenied")])

    def describe() -> Any:
        error = next(tries, None)
        if error:
            raise error
        raise client_error("AWSOrganizationsNotInUseException")

    setup_step.clients["organizations"] = Fake(describe_organization=describe)
    assert run(setup_step, "setup")["status"] == "SUCCESS"
    assert len(setup_step.clients["organizations"].calls) == 3


def test_refused_trusted_access_is_reported_and_billing_still_connects(
    setup_step: Any,
) -> None:
    """AWS allows turning it on only through CloudFormation, so there is no other route."""
    cfn = _management(
        setup_step, activate_organizations_access=client_error("AccessDeniedException")
    )
    reply = run(setup_step, "setup")
    assert reply["status"] == "SUCCESS"
    assert reply["data"]["MemberRollout"].startswith("failed: ClientError")
    assert cfn.names().count("activate_organizations_access") == 12  # IAM propagation retries
    assert "create_stack_set" not in cfn.names()
    assert setup_step.posted[0]["memberRollout"].startswith("failed")


def test_consolidated_billing_only_skips_the_member_rollout(setup_step: Any) -> None:
    cfn = _management(setup_step)
    setup_step.clients["organizations"].answers["describe_organization"] = {
        "Organization": {
            "MasterAccountId": PROPS["AccountId"],
            "FeatureSet": "CONSOLIDATED_BILLING",
        }
    }
    reply = run(setup_step, "setup")
    assert reply["status"] == "SUCCESS"
    assert reply["data"]["MemberRollout"].startswith("skipped: turn on all features")
    assert "create_stack_set" not in cfn.names()
    assert setup_step.posted[0]["management"] is True


NO_ORG_ACCESS = ClientError(
    {
        "Error": {
            "Code": "ValidationError",
            "Message": "You must enable organizations access to operate a service managed "
            "stack set",
        }
    },
    "CreateStackSet",
)


def _management(setup_step: Any, **cfn_answers: Any) -> Any:
    setup_step.clients["organizations"] = Fake(
        describe_organization={
            "Organization": {"MasterAccountId": PROPS["AccountId"], "FeatureSet": "ALL"}
        },
        list_roots={"Roots": [{"Id": "r-ab12"}]},
    )
    answers = {
        "describe_organizations_access": {"Status": "ENABLED"},
        "describe_stack_set": client_error("StackSetNotFoundException"),
        **cfn_answers,
    }
    cfn = Fake(**answers)
    setup_step.clients["cloudformation"] = cfn
    return cfn


def test_trusted_access_is_asked_for_even_when_it_reads_as_on(setup_step: Any) -> None:
    cfn = _management(setup_step)
    assert run(setup_step, "setup")["status"] == "SUCCESS"
    assert "activate_organizations_access" in cfn.names()


def test_stackset_waits_for_trusted_access_to_reach_stacksets(setup_step: Any) -> None:
    tries = iter([NO_ORG_ACCESS, NO_ORG_ACCESS])

    def create(**_: Any) -> Any:
        error = next(tries, None)
        if error:
            raise error
        return {"StackSetId": "s1"}

    cfn = _management(setup_step, create_stack_set=create)
    reply = run(setup_step, "setup")
    assert reply["data"]["MemberRollout"] == "started"
    assert cfn.names().count("create_stack_set") == 3
    assert cfn.names().count("activate_organizations_access") == 3
    assert "create_stack_instances" in cfn.names()


def test_a_failed_rollout_keeps_the_stack_and_still_connects(setup_step: Any) -> None:
    cfn = _management(setup_step, create_stack_set=NO_ORG_ACCESS)
    reply = run(setup_step, "setup")
    assert reply["status"] == "SUCCESS"
    assert reply["data"]["MemberRollout"].startswith("failed: ClientError")
    assert cfn.names().count("create_stack_set") == setup_step.module.ORG_ACCESS_TRIES
    assert setup_step.posted[0]["requestType"] == "Create"


class Context:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    def get_remaining_time_in_millis(self) -> int:
        return int(self.seconds * 1000)


def test_setup_answers_before_the_function_times_out(setup_step: Any) -> None:
    """A slow rollout gives up in time to report back, instead of leaving no reply."""
    clock = {"now": 0.0}
    setup_step.monkeypatch.setattr(setup_step.module.time, "time", lambda: clock["now"])

    def sleep(seconds: float) -> None:
        clock["now"] += seconds

    setup_step.monkeypatch.setattr(setup_step.module.time, "sleep", sleep)
    cfn = _management(
        setup_step,
        describe_organizations_access={"Status": "DISABLED"},
        create_stack_set=NO_ORG_ACCESS,
    )
    event = {"RequestType": "Create", "ResourceProperties": {**PROPS, "Action": "setup"}}
    setup_step.module.handler(event, Context(900))
    reply = setup_step.sent[-1]
    assert reply["status"] == "SUCCESS"
    assert reply["data"]["MemberRollout"].startswith("failed")
    assert clock["now"] < 900 - 60  # time left to send the reply
    assert setup_step.posted[0]["memberRollout"].startswith("failed")
    assert "create_stack_set" in cfn.names()


def test_trusted_access_may_read_the_stacksets_service_linked_roles() -> None:
    statements = load("cloudoptics-access.yaml")["Resources"]["SetupRole"]["Properties"][
        "Policies"
    ][0]["PolicyDocument"]["Statement"]
    [read] = [s for s in statements if s["Sid"] == "StackSetsServiceLinkedRoleRead"]
    assert read["Action"] == "iam:GetRole"


def test_trusted_access_may_create_the_stacksets_service_linked_role() -> None:
    # ActivateOrganizationsAccess was refused iam:CreateServiceLinkedRole under an
    # iam:AWSServiceName condition: CloudFormation's request does not carry that key.
    statements = load("cloudoptics-access.yaml")["Resources"]["SetupRole"]["Properties"][
        "Policies"
    ][0]["PolicyDocument"]["Statement"]
    [slr] = [s for s in statements if s["Sid"] == "StackSetsServiceLinkedRole"]
    assert slr["Action"] == "iam:CreateServiceLinkedRole"
    assert "Condition" not in slr
    assert "role/aws-service-role/stacksets.cloudformation.amazonaws.com*" in slr["Resource"]["Sub"]


def test_create_stack_set_is_allowed_on_every_resource() -> None:
    # CreateStackSet has no resource-level permissions, so a stack set ARN never matches.
    statements = load("cloudoptics-access.yaml")["Resources"]["SetupRole"]["Properties"][
        "Policies"
    ][0]["PolicyDocument"]["Statement"]
    [create] = [s for s in statements if "cloudformation:CreateStackSet" in _actions(s)]
    assert create["Resource"] == "*"


def test_a_long_rollout_failure_is_kept_whole(setup_step: Any) -> None:
    message = "User: arn:aws:sts::1:assumed-role/x is not authorized to perform: " + "x" * 600
    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": message}}, "CreateStackSet")
    _management(setup_step, create_stack_set=denied)
    setup_step.module.DENIED = ()  # no IAM catch-up retries in this test
    rollout = run(setup_step, "setup")["data"]["MemberRollout"]
    assert len(rollout) > 600 and rollout.endswith("x")


def test_a_failed_member_removal_does_not_delete_the_stack_set(setup_step: Any) -> None:
    setup_step.clients["organizations"] = Fake(
        describe_organization={
            "Organization": {"MasterAccountId": PROPS["AccountId"], "FeatureSet": "ALL"}
        },
        list_roots={"Roots": [{"Id": "r-ab12"}]},
    )
    cfn = Fake(
        list_stack_set_operations={"Summaries": []},
        list_stack_instances={"Summaries": [{"Account": "172630973115"}]},
        delete_stack_instances={"OperationId": "op2"},
        describe_stack_set_operation={"StackSetOperation": {"Status": "FAILED"}},
    )
    setup_step.clients["cloudformation"] = cfn
    assert run(setup_step, "setup", kind="Delete")["status"] == "SUCCESS"
    assert "delete_stack_set" not in cfn.names()


def test_custom_resources_time_out_soon_after_the_function() -> None:
    resources = load("cloudoptics-access.yaml")["Resources"]
    timeout = resources["SetupFunction"]["Properties"]["Timeout"]
    for name in ("CleanUp", "CurBucket", "Setup"):
        assert timeout < int(resources[name]["Properties"]["ServiceTimeout"]) <= timeout + 120


class _Pages:
    def __init__(self, pages: list[dict[str, Any]] | Exception) -> None:
        self.pages = pages

    def paginate(self, **_: Any) -> Any:
        if isinstance(self.pages, Exception):
            raise self.pages
        return iter(self.pages)


class _Session:
    def __init__(self, pages: list[dict[str, Any]] | Exception) -> None:
        self.pages = pages

    def client(self, service: str, region: str = "us-east-1") -> Any:
        assert service == "cloudformation"
        return types.SimpleNamespace(get_paginator=lambda name: _Pages(self.pages))


def test_member_rollout_is_explained_in_plain_words() -> None:
    summaries = [
        {"Account": "111111111111", "StackInstanceStatus": {"DetailedStatus": "RUNNING"}},
        {
            "Account": "222222222222",
            "Status": "OUTDATED",
            "StatusReason": "Account 222222222222 should have the service-linked role",
            "StackInstanceStatus": {"DetailedStatus": "FAILED"},
        },
        {"Account": "333333333333", "StackInstanceStatus": {"DetailedStatus": "SUCCEEDED"}},
    ]
    words = aws.member_rollout(_Session([{"Summaries": summaries}]))  # type: ignore[arg-type]
    assert isinstance(words, dict)
    assert "adding" in words["111111111111"]
    assert "should have the service-linked role" in words["222222222222"]
    assert "in place" in words["333333333333"]
    refused = aws.member_rollout(_Session(client_error("AccessDenied")))  # type: ignore[arg-type]
    assert refused == "AccessDenied"


def test_billing_role_may_read_only_its_own_member_rollout() -> None:
    role = load("cloudoptics-access.yaml")["Resources"]["BillingRole"]["Properties"]
    [stmt] = [
        s
        for p in role["Policies"]
        for s in p["PolicyDocument"]["Statement"]
        if s["Sid"] == "ReadMemberRollout"
    ]
    assert all(a.split(":")[1].startswith(("Describe", "List")) for a in stmt["Action"])
    assert all(aws.MEMBER_STACKSET_NAME in str(r) for r in stmt["Resource"])
