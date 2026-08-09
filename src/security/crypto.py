"""Fernet encryption of OAuth refresh tokens at rest.

The key is derived from ``AGENT_SECRET_KEY``. Two forms are accepted:

* a real Fernet key (44-char urlsafe-base64) — used verbatim;
* any other passphrase — deterministically derived via SHA-256 → urlsafe-base64.

Nothing in this module ever logs plaintext or ciphertext.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken


class CryptoError(RuntimeError):
    """Raised for any encryption/decryption failure, with no secret material."""


def derive_fernet_key(secret: str) -> bytes:
    """Return a valid Fernet key for ``secret``."""
    if not secret:
        raise CryptoError(
            "AGENT_SECRET_KEY is empty — set it in .env before storing OAuth tokens"
        )
    candidate = secret.encode("utf-8")
    try:
        Fernet(candidate)
    except (ValueError, TypeError):
        digest = hashlib.sha256(candidate).digest()
        return base64.urlsafe_b64encode(digest)
    return candidate


class TokenCipher:
    """Encrypts/decrypts a single secret string (an OAuth refresh token)."""

    def __init__(self, secret: str | None = None) -> None:
        if secret is None:
            secret = get_secret_key()
        self._fernet = Fernet(derive_fernet_key(secret))

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            raise CryptoError("refusing to encrypt an empty value")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            raise CryptoError("refusing to decrypt an empty value")
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError) as exc:
            raise CryptoError("stored token could not be decrypted") from exc


def get_secret_key() -> str:
    """Read ``AGENT_SECRET_KEY`` from settings, falling back to the environment.

    The settings module is owned by another slice; the env fallback keeps this
    module usable (and testable) regardless of which fields it currently declares.
    """
    try:
        from config.settings import get_settings

        value = getattr(get_settings(), "secret_key", "") or ""
    except Exception:  # pragma: no cover - settings unavailable
        value = ""
    return value or os.environ.get("AGENT_SECRET_KEY", "")
