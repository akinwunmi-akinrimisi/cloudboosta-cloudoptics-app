"""infra/digitalocean/provision.py: runtime settings are applied and never silently dropped."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

PROVISION = Path(__file__).resolve().parents[3] / "infra" / "digitalocean" / "provision.py"
spec = importlib.util.spec_from_file_location("provision", PROVISION)
assert spec and spec.loader
provision = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provision)


def app_spec() -> dict[str, Any]:
    base = [{"key": "CLOUDOPTICS_ENVIRONMENT", "value": "staging"}]
    return {
        "services": [{"name": "api", "envs": list(base)}],
        "workers": [{"name": "worker", "envs": list(base)}],
        "jobs": [{"name": "migrate", "envs": list(base)}],
    }


def envs(spec: dict[str, Any], name: str) -> dict[str, dict[str, Any]]:
    for comp in spec["services"] + spec["workers"] + spec["jobs"]:
        if comp["name"] == name:
            return {e["key"]: e for e in comp["envs"]}
    raise KeyError(name)


def test_supplied_values_reach_api_and_worker_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKOS_CLIENT_ID", "client_01")
    monkeypatch.setenv("WORKOS_API_KEY", "sk_live_x")
    monkeypatch.setenv("BROKER_AWS_ACCESS_KEY_ID", "AKIA1")
    monkeypatch.setenv("BROKER_AWS_SECRET_ACCESS_KEY", "s3cret")
    monkeypatch.setenv("CLOUDOPTICS_AWS_BROKER_ROLE_ARN", "arn:aws:iam::1:role/CloudopticsBroker")
    monkeypatch.setenv(
        "CLOUDOPTICS_CFN_BILLING_TEMPLATE_URL", "https://b.s3/cfn/v1/billing-role.yaml"
    )
    spec = app_spec()
    provision.apply_runtime_config(spec, None)
    for name in ("api", "worker"):
        e = envs(spec, name)
        assert e["CLOUDOPTICS_WORKOS_CLIENT_ID"]["value"] == "client_01"
        assert e["CLOUDOPTICS_WORKOS_API_KEY"]["type"] == "SECRET"
        assert e["AWS_SECRET_ACCESS_KEY"] == {
            "key": "AWS_SECRET_ACCESS_KEY",
            "value": "s3cret",
            "type": "SECRET",
            "scope": "RUN_TIME",
        }
        assert e["CLOUDOPTICS_CLOUDOPTICS_PRINCIPAL_ARN"]["value"].endswith("CloudopticsBroker")
        assert e["CLOUDOPTICS_CFN_QUICK_CREATE_ENABLED"]["value"] == "true"
        assert e["CLOUDOPTICS_ENVIRONMENT"]["value"] == "staging"
    assert "AWS_SECRET_ACCESS_KEY" not in envs(spec, "migrate")


def test_existing_values_survive_a_plain_redeploy(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (*provision.RUNTIME_PLAIN.values(), *provision.RUNTIME_SECRET.values()):
        monkeypatch.delenv(var, raising=False)
    current = app_spec()
    kept = {"key": "CLOUDOPTICS_WORKOS_API_KEY", "type": "SECRET", "value": "EV[1:abc]"}
    current["services"][0]["envs"].append(kept)
    spec = app_spec()
    provision.apply_runtime_config(spec, current)
    assert envs(spec, "api")["CLOUDOPTICS_WORKOS_API_KEY"] == kept
    assert "CLOUDOPTICS_WORKOS_API_KEY" not in envs(spec, "worker")


def test_a_new_value_replaces_the_old_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BROKER_AWS_ACCESS_KEY_ID", "AKIA-NEW")
    current = app_spec()
    current["services"][0]["envs"].append({"key": "AWS_ACCESS_KEY_ID", "value": "AKIA-OLD"})
    spec = app_spec()
    provision.apply_runtime_config(spec, current)
    api = [e for e in spec["services"][0]["envs"] if e["key"] == "AWS_ACCESS_KEY_ID"]
    assert [e["value"] for e in api] == ["AKIA-NEW"]
