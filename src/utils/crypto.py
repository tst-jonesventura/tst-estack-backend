"""Symmetric encryption helpers for persisting sensitive system configuration.

Sensitive company/MCP configuration (tokens, private keys) is encrypted at rest
using Fernet (AES-128-CBC + HMAC). The key is provided via the
``ESTACK_CONFIG_ENCRYPTION_KEY`` environment variable / setting.
"""
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import String, TypeDecorator

from utils.config import settings


class EncryptionKeyMissingError(RuntimeError):
    """Raised when an encrypt/decrypt operation is attempted without a key."""


class DecryptionError(RuntimeError):
    """Raised when a stored value cannot be decrypted with the current key."""


def _get_fernet() -> Fernet:
    key = settings.config_encryption_key
    if not key:
        raise EncryptionKeyMissingError(
            "ESTACK_CONFIG_ENCRYPTION_KEY is not configured; cannot "
            "encrypt/decrypt system configuration."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string, returning a URL-safe base64 token."""
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt_value(token: str) -> str:
    """Decrypt a token produced by :func:`encrypt_value`."""
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise DecryptionError(
            "Failed to decrypt system configuration value; the encryption "
            "key may be wrong or the data corrupted."
        ) from exc


class EncryptedString(TypeDecorator):
    """SQLAlchemy column type that transparently encrypts/decrypts strings.

    Values are encrypted on the way to the database and decrypted on load, so
    application code works with plaintext while the data stays encrypted at
    rest. ``None`` is passed through unchanged.
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value: Optional[str], dialect) -> Optional[str]:
        if value is None:
            return None
        return encrypt_value(value)

    def process_result_value(self, value: Optional[str], dialect) -> Optional[str]:
        if value is None:
            return None
        return decrypt_value(value)
