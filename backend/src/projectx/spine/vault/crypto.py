"""Envelope encryption.

Each business has its own data-encryption key (DEK). DEKs are stored wrapped by a key-encryption
key (KEK) that never touches the database. Locally the KEK is a 32-byte master key from the
environment; in production it should be a cloud KMS key (implement ``KeyEncryptionKey``).
Secrets are sealed with AES-256-GCM and bound to their row with associated data, so a
ciphertext copied into another row (or another business) fails to decrypt.
"""

from __future__ import annotations

import base64
import hashlib
import os
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_BYTES = 12
_DEK_AAD = b"projectx:dek:v1"


class KeyEncryptionKey(Protocol):
    @property
    def kek_id(self) -> str: ...

    def wrap(self, dek: bytes) -> bytes: ...

    def unwrap(self, wrapped: bytes) -> bytes: ...


class LocalKEK:
    def __init__(self, master_key_b64: str) -> None:
        key = base64.b64decode(master_key_b64)
        if len(key) != 32:
            raise ValueError("vault master key must be 32 bytes, base64-encoded")
        self._aes = AESGCM(key)
        self._id = "local:" + hashlib.sha256(key).hexdigest()[:12]

    @property
    def kek_id(self) -> str:
        return self._id

    def wrap(self, dek: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aes.encrypt(nonce, dek, _DEK_AAD)

    def unwrap(self, wrapped: bytes) -> bytes:
        return self._aes.decrypt(wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:], _DEK_AAD)


def new_dek() -> bytes:
    return AESGCM.generate_key(bit_length=256)


def seal(dek: bytes, plaintext: bytes, aad: bytes) -> tuple[bytes, bytes]:
    nonce = os.urandom(NONCE_BYTES)
    return nonce, AESGCM(dek).encrypt(nonce, plaintext, aad)


def open_sealed(dek: bytes, nonce: bytes, ciphertext: bytes, aad: bytes) -> bytes:
    return AESGCM(dek).decrypt(nonce, ciphertext, aad)
