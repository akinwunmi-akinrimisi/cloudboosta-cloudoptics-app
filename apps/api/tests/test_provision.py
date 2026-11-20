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


def test_the_applied_spec_keeps_every_alert_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKOS_API_KEY", "sk_x")
    spec = provision.render_spec({"APP_DATABASE_URL": "u", "LOCAL_DATA_KEY": "k"})
    provision.apply_runtime_config(spec, None)
    assert {a["rule"] for a in spec["alerts"]} == {"DEPLOYMENT_FAILED", "DOMAIN_FAILED"}
    for comp in (spec["services"][0], spec["workers"][0]):
        assert {a["rule"] for a in comp["alerts"]} >= {
            "CPU_UTILIZATION",
            "MEM_UTILIZATION",
            "RESTART_COUNT",
        }
        assert all(a["operator"] == "GREATER_THAN" and a["window"] for a in comp["alerts"])


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.alerts: dict[str, list[dict[str, Any]]] = {}

    def __call__(self, method: str, path: str, body: Any = None) -> dict[str, Any]:
        self.calls.append((method, path, body))
        if path == "/apps/app-1/alerts":
            return {"alerts": [{"id": "a1", "slack_webhooks": [{"url": "s"}]}, {"id": "a2"}]}
        if path.startswith("/uptime/checks?"):
            return {"checks": self.checks}
        if method == "POST" and path == "/uptime/checks":
            check = {"id": f"c{len(self.checks)}", "name": body["name"]}
            self.checks.append(check)
            return {"check": check}
        if path.endswith("/alerts") and path.startswith("/uptime/checks/"):
            check_id = path.split("/")[3]
            if method == "POST":
                self.alerts.setdefault(check_id, []).append({"id": body["name"], **body})
            return {"alerts": self.alerts.get(check_id, [])}
        return {}


def test_alerting_is_skipped_without_recipients(monkeypatch: pytest.MonkeyPatch) -> None:
    api = FakeApi()
    monkeypatch.setattr(provision, "call", api)
    monkeypatch.delenv("ALERT_EMAILS", raising=False)
    provision.configure_alerting("app-1")
    assert api.calls == []


def test_alerting_sets_recipients_and_uptime_checks_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeApi()
    monkeypatch.setattr(provision, "call", api)
    monkeypatch.setenv("ALERT_EMAILS", "oncall@example.com, founder@example.com")
    provision.configure_alerting("app-1")
    destinations = [c for c in api.calls if c[1].endswith("/destinations")]
    assert [c[1] for c in destinations] == [
        "/apps/app-1/alerts/a1/destinations",
        "/apps/app-1/alerts/a2/destinations",
    ]
    assert destinations[0][2] == {
        "emails": ["oncall@example.com", "founder@example.com"],
        "slack_webhooks": [{"url": "s"}],
    }
    targets = {c[2]["target"] for c in api.calls if c[:2] == ("POST", "/uptime/checks")}
    assert targets == {
        "https://cloudoptics.cloudboosta.com/api/readyz",
        "https://cloudoptics.cloudboosta.com/",
    }
    created_alerts = sum(len(v) for v in api.alerts.values())
    assert created_alerts == 4

    api.calls.clear()
    provision.configure_alerting("app-1")  # a second run updates, never duplicates
    assert not [c for c in api.calls if c[0] == "POST" and "/uptime/" in c[1]]
    assert sum(len(v) for v in api.alerts.values()) == 4


def test_roles_anywhere_and_kms_settings_reach_api_and_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLOUDOPTICS_BROKER_CREDENTIALS", "roles_anywhere")
    monkeypatch.setenv("CLOUDOPTICS_ROLES_ANYWHERE_CERTIFICATE", "Q0VSVA==")
    monkeypatch.setenv("CLOUDOPTICS_ROLES_ANYWHERE_PRIVATE_KEY", "S0VZ")
    monkeypatch.setenv("CLOUDOPTICS_KMS_KEY_ID", "arn:aws:kms:eu-west-2:1:key/k")
    monkeypatch.setenv("CLOUDOPTICS_KMS_REQUIRED", "false")
    spec = app_spec()
    provision.apply_runtime_config(spec, None)
    for name in ("api", "worker"):
        e = envs(spec, name)
        assert e["CLOUDOPTICS_ROLES_ANYWHERE_PRIVATE_KEY"]["type"] == "SECRET"
        assert "type" not in e["CLOUDOPTICS_ROLES_ANYWHERE_CERTIFICATE"]
        assert e["CLOUDOPTICS_BROKER_CREDENTIALS"]["value"] == "roles_anywhere"
        assert e["CLOUDOPTICS_KMS_KEY_ID"]["value"].endswith("key/k")
    assert "CLOUDOPTICS_ROLES_ANYWHERE_PRIVATE_KEY" not in envs(spec, "migrate")


def test_the_deleted_broker_users_key_is_taken_off_the_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (*provision.RUNTIME_PLAIN.values(), *provision.RUNTIME_SECRET.values()):
        monkeypatch.delenv(var, raising=False)
    current = app_spec()
    for comp in current["services"] + current["workers"]:
        comp["envs"] += [
            {"key": "AWS_ACCESS_KEY_ID", "type": "SECRET", "value": "EV[1:a]"},
            {"key": "AWS_SECRET_ACCESS_KEY", "type": "SECRET", "value": "EV[1:b]"},
        ]
    spec = app_spec()
    provision.apply_runtime_config(spec, current)
    assert "AWS_ACCESS_KEY_ID" in envs(spec, "api")  # kept on a plain redeploy
    monkeypatch.setenv("BROKER_USER_DELETED", "true")
    spec = app_spec()
    provision.apply_runtime_config(spec, current)
    for name in ("api", "worker"):
        assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} & set(envs(spec, name))


def test_digitalocean_oauth_client_is_plain_and_its_secret_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-021: the "Connect DigitalOcean" OAuth app's client ID and secret."""
    for var in (*provision.RUNTIME_PLAIN.values(), *provision.RUNTIME_SECRET.values()):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLOUDOPTICS_DO_OAUTH_CLIENT_ID", "do-client")
    monkeypatch.setenv("CLOUDOPTICS_DO_OAUTH_CLIENT_SECRET", "do-secret")
    spec = app_spec()
    provision.apply_runtime_config(spec, None)
    for name in ("api", "worker"):
        e = envs(spec, name)
        assert e["CLOUDOPTICS_DO_OAUTH_CLIENT_ID"]["value"] == "do-client"
        assert "type" not in e["CLOUDOPTICS_DO_OAUTH_CLIENT_ID"]
        assert e["CLOUDOPTICS_DO_OAUTH_CLIENT_SECRET"]["type"] == "SECRET"
    assert "CLOUDOPTICS_DO_OAUTH_CLIENT_SECRET" not in envs(spec, "migrate")


def test_gcp_settings_reach_api_and_worker_as_plain_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-019: the two values infra/gcp/setup.sh prints, set by Staging setup."""
    for var in (*provision.RUNTIME_PLAIN.values(), *provision.RUNTIME_SECRET.values()):
        monkeypatch.delenv(var, raising=False)
    provider = "projects/1/locations/global/workloadIdentityPools/cloudoptics/providers/aws-broker"
    monkeypatch.setenv("CLOUDOPTICS_GCP_WORKLOAD_PROVIDER", provider)
    monkeypatch.setenv("CLOUDOPTICS_GCP_SERVICE_ACCOUNT_PROJECT", "cloudoptics-staging")
    spec = app_spec()
    provision.apply_runtime_config(spec, None)
    for name in ("api", "worker"):
        e = envs(spec, name)
        assert e["CLOUDOPTICS_GCP_WORKLOAD_PROVIDER"]["value"] == provider
        assert e["CLOUDOPTICS_GCP_SERVICE_ACCOUNT_PROJECT"]["value"] == "cloudoptics-staging"
        assert "type" not in e["CLOUDOPTICS_GCP_WORKLOAD_PROVIDER"]
