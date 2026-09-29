"""Tests for token encryption utilities."""

from unittest.mock import patch

import pytest

from app.utils.token_encryption import decrypt_token, encrypt_token, is_encryption_configured


def test_encrypt_decrypt_token_with_key():
    """Test token encryption and decryption with encryption key."""
    with patch("app.core.config.settings.JWT_SECRET", "test_jwt_secret_key_for_testing"):
        token = "test_access_token_value"
        encrypted = encrypt_token(token)

        # Should be encrypted (different from original)
        assert encrypted != token

        # Should decrypt back to original
        decrypted = decrypt_token(encrypted)
        assert decrypted == token


def test_encrypt_token_without_key():
    """Test token encryption without key raises ValueError."""
    with patch("app.core.config.settings.JWT_SECRET", None):
        with pytest.raises(ValueError, match="JWT_SECRET must be configured"):
            encrypt_token("test_token")


def test_decrypt_token_without_key():
    """Test token decryption without key raises ValueError."""
    with patch("app.core.config.settings.JWT_SECRET", None):
        with pytest.raises(ValueError, match="JWT_SECRET must be configured"):
            decrypt_token("encrypted_token")


def test_encrypt_none_token():
    """Test encrypting None token raises ValueError."""
    with pytest.raises(ValueError, match="Cannot encrypt empty token"):
        encrypt_token(None)


def test_decrypt_none_token():
    """Test decrypting None token raises ValueError."""
    with pytest.raises(ValueError, match="Cannot decrypt empty token"):
        decrypt_token(None)


def test_encrypt_empty_token():
    """Test encrypting empty string token raises ValueError."""
    with patch("app.core.config.settings.JWT_SECRET", "test_jwt_secret_key"):
        with pytest.raises(ValueError, match="Cannot encrypt empty token"):
            encrypt_token("")


def test_decrypt_invalid_token():
    """Test decrypting invalid ciphertext raises ValueError."""
    with patch("app.core.config.settings.JWT_SECRET", "test_jwt_secret_key"):
        with pytest.raises(ValueError, match="Failed to decrypt token"):
            decrypt_token("not_a_valid_encrypted_token")


def test_encrypt_long_token():
    """Test encrypting a long token."""
    with patch("app.core.config.settings.JWT_SECRET", "test_jwt_secret_key_for_long_tokens"):
        long_token = "a" * 1000
        encrypted = encrypt_token(long_token)
        decrypted = decrypt_token(encrypted)
        assert decrypted == long_token


def test_encrypt_special_characters():
    """Test encrypting token with special characters."""
    with patch("app.core.config.settings.JWT_SECRET", "test_jwt_secret_key_special"):
        token = "token!@#$%^&*()_+-={}[]|:;<>?,./"
        encrypted = encrypt_token(token)
        decrypted = decrypt_token(encrypted)
        assert decrypted == token


def test_encrypt_unicode_token():
    """Test encrypting token with unicode characters."""
    with patch("app.core.config.settings.JWT_SECRET", "test_jwt_secret_key_unicode"):
        token = "token_with_unicode_🔐🔑"
        encrypted = encrypt_token(token)
        decrypted = decrypt_token(encrypted)
        assert decrypted == token


def test_is_encryption_configured_with_valid_key():
    """Test is_encryption_configured returns True with valid JWT_SECRET."""
    with patch("app.core.config.settings.JWT_SECRET", "valid_secret"):
        assert is_encryption_configured() is True


def test_is_encryption_configured_with_change_me():
    """Test is_encryption_configured returns False with default 'change_me'."""
    with patch("app.core.config.settings.JWT_SECRET", "change_me"):
        assert is_encryption_configured() is False


def test_is_encryption_configured_with_none():
    """Test is_encryption_configured returns False with None."""
    with patch("app.core.config.settings.JWT_SECRET", None):
        assert is_encryption_configured() is False
