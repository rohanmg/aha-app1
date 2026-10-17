"""app/crypto.py

Credential encryption at rest with a Fernet key derived from
APP_SECRET_KEY. Secrets are never logged; encrypted values carry a version
prefix so keys can later be rotated.

Losing APP_SECRET_KEY makes every stored credential unreadable - documented
in docs/DEPLOY.md (added in a later phase).
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

_ENC_PREFIX = "enc:v1:fernet:"


def fernet_from_secret(secret_key: str) -> Fernet:
    """Derive a stable Fernet key from APP_SECRET_KEY (SHA-256 -> urlsafe b64)."""
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_obj(fernet: Fernet, obj: dict[str, Any]) -> str:
    """Encrypt a JSON-serialisable dict into a prefixed string (never log it)."""
    blob = fernet.encrypt(json.dumps(obj, sort_keys=True).encode("utf-8"))
    return _ENC_PREFIX + blob.decode("ascii")


def decrypt_obj(fernet: Fernet, ciphertext: str) -> dict[str, Any]:
    """Decrypt a value produced by encrypt_obj; raises on wrong/lost key."""
    if not ciphertext.startswith(_ENC_PREFIX):
        raise ValueError("Stored credential has an unexpected format and cannot be read.")
    blob = ciphertext[len(_ENC_PREFIX):].encode("ascii")
    try:
        return json.loads(fernet.decrypt(blob))
    except InvalidToken as exc:
        raise ValueError(
            "Cannot decrypt stored credentials: the app secret key does not "
            "match the one they were encrypted with."
        ) from exc


def mask_secret(value: str | None, visible: int = 3) -> str:
    """Render a secret value for display after saving (never the raw value)."""
    if value is None or value == "":
        return "(not set)"
    if len(value) <= visible:
        return "•" * 8
    return value[:visible] + "…" + "•" * 4


__all__ = ["fernet_from_secret", "encrypt_obj", "decrypt_obj", "mask_secret"]
