"""Tests for encryption utilities."""

import pytest

from app.core.encryption import _get_cipher, decrypt_value, encrypt_value


@pytest.fixture(autouse=True)
def clear_cipher_cache():
    """Clear LRU cache before each test."""
    _get_cipher.cache_clear()
    yield
    _get_cipher.cache_clear()


def test_encrypt_decrypt_with_valid_key():
    """Test encryption and decryption with a valid key."""
    from app.core.config import settings

    orig_key = settings.ENCRYPTION_KEY
    try:
        settings.ENCRYPTION_KEY = "12345678901234567890123456789012"  # Exactly 32 bytes
        _get_cipher.cache_clear()
        plaintext = "sensitive_data"
        encrypted = encrypt_value(plaintext)
        assert encrypted is not None
        assert encrypted != plaintext

        decrypted = decrypt_value(encrypted)
        assert decrypted == plaintext
    finally:
        settings.ENCRYPTION_KEY = orig_key
        _get_cipher.cache_clear()


def test_encrypt_none_returns_none():
    """Test that encrypting None returns None."""
    assert encrypt_value(None) is None


def test_decrypt_none_returns_none():
    """Test that decrypting None returns None."""
    assert decrypt_value(None) is None


def test_encrypt_without_key_returns_plaintext():
    """Test that encryption without key returns plaintext."""
    from app.core.config import settings

    orig_key = settings.ENCRYPTION_KEY
    try:
        settings.ENCRYPTION_KEY = None
        _get_cipher.cache_clear()
        plaintext = "test_data"
        result = encrypt_value(plaintext)
        assert result == plaintext
    finally:
        settings.ENCRYPTION_KEY = orig_key
        _get_cipher.cache_clear()


def test_decrypt_without_key_returns_ciphertext():
    """Test that decryption without key returns original value."""
    from app.core.config import settings

    orig_key = settings.ENCRYPTION_KEY
    try:
        settings.ENCRYPTION_KEY = None
        _get_cipher.cache_clear()
        ciphertext = "encrypted_data"
        result = decrypt_value(ciphertext)
        assert result == ciphertext
    finally:
        settings.ENCRYPTION_KEY = orig_key
        _get_cipher.cache_clear()


def test_decrypt_invalid_token_returns_original():
    """Test that decryption with invalid token returns original value."""
    from app.core.config import settings

    orig_key = settings.ENCRYPTION_KEY
    try:
        settings.ENCRYPTION_KEY = "test_key_that_is_32_bytes_long!"
        _get_cipher.cache_clear()
        invalid_ciphertext = "not_valid_encrypted_data"
        result = decrypt_value(invalid_ciphertext)
        # Should return original if decryption fails
        assert result == invalid_ciphertext
    finally:
        settings.ENCRYPTION_KEY = orig_key
        _get_cipher.cache_clear()


def test_encrypt_with_base64_key():
    """Test encryption with base64 encoded key."""
    from cryptography.fernet import Fernet

    from app.core.config import settings

    key = Fernet.generate_key().decode()
    orig_key = settings.ENCRYPTION_KEY
    try:
        settings.ENCRYPTION_KEY = key
        _get_cipher.cache_clear()
        plaintext = "test_value"
        encrypted = encrypt_value(plaintext)
        assert encrypted != plaintext

        decrypted = decrypt_value(encrypted)
        assert decrypted == plaintext
    finally:
        settings.ENCRYPTION_KEY = orig_key
        _get_cipher.cache_clear()


def test_cipher_caching():
    """Test that cipher is cached properly."""
    from app.core.config import settings

    orig_key = settings.ENCRYPTION_KEY
    try:
        settings.ENCRYPTION_KEY = "test_key_that_is_32_bytes_long!"
        _get_cipher.cache_clear()
        cipher1 = _get_cipher()
        cipher2 = _get_cipher()
        # Should be same instance due to caching
        assert cipher1 is cipher2
    finally:
        settings.ENCRYPTION_KEY = orig_key
        _get_cipher.cache_clear()
