"""Encryption at rest for integration credentials (Fernet: AES-128-CBC + HMAC-SHA256)."""

from __future__ import annotations

import base64
import json

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import get_settings


class CredentialError(RuntimeError):
    pass


def _fernet() -> Fernet:
    s = get_settings()
    if s.credentials_encryption_key:
        try:
            return Fernet(s.credentials_encryption_key.encode())
        except ValueError as e:
            raise CredentialError("CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key") from e
    if s.environment == "production":
        raise CredentialError("CREDENTIALS_ENCRYPTION_KEY must be set in production to store integration credentials")
    # development/test: derive a key from SECRET_KEY (documented; rotating SECRET_KEY invalidates stored credentials)
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"agentready", info=b"integration-credentials").derive(
        s.secret_key.encode())
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_json(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode()).decode()


def decrypt_json(token: str | None) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode()))
    except InvalidToken as e:
        raise CredentialError("Stored credentials cannot be decrypted (encryption key changed?). Reconnect the store.") from e


def hint(secret: str) -> str:
    secret = secret or ""
    return f"{secret[:3]}…{secret[-4:]}" if len(secret) > 10 else "…"
