"""IAM Roles Anywhere CreateSession signing (ADR-007).

AWS publishes the signing layout but no test vectors for it, so the request is checked
three ways: botocore's own SigV4 code builds the same canonical request and string to sign
(only the algorithm name differs), the signature verifies with the certificate's public key,
and a fixed key, certificate and time give a fixed signature (RSA PKCS#1 v1.5 is
deterministic), so any change to the signed bytes is caught.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

from cloudoptics.connectors import rolesanywhere as ra

NOW = datetime(2021, 11, 3, 12, 0, 0, tzinfo=UTC)  # the documented example's X-Amz-Date
REGION = "us-east-1"
ANCHOR = "arn:aws:rolesanywhere:us-east-1:123456789012:trust-anchor/aaaa-bbbb"
PROFILE = "arn:aws:rolesanywhere:us-east-1:123456789012:profile/cccc-dddd"
ROLE = "arn:aws:iam::123456789012:role/CloudopticsBroker"


def _is_probable_prime(n: int) -> bool:
    if n % 2 == 0:
        return False
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53):
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(rng: random.Random, bits: int) -> int:
    while True:
        n = rng.getrandbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if n % 65537 != 1 and _is_probable_prime(n):
            return n


def fixed_rsa_key() -> rsa.RSAPrivateKey:
    """The same 2048-bit RSA key on every run, made from seeded primes (no key in the repo)."""
    rng = random.Random(20211103)  # noqa: S311 - test key material, deliberately reproducible
    p, q = _prime(rng, 1024), _prime(rng, 1024)
    e = 65537
    d = pow(e, -1, (p - 1) * (q - 1))
    numbers = rsa.RSAPrivateNumbers(
        p=p,
        q=q,
        d=d,
        dmp1=rsa.rsa_crt_dmp1(d, p),
        dmq1=rsa.rsa_crt_dmq1(d, q),
        iqmp=rsa.rsa_crt_iqmp(p, q),
        public_numbers=rsa.RSAPublicNumbers(e, p * q),
    )
    return numbers.private_key()


def certificate(key: Any, serial: int = 0x1F71C5114A119FC0CC5A5A52FB3720AD) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "cloudoptics-test-broker")])
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )


BODY = json.dumps(
    {"durationSeconds": 3600, "profileArn": PROFILE, "roleArn": ROLE, "trustAnchorArn": ANCHOR},
    separators=(",", ":"),
).encode()


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return fixed_rsa_key()


def botocore_view(signed: ra.SignedRequest) -> tuple[str, str]:
    """botocore's SigV4 canonical request and string to sign for the same request."""
    req = AWSRequest(
        method="POST",
        url=signed.url,
        data=signed.body,
        headers={k: v for k, v in signed.headers.items() if k != "Authorization"},
    )
    req.context["timestamp"] = signed.headers["X-Amz-Date"]
    auth = SigV4Auth(Credentials("unused", "unused"), ra.SERVICE, REGION)
    canonical = auth.canonical_request(req)
    return canonical, auth.string_to_sign(req, canonical)


def test_the_canonical_request_has_the_documented_layout(rsa_key: rsa.RSAPrivateKey) -> None:
    cert = certificate(rsa_key)
    signed = ra.sign_create_session(
        certificate=cert, private_key=rsa_key, region=REGION, body=b"", now=NOW
    )
    der = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
    # The example in the Roles Anywhere signing process (Task 1), with an empty payload.
    assert signed.canonical_request == (
        "POST\n/sessions\n\n"
        "content-type:application/json\n"
        "host:rolesanywhere.us-east-1.amazonaws.com\n"
        "x-amz-date:20211103T120000Z\n"
        f"x-amz-x509:{der}\n\n"
        "content-type;host;x-amz-date;x-amz-x509\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )


def test_botocore_sigv4_builds_the_same_canonical_request(rsa_key: rsa.RSAPrivateKey) -> None:
    signed = ra.sign_create_session(
        certificate=certificate(rsa_key), private_key=rsa_key, region=REGION, body=BODY, now=NOW
    )
    canonical, string_to_sign = botocore_view(signed)
    assert signed.canonical_request == canonical
    assert string_to_sign.startswith("AWS4-HMAC-SHA256\n")
    assert signed.string_to_sign == string_to_sign.replace("AWS4-HMAC-SHA256", ra.RSA_ALGORITHM, 1)
    assert signed.string_to_sign.split("\n")[:3] == [
        "AWS4-X509-RSA-SHA256",
        "20211103T120000Z",
        "20211103/us-east-1/rolesanywhere/aws4_request",
    ]


def test_rsa_signature_is_fixed_for_a_fixed_key_and_time(rsa_key: rsa.RSAPrivateKey) -> None:
    cert = certificate(rsa_key)
    signed = ra.sign_create_session(
        certificate=cert, private_key=rsa_key, region=REGION, body=BODY, now=NOW
    )
    auth = signed.headers["Authorization"]
    prefix = (
        "AWS4-X509-RSA-SHA256 Credential=41796794418840706582093025104159514797/"
        "20211103/us-east-1/rolesanywhere/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date;x-amz-x509, Signature="
    )
    assert str(cert.serial_number) == "41796794418840706582093025104159514797"  # decimal
    assert auth.startswith(prefix)
    signature = bytes.fromhex(auth[len(prefix) :])
    rsa_key.public_key().verify(
        signature, signed.string_to_sign.encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    again = ra.sign_create_session(
        certificate=cert, private_key=rsa_key, region=REGION, body=BODY, now=NOW
    )
    assert again.headers == signed.headers
    assert hashlib.sha256(auth.encode()).hexdigest() == FIXED_AUTHORIZATION_SHA256


# sha256 of the Authorization header above. It changes only if the signed bytes change.
FIXED_AUTHORIZATION_SHA256 = "38cefcba9801b03241bd20a48799cd0c864ada38c8a3365f94ec749b4df63071"


def test_ecdsa_keys_sign_with_the_ecdsa_algorithm_and_a_chain_is_signed_too() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    cert = certificate(key, serial=291)
    intermediate = certificate(ec.generate_private_key(ec.SECP256R1()), serial=17767)
    signed = ra.sign_create_session(
        certificate=cert, private_key=key, region=REGION, body=BODY, now=NOW, chain=[intermediate]
    )
    auth = signed.headers["Authorization"]
    assert auth.startswith("AWS4-X509-ECDSA-SHA256 Credential=291/20211103/")
    assert "SignedHeaders=content-type;host;x-amz-date;x-amz-x509;x-amz-x509-chain," in auth
    chain_der = base64.b64encode(intermediate.public_bytes(serialization.Encoding.DER)).decode()
    assert signed.headers["X-Amz-X509-Chain"] == chain_der
    assert f"x-amz-x509-chain:{chain_der}\n" in signed.canonical_request
    canonical, _ = botocore_view(signed)
    assert canonical == signed.canonical_request
    key.public_key().verify(
        bytes.fromhex(auth.rsplit("Signature=", 1)[1]),
        signed.string_to_sign.encode(),
        ec.ECDSA(hashes.SHA256()),
    )


def test_a_key_that_does_not_match_the_certificate_is_refused(
    rsa_key: rsa.RSAPrivateKey,
) -> None:
    other = ec.generate_private_key(ec.SECP256R1())
    with pytest.raises(ValueError, match="does not belong"):
        ra.sign_create_session(
            certificate=certificate(rsa_key), private_key=other, region=REGION, body=b"", now=NOW
        )


def pem(cert: x509.Certificate) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def key_pem(key: Any) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def credentials(
    post: Any, *, b64: bool = False, clock: Any = lambda: NOW
) -> ra.RolesAnywhereCredentials:
    key = ec.generate_private_key(ec.SECP256R1())
    cert, private = pem(certificate(key)), key_pem(key)
    if b64:
        cert = base64.b64encode(cert.encode()).decode()
        private = base64.b64encode(private.encode()).decode()
    return ra.RolesAnywhereCredentials(
        certificate=cert,
        private_key=private,
        trust_anchor_arn=ANCHOR,
        profile_arn=PROFILE,
        role_arn=ROLE,
        post=post,
        clock=clock,
    )


def session_response(expiration: str = "2021-11-03T13:00:00Z") -> dict[str, Any]:
    return {
        "credentialSet": [
            {
                "credentials": {
                    "accessKeyId": "ASIAEXAMPLE",
                    "secretAccessKey": "secret",
                    "sessionToken": "token",
                    "expiration": expiration,
                }
            }
        ]
    }


@pytest.mark.parametrize("b64", [False, True])
def test_create_session_returns_sts_shaped_credentials(b64: bool) -> None:
    sent: list[tuple[str, dict[str, str], bytes]] = []

    def post(url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
        sent.append((url, headers, body))
        return httpx.Response(201, json=session_response())

    creds = credentials(post, b64=b64).fetch()
    assert creds == {
        "AccessKeyId": "ASIAEXAMPLE",
        "SecretAccessKey": "secret",
        "SessionToken": "token",
        "Expiration": datetime(2021, 11, 3, 13, 0, tzinfo=UTC),
    }
    [(url, headers, body)] = sent
    assert url == "https://rolesanywhere.us-east-1.amazonaws.com/sessions"
    assert headers["Authorization"].startswith("AWS4-X509-ECDSA-SHA256 Credential=")
    assert json.loads(body) == {
        "durationSeconds": 3600,
        "profileArn": PROFILE,
        "roleArn": ROLE,
        "trustAnchorArn": ANCHOR,
    }


def test_errors_never_carry_credentials() -> None:
    def denied(url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
        return httpx.Response(
            403, json={"message": "Untrusted certificate. Insufficient certificate"}
        )

    with pytest.raises(ra.RolesAnywhereError, match="HTTP 403 Untrusted certificate"):
        credentials(denied).fetch()

    def broken(url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(ra.RolesAnywhereError, match="ConnectError"):
        credentials(broken).fetch()

    def empty(url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
        return httpx.Response(201, json={"credentialSet": []})

    with pytest.raises(ra.RolesAnywhereError, match="without credentials"):
        credentials(empty).fetch()


def test_an_expired_certificate_is_reported_before_calling_aws() -> None:
    def post(url: str, headers: dict[str, str], body: bytes) -> httpx.Response:
        raise AssertionError("called AWS")

    later = NOW + timedelta(days=31)
    with pytest.raises(ra.RolesAnywhereError, match="expired"):
        credentials(post, clock=lambda: later).fetch()


@pytest.mark.parametrize(
    "arn", ["arn:aws:iam::1:role/x", "arn:aws:rolesanywhere::1:trust-anchor/x", "nope"]
)
def test_the_region_comes_from_a_roles_anywhere_arn(arn: str) -> None:
    assert ra.region_of(ANCHOR) == "us-east-1"
    with pytest.raises(ValueError):
        ra.region_of(arn)


def test_bad_pem_is_refused() -> None:
    with pytest.raises(ValueError):
        ra.load_certificates("not base64 !!")
    with pytest.raises(ValueError):
        ra.load_certificates(base64.b64encode(b"-----BEGIN NOTHING-----").decode())
