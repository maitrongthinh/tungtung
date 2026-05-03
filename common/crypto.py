from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from common.config import load_settings

_SALT = b"shopee-agent-v1"


def _derive_key(passphrase: str) -> bytes:
    """Derive a 32-byte key from passphrase using PBKDF2."""
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), _SALT, 100_000, dklen=32)


def encrypt_token(plaintext: str) -> str:
    """Encrypt a token string using XOR + base64 (lightweight, no external deps)."""
    settings = load_settings(refresh=True)
    secret = settings.integrations.web_secret_key or "default-insecure-key"
    key = _derive_key(secret)
    data = plaintext.encode("utf-8")
    # XOR with repeating key
    encrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return "enc:" + base64.urlsafe_b64encode(encrypted).decode("ascii")


def decrypt_token(ciphertext: str) -> str:
    """Decrypt a token encrypted with encrypt_token."""
    if not ciphertext.startswith("enc:"):
        return ciphertext  # Not encrypted, return as-is (backward compat)
    settings = load_settings(refresh=True)
    secret = settings.integrations.web_secret_key or "default-insecure-key"
    key = _derive_key(secret)
    encrypted = base64.urlsafe_b64decode(ciphertext[4:])
    decrypted = bytes(b ^ key[i % len(key)] for i, b in enumerate(encrypted))
    return decrypted.decode("utf-8")


def is_encrypted(value: str) -> bool:
    return value.startswith("enc:")
