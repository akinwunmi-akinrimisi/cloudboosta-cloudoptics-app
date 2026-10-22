# ruff: noqa: N803, N818  (the fake mirrors boto3's keyword names)
"""infra/aws/bootstrap.py: IAM objects are created in an order AWS accepts."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

BOOTSTRAP = Path(__file__).resolve().parents[3] / "infra" / "aws" / "bootstrap.py"
spec = importlib.util.spec_from_file_location("bootstrap", BOOTSTRAP)
assert spec and spec.loader
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class NoSuchEntity(Exception):
    pass


def malformed(message: str) -> ClientError:
    return ClientError({"Error": {"Code": "MalformedPolicyDocument", "Message": message}}, "op")


class FakeIAM:
    """Rejects a trust policy naming a user that does not exist (or is not visible yet)."""

    exceptions = type("E", (), {"NoSuchEntityException": NoSuchEntity})

    def __init__(self, invisible_for: int = 0) -> None:
        self.users: set[str] = set()
        self.roles: dict[str, str] = {}
        self.keys: list[dict[str, Any]] = []
        self.invisible_for = invisible_for

    def _check_principal(self, trust: str) -> None:
        if f"user/{bootstrap.USER}" in trust:
            if bootstrap.USER not in self.users:
                raise malformed("Invalid principal in policy")
            if self.invisible_for:
                self.invisible_for -= 1
                raise malformed("Invalid principal in policy")

    def get_user(self, UserName: str) -> None:
        if UserName not in self.users:
            raise NoSuchEntity

    def create_user(self, UserName: str, Tags: Any) -> None:
        self.users.add(UserName)

    def get_role(self, RoleName: str) -> dict[str, Any]:
        if RoleName not in self.roles:
            raise NoSuchEntity
        return {"Role": {"Arn": self.roles[RoleName]}}

    def create_role(self, RoleName: str, AssumeRolePolicyDocument: str, **_: Any) -> Any:
        self._check_principal(AssumeRolePolicyDocument)
        self.roles[RoleName] = f"arn:aws:iam::1:role/{RoleName}"
        return {"Role": {"Arn": self.roles[RoleName]}}

    def update_assume_role_policy(self, RoleName: str, PolicyDocument: str) -> None:
        self._check_principal(PolicyDocument)

    def put_role_policy(self, **_: Any) -> None: ...

    def put_user_policy(self, **_: Any) -> None: ...

    def list_access_keys(self, UserName: str) -> dict[str, Any]:
        return {"AccessKeyMetadata": list(self.keys)}

    def delete_access_key(self, UserName: str, AccessKeyId: str) -> None:
        self.keys = [k for k in self.keys if k["AccessKeyId"] != AccessKeyId]

    def create_access_key(self, UserName: str) -> dict[str, Any]:
        n = len(self.keys) + 1
        self.keys.append({"AccessKeyId": f"AKIA{n}", "CreateDate": datetime.now(UTC)})
        return {"AccessKey": {"AccessKeyId": f"AKIA{n}", "SecretAccessKey": "s"}}


def run(iam: FakeIAM) -> tuple[str, dict[str, str]]:
    bootstrap.ensure_user(iam)
    arn = bootstrap.ensure_role(iam, "1")
    return arn, bootstrap.rotate_user_key(iam, "1")


def test_first_run_creates_user_before_the_role_that_trusts_it() -> None:
    iam = FakeIAM()
    arn, key = run(iam)
    assert arn.endswith("role/CloudopticsBroker")
    assert key["id"] == "AKIA1"


def test_waits_for_a_new_user_to_become_a_valid_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _: None)
    arn, _ = run(FakeIAM(invisible_for=3))
    assert arn.endswith("role/CloudopticsBroker")


def test_rerun_keeps_a_single_fresh_key() -> None:
    iam = FakeIAM()
    run(iam)
    _, key = run(iam)
    assert [k["AccessKeyId"] for k in iam.keys] == [key["id"]]
