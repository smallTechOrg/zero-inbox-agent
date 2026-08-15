"""Encrypted-at-rest refresh tokens (auth-gmail slice): src/security/crypto.py."""

from __future__ import annotations

import base64

import pytest

from security.crypto import CryptoError, TokenCipher, derive_fernet_key


def test_roundtrip_and_ciphertext_never_contains_plaintext():
    cipher = TokenCipher("a passphrase kept only in .env")
    token = "1//0real-looking-refresh-token-µñïçødé"
    ciphertext = cipher.encrypt(token)
    assert token not in ciphertext
    assert cipher.decrypt(ciphertext) == token


def test_a_real_fernet_key_is_used_verbatim_and_a_passphrase_is_derived():
    fernet_key = base64.urlsafe_b64encode(b"0" * 32).decode()
    assert derive_fernet_key(fernet_key) == fernet_key.encode()
    derived = derive_fernet_key("just-a-passphrase")
    assert derived != b"just-a-passphrase" and len(derived) == 44


def test_tampered_ciphertext_and_wrong_key_raise_crypto_error():
    good = TokenCipher("secret-one")
    ciphertext = good.encrypt("tok")
    with pytest.raises(CryptoError):
        good.decrypt(ciphertext[:-4] + "AAAA")
    with pytest.raises(CryptoError):
        TokenCipher("secret-two").decrypt(ciphertext)


def test_empty_values_are_refused_everywhere():
    cipher = TokenCipher("secret")
    with pytest.raises(CryptoError):
        cipher.encrypt("")
    with pytest.raises(CryptoError):
        cipher.decrypt("")
    with pytest.raises(CryptoError):
        derive_fernet_key("")
