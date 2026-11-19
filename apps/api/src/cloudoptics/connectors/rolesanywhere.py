"""Short-lived broker credentials from IAM Roles Anywhere (ADR-007).

``CreateSession`` is signed with the workload's X.509 certificate and private key, as the
Roles Anywhere signing process describes (SigV4 with the ``AWS4-X509-RSA-SHA256`` or
``AWS4-X509-ECDSA-SHA256`` algorithm, the certificate in ``X-Amz-X509`` and its serial number,
in decimal, in place of an access key ID):
https://docs.aws.amazon.com/rolesanywhere/latest/userguide/authentication-sign-process.html

boto3 has no client for this call (it is not a SigV4-HMAC API), and the lockfile has no
signing helper, so it is implemented here and tested against the documented layout.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

log = structlog.get_logger(__name__)

SERVICE = "rolesanywhere"
RSA_ALGORITHM = "AWS4-X509-RSA-SHA256"
ECDSA_ALGORITHM = "AWS4-X509-ECDSA-SHA256"
# Log a warning when the certificate has less than this left: Staging setup rotates it.
EXPIRY_WARNING = timedelta(days=7)

PrivateKey = rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey


class RolesAnywhereError(RuntimeError):
    """CreateSession failed. The message never holds credentials."""


def _pem_bytes(value: str) -> bytes:
    """PEM text, or base64 of PEM (one line, as GitHub and App Platform settings prefer)."""
    text = value.strip()
    if text.startswith("-----BEGIN"):
        return text.encode()
    try:
        return base64.b64decode(text, validate=True)
    except binascii.Error as exc:
        raise ValueError("expected PEM or base64-encoded PEM") from exc


def load_certificates(value: str) -> list[x509.Certificate]:
    """The end-entity certificate first, then any intermediates."""
    certs = x509.load_pem_x509_certificates(_pem_bytes(value))
    if not certs:
        raise ValueError("no certificate found")
    return certs


def load_private_key(value: str) -> PrivateKey:
    key = serialization.load_pem_private_key(_pem_bytes(value), password=None)
    if not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise ValueError("Roles Anywhere signs with an RSA or EC private key")
    return key


def region_of(arn: str) -> str:
    """``arn:aws:rolesanywhere:<region>:<account>:trust-anchor/<id>`` -> region."""
    parts = arn.split(":")
    if len(parts) < 6 or parts[2] != SERVICE or not parts[3]:
        raise ValueError("not a Roles Anywhere ARN")
    return parts[3]


def _der_b64(cert: x509.Certificate) -> str:
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()


@dataclass(frozen=True)
class SignedRequest:
    url: str
    headers: dict[str, str]
    body: bytes
    canonical_request: str
    string_to_sign: str


def sign_create_session(
    *,
    certificate: x509.Certificate,
    private_key: PrivateKey,
    region: str,
    body: bytes,
    now: datetime,
    chain: list[x509.Certificate] | None = None,
) -> SignedRequest:
    """Sign ``POST /sessions`` (Tasks 1-4 of the Roles Anywhere signing process)."""
    spki = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    if certificate.public_key().public_bytes(*spki) != private_key.public_key().public_bytes(*spki):
        raise ValueError("the private key does not belong to the certificate")
    rsa_key = isinstance(private_key, rsa.RSAPrivateKey)
    algorithm = RSA_ALGORITHM if rsa_key else ECDSA_ALGORITHM
    host = f"{SERVICE}.{region}.amazonaws.com"
    amz_date = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    headers = {
        "content-type": "application/json",
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-x509": _der_b64(certificate),
    }
    if chain:
        headers["x-amz-x509-chain"] = ",".join(_der_b64(c) for c in chain)
    names = sorted(headers)
    signed_headers = ";".join(names)
    canonical_headers = "".join(f"{n}:{' '.join(headers[n].split())}\n" for n in names)
    canonical_request = "\n".join(
        [
            "POST",
            "/sessions",
            "",
            canonical_headers,
            signed_headers,
            hashlib.sha256(body).hexdigest(),
        ]
    )
    scope = f"{amz_date[:8]}/{region}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [algorithm, amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    if isinstance(private_key, rsa.RSAPrivateKey):
        signature = private_key.sign(string_to_sign.encode(), padding.PKCS1v15(), hashes.SHA256())
    else:
        signature = private_key.sign(string_to_sign.encode(), ec.ECDSA(hashes.SHA256()))
    credential = f"{certificate.serial_number}/{scope}"
    out = {
        "Content-Type": headers["content-type"],
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-X509": headers["x-amz-x509"],
        "Authorization": (
            f"{algorithm} Credential={credential}, SignedHeaders={signed_headers}, "
            f"Signature={signature.hex()}"
        ),
    }
    if chain:
        out["X-Amz-X509-Chain"] = headers["x-amz-x509-chain"]
    return SignedRequest(
        url=f"https://{host}/sessions",
        headers=out,
        body=body,
        canonical_request=canonical_request,
        string_to_sign=string_to_sign,
    )


Post = Callable[[str, dict[str, str], bytes], httpx.Response]


def _post(url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
    return httpx.post(url, headers=headers, content=body, timeout=30)


class RolesAnywhereCredentials:
    """Calls CreateSession for the broker role. ``fetch`` returns STS-shaped credentials
    (``AccessKeyId``, ``SecretAccessKey``, ``SessionToken``, ``Expiration``); the caller
    (``SessionFactory``) caches them and asks again before they expire."""

    def __init__(
        self,
        *,
        certificate: str,
        private_key: str,
        trust_anchor_arn: str,
        profile_arn: str,
        role_arn: str,
        session_seconds: int = 3600,
        post: Post = _post,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        certs = load_certificates(certificate)
        self._cert, self._chain = certs[0], certs[1:]
        self._key = load_private_key(private_key)
        self.trust_anchor_arn = trust_anchor_arn
        self.profile_arn = profile_arn
        self.role_arn = role_arn
        self.region = region_of(trust_anchor_arn)
        self._seconds = session_seconds
        self._post = post
        self._clock = clock

    @property
    def certificate_expires(self) -> datetime:
        return self._cert.not_valid_after_utc

    def fetch(self) -> dict[str, Any]:
        now = self._clock()
        left = self.certificate_expires - now
        if left <= timedelta(0):
            raise RolesAnywhereError(
                "the Roles Anywhere certificate has expired: run Staging setup to rotate it"
            )
        if left < EXPIRY_WARNING:
            log.warning(
                "roles_anywhere.certificate_expiring", expires=str(self.certificate_expires)
            )
        body = json.dumps(
            {
                "durationSeconds": self._seconds,
                "profileArn": self.profile_arn,
                "roleArn": self.role_arn,
                "trustAnchorArn": self.trust_anchor_arn,
            },
            separators=(",", ":"),
        ).encode()
        req = sign_create_session(
            certificate=self._cert,
            private_key=self._key,
            region=self.region,
            body=body,
            now=now,
            chain=self._chain,
        )
        try:
            res = self._post(req.url, req.headers, req.body)
        except httpx.HTTPError as exc:
            raise RolesAnywhereError(f"CreateSession failed: {type(exc).__name__}") from None
        if res.status_code != 201 and res.status_code != 200:
            raise RolesAnywhereError(
                f"CreateSession failed: HTTP {res.status_code} {_error_message(res)}"
            )
        try:
            creds = res.json()["credentialSet"][0]["credentials"]
            return {
                "AccessKeyId": creds["accessKeyId"],
                "SecretAccessKey": creds["secretAccessKey"],
                "SessionToken": creds["sessionToken"],
                "Expiration": datetime.fromisoformat(creds["expiration"].replace("Z", "+00:00")),
            }
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise RolesAnywhereError("CreateSession answered without credentials") from exc


def _error_message(res: httpx.Response) -> str:
    """AWS's error message (no secrets in it), trimmed."""
    try:
        data = res.json()
        text = str(data.get("message") or data.get("Message") or "")
    except ValueError:
        text = res.text
    return text[:300]
