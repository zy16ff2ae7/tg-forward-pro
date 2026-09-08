"""Мёртвая сессия аккаунта: человек должен понять, что делать.

Ключ Telethon умирает сам: аккаунт вышел из Telegram («Устройства» → завершить
сеанс), сменил облачный пароль или тем же ключом вошли с другой машины. Сервису
чинить тут нечего — нужен повторный вход по номеру.

Раньше запуск аккаунта звал ``client.start()``. Для неавторизованной сессии
Telethon спрашивает номер и код через ``input()``, а под systemd stdin закрыт —
и в журнале боевого сервиса оседало «Аккаунт #N (+…) не запустился: EOF when
reading a line». Та же строка уходила человеку в кабинет как причина: по «EOF»
не догадаться, что аккаунт надо подключить заново.

Здесь проверяем, что:

* мёртвая сессия объясняет себя словами и аккаунт гаснет;
* консоль никто не спрашивает — ``start()`` не зовётся вовсе;
* соединение за собой закрывают, среди живых клиентов мёртвого не остаётся;
* живая сессия по-прежнему выходит на связь;
* обрыв связи не выдают за отзыв сессии;
* ``start_all`` не затирает названную причину общей подписью.

В Telegram здесь не ходит никто: ``_new_client`` отдаёт подделку.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.db.database import SessionLocal, session_scope
from app.db.models import TelegramAccount
from app.security import encrypt_session
from app.telegram_client.manager import SESSION_REVOKED, manager
from tests.helpers import TEST_USER_ID

PHONE = "+79001234567"
SESSION = "1AaBbCc-fake-session"


class FakeClient:
    """Telethon без Telegram: отвечает тем, что попросил тест, и ведёт журнал.

    ``start()`` здесь нарочно падает тем самым EOF: если запуск снова свернёт
    на интерактивный путь, тест это увидит, а не сделает вид, что всё хорошо.
    """

    def __init__(
        self, *, authorized: bool = True, connect_error: Exception | None = None
    ) -> None:
        self.authorized = authorized
        self.connect_error = connect_error
        self.me: SimpleNamespace | None = SimpleNamespace(id=777, username="doch")
        self.handlers: list = []
        self.connects = 0
        self.disconnects = 0
        self.starts = 0
        self.connected = False

    def add_event_handler(self, callback, event=None) -> None:
        self.handlers.append((callback, event))

    async def connect(self) -> None:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def start(self, *args, **kwargs):
        self.starts += 1
        raise EOFError("EOF when reading a line")

    async def get_me(self):
        return self.me

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected


# ──────────────────────────────── обстановка ──────────────────────────────────


@pytest.fixture(autouse=True)
async def stop_manager():
    """Менеджер один на весь процесс: поднятое одним тестом гасим за ним же."""
    yield
    await manager.stop_all()


@pytest.fixture
async def account_id(create_user) -> int:
    """Активный аккаунт с читаемой сессией — какой поднимает боевой запуск."""
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
    """Тот же аккаунт объектом: ``start_account`` принимает строку из БД."""
    async with SessionLocal() as session:
        row = await session.get(TelegramAccount, account_id)
        assert row is not None
        # Отцепляем от сессии: та закроется, а объект живёт до конца теста.
        session.expunge(row)
        return row


async def account_state(account_id: int) -> tuple[str | None, bool]:
    """Что видит кабинет: причина отказа и включён ли аккаунт."""
    async with SessionLocal() as session:
        row = await session.get(TelegramAccount, account_id)
        assert row is not None
        return row.last_error, row.is_active


@pytest.fixture
def dead(monkeypatch) -> FakeClient:
    """Ключ есть, но Telegram его больше не признаёт."""
    client = FakeClient(authorized=False)
    monkeypatch.setattr(manager, "_new_client", lambda session_string="", creds=None, **kwargs: client)
    return client


# ─────────────────────────── один аккаунт: запуск ─────────────────────────────


async def test_a_dead_session_says_what_to_do(account, dead: FakeClient):
    """Причина в кабинете — про повторный вход, а не про исключение Telethon."""
    ok = await manager.start_account(account, SESSION)

    assert ok is False
    error, active = await account_state(account.id)
    assert error == SESSION_REVOKED
    assert active is False, "мёртвый ключ не оживить — аккаунт гасим"


async def test_nobody_is_asked_for_a_code_in_the_console(account, dead: FakeClient):
    """Интерактивный путь закрыт: именно он давал «EOF when reading a line»."""
    await manager.start_account(account, SESSION)

    assert dead.starts == 0, "start() спросил бы номер и код через input()"
    assert dead.connects == 1, "подключаемся сами"
    error, _ = await account_state(account.id)
    assert "EOF" not in (error or ""), f"в кабинете снова невнятица: {error}"


async def test_the_dead_connection_is_released(account, dead: FakeClient):
    """Соединение закрыто, среди живых клиентов мёртвого нет."""
    await manager.start_account(account, SESSION)

    assert dead.disconnects == 1
    assert account.id not in manager._clients
    assert manager.is_online(account.id) is False


async def test_a_live_session_goes_online(account, monkeypatch):
    """Обычный аккаунт поднимается как раньше — и слушает входящие."""
    client = FakeClient(authorized=True)
    monkeypatch.setattr(manager, "_new_client", lambda session_string="", creds=None, **kwargs: client)

    ok = await manager.start_account(account, SESSION)

    assert ok is True
    assert manager.is_online(account.id) is True
    assert client.starts == 0
    assert client.handlers, "без обработчика входящих задачи не сработают"


async def test_a_broken_link_is_not_a_revoked_session(account, monkeypatch):
    """Сеть отвалилась — так и написано: это другая беда, чинится сама."""
    client = FakeClient(connect_error=OSError("network is unreachable"))
    monkeypatch.setattr(manager, "_new_client", lambda session_string="", creds=None, **kwargs: client)

    ok = await manager.start_account(account, SESSION)

    assert ok is False
    error, _ = await account_state(account.id)
    assert error != SESSION_REVOKED, "повторный вход тут ничего не исправит"
    assert "OSError" in (error or "") and "unreachable" in (error or "")


# ───────────────────────── запуск всех: причина цела ──────────────────────────


async def test_start_all_keeps_the_named_reason(account_id, dead, mtproto_on):
    """Общая подпись больше не затирает «подключите номер заново».

    Раньше start_all писал «Не удалось запустить сессию» на любой отказ — уже
    после того, как start_account назвал настоящую причину. В кабинете от
    объяснения не оставалось ничего.
    """
    await manager.start_all()

    error, active = await account_state(account_id)
    assert error == SESSION_REVOKED
    assert active is False


async def test_start_all_still_names_a_silent_failure(account_id, mtproto_on, monkeypatch):
    """Отказ без объяснения по-прежнему получает общую подпись, а не пустоту.

    Аккаунт при этом остаётся в работе: подпись общая как раз потому, что
    причина неизвестна, а неизвестная беда чаще проходит сама
    (см. ``tests/test_account_revive.py``).
    """

    async def start_account(account, session_string: str) -> bool:
        return False

    monkeypatch.setattr(manager, "start_account", start_account)

    await manager.start_all()

    error, active = await account_state(account_id)
    assert error == "Не удалось запустить сессию"
    assert active is True


async def test_start_all_clears_an_old_reason_on_success(account_id, mtproto_on, monkeypatch):
    """Поднялся — прошлая жалоба уходит: иначе она висела бы в кабинете вечно."""
    async with session_scope() as session:
        row = await session.get(TelegramAccount, account_id)
        row.last_error = SESSION_REVOKED

    client = FakeClient(authorized=True)
    monkeypatch.setattr(manager, "_new_client", lambda session_string="", creds=None, **kwargs: client)

    await manager.start_all()

    error, active = await account_state(account_id)
    assert error is None
    assert active is True
