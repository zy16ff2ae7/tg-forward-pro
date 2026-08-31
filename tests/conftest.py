"""Общая настройка тестов.

Окружение подменяется до первого импорта ``app.*``: и настройки, и движок БД
создаются в момент импорта модулей, поэтому менять их позже уже бесполезно.
"""
from __future__ import annotations

import itertools
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

_TMP_DIR = Path(tempfile.mkdtemp(prefix="tgf-tests-"))

os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TMP_DIR / 'test.db'}"
os.environ["BOT_TOKEN"] = "123456789:AAFakeTokenForUnitTests0000000000"
os.environ["SECRET_KEY"] = Fernet.generate_key().decode()
os.environ["ADMIN_IDS"] = "1"
os.environ["LOG_LEVEL"] = "WARNING"

import pytest  # noqa: E402

from app.db.database import Base, engine, init_db, session_scope  # noqa: E402
from app.db.models import TelegramAccount, User  # noqa: E402

# Подписки, правила и платежи ссылаются на users.id внешним ключом,
# поэтому пользователя нужно создавать до всего остального.
_user_ids = itertools.count(555_000_000)


@pytest.fixture(autouse=True)
async def database():
    """Чистая схема под каждый тест: создаём таблицы, после — сносим."""
    await init_db()
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture
def create_user():
    async def _create(**kwargs) -> int:
        user_id = kwargs.pop("id", None) or next(_user_ids)
        async with session_scope() as session:
            session.add(User(id=user_id, **kwargs))
        return user_id

    return _create


@pytest.fixture
def create_account():
    """Правила ссылаются на аккаунт внешним ключом — без него правило не создать."""

    async def _create(user_id: int, phone: str = "+79000000000") -> int:
        async with session_scope() as session:
            account = TelegramAccount(
                user_id=user_id, phone=phone, session_encrypted="test-session"
            )
            session.add(account)
            await session.flush()
            return account.id

    return _create
