"""Подключение к БД: асинхронный движок и фабрика сессий SQLAlchemy 2.0."""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import import_module

from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import settings


class Base(DeclarativeBase):
    """Базовый класс для всех моделей."""


def _engine_kwargs(database_url: str) -> dict:
    kwargs: dict = {"echo": False, "future": True}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return kwargs


engine: AsyncEngine = create_async_engine(
    settings.database_url, **_engine_kwargs(settings.database_url)
)


if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        """Настройки SQLite для одновременной работы бота, мини-аппа и фона.

        WAL вместо rollback-журнала: журнал не создаётся и не удаляется на каждом
        коммите, а читатели не блокируются писателем. Это убирает «disk I/O error»
        в средах, где удаление файлов ограничено, и заметно ускоряет конкурентную
        запись — бот, мини-апп и фоновые задачи пишут в одну БД параллельно.
        """
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
        finally:
            cursor.close()

SessionLocal = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Транзакционная сессия: коммит при успехе, откат при ошибке."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """Зависимость для aiogram-хендлеров."""
    async with SessionLocal() as session:
        yield session


# Колонки, которых не было в первых версиях схемы. create_all создаёт новые
# таблицы, но не добавляет колонки в уже существующие — поэтому они перечислены
# здесь и доливаются точечно. Значения по умолчанию совпадают с моделями,
# поэтому старые строки получают ровно то же, что имели бы при создании с нуля.
ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "channel_bonus_at": "DATETIME",
        "pending_promo_id": "INTEGER",
        # Старым строкам — 1: раньше дни дарили за регистрацию, и все старые
        # связи уже оплачены. Новым строкам ORM пишет свой default=False:
        # единица в DDL нужна только для доливки, дальше её перекрывает модель.
        "referred_rewarded": "BOOLEAN NOT NULL DEFAULT 1",
        "onboard_day0_at": "DATETIME",
        "onboard_day1_at": "DATETIME",
        "onboard_day2_at": "DATETIME",
    },
    "promo_codes": {
        "percent": "INTEGER NOT NULL DEFAULT 0",
        "owner_id": "BIGINT",
    },
    "rules": {
        "kind": "TEXT NOT NULL DEFAULT 'forward'",
        "archived": "BOOLEAN NOT NULL DEFAULT 0",
        "silent_notified_at": "DATETIME",
    },
    "subscriptions": {
        "banked_days": "INTEGER NOT NULL DEFAULT 0",
        "period_start": "DATETIME",
        "expired_notified_at": "DATETIME",
        "lastday_notified_at": "DATETIME",
        "winback_notified_at": "DATETIME",
    },
    "payments": {
        "tx_id": "TEXT",
        "reminded_at": "DATETIME",
        "promo_code": "TEXT",
    },
    "pending_logins": {
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "api_id": "INTEGER",
        "api_hash_encrypted": "TEXT",
    },
    "telegram_accounts": {
        "error_notified_at": "DATETIME",
        "api_id": "INTEGER",
        "api_hash_encrypted": "TEXT",
        "disabled_at": "DATETIME",
    },
}

# Индексы, добавленные позже: create_all создаёт индексы только вместе с новой
# таблицей, поэтому для уже существующих их доливаем отдельно. Уникальный
# индекс по tx_id — это и есть защита от повторного зачёта одного перевода,
# поэтому на старых базах он обязателен, а не «желателен».
ADDED_INDEXES: dict[str, dict[str, str]] = {
    "payments": {
        "ux_payments_tx": 'CREATE UNIQUE INDEX IF NOT EXISTS "ux_payments_tx" '
        'ON "payments" ("tx_id")',
    },
}


def _existing_columns(connection, table: str) -> set[str]:
    if connection.dialect.name == "sqlite":
        rows = connection.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
        return {row[1] for row in rows}
    rows = connection.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :table"
        ),
        {"table": table},
    ).fetchall()
    return {row[0] for row in rows}


def _add_missing_columns(connection) -> None:
    for table, columns in ADDED_COLUMNS.items():
        if not connection.dialect.has_table(connection, table):
            continue
        existing = _existing_columns(connection, table)
        for name, ddl in columns.items():
            if name not in existing:
                connection.execute(
                    text(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')
                )


def _add_missing_indexes(connection) -> None:
    for table, indexes in ADDED_INDEXES.items():
        if not connection.dialect.has_table(connection, table):
            continue
        for name, ddl in indexes.items():
            try:
                connection.execute(text(ddl))
            except Exception as exc:  # noqa: BLE001
                # Единственная реальная причина — дубликаты в уже накопленных
                # данных. Молча падать на старте из-за этого нельзя, но и
                # прятать тоже: пишем в лог, дальше защиту держит проверка в коде.
                logger.warning("Не создали индекс {} на {}: {}", name, table, exc)


async def ensure_schema() -> None:
    """Доливает колонки и индексы, появившиеся после первого запуска."""
    async with engine.begin() as conn:
        await conn.run_sync(_add_missing_columns)
        await conn.run_sync(_add_missing_indexes)


async def init_db() -> None:
    """Создаёт таблицы при первом запуске и доливает новые колонки."""
    # Импорт нужен ради побочного эффекта: модели регистрируют себя в
    # Base.metadata. Прямой from-import сделать нельзя — models тянет Base
    # отсюда же, получился бы цикл.
    import_module("app.db.models")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await ensure_schema()


async def dispose_db() -> None:
    await engine.dispose()
