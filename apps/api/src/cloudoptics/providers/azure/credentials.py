"""Azure access without a client secret (ADR-020).

Cloudoptics is one multi-tenant Entra app. Each customer's admin consents to it in their
directory and gives its service principal read-only roles. To act in a customer's directory
the app asks that directory's token endpoint for an access token (client credentials) and
proves who it is with a **client assertion**: a short JWT signed by the private key of a
certificate registered on the app (OAuth 2.0 JWT bearer, RFC 7523):
https://learn.microsoft.com/entra/identity-platform/certificate-credentials

The assertion comes from an ``AssertionSource``, so the signer can change without touching
the callers: ``CertificateAssertion`` (the key from settings; certificate rotated by Staging
setup, like the Roles Anywhere one) today; a KMS-backed signer for a federated identity
credential later (ADR-020, option b).

Tokens are cached per (directory, resource) until five minutes before they expire. Error
messages carry Entra's AADSTS code, never a token or the assertion.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from cloudoptics.connectors.rolesanywhere import load_certificates

ARM_RESOURCE = "https://management.azure.com"
STORAGE_RESOURCE = "https://storage.azure.com"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
ASSERTION_SECONDS = 600
REFRESH_BEFORE = 300
_AADSTS = re.compile(r"AADSTS(\d+)")

# AADSTS codes -> what they mean for the connection.
NOT_CONSENTED = {"700016", "65001", "7000112", "500011"}
UNKNOWN_DIRECTORY = {"90002", "900023", "90072"}
OUR_CREDENTIAL = {"700027", "7000215", "7000274", "700024", "1000500", "50012"}


class AzureAuthError(RuntimeError):
    """No token. ``code`` is the AADSTS number (or a short reason); ``kind`` is
    ``consent``, ``directory``, ``credential`` or ``other``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        if code in NOT_CONSENTED:
            self.kind = "consent"
        elif code in UNKNOWN_DIRECTORY:
            self.kind = "directory"
        elif code in OUR_CREDENTIAL or code == "not_configured":
            self.kind = "credential"
        else:
            self.kind = "other"


class AssertionSource(Protocol):
    """Signs the client assertion for one token request."""

    def assertion(self, client_id: str, audience: str) -> str: ...


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _pem(value: str) -> bytes:
    text = value.strip()
    return text.encode() if text.startswith("-----BEGIN") else base64.b64decode(text)


@dataclass(frozen=True)
class CertificateAssertion:
    """PS256 assertion with the certificate's SHA-256 thumbprint (``x5t#S256``)."""

    private_key: rsa.RSAPrivateKey
    certificate: x509.Certificate

    @classmethod
    def from_pem(cls, certificate: str, private_key: str) -> CertificateAssertion:
        cert = load_certificates(certificate)[0]
        key = serialization.load_pem_private_key(_pem(private_key), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("Entra certificate credentials use an RSA key")
        public = cert.public_key()
        if not isinstance(public, rsa.RSAPublicKey) or public.public_numbers() != (
            key.public_key().public_numbers()
        ):
            raise ValueError("the private key does not match the certificate")
        return cls(key, cert)

    @property
    def thumbprint(self) -> str:
        return _b64url(self.certificate.fingerprint(hashes.SHA256()))

    @property
    def not_after(self) -> float:
        return self.certificate.not_valid_after_utc.timestamp()

    def assertion(self, client_id: str, audience: str) -> str:
        now = int(time.time())
        header = {"alg": "PS256", "typ": "JWT", "x5t#S256": self.thumbprint}
        claims = {
            "aud": audience,
            "iss": client_id,
            "sub": client_id,
            "jti": str(uuid.uuid4()),
            "nbf": now - 60,
            "iat": now,
            "exp": now + ASSERTION_SECONDS,
        }
        signing_input = (
            f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
            f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
        )
        signature = self.private_key.sign(
            signing_input.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
            hashes.SHA256(),
        )
        return f"{signing_input}.{_b64url(signature)}"


def certificate_thumbprint_sha256(cert: x509.Certificate) -> str:
    """Hex SHA-256 thumbprint, as the Entra portal lists certificates."""
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


@dataclass
class AzureTokens:
    """Client-credentials tokens in customers' directories, cached."""

    client_id: str
    source: AssertionSource | None
    authority: str
    http: httpx.AsyncClient
    _cache: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def token_url(self, directory_id: str) -> str:
        return f"{self.authority.rstrip('/')}/{directory_id}/oauth2/v2.0/token"

    async def token(self, directory_id: str, resource: str) -> str:
        key = (directory_id, resource)
        cached = self._cache.get(key)
        if cached and cached[1] - REFRESH_BEFORE > time.time():
            return cached[0]
        if self.source is None or not self.client_id:
            raise AzureAuthError("not_configured", "The Azure app credential is not configured.")
        async with self._lock:
            url = self.token_url(directory_id)
            try:
                response = await self.http.post(
                    url,
                    data={
                        "client_id": self.client_id,
                        "scope": f"{resource}/.default",
                        "grant_type": "client_credentials",
                        "client_assertion_type": ASSERTION_TYPE,
                        "client_assertion": self.source.assertion(self.client_id, url),
                    },
                )
            except httpx.HTTPError as exc:
                # from None: the cause may hold the request, assertion included.
                raise AzureAuthError(
                    "network", f"Entra ID did not answer ({type(exc).__name__})."
                ) from None
            body = _json(response)
            if response.status_code != 200 or "access_token" not in body:
                description = str(body.get("error_description") or body.get("error") or "")
                # error_codes is Entra's list of AADSTS numbers; the description repeats them.
                listed = body.get("error_codes")
                match = _AADSTS.search(description)
                if isinstance(listed, list) and listed:
                    code = str(listed[0])
                elif match:
                    code = match.group(1)
                else:
                    code = str(body.get("error") or response.status_code)
                # The first line only: Entra adds trace and correlation IDs after it.
                raise AzureAuthError(
                    code, description.splitlines()[0][:300] if description else code
                )
            token = str(body["access_token"])
            self._cache[key] = (token, time.time() + float(body.get("expires_in", 3600)))
            return token

    def forget(self, directory_id: str) -> None:
        for key in [k for k in self._cache if k[0] == directory_id]:
            del self._cache[key]


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
