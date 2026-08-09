"""Unit tests for Fernet encryption of OAuth refresh tokens at rest."""

import pytest


def test_encrypt_then_decrypt_returns_the_original_token():
    from security.crypto import TokenCipher

    cipher = TokenCipher("a-test-secret-key-value")

    encrypted = cipher.encrypt("1//0gRefreshTokenValue")

    assert cipher.decrypt(encrypted) == "1//0gRefreshTokenValue"


def test_ciphertext_never_contains_the_plaintext_token():
    from security.crypto import TokenCipher

    cipher = TokenCipher("a-test-secret-key-value")

    encrypted = cipher.encrypt("1//0gRefreshTokenValue")

    assert "1//0gRefreshTokenValue" not in encrypted
    assert encrypted != "1//0gRefreshTokenValue"


def test_encrypting_the_same_token_twice_yields_different_ciphertext():
    from security.crypto import TokenCipher

    cipher = TokenCipher("a-test-secret-key-value")

    first = cipher.encrypt("same-token")
    second = cipher.encrypt("same-token")

    assert first != second
    assert cipher.decrypt(first) == cipher.decrypt(second) == "same-token"


def test_a_raw_fernet_key_is_accepted_verbatim():
    from cryptography.fernet import Fernet

    from security.crypto import TokenCipher

    raw_key = Fernet.generate_key().decode()
    cipher = TokenCipher(raw_key)

    assert cipher.decrypt(cipher.encrypt("tok")) == "tok"


def test_decrypting_with_a_different_secret_raises():
    from security.crypto import CryptoError, TokenCipher

    encrypted = TokenCipher("secret-one").encrypt("tok")

    with pytest.raises(CryptoError):
        TokenCipher("secret-two").decrypt(encrypted)


def test_empty_secret_is_rejected():
    from security.crypto import CryptoError, TokenCipher

    with pytest.raises(CryptoError):
        TokenCipher("")


def test_empty_plaintext_is_rejected():
    from security.crypto import CryptoError, TokenCipher

    with pytest.raises(CryptoError):
        TokenCipher("secret-one").encrypt("")


def test_decrypting_garbage_raises_crypto_error_not_a_raw_fernet_error():
    from security.crypto import CryptoError, TokenCipher

    with pytest.raises(CryptoError):
        TokenCipher("secret-one").decrypt("not-a-fernet-token")
