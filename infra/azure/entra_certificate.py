"""Rotate the certificate credential of the Cloudoptics Entra app (ADR-020).

Run by Actions -> Staging setup with ``azure_certificate=true``; never by hand with a
personal account. Nothing here is a long-lived secret:

1. **Sign in to Microsoft Graph without a secret.** The workflow's GitHub OIDC token
   (audience ``api://AzureADTokenExchange``) is exchanged at our own Entra directory for a
   Graph token of the *deployer* app (``AZURE_DEPLOYER_CLIENT_ID``), which trusts this
   repository's environment through a federated identity credential and holds
   ``Application.ReadWrite.OwnedBy`` as the owner of the Cloudoptics app only.
2. **New key pair.** RSA 2048 and a self-signed certificate valid ``AZURE_CERTIFICATE_DAYS``
   (default 30). Entra trusts the certificate itself, so there is no CA to keep: the same
   "nothing kept but the leaf" rule as the Roles Anywhere certificate (ADR-007 amendment).
3. **Two certificates on the app at most.** The new one and the one the running app uses
   (read from App Platform, ``CURRENT_AZURE_CERTIFICATE``), so the app keeps working until
   the new deployment is live. Every older certificate is removed.
4. **Hand over.** ``CLOUDOPTICS_AZURE_CLIENT_ID``, ``CLOUDOPTICS_AZURE_CERTIFICATE`` (base64
   PEM, not secret) and ``CLOUDOPTICS_AZURE_PRIVATE_KEY`` (base64 PEM, masked) go to
   ``GITHUB_ENV``; ``infra/digitalocean/provision.py`` puts them on the API and worker.

Inputs (environment): ``AZURE_DIRECTORY_ID`` (our own directory), ``AZURE_DEPLOYER_CLIENT_ID``,
``AZURE_APP_OBJECT_ID`` and ``AZURE_APP_CLIENT_ID`` (the Cloudoptics app), optional
``AZURE_CERTIFICATE_DAYS`` and ``CURRENT_AZURE_CERTIFICATE``; the GitHub OIDC variables
(``ACTIONS_ID_TOKEN_REQUEST_URL``/``_TOKEN``, from ``permissions: id-token: write``).
"""

from __future__ import annotations

import base64
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com"
CN = "cloudoptics-staging-azure"
KEEP = 2


def new_certificate(now: datetime, days: int) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cloudoptics"),
            x509.NameAttribute(NameOID.COMMON_NAME, CN),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def _pem_or_b64(value: str) -> bytes:
    text = value.strip()
    return text.encode() if text.startswith("-----BEGIN") else base64.b64decode(text)


def key_credential(cert: x509.Certificate) -> dict[str, Any]:
    """Graph's ``keyCredential`` for a certificate (public part only)."""
    return {
        "type": "AsymmetricX509Cert",
        "usage": "Verify",
        "key": base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode(),
        "displayName": f"CN={CN} {cert.not_valid_before_utc:%Y-%m-%d}",
        "startDateTime": cert.not_valid_before_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": cert.not_valid_after_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def credentials_to_keep(
    new: x509.Certificate, current: str | None, now: datetime
) -> list[dict[str, Any]]:
    """The new certificate, plus the running app's while it is still valid. Graph replaces
    the whole list, so every other certificate goes."""
    keep = [key_credential(new)]
    if current:
        try:
            running = x509.load_pem_x509_certificates(_pem_or_b64(current))[0]
        except ValueError:
            running = None
        if running is not None and running.not_valid_after_utc > now:
            keep.append(key_credential(running))
    return keep[:KEEP]


def github_oidc_token(http: httpx.Client) -> str:
    url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
    request_token = os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
    response = http.get(
        url,
        params={"audience": "api://AzureADTokenExchange"},
        headers={"Authorization": f"Bearer {request_token}"},
    )
    response.raise_for_status()
    return str(response.json()["value"])


def graph_token(http: httpx.Client, directory: str, deployer: str, assertion: str) -> str:
    response = http.post(
        f"{AUTHORITY}/{directory}/oauth2/v2.0/token",
        data={
            "client_id": deployer,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion,
        },
    )
    if response.status_code != 200:
        body = response.json() if response.content else {}
        first = str(body.get("error_description") or body.get("error") or response.status_code)
        sys.exit(f"Entra refused the deployer sign-in: {first.splitlines()[0]}")
    return str(response.json()["access_token"])


def set_credentials(
    http: httpx.Client, token: str, object_id: str, keep: list[dict[str, Any]]
) -> None:
    response = http.patch(
        f"{GRAPH}/applications/{object_id}",
        json={"keyCredentials": keep},
        headers={"Authorization": f"Bearer {token}"},
    )
    if response.status_code not in (200, 204):
        sys.exit(f"Graph refused the certificate update (HTTP {response.status_code}).")


def emit(outputs: dict[str, str], secret: set[str]) -> None:
    env_file = os.environ.get("GITHUB_ENV")
    if not env_file:
        for k, v in outputs.items():
            print(f"{k}={'***' if k in secret else v}")
        return
    with open(env_file, "a") as fh:
        for k, v in outputs.items():
            if k in secret:
                print(f"::add-mask::{v}")
            fh.write(f"{k}={v}\n")


def main(http: httpx.Client | None = None) -> int:
    required = ("AZURE_DIRECTORY_ID", "AZURE_DEPLOYER_CLIENT_ID", "AZURE_APP_OBJECT_ID",
                "AZURE_APP_CLIENT_ID")  # fmt: skip
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        sys.exit(f"Missing: {', '.join(missing)} (ADR-020 owner steps)")
    days = int(os.environ.get("AZURE_CERTIFICATE_DAYS") or "30")
    if not 1 <= days <= 90:
        sys.exit("AZURE_CERTIFICATE_DAYS must be between 1 and 90")
    http = http or httpx.Client(timeout=30)
    now = datetime.now(UTC)
    token = graph_token(
        http,
        os.environ["AZURE_DIRECTORY_ID"],
        os.environ["AZURE_DEPLOYER_CLIENT_ID"],
        github_oidc_token(http),
    )
    cert, key = new_certificate(now, days)
    keep = credentials_to_keep(cert, os.environ.get("CURRENT_AZURE_CERTIFICATE"), now)
    set_credentials(http, token, os.environ["AZURE_APP_OBJECT_ID"], keep)
    print(f"Azure app certificate rotated: valid {days} days, {len(keep)} on the app")
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    emit(
        {
            "CLOUDOPTICS_AZURE_CLIENT_ID": os.environ["AZURE_APP_CLIENT_ID"],
            "CLOUDOPTICS_AZURE_CERTIFICATE": base64.b64encode(
                cert.public_bytes(serialization.Encoding.PEM)
            ).decode(),
            "CLOUDOPTICS_AZURE_PRIVATE_KEY": base64.b64encode(key_pem).decode(),
        },
        secret={"CLOUDOPTICS_AZURE_PRIVATE_KEY"},
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
