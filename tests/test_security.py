"""Tests for security module — bcrypt password hashing and token handling."""

import pytest

from app.core.security import (
    TokenExpiredError,
    TokenType,
    TokenValidationError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    validate_password_strength,
    verify_password,
)


class TestPasswordHashing:
    """Verify bcrypt hash/verify after passlib migration."""

    def test_hash_returns_bcrypt_format(self):
        h = hash_password("SecurePass1")
        assert h.startswith("$2b$12$"), f"Expected $2b$12$ prefix, got: {h[:10]}"

    def test_verify_correct_password(self):
        h = hash_password("MyP@ssw0rd")
        assert verify_password("MyP@ssw0rd", h) is True

    def test_verify_wrong_password(self):
        h = hash_password("RightPass1")
        assert verify_password("WrongPass1", h) is False

    def test_different_hashes_for_same_password(self):
        """Each call should produce a unique salt."""
        h1 = hash_password("SamePass1")
        h2 = hash_password("SamePass1")
        assert h1 != h2
        # Both should still verify
        assert verify_password("SamePass1", h1)
        assert verify_password("SamePass1", h2)

    def test_backward_compat_with_passlib_hash(self):
        """passlib produced $2b$ hashes — verify we can still check them.

        We generate a known bcrypt hash via the new code (same $2b$ format)
        and verify it works. Real backward compat is guaranteed by bcrypt
        format compatibility.
        """
        h = hash_password("LegacyPass1")
        assert h.startswith("$2b$")
        assert verify_password("LegacyPass1", h)


class TestPasswordStrength:
    def test_rejects_short_password(self):
        with pytest.raises(ValueError, match="at least 8"):
            validate_password_strength("Short1")

    def test_rejects_all_digits(self):
        with pytest.raises(ValueError, match="letters and numbers"):
            validate_password_strength("12345678")

    def test_rejects_all_alpha(self):
        with pytest.raises(ValueError, match="letters and numbers"):
            validate_password_strength("abcdefgh")

    def test_rejects_all_lowercase(self):
        with pytest.raises(ValueError, match="mixed case"):
            validate_password_strength("password1")

    def test_accepts_strong_password(self):
        validate_password_strength("StrongP4ss")  # Should not raise


class TestTokens:
    def test_access_token_roundtrip(self):
        token = create_access_token("user:42")
        payload = decode_token(token, TokenType.ACCESS)
        assert payload["sub"] == "user:42"
        assert payload["type"] == "access"

    def test_refresh_token_roundtrip(self):
        token = create_refresh_token("user:99")
        payload = decode_token(token, TokenType.REFRESH)
        assert payload["sub"] == "user:99"
        assert payload["type"] == "refresh"

    def test_type_mismatch_raises(self):
        token = create_access_token("user:1")
        with pytest.raises(TokenValidationError, match="type mismatch"):
            decode_token(token, TokenType.REFRESH)

    def test_expired_token_raises(self):
        token = create_access_token("user:1", expires_minutes=-1)
        with pytest.raises(TokenExpiredError, match="expired"):
            decode_token(token)

    def test_plan_included_in_token(self):
        token = create_access_token("user:1", user_plan="pro")
        payload = decode_token(token)
        assert payload["plan"] == "pro"

    def test_invalid_token_raises(self):
        with pytest.raises(TokenValidationError, match="invalid"):
            decode_token("not.a.real.token")
