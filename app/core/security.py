from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import bcrypt
import jwt
from jwt import ExpiredSignatureError
from jwt import InvalidTokenError as PyJWTInvalidTokenError

from app.core.config import settings


class TokenType(str, Enum):
    ACCESS = "access"
    REFRESH = "refresh"


ALGORITHM = "HS256"

# ── bcrypt cost factor ──────────────────────────────────────────────────
# 12 rounds is the modern recommended default (2^12 ≈ 4096 iterations).
_BCRYPT_ROUNDS = 12


class TokenValidationError(Exception):
    """Raised when a token cannot be validated."""


class TokenExpiredError(TokenValidationError):
    """Raised when a token is expired."""


def hash_password(password: str) -> str:
    """Hash a password using bcrypt with a random salt."""
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


def create_access_token(subject: str, expires_minutes: int = 30, user_plan: str | None = None) -> str:
    return _create_token(subject, timedelta(minutes=expires_minutes), TokenType.ACCESS, user_plan)


def create_refresh_token(subject: str, expires_days: int = 14) -> str:
    return _create_token(subject, timedelta(days=expires_days), TokenType.REFRESH)


def decode_token(token: str, expected_type: TokenType = TokenType.ACCESS) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[ALGORITHM])
    except ExpiredSignatureError as exc:
        raise TokenExpiredError("Token has expired") from exc
    except PyJWTInvalidTokenError as exc:
        raise TokenValidationError("Token is invalid") from exc
    token_type = payload.get("type", TokenType.ACCESS.value)
    if token_type != expected_type.value:
        raise TokenValidationError("Token type mismatch")
    return payload


def validate_password_strength(password: str) -> None:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters long")
    if password.isdigit() or password.isalpha():
        raise ValueError("Password must include both letters and numbers")
    if password.lower() == password or password.upper() == password:
        raise ValueError("Password must include mixed case characters")


def _create_token(subject: str, delta: timedelta, token_type: TokenType, user_plan: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + delta,
        "type": token_type.value,
    }
    # Add user plan for dynamic rate limiting (Strategy pattern)
    if user_plan:
        payload["plan"] = user_plan
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=ALGORITHM)
