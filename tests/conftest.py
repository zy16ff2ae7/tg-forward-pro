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
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from app.db.database import Base, engine, init_db, session_scope  # noqa: E402
from app.db.models import TelegramAccount, User  # noqa: E402
from app.errors import http_error_middleware  # noqa: E402
from app.webapp_api import setup_webapp_routes  # noqa: E402
from tests.helpers import sign_init_data  # noqa: E402

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


# ─────────────────────────── HTTP-клиенты кабинета ────────────────────────────


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Подпись Telegram: без неё любой эндпоинт кабинета отвечает 401."""
    return {"X-Telegram-Init-Data": sign_init_data()}


@pytest.fixture
async def client():
    """Кабинет без живого бота — как при работе только веб-части сервиса."""
    app = web.Application(middlewares=[http_error_middleware])
    setup_webapp_routes(app, bot=None)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


@pytest.fixture
async def bot_client():
    """Клиент, у которого есть «живой» бот.

    setup_webapp_routes пишет бота в глобальную переменную модуля, поэтому
    после теста её обязательно возвращаем в None — иначе следующий тест
    неожиданно увидит рабочего бота там, где ожидается его отсутствие.
    """
    import app.webapp_api as webapp_api

    started: list[TestClient] = []

    async def factory(bot):
        app = web.Application(middlewares=[http_error_middleware])
        setup_webapp_routes(app, bot=bot)
        test_client = TestClient(TestServer(app))
        await test_client.start_server()
        started.append(test_client)
        return test_client

    try:
        yield factory
    finally:
        for test_client in started:
            await test_client.close()
        webapp_api._bot = None
