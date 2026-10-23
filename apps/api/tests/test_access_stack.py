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
        ("BillingRole", "CloudopticsBillingReadOnly"),
        ("ResourceRole", "CloudopticsResourceReadOnly"),
    ):
        role = resources[name]
        props = role["Properties"]
        assert props["RoleName"] == role_name
        assert role["DependsOn"] == "CleanUp"  # the old stacks with these names go first
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


def test_setup_role_writes_only_to_cloudoptics_resources() -> None:
    props = load("cloudoptics-access.yaml")["Resources"]["SetupRole"]["Properties"]
    assert props["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {
        "Service": "lambda.amazonaws.com"
    }
    unscoped_writes = {
        # No resource-level scope exists for these.
        "cloudformation:ActivateOrganizationsAccess",
        "organizations:EnableAWSServiceAccess",
        "iam:CreateServiceLinkedRole",  # limited by a condition to the StackSets role
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
                assert "cloudoptics" in text or "type/resource" in text, (action, resource)


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
        "data": {"Management": "false"},
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
        describe_organization={"Organization": {"MasterAccountId": PROPS["AccountId"]}},
        list_roots={"Roots": [{"Id": "r-ab12"}]},
    )
    cfn = Fake(
        describe_organizations_access={"Status": "ENABLED"},
        describe_stack_set=client_error("StackSetNotFoundException"),
    )
    setup_step.clients["cloudformation"] = cfn
    reply = run(setup_step, "setup")
    assert reply["data"] == {"Management": "true"}
    calls = dict(cfn.calls)
    assert calls["create_stack_set"]["PermissionModel"] == "SERVICE_MANAGED"
    assert calls["create_stack_set"]["AutoDeployment"]["Enabled"] is True
    created = calls["create_stack_set"]["Parameters"]
    params = {p["ParameterKey"]: p["ParameterValue"] for p in created}
    assert params == {
        "ExternalId": PROPS["ExternalId"],
        "CloudopticsPrincipalArn": PROPS["CloudopticsPrincipalArn"],
    }
    assert calls["create_stack_instances"]["DeploymentTargets"] == {
        "OrganizationalUnitIds": ["r-ab12"]
    }
    assert setup_step.posted[0]["management"] is True


def test_update_refreshes_the_existing_rollout(setup_step: Any) -> None:
    setup_step.clients["organizations"] = Fake(
        describe_organization={"Organization": {"MasterAccountId": PROPS["AccountId"]}},
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
        describe_organization={"Organization": {"MasterAccountId": PROPS["AccountId"]}},
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
