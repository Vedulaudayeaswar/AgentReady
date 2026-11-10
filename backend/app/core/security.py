"""Password hashing, JWT access tokens and opaque token helpers."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from app.core.config import get_settings

ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    # bcrypt only uses the first 72 bytes; pre-hash so long passphrases are not truncated.
    digest = hashlib.sha256(password.encode()).hexdigest().encode()
    return bcrypt.hashpw(digest, bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    digest = hashlib.sha256(password.encode()).hexdigest().encode()
    try:
        return bcrypt.checkpw(digest, password_hash.encode())
    except ValueError:
        return False


def create_access_token(user_id: uuid.UUID) -> tuple[str, datetime]:
    s = get_settings()
    now = datetime.now(timezone.utc)
    exp = now + timedelta(minutes=s.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        # millisecond issue time: used to invalidate sessions on password change
        "iat_ms": int(now.timestamp() * 1000),
        "exp": exp,
        "jti": secrets.token_hex(16),
        "typ": "access",
    }
    return jwt.encode(payload, s.secret_key, algorithm=ALGORITHM), exp


def decode_access_token(token: str) -> dict:
    return jwt.decode(
        token,
        get_settings().secret_key,
        algorithms=[ALGORITHM],
        options={"require": ["sub", "exp", "jti", "iat", "iat_ms"]},
    )


def generate_opaque_token(prefix: str = "") -> str:
    return prefix + secrets.token_urlsafe(32)


def hash_opaque_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
