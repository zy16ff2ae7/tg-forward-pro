"""Шифрование Telethon-сессий ключом из SECRET_KEY (Fernet, симметричное)."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


class CryptoError(RuntimeError):
    """Не удалось расшифровать сессию — обычно значит, что сменился SECRET_KEY."""


def _fernet() -> Fernet:
    key = settings.secret_key
    if not key:
        raise CryptoError("SECRET_KEY не задан в .env")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:  # битый ключ
        raise CryptoError(f"Некорректный SECRET_KEY: {exc}") from exc


def encrypt_session(session_string: str) -> str:
    return _fernet().encrypt(session_string.encode()).decode()


def decrypt_session(encrypted: str) -> str:
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except InvalidToken as exc:
        raise CryptoError(
            "Не удалось расшифровать сессию. Проверьте, что SECRET_KEY не менялся."
        ) from exc


def generate_key() -> str:
    """Генератор ключа для .env (используется в scripts/gen_secret.py)."""
    return Fernet.generate_key().decode()
