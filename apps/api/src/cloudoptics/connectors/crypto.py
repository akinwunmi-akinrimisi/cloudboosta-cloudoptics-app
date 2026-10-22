"""Envelope encryption for connection secrets (External IDs).

``LocalKeyEncryptor`` uses AES-256-GCM with a key from settings and is for local, test and
demo use. Staging and production use KMS data keys (ADR-007), behind the same interface.
The tenant ID is bound as associated data, so a ciphertext copied to another tenant's row
does not decrypt.
"""

from __future__ import annotations

import base64
import os
import uuid
from functools import lru_cache
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cloudoptics.platform.settings import get_settings

VERSION = b"\x01"


class Encryptor(Protocol):
    def encrypt(self, plaintext: str, tenant_id: uuid.UUID) -> bytes: ...
    def decrypt(self, ciphertext: bytes, tenant_id: uuid.UUID) -> str: ...


class LocalKeyEncryptor:
    def __init__(self, key_b64: str) -> None:
        key = base64.b64decode(key_b64)
        if len(key) != 32:
            raise ValueError("local data key must be 32 bytes (base64)")
        self._aead = AESGCM(key)

    def encrypt(self, plaintext: str, tenant_id: uuid.UUID) -> bytes:
        nonce = os.urandom(12)
        return VERSION + nonce + self._aead.encrypt(nonce, plaintext.encode(), tenant_id.bytes)

    def decrypt(self, ciphertext: bytes, tenant_id: uuid.UUID) -> str:
        if ciphertext[:1] != VERSION:
            raise ValueError("unknown ciphertext version")
        nonce, body = ciphertext[1:13], ciphertext[13:]
        return self._aead.decrypt(nonce, body, tenant_id.bytes).decode()


@lru_cache
def get_encryptor() -> Encryptor:
    return LocalKeyEncryptor(get_settings().local_data_key)
