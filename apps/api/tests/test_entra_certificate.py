"""infra/azure/entra_certificate.py (ADR-020): the Entra app's certificate rotation."""

import base64
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

SCRIPT = Path(__file__).resolve().parents[3] / "infra" / "azure" / "entra_certificate.py"
spec = importlib.util.spec_from_file_location("entra_certificate", SCRIPT)
assert spec and spec.loader
entra = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entra)

NOW = datetime(2026, 11, 20, tzinfo=UTC)


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def test_the_certificate_is_short_lived_rsa_for_client_auth() -> None:
    cert, key = entra.new_certificate(NOW, 30)
    assert cert.not_valid_after_utc - NOW == timedelta(days=30)
    assert key.key_size == 2048
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()


def test_keeps_the_running_certificate_and_drops_the_rest() -> None:
    new, _ = entra.new_certificate(NOW, 30)
    running, _ = entra.new_certificate(NOW - timedelta(days=20), 30)
    expired, _ = entra.new_certificate(NOW - timedelta(days=40), 30)
    keep = entra.credentials_to_keep(new, pem(running), NOW)
    assert len(keep) == 2 and keep[0]["type"] == "AsymmetricX509Cert"
    assert (
        keep[1]["key"]
        == base64.b64encode(running.public_bytes(serialization.Encoding.DER)).decode()
    )
    b64 = base64.b64encode(pem(running).encode()).decode()
    assert len(entra.credentials_to_keep(new, b64, NOW)) == 2
    assert len(entra.credentials_to_keep(new, pem(expired), NOW)) == 1
    assert len(entra.credentials_to_keep(new, "garbage", NOW)) == 1
    assert len(entra.credentials_to_keep(new, None, NOW)) == 1


def test_main_signs_in_with_github_oidc_and_patches_the_app(monkeypatch, tmp_path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "token.actions.example":
            assert request.url.params["audience"] == "api://AzureADTokenExchange"
            return httpx.Response(200, json={"value": "github-oidc-jwt"})
        if request.url.host == "login.microsoftonline.com":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert form["client_assertion"] == "github-oidc-jwt"
            assert form["client_id"] == "deployer" and "client_secret" not in form
            return httpx.Response(200, json={"access_token": "graph"})
        assert request.method == "PATCH" and request.url.path == "/v1.0/applications/obj"
        seen["patch"] = json.loads(request.content)
        return httpx.Response(204)

    env = tmp_path / "github_env"
    for k, v in {
        "AZURE_DIRECTORY_ID": "ours",
        "AZURE_DEPLOYER_CLIENT_ID": "deployer",
        "AZURE_APP_OBJECT_ID": "obj",
        "AZURE_APP_CLIENT_ID": "app-id",
        "AZURE_CERTIFICATE_DAYS": "30",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.example/x",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "t",
        "GITHUB_ENV": str(env),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("CURRENT_AZURE_CERTIFICATE", raising=False)
    assert entra.main(httpx.Client(transport=httpx.MockTransport(handler))) == 0
    assert len(seen["patch"]["keyCredentials"]) == 1  # type: ignore[index]
    written = dict(line.split("=", 1) for line in env.read_text().splitlines())
    assert written["CLOUDOPTICS_AZURE_CLIENT_ID"] == "app-id"
    assert base64.b64decode(written["CLOUDOPTICS_AZURE_PRIVATE_KEY"]).startswith(b"-----BEGIN")


def test_main_refuses_missing_inputs(monkeypatch) -> None:
    monkeypatch.delenv("AZURE_APP_OBJECT_ID", raising=False)
    with pytest.raises(SystemExit, match="Missing"):
        entra.main(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))))
