# ruff: noqa: N803  (the fakes mirror boto3's keyword names)
"""infra/aws/bootstrap.py: the KMS key, Roles Anywhere and removing the broker user."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_bootstrap import FakeIAM, bootstrap

from cloudoptics.connectors import rolesanywhere

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
ACCOUNT = "111111111111"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/CloudopticsBroker"


class IAM(FakeIAM):
    def __init__(self) -> None:
        super().__init__()
        self.trust: list[dict[str, Any]] = []
        self.deleted_user = False

    def _check_principal(self, trust: str) -> None:
        self.trust.append(json.loads(trust))
        super()._check_principal(trust)

    def list_user_policies(self, UserName: str) -> dict[str, Any]:
        return {"PolicyNames": ["assume-broker-only"]}

    def delete_user_policy(self, UserName: str, PolicyName: str) -> None: ...

    def delete_user(self, UserName: str) -> None:
        self.users.discard(UserName)
        self.deleted_user = True


class NotFoundError(Exception):
    pass


class KMS:
    exceptions = type("E", (), {"NotFoundException": NotFoundError})

    def __init__(self, exists: bool = False) -> None:
        self.arn = f"arn:aws:kms:eu-west-2:{ACCOUNT}:key/k1" if exists else None
        self.calls: list[str] = []
        self.policy: dict[str, Any] | None = None

    def describe_key(self, KeyId: str) -> dict[str, Any]:
        assert KeyId == bootstrap.KMS_ALIAS
        if not self.arn:
            raise NotFoundError
        return {"KeyMetadata": {"Arn": self.arn}}

    def put_key_policy(self, KeyId: str, PolicyName: str, Policy: str) -> None:
        self.calls.append("put_key_policy")
        self.policy = json.loads(Policy)

    def create_key(self, Policy: str, **kw: Any) -> dict[str, Any]:
        assert kw["KeySpec"] == "SYMMETRIC_DEFAULT" and kw["KeyUsage"] == "ENCRYPT_DECRYPT"
        self.calls.append("create_key")
        self.policy = json.loads(Policy)
        self.arn = f"arn:aws:kms:eu-west-2:{ACCOUNT}:key/new"
        return {"KeyMetadata": {"Arn": self.arn}}

    def create_alias(self, AliasName: str, TargetKeyId: str) -> None:
        self.calls.append(f"create_alias {AliasName}")

    def enable_key_rotation(self, KeyId: str) -> None:
        self.calls.append("enable_key_rotation")


class Paginator:
    def __init__(self, key: str, items: list[dict[str, Any]]) -> None:
        self.key, self.items = key, items

    def paginate(self) -> list[dict[str, Any]]:
        return [{self.key: list(self.items)}]


class RolesAnywhere:
    def __init__(self) -> None:
        self.anchors: dict[str, dict[str, Any]] = {}
        self.profiles: list[dict[str, Any]] = []
        self.clock = NOW
        self.updated: list[str] = []

    def _tick(self) -> datetime:
        self.clock += timedelta(minutes=1)
        return self.clock

    def get_paginator(self, name: str) -> Paginator:
        if name == "list_trust_anchors":
            return Paginator("trustAnchors", list(self.anchors.values()))
        return Paginator("profiles", self.profiles)

    def create_trust_anchor(self, name: str, source: Any, enabled: bool, tags: Any) -> Any:
        anchor = {
            "name": name,
            "trustAnchorId": f"id-{name}",
            "trustAnchorArn": f"arn:aws:rolesanywhere:eu-west-2:{ACCOUNT}:trust-anchor/id-{name}",
            "enabled": enabled,
            "source": source,
            "updatedAt": self._tick(),
        }
        self.anchors[name] = anchor
        return {"trustAnchor": anchor}

    def update_trust_anchor(self, trustAnchorId: str, source: Any) -> None:
        for anchor in self.anchors.values():
            if anchor["trustAnchorId"] == trustAnchorId:
                anchor["source"], anchor["updatedAt"] = source, self._tick()
                self.updated.append(anchor["name"])

    def enable_trust_anchor(self, trustAnchorId: str) -> None: ...

    def create_profile(self, name: str, roleArns: list[str], **kw: Any) -> Any:
        prof = {
            "name": name,
            "profileId": "p1",
            "profileArn": f"arn:aws:rolesanywhere:eu-west-2:{ACCOUNT}:profile/p1",
            "roleArns": roleArns,
            "enabled": True,
        }
        self.profiles.append(prof)
        return {"profile": prof}

    def update_profile(self, profileId: str, roleArns: list[str], durationSeconds: int) -> None:
        assert durationSeconds == 3600
        self.profiles[0]["roleArns"] = roleArns

    def enable_profile(self, profileId: str) -> None: ...


class Session:
    def __init__(self, ra: RolesAnywhere) -> None:
        self.ra = ra

    def client(self, name: str) -> Any:
        assert name == "rolesanywhere"
        return self.ra


def test_the_trust_policy_can_trust_the_user_roles_anywhere_or_both() -> None:
    anchors = ["arn:b", "arn:a"]
    both = bootstrap.broker_trust_policy(ACCOUNT, anchor_arns=anchors)
    assert [s["Sid"] for s in both["Statement"]] == ["BrokerUser", "RolesAnywhere"]
    ra = bootstrap.broker_trust_policy(ACCOUNT, user=False, anchor_arns=anchors)["Statement"]
    assert ra == [
        {
            "Sid": "RolesAnywhere",
            "Effect": "Allow",
            "Principal": {"Service": "rolesanywhere.amazonaws.com"},
            "Action": ["sts:AssumeRole", "sts:TagSession", "sts:SetSourceIdentity"],
            "Condition": {
                "StringEquals": {
                    "aws:PrincipalTag/x509Subject/CN": "cloudoptics-staging-broker",
                    "aws:SourceAccount": ACCOUNT,
                },
                "ArnEquals": {"aws:SourceArn": ["arn:a", "arn:b"]},
            },
        }
    ]
    with pytest.raises(ValueError):
        bootstrap.broker_trust_policy(ACCOUNT, user=False)


def test_the_key_policy_lets_only_the_broker_use_tenant_data_keys() -> None:
    policy = bootstrap.kms_key_policy(ACCOUNT, ROLE_ARN)
    use = policy["Statement"][1]
    assert use["Principal"] == {"AWS": ROLE_ARN}
    assert use["Action"] == ["kms:GenerateDataKey", "kms:Decrypt"]
    assert use["Condition"]["Null"] == {"kms:EncryptionContext:tenant_id": "false"}
    assert use["Condition"]["StringEquals"] == {
        "kms:EncryptionContext:purpose": "connection-secret"
    }
    # The app's encryption context satisfies it.
    from cloudoptics.connectors.crypto import PURPOSE, encryption_context

    assert PURPOSE == "connection-secret"
    assert set(encryption_context(uuid.uuid4())) == {"tenant_id", "purpose"}


def test_the_kms_key_is_created_once_then_its_policy_is_kept_current() -> None:
    kms = KMS()
    arn = bootstrap.ensure_kms_key(kms, ACCOUNT, ROLE_ARN)
    assert arn.endswith("key/new")
    assert kms.calls == [
        "create_key",
        "create_alias alias/cloudoptics-staging-secrets",
        "enable_key_rotation",
    ]
    existing = KMS(exists=True)
    assert bootstrap.ensure_kms_key(existing, ACCOUNT, ROLE_ARN).endswith("key/k1")
    assert existing.calls == ["put_key_policy", "enable_key_rotation"]
    assert existing.policy == bootstrap.kms_key_policy(ACCOUNT, ROLE_ARN)


def test_certificates_meet_the_roles_anywhere_requirements() -> None:
    ca, ca_key = bootstrap.new_ca(NOW, 30)
    leaf, leaf_key = bootstrap.new_leaf(ca, ca_key, NOW, 30)
    assert ca.version == x509.Version.v3 and leaf.version == x509.Version.v3
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert leaf.extensions.get_extension_for_class(x509.KeyUsage).value.digital_signature
    assert leaf.signature_hash_algorithm is not None
    assert leaf.signature_hash_algorithm.name == "sha256"
    leaf.verify_directly_issued_by(ca)
    assert leaf.not_valid_after_utc == NOW + timedelta(days=30)
    assert ca.not_valid_after_utc > leaf.not_valid_after_utc
    cn = leaf.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
    assert cn == bootstrap.LEAF_CN
    assert isinstance(leaf_key, ec.EllipticCurvePrivateKey)


def test_each_rotation_replaces_the_older_anchor_and_the_app_can_sign_with_the_result() -> None:
    ra = RolesAnywhere()
    anchors = bootstrap.ensure_trust_anchors(ra, NOW)
    assert [a["name"] for a in anchors] == list(bootstrap.TRUST_ANCHORS)
    first = bootstrap.rotate_certificate(ra, anchors[0], NOW, 30)
    assert ra.updated == ["cloudoptics-staging-a"]
    again = bootstrap.ensure_trust_anchors(ra, NOW)
    bootstrap.rotate_certificate(ra, again[0], NOW, 30)
    assert ra.updated == ["cloudoptics-staging-a", "cloudoptics-staging-b"]
    # The certificate and key are what the app reads (base64 of PEM), and match each other.
    certs = rolesanywhere.load_certificates(first["certificate"])
    key = rolesanywhere.load_private_key(first["private_key"])
    ca_pem = ra.anchors["cloudoptics-staging-a"]["source"]["sourceData"]["x509CertificateData"]
    certs[0].verify_directly_issued_by(x509.load_pem_x509_certificate(ca_pem.encode()))
    signed = rolesanywhere.sign_create_session(
        certificate=certs[0], private_key=key, region="eu-west-2", body=b"{}", now=NOW
    )
    assert signed.headers["Authorization"].startswith("AWS4-X509-ECDSA-SHA256 ")
    pem = base64.b64decode(first["private_key"])
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert serialization.load_pem_private_key(pem, None)


def test_switching_to_roles_anywhere_keeps_the_user_until_asked_to_delete_it() -> None:
    ra, iam = RolesAnywhere(), IAM()
    inputs = {"broker": "roles-anywhere", "delete_user": False, "days": 30, "kms": "off"}
    outputs, secret = bootstrap.setup_broker(Session(ra), iam, ACCOUNT, inputs)
    assert outputs["CLOUDOPTICS_BROKER_CREDENTIALS"] == "roles_anywhere"
    assert outputs["CLOUDOPTICS_ROLES_ANYWHERE_ROLE_ARN"].endswith("role/CloudopticsBroker")
    assert outputs["CLOUDOPTICS_ROLES_ANYWHERE_PROFILE_ARN"].endswith("profile/p1")
    assert secret == {"CLOUDOPTICS_ROLES_ANYWHERE_PRIVATE_KEY"}
    assert "BROKER_AWS_SECRET_ACCESS_KEY" not in outputs
    assert [s["Sid"] for s in iam.trust[-1]["Statement"]] == ["BrokerUser", "RolesAnywhere"]
    assert not iam.deleted_user

    inputs["delete_user"] = True
    outputs, _ = bootstrap.setup_broker(Session(ra), iam, ACCOUNT, inputs)
    assert [s["Sid"] for s in iam.trust[-1]["Statement"]] == ["RolesAnywhere"]
    assert iam.deleted_user and outputs["BROKER_USER_DELETED"] == "true"
    assert len(ra.profiles) == 1


def test_the_user_key_path_says_so_explicitly() -> None:
    iam = IAM()
    inputs = {"broker": "iam-user", "delete_user": False, "days": 30, "kms": "off"}
    outputs, secret = bootstrap.setup_broker(None, iam, ACCOUNT, inputs)
    assert outputs["CLOUDOPTICS_BROKER_CREDENTIALS"] == "default_chain"
    assert secret == {"BROKER_AWS_SECRET_ACCESS_KEY"}


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"KMS_MODE": "on"}, "KMS_MODE"),
        ({"BROKER_CREDENTIALS": "keys"}, "BROKER_CREDENTIALS"),
        ({"DELETE_BROKER_USER": "true"}, "needs BROKER_CREDENTIALS=roles-anywhere"),
        ({"BROKER_CREDENTIALS": "roles-anywhere", "CERTIFICATE_DAYS": "365"}, "CERTIFICATE_DAYS"),
    ],
)
def test_inputs_are_checked(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    for name in ("KMS_MODE", "BROKER_CREDENTIALS", "DELETE_BROKER_USER", "CERTIFICATE_DAYS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit, match=message):
        bootstrap.read_inputs()


def test_default_inputs_change_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("KMS_MODE", "BROKER_CREDENTIALS", "DELETE_BROKER_USER", "CERTIFICATE_DAYS"):
        monkeypatch.setenv(name, "")
    assert bootstrap.read_inputs() == {
        "kms": "off",
        "broker": "iam-user",
        "delete_user": False,
        "days": 30,
    }
