"""
OAuth Token Encryption Utilities.

Provides secure encryption/decryption for OAuth refresh tokens and access tokens
using Fernet (symmetric encryption with AES-128 in CBC mode).

Security:
- Derives encryption key from JWT_SECRET using PBKDF2-HMAC-SHA256
- 100,000 iterations for key stretching
- Unique salt for OAuth token encryption
- Tokens encrypted at rest in database
"""

import base64
import hashlib
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

logger = logging.getLogger(__name__)

# Salt for PBKDF2 key derivation (constant, not a secret)
_OAUTH_SALT = b"suoops_oauth_token_encryption_v1"


def _get_fernet_key() -> bytes:
    """
    Derive Fernet encryption key from JWT_SECRET.

    Uses PBKDF2-HMAC-SHA256 with 100,000 iterations to derive a proper
    32-byte key suitable for Fernet encryption.

    Returns:
        Base64-encoded 32-byte key for Fernet

    Raises:
        ValueError: If JWT_SECRET is not configured
    """
    if not settings.JWT_SECRET or settings.JWT_SECRET == "change_me":
        raise ValueError("JWT_SECRET must be configured for token encryption")

    key_material = settings.JWT_SECRET.encode("utf-8")

    # Derive 32-byte key using PBKDF2
    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        key_material,
        _OAUTH_SALT,
        100000,  # 100k iterations
        dklen=32,  # 32 bytes for AES-256
    )

    # Fernet requires base64-encoded key
    return base64.urlsafe_b64encode(derived_key)


def encrypt_token(token: str) -> str:
    """
    Encrypt OAuth token for secure storage.

    Uses Fernet symmetric encryption (AES-128 CBC + HMAC).

    Args:
        token: Plain OAuth token to encrypt

    Returns:
        Base64-encoded encrypted token

    Raises:
        ValueError: If token is empty or JWT_SECRET not configured

    Example:
        >>> encrypted = encrypt_token("ya29.a0AfH6SMB...")
        >>> assert encrypted != "ya29.a0AfH6SMB..."
    """
    if not token:
        raise ValueError("Cannot encrypt empty token")

    try:
        fernet = Fernet(_get_fernet_key())
        encrypted_bytes = fernet.encrypt(token.encode("utf-8"))
        return encrypted_bytes.decode("utf-8")
    except Exception as e:
        # Log as warning to avoid Sentry noise when OAuth tokens aren't used
        logger.warning(f"Token encryption failed: {e}")
        raise


def decrypt_token(encrypted_token: str) -> str:
    """
    Decrypt OAuth token from storage.

    Args:
        encrypted_token: Base64-encoded encrypted token

    Returns:
        Decrypted plain token

    Raises:
        ValueError: If encrypted_token is invalid or JWT_SECRET not configured
        InvalidToken: If decryption fails (corrupted data or wrong key)

    Example:
        >>> plain = decrypt_token(encrypted)
        >>> assert plain == "ya29.a0AfH6SMB..."
    """
    if not encrypted_token:
        raise ValueError("Cannot decrypt empty token")

    try:
        fernet = Fernet(_get_fernet_key())
        decrypted_bytes = fernet.decrypt(encrypted_token.encode("utf-8"))
        return decrypted_bytes.decode("utf-8")
    except InvalidToken as e:
        # Log as warning - invalid tokens are expected in some scenarios
        logger.warning("Token decryption failed: invalid token or wrong key")
        raise ValueError("Failed to decrypt token") from e
    except Exception as e:
        # Log as warning to avoid Sentry noise
        logger.warning(f"Token decryption failed: {e}")
        raise


def is_encryption_configured() -> bool:
    """
    Check if token encryption is properly configured.

    Returns:
        True if JWT_SECRET is set and not using default value
    """
    return bool(settings.JWT_SECRET and settings.JWT_SECRET != "change_me")
