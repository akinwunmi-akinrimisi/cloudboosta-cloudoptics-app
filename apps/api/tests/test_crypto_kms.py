# ruff: noqa: N803  (the fake mirrors boto3's keyword names)
"""KMS envelope encryption of connection secrets (ADR-007): format, binding, caching."""

from __future__ import annotations

import base64
import os
import secrets
import struct
import uuid
from typing import Any

import boto3
import pytest
from botocore.stub import Stubber
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cloudoptics.connectors import crypto
from cloudoptics.connectors.crypto import (
    VERSION,
    VERSION_KMS,
    KmsEnvelopeEncryptor,
    LocalKeyEncryptor,
    encryption_context,
)
from cloudoptics.platform.settings import DEV_DATA_KEY, Settings

KEY_ARN = "arn:aws:kms:eu-west-2:111111111111:key/00000000-0000-0000-0000-000000000001"
A, B = uuid.uuid4(), uuid.uuid4()


class InvalidCiphertextError(Exception):
    """What KMS raises when the encryption context does not match."""


class FakeKms:
    """Wraps data keys under a master key, with the encryption context as associated data,
    so a different context fails exactly as KMS does."""

    def __init__(self) -> None:
        self._master = AESGCM(AESGCM.generate_key(256))
        self.generated: list[dict[str, str]] = []
        self.decrypted: list[dict[str, str]] = []

    @staticmethod
    def _aad(context: dict[str, str]) -> bytes:
        return repr(sorted(context.items())).encode()

    def generate_data_key(self, KeyId: str, KeySpec: str, EncryptionContext: dict[str, str]):  # type: ignore[no-untyped-def]
        assert KeyId == KEY_ARN and KeySpec == "AES_256"
        self.generated.append(EncryptionContext)
        key, nonce = os.urandom(32), os.urandom(12)
        blob = nonce + self._master.encrypt(nonce, key, self._aad(EncryptionContext))
        return {"Plaintext": key, "CiphertextBlob": blob, "KeyId": KeyId}

    def decrypt(self, CiphertextBlob: bytes, KeyId: str, EncryptionContext: dict[str, str]):  # type: ignore[no-untyped-def]
        assert KeyId == KEY_ARN
        self.decrypted.append(EncryptionContext)
        try:
            key = self._master.decrypt(
                CiphertextBlob[:12], CiphertextBlob[12:], self._aad(EncryptionContext)
            )
        except InvalidTag as exc:
            raise InvalidCiphertextError from exc
        return {"Plaintext": key, "KeyId": KeyId}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(kms: FakeKms | None = None, **kw: Any) -> tuple[KmsEnvelopeEncryptor, FakeKms, Clock]:
    kms = kms or FakeKms()
    clock = Clock()
    enc = KmsEnvelopeEncryptor(KEY_ARN, lambda: kms, clock=clock, **kw)
    return enc, kms, clock


def test_version_2_format_holds_the_encrypted_data_key_then_nonce_and_body() -> None:
    enc, kms, _ = make()
    ct = enc.encrypt("ext-id-123", A)
    assert ct[:1] == VERSION_KMS
    (size,) = struct.unpack(">H", ct[1:3])
    edk = ct[3 : 3 + size]
    assert edk and len(ct) == 3 + size + 12 + len("ext-id-123") + 16
    assert b"ext-id-123" not in ct
    assert enc.decrypt(ct, A) == "ext-id-123"
    assert kms.generated == [{"tenant_id": str(A), "purpose": "connection-secret"}]
    assert not enc.needs_upgrade(ct)


def test_the_kms_encryption_context_names_the_tenant() -> None:
    assert encryption_context(A) == {"tenant_id": str(A), "purpose": "connection-secret"}


def test_a_ciphertext_copied_to_another_tenant_does_not_decrypt() -> None:
    enc, kms, _ = make()
    ct = enc.encrypt("secret", A)
    # A fresh process (empty cache): KMS refuses the data key for tenant B's context.
    fresh, _, _ = make(kms)
    with pytest.raises(InvalidCiphertextError):
        fresh.decrypt(ct, B)
    # The process that wrote it caches the key under (tenant A, data key) only.
    with pytest.raises(InvalidCiphertextError):
        enc.decrypt(ct, B)


def test_the_header_is_bound_into_the_associated_data() -> None:
    enc, _, _ = make()
    ct = bytearray(enc.encrypt("secret", A))
    ct[-1] ^= 1
    with pytest.raises(InvalidTag):
        enc.decrypt(bytes(ct), A)


def test_aad_covers_the_tenant_even_with_the_same_data_key() -> None:
    """A body sealed for tenant B under A's data key does not open as tenant A's."""
    enc, _, _ = make()
    ct = enc.encrypt("secret", A)
    (size,) = struct.unpack(">H", ct[1:3])
    header = ct[: 3 + size]
    key = enc._plaintext_key(A, header[3:])
    nonce = os.urandom(12)
    forged = header + nonce + AESGCM(key).encrypt(nonce, b"secret", enc._aad(header, B))
    with pytest.raises(InvalidTag):
        enc.decrypt(forged, A)


@pytest.mark.parametrize("bad", [b"", b"\x09abc", b"\x02", b"\x02\x00\x10abc", b"\x02\x00\x00"])
def test_unknown_or_truncated_ciphertexts_are_refused(bad: bytes) -> None:
    enc, _, _ = make()
    with pytest.raises(ValueError):
        enc.decrypt(bad, A)


def test_data_keys_are_cached_per_tenant_for_a_short_time() -> None:
    enc, kms, clock = make(cache_seconds=300)
    cts = [enc.encrypt(f"s{i}", A) for i in range(5)]
    assert len(kms.generated) == 1
    assert [enc.decrypt(c, A) for c in cts] == [f"s{i}" for i in range(5)]
    assert kms.decrypted == []  # the key that encrypted them is still cached
    enc.encrypt("other", B)
    assert len(kms.generated) == 2
    clock.now += 301
    enc.encrypt("later", A)
    assert len(kms.generated) == 3
    assert enc.decrypt(cts[0], A) == "s0"
    assert kms.decrypted == [encryption_context(A)]
    assert enc.decrypt(cts[1], A) == "s1"  # same data key: cached again
    assert len(kms.decrypted) == 1


def test_a_data_key_encrypts_a_bounded_number_of_records() -> None:
    enc, kms, _ = make(max_uses=3)
    for i in range(7):
        enc.encrypt(f"s{i}", A)
    assert len(kms.generated) == 3


def test_the_cache_holds_a_bounded_number_of_keys() -> None:
    enc, kms, _ = make(cache_entries=2)
    tenants = [uuid.uuid4() for _ in range(3)]
    for t in tenants:
        enc.encrypt("x", t)
    enc.encrypt("x", tenants[0])  # evicted (least recently used)
    assert len(kms.generated) == 4
    assert len(enc._enc_keys) == 2 and len(enc._dec_keys) == 2


def test_legacy_ciphertexts_are_read_and_flagged_for_re_encryption() -> None:
    local = LocalKeyEncryptor(base64.b64encode(secrets.token_bytes(32)).decode())
    old = local.encrypt("legacy-id", A)
    enc, _, _ = make(legacy=local)
    assert old[:1] == VERSION and enc.needs_upgrade(old)
    assert enc.decrypt(old, A) == "legacy-id"
    new = enc.reencrypt(old, A)
    assert new is not None and new[:1] == VERSION_KMS
    assert enc.decrypt(new, A) == "legacy-id"
    assert enc.reencrypt(new, A) is None  # idempotent
    with pytest.raises(InvalidTag):
        enc.reencrypt(old, B)  # the tenant binding survives re-encryption


def test_legacy_ciphertexts_need_the_local_key() -> None:
    local = LocalKeyEncryptor(base64.b64encode(secrets.token_bytes(32)).decode())
    enc, _, _ = make()
    with pytest.raises(ValueError, match="LOCAL_DATA_KEY"):
        enc.decrypt(local.encrypt("x", A), A)


def test_the_local_encryptor_never_asks_for_an_upgrade() -> None:
    local = LocalKeyEncryptor(DEV_DATA_KEY)
    assert not local.needs_upgrade(local.encrypt("x", A))


def test_requests_match_the_real_kms_api() -> None:
    """botocore validates the parameters against the KMS service model."""
    client = boto3.client(
        "kms",
        region_name="eu-west-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",  # noqa: S106 - stubbed, never sent
    )
    stub = Stubber(client)
    key, edk = os.urandom(32), os.urandom(184)
    context = encryption_context(A)
    stub.add_response(
        "generate_data_key",
        {"Plaintext": key, "CiphertextBlob": edk, "KeyId": KEY_ARN},
        {"KeyId": KEY_ARN, "KeySpec": "AES_256", "EncryptionContext": context},
    )
    stub.add_response(
        "decrypt",
        {"Plaintext": key, "KeyId": KEY_ARN},
        {"CiphertextBlob": edk, "KeyId": KEY_ARN, "EncryptionContext": context},
    )
    with stub:
        writer = KmsEnvelopeEncryptor(KEY_ARN, lambda: client)
        ct = writer.encrypt("ext", A)
        reader = KmsEnvelopeEncryptor(KEY_ARN, lambda: client)
        assert reader.decrypt(ct, A) == "ext"
    stub.assert_no_pending_responses()


def deployed(**kw: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "staging",
        "auth_mode": "oidc",
        "jobs_mode": "queue",
        "local_data_key": base64.b64encode(secrets.token_bytes(32)).decode(),
        **kw,
    }
    return Settings(**values)


def test_kms_required_needs_a_key() -> None:
    with pytest.raises(ValueError, match="kms_required"):
        deployed(kms_required=True)
    with pytest.raises(ValueError, match="kms_required"):
        Settings(kms_required=True)


def test_with_kms_required_the_development_key_is_ignored_not_refused() -> None:
    with pytest.raises(ValueError, match="development data key"):
        deployed(local_data_key=DEV_DATA_KEY)
    s = deployed(local_data_key=DEV_DATA_KEY, kms_required=True, kms_key_id=KEY_ARN)
    assert s.kms_required


@pytest.fixture
def settings_of(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    def use(s: Settings) -> None:
        monkeypatch.setattr(crypto, "get_settings", lambda: s)
        crypto.get_encryptor.cache_clear()

    yield use
    crypto.get_encryptor.cache_clear()


def test_settings_select_the_encryptor(settings_of, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    settings_of(Settings())
    assert isinstance(crypto.get_encryptor(), LocalKeyEncryptor)

    kms = FakeKms()
    monkeypatch.setattr(crypto, "_broker_kms_client", lambda: kms)
    settings_of(Settings(kms_key_id=KEY_ARN))
    enc = crypto.get_encryptor()
    assert isinstance(enc, KmsEnvelopeEncryptor)
    assert enc.decrypt(enc.encrypt("x", A), A) == "x"
    assert enc._legacy is not None  # still reads the local key's ciphertexts


def test_deployed_with_kms_required_and_the_dev_key_has_no_legacy_reader(settings_of) -> None:  # type: ignore[no-untyped-def]
    settings_of(deployed(local_data_key=DEV_DATA_KEY, kms_required=True, kms_key_id=KEY_ARN))
    enc = crypto.get_encryptor()
    assert isinstance(enc, KmsEnvelopeEncryptor) and enc._legacy is None


def test_without_any_key_there_is_no_encryptor(settings_of) -> None:  # type: ignore[no-untyped-def]
    settings_of(Settings(local_data_key=""))
    with pytest.raises(RuntimeError, match="no data key"):
        crypto.get_encryptor()


def test_the_kms_client_comes_from_the_broker_session(monkeypatch: pytest.MonkeyPatch) -> None:
    from cloudoptics.connectors import service

    calls: list[tuple[str, str]] = []

    class Factory:
        def broker_client(self, svc: str, region: str) -> str:
            calls.append((svc, region))
            return "client"

    monkeypatch.setattr(service, "_session_factory", Factory())
    assert crypto._broker_kms_client() == "client"
    assert calls == [("kms", "eu-west-2")]
