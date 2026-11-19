"""Envelope encryption for connection secrets (External IDs), ADR-007.

Two ciphertext versions, told apart by the first byte:

* ``0x01`` (``LocalKeyEncryptor``): ``01 | nonce(12) | AES-256-GCM(body)`` under one key from
  settings (``CLOUDOPTICS_LOCAL_DATA_KEY``). Local, test and demo use it; staging used it
  before KMS, so the KMS encryptor still reads it and rewrites it as version 2.
* ``0x02`` (``KmsEnvelopeEncryptor``): ``02 | len(edk) u16 | edk | nonce(12) | AES-256-GCM``.
  ``edk`` is a 256-bit data key encrypted by AWS KMS (``GenerateDataKey``) in Cloudoptics'
  broker account. The KMS encryption context names the tenant, so KMS refuses to decrypt the
  data key for any other tenant, and the AES associated data binds the header and the tenant.

Either way the tenant ID is bound as associated data, so a ciphertext copied to another
tenant's row does not decrypt.

Data keys are cached in memory for a short time (``data_key_cache_seconds``), per tenant for
encryption and per encrypted data key for decryption, with a bound on entries and on how many
records one data key encrypts, so KMS is called once per tenant per few minutes, not per row.
"""

from __future__ import annotations

import base64
import os
import struct
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cloudoptics.platform.settings import DEV_DATA_KEY, get_settings

VERSION = b"\x01"
VERSION_KMS = b"\x02"
_NONCE = 12
# Bound into the AES associated data of version 2, with the header and the tenant ID.
_AAD_LABEL = b"cloudoptics/connection-secret/v2"
PURPOSE = "connection-secret"


class Encryptor(Protocol):
    def encrypt(self, plaintext: str, tenant_id: uuid.UUID) -> bytes: ...
    def decrypt(self, ciphertext: bytes, tenant_id: uuid.UUID) -> str: ...
    def needs_upgrade(self, ciphertext: bytes) -> bool: ...


class LocalKeyEncryptor:
    def __init__(self, key_b64: str) -> None:
        key = base64.b64decode(key_b64)
        if len(key) != 32:
            raise ValueError("local data key must be 32 bytes (base64)")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, tenant_id: uuid.UUID) -> bytes:
        nonce = os.urandom(_NONCE)
        return VERSION + nonce + self._aead.encrypt(nonce, plaintext.encode(), tenant_id.bytes)

    def decrypt(self, ciphertext: bytes, tenant_id: uuid.UUID) -> str:
        if ciphertext[:1] != VERSION:
            raise ValueError("unknown ciphertext version")
        nonce, body = ciphertext[1 : 1 + _NONCE], ciphertext[1 + _NONCE :]
        return self._aead.decrypt(nonce, body, tenant_id.bytes).decode()

    def needs_upgrade(self, ciphertext: bytes) -> bool:
        return False


def encryption_context(tenant_id: uuid.UUID) -> dict[str, str]:
    """The KMS encryption context: logged by CloudTrail, required by the key policy."""
    return {"tenant_id": str(tenant_id), "purpose": PURPOSE}


@dataclass
class _DataKey:
    plaintext: bytes
    encrypted: bytes
    created: float
    uses: int = 0


class _TtlCache:
    """A small LRU with a time-to-live per entry. Thread-safe: jobs run in threads too."""

    def __init__(self, ttl: float, entries: int, clock: Callable[[], float]) -> None:
        self._ttl, self._entries, self._clock = ttl, entries, clock
        self._data: OrderedDict[Any, _DataKey] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any) -> _DataKey | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            if self._clock() - item.created >= self._ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return item

    def put(self, key: Any, item: _DataKey) -> None:
        with self._lock:
            self._data[key] = item
            self._data.move_to_end(key)
            while len(self._data) > self._entries:
                self._data.popitem(last=False)

    def drop(self, key: Any) -> None:
        with self._lock:
            self._data.pop(key, None)

    def __len__(self) -> int:
        return len(self._data)


class KmsEnvelopeEncryptor:
    """AES-256-GCM under per-tenant data keys from AWS KMS (version 2 ciphertexts).

    ``kms`` returns a KMS client; it is called for every KMS request so that refreshed broker
    credentials are picked up. ``legacy`` reads version 1 ciphertexts until they are all
    re-encrypted (``needs_upgrade`` says which).
    """

    def __init__(
        self,
        key_id: str,
        kms: Callable[[], Any],
        *,
        legacy: LocalKeyEncryptor | None = None,
        cache_seconds: float = 300,
        cache_entries: int = 1024,
        max_uses: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not key_id:
            raise ValueError("a KMS key ID is required")
        self._key_id = key_id
        self._kms = kms
        self._legacy = legacy
        self._max_uses = max_uses
        self._enc_keys = _TtlCache(cache_seconds, cache_entries, clock)
        self._dec_keys = _TtlCache(cache_seconds, cache_entries, clock)
        self._clock = clock

    def _data_key(self, tenant_id: uuid.UUID) -> _DataKey:
        item = self._enc_keys.get(tenant_id)
        if item is not None and item.uses < self._max_uses:
            item.uses += 1
            return item
        res = self._kms().generate_data_key(
            KeyId=self._key_id,
            KeySpec="AES_256",
            EncryptionContext=encryption_context(tenant_id),
        )
        item = _DataKey(bytes(res["Plaintext"]), bytes(res["CiphertextBlob"]), self._clock(), 1)
        self._enc_keys.put(tenant_id, item)
        # The same key decrypts what it just encrypted without another KMS call.
        self._dec_keys.put((tenant_id, item.encrypted), item)
        return item

    def _plaintext_key(self, tenant_id: uuid.UUID, edk: bytes) -> bytes:
        item = self._dec_keys.get((tenant_id, edk))
        if item is not None:
            return item.plaintext
        res = self._kms().decrypt(
            CiphertextBlob=edk,
            KeyId=self._key_id,
            EncryptionContext=encryption_context(tenant_id),
        )
        key = bytes(res["Plaintext"])
        self._dec_keys.put((tenant_id, edk), _DataKey(key, edk, self._clock()))
        return key

    @staticmethod
    def _header(edk: bytes) -> bytes:
        return VERSION_KMS + struct.pack(">H", len(edk)) + edk

    @staticmethod
    def _aad(header: bytes, tenant_id: uuid.UUID) -> bytes:
        return _AAD_LABEL + header + tenant_id.bytes

    def encrypt(self, plaintext: str, tenant_id: uuid.UUID) -> bytes:
        item = self._data_key(tenant_id)
        header = self._header(item.encrypted)
        nonce = os.urandom(_NONCE)
        body = AESGCM(item.plaintext).encrypt(
            nonce, plaintext.encode(), self._aad(header, tenant_id)
        )
        return header + nonce + body

    def decrypt(self, ciphertext: bytes, tenant_id: uuid.UUID) -> str:
        ciphertext = bytes(ciphertext)
        version = ciphertext[:1]
        if version == VERSION:
            if self._legacy is None:
                raise ValueError("a version 1 ciphertext needs CLOUDOPTICS_LOCAL_DATA_KEY")
            return self._legacy.decrypt(ciphertext, tenant_id)
        if version != VERSION_KMS or len(ciphertext) < 3:
            raise ValueError("unknown ciphertext version")
        (size,) = struct.unpack(">H", ciphertext[1:3])
        header, rest = ciphertext[: 3 + size], ciphertext[3 + size :]
        if len(header) != 3 + size or len(rest) < _NONCE + 16:
            raise ValueError("truncated ciphertext")
        key = self._plaintext_key(tenant_id, header[3:])
        nonce, body = rest[:_NONCE], rest[_NONCE:]
        return AESGCM(key).decrypt(nonce, body, self._aad(header, tenant_id)).decode()

    def needs_upgrade(self, ciphertext: bytes) -> bool:
        return bytes(ciphertext[:1]) != VERSION_KMS

    def reencrypt(self, ciphertext: bytes, tenant_id: uuid.UUID) -> bytes | None:
        """A version 2 ciphertext of a legacy one, or None when it is already version 2."""
        if not self.needs_upgrade(ciphertext):
            return None
        return self.encrypt(self.decrypt(ciphertext, tenant_id), tenant_id)


def _legacy_key() -> LocalKeyEncryptor | None:
    settings = get_settings()
    key = settings.local_data_key
    if not key:
        return None
    if key == DEV_DATA_KEY and settings.environment in ("staging", "production"):
        return None  # only reachable with kms_required (settings refuse it otherwise)
    return LocalKeyEncryptor(key)


def _broker_kms_client() -> Any:
    from cloudoptics.connectors.service import get_session_factory

    return get_session_factory().broker_client("kms", get_settings().kms_region)


@lru_cache
def get_encryptor() -> Encryptor:
    """KMS envelope encryption when ``CLOUDOPTICS_KMS_KEY_ID`` is set, else the local key.

    Settings refuse ``kms_required`` without a key, so a deployment that requires KMS never
    falls back to the local key for new ciphertexts.
    """
    settings = get_settings()
    legacy = _legacy_key()
    if settings.kms_key_id:
        return KmsEnvelopeEncryptor(
            settings.kms_key_id,
            _broker_kms_client,
            legacy=legacy,
            cache_seconds=settings.data_key_cache_seconds,
            cache_entries=settings.data_key_cache_entries,
            max_uses=settings.data_key_max_uses,
        )
    if legacy is None:
        raise RuntimeError("no data key: set CLOUDOPTICS_KMS_KEY_ID or CLOUDOPTICS_LOCAL_DATA_KEY")
    return legacy
