"""Staging and production refuse placeholder or foreign AWS connection settings (audit lead 6)."""

import asyncio
import base64
import secrets

import pytest

from cloudoptics.platform import settings as settings_module
from cloudoptics.platform.settings import Settings

BUCKET = "https://cloudoptics-public-123456789012.s3.eu-west-2.amazonaws.com/cfn/v1"
GOOD = {
    "cloudoptics_principal_arn": "arn:aws:iam::123456789012:role/CloudopticsBroker",
    "cfn_billing_template_url": f"{BUCKET}/billing-role.yaml",
    "cfn_resource_template_url": f"{BUCKET}/resource-role.yaml",
}
ENV_NAMES = {
    "cloudoptics_principal_arn": "CLOUDOPTICS_CLOUDOPTICS_PRINCIPAL_ARN",
    "cfn_billing_template_url": "CLOUDOPTICS_CFN_BILLING_TEMPLATE_URL",
    "cfn_resource_template_url": "CLOUDOPTICS_CFN_RESOURCE_TEMPLATE_URL",
}


@pytest.fixture(autouse=True)
def _no_aws_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_NAMES.values():
        monkeypatch.delenv(name, raising=False)


def deployed(env: str = "staging", **overrides: str) -> Settings:
    values: dict[str, str] = {**GOOD, **overrides}
    return Settings(
        environment=env,  # type: ignore[arg-type]
        auth_mode="oidc",
        jobs_mode="queue",
        local_data_key=base64.b64encode(secrets.token_bytes(32)).decode(),
        **values,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("env", ["staging", "production"])
def test_explicit_cloudoptics_values_are_accepted(env: str) -> None:
    deployed(env).require_aws_connection_settings()


def test_values_from_the_environment_are_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    for field, name in ENV_NAMES.items():
        monkeypatch.setenv(name, GOOD[field])
    s = Settings(
        environment="staging",
        auth_mode="oidc",
        jobs_mode="queue",
        local_data_key=base64.b64encode(secrets.token_bytes(32)).decode(),
    )
    s.require_aws_connection_settings()


@pytest.mark.parametrize("env", ["staging", "production"])
@pytest.mark.parametrize("field", list(ENV_NAMES))
def test_unset_values_are_refused(env: str, field: str) -> None:
    s = Settings(
        environment=env,  # type: ignore[arg-type]
        auth_mode="oidc",
        jobs_mode="queue",
        local_data_key=base64.b64encode(secrets.token_bytes(32)).decode(),
        **{k: v for k, v in GOOD.items() if k != field},  # type: ignore[arg-type]
    )
    with pytest.raises(RuntimeError, match=f"{ENV_NAMES[field]} is not set"):
        s.require_aws_connection_settings()


@pytest.mark.parametrize(
    "url",
    [
        # The old default: a bucket nobody in this repository owns.
        "https://cloudoptics-public.s3.eu-west-2.amazonaws.com/cfn/v1/resource-role.yaml",
        # The local placeholder.
        settings_module.PLACEHOLDER_TEMPLATE_BASE + "/resource-role.yaml",
        "http://cloudoptics-public-123456789012.s3.eu-west-2.amazonaws.com/cfn/v1/resource-role.yaml",
        "https://cloudoptics-public-12345.s3.eu-west-2.amazonaws.com/cfn/v1/resource-role.yaml",
        "https://someone-else.s3.eu-west-2.amazonaws.com/cfn/v1/resource-role.yaml",
        "https://cloudoptics-public-123456789012.s3.eu-west-2.amazonaws.com.evil.example/resource-role.yaml",
        "https://evil.example/cloudoptics-public-123456789012.s3.amazonaws.com/resource-role.yaml",
        "https://x@cloudoptics-public-123456789012.s3.eu-west-2.amazonaws.com/cfn/v1/resource-role.yaml",
        "https://cloudoptics-public-123456789012.s3.eu-west-2.amazonaws.com:8443/cfn/v1/resource-role.yaml",
        "https://s3.eu-west-2.amazonaws.com/cloudoptics-public-123456789012/cfn/v1/resource-role.yaml",
        f"{BUCKET}/billing-role.yaml",  # the wrong template
        f"{BUCKET}/resource-role.yaml?x=1",
        "",
    ],
)
def test_foreign_or_placeholder_resource_template_urls_are_refused(url: str) -> None:
    with pytest.raises(RuntimeError, match="CLOUDOPTICS_CFN_RESOURCE_TEMPLATE_URL must be"):
        deployed(cfn_resource_template_url=url).require_aws_connection_settings()


def test_old_default_billing_template_url_is_refused() -> None:
    old = "https://cloudoptics-public.s3.eu-west-2.amazonaws.com/cfn/v1/billing-role.yaml"
    with pytest.raises(RuntimeError, match="CLOUDOPTICS_CFN_BILLING_TEMPLATE_URL must be"):
        deployed(cfn_billing_template_url=old).require_aws_connection_settings()


@pytest.mark.parametrize(
    "arn",
    [
        settings_module.PLACEHOLDER_PRINCIPAL_ARN,
        "arn:aws:iam::000000000000:role/Other",
        "arn:aws:iam::1:role/CloudopticsBroker",
        "arn:aws:iam::123456789012:user/broker",
        "",
    ],
)
def test_placeholder_or_malformed_principal_is_refused(arn: str) -> None:
    with pytest.raises(RuntimeError, match="PRINCIPAL_ARN is not an IAM role ARN"):
        deployed(cloudoptics_principal_arn=arn).require_aws_connection_settings()


def test_every_problem_is_reported_at_once() -> None:
    s = deployed(
        cloudoptics_principal_arn=settings_module.PLACEHOLDER_PRINCIPAL_ARN,
        cfn_billing_template_url="https://evil.example/billing-role.yaml",
        cfn_resource_template_url="https://evil.example/resource-role.yaml",
    )
    with pytest.raises(RuntimeError) as exc:
        s.require_aws_connection_settings()
    assert str(exc.value).count(";") == 2


@pytest.mark.parametrize("env", ["local", "test", "demo"])
def test_local_environments_keep_the_placeholders(env: str) -> None:
    s = Settings(environment=env)  # type: ignore[arg-type]
    s.require_aws_connection_settings()
    assert "cloudoptics-public." not in s.cfn_resource_template_url
    assert "cloudoptics-public." not in s.cfn_billing_template_url
    assert s.cfn_resource_template_url.endswith("/resource-role.yaml")
    assert s.cfn_access_template_url.endswith("/cloudoptics-access.yaml")


def test_the_api_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from cloudoptics import main

    bad = deployed(cfn_resource_template_url="https://evil.example/resource-role.yaml")
    monkeypatch.setattr(main, "get_settings", lambda: bad)
    with pytest.raises(RuntimeError, match="refusing to start in staging"):
        main.create_app()


def test_the_worker_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    from cloudoptics import worker

    bad = deployed(cloudoptics_principal_arn=settings_module.PLACEHOLDER_PRINCIPAL_ARN)
    monkeypatch.setattr(worker, "get_settings", lambda: bad)
    monkeypatch.setattr(worker, "get_job_app", lambda: pytest.fail("worker started"))
    with pytest.raises(RuntimeError, match="refusing to start in staging"):
        asyncio.run(worker.main())
