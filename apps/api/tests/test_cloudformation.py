"""Static checks on the customer-facing CloudFormation templates (read-only promise)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

TEMPLATES = Path(__file__).resolve().parents[3] / "infra" / "cloudformation"
WRITE_VERBS = (
    "Create",
    "Put",
    "Delete",
    "Modify",
    "Update",
    "Tag",
    "Purchase",
    "Attach",
    "Start",
    "Stop",
    "Terminate",
    "Run",
    "Release",
    "Accept",
    "Reject",
)


class CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader: yaml.Loader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        return {suffix: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {suffix: loader.construct_sequence(node, deep=True)}
    return {suffix: loader.construct_mapping(node, deep=True)}  # type: ignore[arg-type]


CfnLoader.add_multi_constructor("!", _tag)


def load(name: str) -> dict[str, Any]:
    return yaml.load((TEMPLATES / name).read_text(), Loader=CfnLoader)  # noqa: S506


def role_actions(template: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    for resource in template["Resources"].values():
        if resource["Type"] != "AWS::IAM::Role":
            continue
        for policy in resource["Properties"]["Policies"]:
            for stmt in policy["PolicyDocument"]["Statement"]:
                if "If" in stmt:  # Fn::If [condition, statement, NoValue]
                    stmt = stmt["If"][1]
                assert stmt["Effect"] == "Allow"
                acts = stmt["Action"]
                actions += [acts] if isinstance(acts, str) else acts
    return actions


@pytest.mark.parametrize("name", ["billing-role.yaml", "resource-role.yaml"])
def test_roles_are_read_only(name: str) -> None:
    actions = role_actions(load(name))
    assert actions
    for action in actions:
        service, verb = action.split(":")
        assert service not in {"support", "iam", "sts", "secretsmanager", "ssm", "kms"}, action
        assert not verb.startswith(WRITE_VERBS), action
        assert action != "*" and verb != "*", action
    assert "s3:GetObject" not in actions or name == "billing-role.yaml"


@pytest.mark.parametrize("name", ["billing-role.yaml", "resource-role.yaml"])
def test_trust_requires_external_id_and_short_sessions(name: str) -> None:
    template = load(name)
    roles = [r for r in template["Resources"].values() if r["Type"] == "AWS::IAM::Role"]
    assert len(roles) == 1
    props = roles[0]["Properties"]
    assert props["MaxSessionDuration"] == 3600
    [stmt] = props["AssumeRolePolicyDocument"]["Statement"]
    assert stmt["Condition"]["StringEquals"]["sts:ExternalId"] == {"Ref": "ExternalId"}
    assert template["Parameters"]["ExternalId"]["NoEcho"] is True
    assert template["Parameters"]["ExternalId"]["MinLength"] >= 32


def test_billing_role_reads_only_the_export_bucket() -> None:
    template = load("billing-role.yaml")
    policies = template["Resources"]["BillingRole"]["Properties"]["Policies"]
    statements = policies[0]["PolicyDocument"]["Statement"]
    s3 = next(s["If"][1] for s in statements if "If" in s)
    assert s3["Sid"] == "ReadExportBucketOnly"
    assert all("If" in r for r in s3["Resource"])


def test_stackset_rolls_the_resource_role_out_to_the_organization() -> None:
    template = load("resource-stackset.yaml")
    [stackset] = template["Resources"].values()
    props = stackset["Properties"]
    assert stackset["Type"] == "AWS::CloudFormation::StackSet"
    assert props["PermissionModel"] == "SERVICE_MANAGED"
    assert props["AutoDeployment"]["Enabled"] is True  # new accounts get the role too
    assert props["Capabilities"] == ["CAPABILITY_NAMED_IAM"]
    assert props["TemplateURL"] == {"Ref": "ResourceTemplateUrl"}
    passed = {p["ParameterKey"]: p["ParameterValue"] for p in props["Parameters"]}
    assert passed == {
        "ExternalId": {"Ref": "ExternalId"},
        "CloudopticsPrincipalArn": {"Ref": "CloudopticsPrincipalArn"},
    }
    assert template["Parameters"]["ExternalId"]["NoEcho"] is True
    [group] = props["StackInstancesGroup"]
    assert group["DeploymentTargets"]["OrganizationalUnitIds"] == [{"Ref": "OrganizationRootId"}]
