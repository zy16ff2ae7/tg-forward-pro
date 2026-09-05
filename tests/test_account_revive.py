"""Одна осечка — не приговор: аккаунт возвращают в работу.

В боевой БД нашёлся аккаунт ``is_active=0, last_error='Не удалось запустить
сессию'``: он лежал так с утра, пересылка на нём стояла, и никто об этом не
знал. Причина простая. ``set_account_error`` выключает аккаунт на любой беде,
``all_active_accounts`` выключенный больше не отдаёт, а отключение в кабинете
удаляет строку целиком — значит выключенный аккаунт всегда означал «сервис
сдался», и вернуть его мог только полный вход по номеру заново. Между тем беда
там была из проходящих: сервис поднялся раньше сети (за неделю 24 перезапуска).

Здесь проверяем, что:

* проходящая беда аккаунт из работы не убирает, но причину показывает;
* сервис возвращается к упавшему аккаунту сам, включая старые «припаркованные»;
* мёртвую сессию он повторами не мучает — там нужен вход, и так и написано;
* человек может попросить попытку сразу и услышать внятный ответ.

В Telegram здесь не ходит никто: ``_new_client`` отдаёт подделку.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.db.models import TelegramAccount
from app.security import encrypt_session
from app.telegram_client import manager as manager_module
from app.telegram_client.manager import (
    ACCOUNT_SILENT,
    SESSION_REVOKED,
    SESSION_UNREADABLE,
    manager,
)
from tests.helpers import TEST_USER_ID
from tests.test_account_session import (  # общая подделка Telethon
    FakeClient,
    account_state,
)

PHONE = "+79001234567"
SESSION = "1AaBbCc-fake-session"
PARKED = "Не удалось запустить сессию"


@pytest.fixture(autouse=True)
async def stop_manager():
    """Менеджер один на весь процесс: поднятое одним тестом гасим за ним же."""
    yield
    await manager.stop_all()


@pytest.fixture
def mtproto_on(monkeypatch):
    """Ключи MTProto на месте: без них start_all не доходит до аккаунтов."""
    monkeypatch.setattr(type(settings), "mtproto_ready", property(lambda self: True))


@pytest.fixture
async def account_id(create_user) -> int:
    user_id = await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        row = TelegramAccount(
            user_id=user_id, phone=PHONE, session_encrypted=encrypt_session(SESSION)
        )
        session.add(row)
        await session.flush()
        return row.id


@pytest.fixture
async def account(account_id: int) -> TelegramAccount:
    async with SessionLocal() as session:
        row = await session.get(TelegramAccount, account_id)
        assert row is not None
        session.expunge(row)
        return row


async def park(account_id: int, error: str) -> None:
    """Ставит аккаунт в то состояние, в каком его находили в боевой БД."""
    async with session_scope() as session:
        row = await session.get(TelegramAccount, account_id)
        row.last_error = error
        row.is_active = False


def live(monkeypatch) -> FakeClient:
    client = FakeClient(authorized=True)
    monkeypatch.setattr(manager, "_new_client", lambda session_string="": client)
    return client


# ───────────────────────── что считать приговором ─────────────────────────────


async def test_a_passing_trouble_keeps_the_account_in_work(account_id):
    """Причина видна, аккаунт остаётся среди тех, кого сервис поднимает."""
    async with session_scope() as session:
        row = await session.get(TelegramAccount, account_id)
        await repo.note_account_trouble(session, row, "TimeoutError: Telegram молчит")

    error, active = await account_state(account_id)
    assert error == "TimeoutError: Telegram молчит"
    assert active is True

    async with SessionLocal() as session:
        ids = [row.id for row in await repo.accounts_to_start(session)]
    assert ids == [account_id]


async def test_a_parked_account_is_taken_back(account_id):
    """Выключенный из-за проходящей беды — в списке на подъём.

    Это и есть наследство: строки, выключенные прежним поведением, иначе так и
    лежали бы мёртвым грузом.
    """
    await park(account_id, PARKED)

    async with SessionLocal() as session:
        ids = [
            row.id
            for row in await repo.accounts_to_start(session, (SESSION_REVOKED,))
        ]
    assert ids == [account_id]


async def test_a_revoked_session_is_left_alone(account_id):
    """Мёртвую сессию повторами не мучаем: её оживит только вход по номеру."""
    await park(account_id, SESSION_REVOKED)

    async with SessionLocal() as session:
        rows = await repo.accounts_to_start(session, (SESSION_REVOKED,))
    assert list(rows) == []


async def test_an_unreadable_session_asks_for_a_new_login(account, monkeypatch):
    """Нечитаемую сессию не пересказываем исключением — говорим, что делать."""

    def broken(value: str) -> str:
        raise ValueError("Fernet key rotated")

    # Расшифровка берётся из ``app.security`` внутри функции, её и подменяем.
    monkeypatch.setattr("app.security.decrypt_session", broken)

    ok = await manager._start_and_record(account)

    assert ok is False
    error, active = await account_state(account.id)
    assert error == SESSION_UNREADABLE
    assert active is False, "перебирать нечитаемую строку каждые три минуты незачем"


# ──────────────────────── сервис возвращается сам ─────────────────────────────


async def test_start_all_revives_a_parked_account(account_id, mtproto_on, monkeypatch):
    """Запуск сервиса поднимает и тех, кого прошлое поведение выключило."""
    await park(account_id, PARKED)
    client = live(monkeypatch)

    await manager.start_all()

    assert client.connects == 1
    assert manager.is_online(account_id) is True
    error, active = await account_state(account_id)
    assert (error, active) == (None, True)
    # И круг подъёма запущен: без него следующая осечка снова стала бы вечной.
    assert manager._revive_task is not None and not manager._revive_task.done()


async def test_start_all_does_not_touch_a_revoked_session(account_id, mtproto_on, monkeypatch):
    """Аккаунт с мёртвой сессией не трогают: ни клиента, ни новой причины."""
    await park(account_id, SESSION_REVOKED)
    client = live(monkeypatch)

    await manager.start_all()

    assert client.connects == 0, "лишний стук в Telegram ничего не изменит"
    assert await account_state(account_id) == (SESSION_REVOKED, False)


async def test_the_service_comes_back_on_its_own(account_id, mtproto_on, monkeypatch):
    """Круг подъёма возвращает аккаунт в работу без участия человека."""
    await park(account_id, PARKED)
    client = live(monkeypatch)
    monkeypatch.setattr(manager_module, "REVIVE_INTERVAL", 0.02)

    task = asyncio.create_task(manager._revive_loop())
    try:
        for _ in range(100):
            if manager.is_online(account_id):
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()

    assert manager.is_online(account_id) is True, "аккаунт так и остался офлайн"
    assert client.connects == 1
    assert await account_state(account_id) == (None, True)


async def test_the_revive_loop_skips_accounts_already_online(account_id, mtproto_on, monkeypatch):
    """Второй клиент на ту же сессию не поднимают — за это Telegram отбирает ключ."""
    client = live(monkeypatch)
    assert await manager.start_account(await _detached(account_id), SESSION) is True

    await manager._start_pending_accounts()

    assert client.connects == 1, "подключились второй раз к тому же аккаунту"


async def test_the_revive_loop_survives_a_failure(account_id, mtproto_on, monkeypatch):
    """Отказ внутри круга не убивает круг: следующий заход всё равно будет."""
    monkeypatch.setattr(manager_module, "REVIVE_INTERVAL", 0.02)
    calls: list[int] = []

    async def boom() -> list[int]:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("БД занята")
        return []

    monkeypatch.setattr(manager, "_start_pending_accounts", boom)

    task = asyncio.create_task(manager._revive_loop())
    try:
        for _ in range(100):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()

    assert len(calls) >= 2, f"круг оборвался после первой ошибки: заходов {len(calls)}"


# ─────────────────────── попытка по просьбе человека ──────────────────────────


async def test_retry_brings_the_account_back(account_id, monkeypatch):
    """Кнопка «Попробовать снова» поднимает аккаунт и убирает жалобу."""
    await park(account_id, PARKED)
    client = live(monkeypatch)

    online, error = await manager.retry_account(account_id)

    assert (online, error) == (True, None)
    assert client.connects == 1
    assert await account_state(account_id) == (None, True)


async def test_retry_names_the_reason_when_it_fails_again(account_id, monkeypatch):
    """Не вышло — человек слышит причину, а аккаунт остаётся в работе."""
    client = FakeClient(connect_error=OSError("network is unreachable"))
    monkeypatch.setattr(manager, "_new_client", lambda session_string="": client)

    online, error = await manager.retry_account(account_id)

    assert online is False
    assert "unreachable" in (error or ""), f"невнятный ответ: {error}"
    _, active = await account_state(account_id)
    assert active is True, "проходящая беда аккаунт не выключает"


async def test_retry_of_a_dead_session_says_what_to_do(account_id, monkeypatch):
    """Повтор мёртвой сессии честно отвечает, что нужен вход по номеру."""
    client = FakeClient(authorized=False)
    monkeypatch.setattr(manager, "_new_client", lambda session_string="": client)

    online, error = await manager.retry_account(account_id)

    assert (online, error) == (False, SESSION_REVOKED)


async def test_retry_of_an_account_online_is_not_a_second_client(account_id, monkeypatch):
    """Аккаунт уже на связи — отвечаем «да» и в Telegram не идём."""
    client = live(monkeypatch)
    await manager.start_account(await _detached(account_id), SESSION)

    online, error = await manager.retry_account(account_id)

    assert (online, error) == (True, None)
    assert client.connects == 1


async def test_retry_of_an_unknown_account(monkeypatch):
    """Аккаунта нет — отказ с внятным текстом, а не исключение."""
    live(monkeypatch)

    assert await manager.retry_account(424_242) == (False, "Аккаунт не найден")


async def test_a_silent_telegram_keeps_the_account_in_work(account, monkeypatch):
    """Соединение есть, данных нет: причина записана, аккаунт не выключен."""
    client = FakeClient(authorized=True)
    client.me = None
    monkeypatch.setattr(manager, "_new_client", lambda session_string="": client)

    ok = await manager.start_account(account, SESSION)

    assert ok is False
    assert client.disconnects == 1, "соединение надо закрыть за собой"
    error, active = await account_state(account.id)
    assert error == ACCOUNT_SILENT
    assert active is True


async def _detached(account_id: int) -> TelegramAccount:
    """Строка аккаунта, живущая после закрытия сессии БД."""
    async with SessionLocal() as session:
        row = await session.get(TelegramAccount, account_id)
        session.expunge(row)
        return row
