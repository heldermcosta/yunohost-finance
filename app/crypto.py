"""Encryption at rest for statement PDFs (AES-256-GCM).

The key is server-managed: it is created once in a file readable only by the
service user, and it is never stored in the database or in backups of the database.
Each file is bound to its SHA-256 through the associated data, so a ciphertext
cannot be swapped onto another statement without detection.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES = 32
NONCE_BYTES = 12


def load_or_create_key(path: Path) -> bytes:
    if path.exists():
        key = path.read_bytes()
        if len(key) != KEY_BYTES:
            raise ValueError(f"key file {path} has the wrong length")
        return key

    path.parent.mkdir(parents=True, exist_ok=True)
    key = AESGCM.generate_key(bit_length=256)
    # O_EXCL: never overwrite an existing key. 0600: owner read/write only.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(key)
    return key


def encrypt(key: bytes, plaintext: bytes, associated: bytes) -> bytes:
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, associated)


def decrypt(key: bytes, blob: bytes, associated: bytes) -> bytes:
    nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, ciphertext, associated)
