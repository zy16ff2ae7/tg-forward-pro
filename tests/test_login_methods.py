"""Свои ключи API и вход по QR-коду.

Номер — не единственный путь: продвинутые входят своими ключами
(my.telegram.org/apps), а торопящиеся — сканированием QR из приложения.
Проверяем:

* ключи проверяются до похода в Telegram: пара или никак;
* свои ключи едут через все шаги номера и остаются на аккаунте;
* чужие ключи отклоняются словами про ключи, а не про сервис;
* QR: код картинкой → сканирование → аккаунт; 2FA — отдельным шагом;
* протухший QR и отмена закрывают соединение и не виснут;
* один вход на человека: номер гасит QR и наоборот.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from telethon.errors import ApiIdInvalidError, SessionPasswordNeededError

from app import accounts_login
from app.config import settings
from app.db import repo
from app.db.database import session_scope
from app.errors import ConflictError, ValidationError
from app.security import decrypt_session
from app.telegram_client.manager import LoginCreds, manager

PHONE = "+79001234567"
SESSION = "1A" * 32
SIGNED = "2B" * 32
API_ID = "123456"
API_HASH = "ab12cd34ef56ab12cd34ef56ab12cd34"


@pytest.fixture(autouse=True)
def _clean_qr():
    yield
    for user_id in list(accounts_login._qr_sessions):
        wait = accounts_login._qr_sessions.pop(user_id)
        if wait.task is not None and not wait.task.done():
            wait.task.cancel()


@pytest.fixture
def ready(monkeypatch):
    monkeypatch.setattr(type(settings), "mtproto_ready", property(lambda self: True))


class FakeQr:
    def __init__(self, url="tg://login?token=abc", error=None, password=False):
        self.url = url
        self.error = error
        self.password = password

    async def wait(self):
        if self.password:
            raise SessionPasswordNeededError(request=None)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(id=1)


class FakeLoginClient:
    def __init__(self, qr=None, phone="79001234567", password_ok=True):
        self._qr = qr or FakeQr()
        self._phone = phone
        self._password_ok = password_ok
        self.session = SimpleNamespace(save=lambda: SESSION)
        self.disconnected = False

    async def qr_login(self):
        return self._qr

    async def get_me(self):
        return SimpleNamespace(
            phone=self._phone, first_name="Q", last_name="R",
            username=None, id=1,
        )

    async def sign_in(self, password=None):
        if not self._password_ok:
            raise ValueError("PASSWORD_HASH_INVALID")
        return SimpleNamespace(id=1)

    async def disconnect(self):
        self.disconnected = True


@pytest.fixture
def gateway(monkeypatch):
    """Шлюз, помнящий ключи каждого вызова."""
    calls: dict = {"check": [], "start": []}

    async def send_code(phone, creds=None):
        calls.setdefault("send", []).append(creds)
        return SESSION, "hash-4567", {"via": "app", "next": "sms", "timeout": 60}

    async def resend_code(phone, session_string, phone_code_hash, creds=None):
        calls.setdefault("resend", []).append(creds)
        return SESSION, "resend-4567", {"via": "sms", "next": "call", "timeout": 60}

    async def sign_in_code(phone, code, session_string, phone_code_hash, creds=None):
        calls.setdefault("code", []).append(creds)
        return SIGNED

    async def sign_in_password(password, session_string, creds=None):
        calls.setdefault("password", []).append(creds)
        return SIGNED

    async def check_session(session_string, creds=None):
        calls["check"].append(creds)
        return True, "Тест Тестов", None

    async def start_account(account, session_string):
        calls["start"].append(int(account.id))
        return True

    async def refresh_rules():
        return None

    for name, func in (
        ("send_code", send_code), ("resend_code", resend_code),
        ("sign_in_code", sign_in_code), ("sign_in_password", sign_in_password),
        ("check_session", check_session), ("start_account", start_account),
        ("refresh_rules", refresh_rules),
    ):
        monkeypatch.setattr(manager, name, func)
    return calls


def _login_client(monkeypatch, client):
    async def create_login_client(creds=None):
        client.creds = creds
        return client

    monkeypatch.setattr(manager, "create_login_client", create_login_client)


def test_creds_pair_or_nothing():
    """Ключи: оба пустые — сервисные; мусор — ошибка ввода."""
    assert accounts_login.normalize_creds(None, None) is None
    assert accounts_login.normalize_creds("", "") is None
    creds = accounts_login.normalize_creds(API_ID, API_HASH)
    assert creds == LoginCreds(api_id=123456, api_hash=API_HASH)
    for bad_id, bad_hash in (
        (API_ID, ""), ("", API_HASH), ("abc", API_HASH),
        ("0", API_HASH), (API_ID, "zzz"), (API_ID, "zz" * 16),
    ):
        with pytest.raises(ValidationError):
            accounts_login.normalize_creds(bad_id, bad_hash)


async def test_custom_creds_ride_all_phone_steps(create_user, ready, gateway):
    """Свои ключи: send → pending → sign_in → check → строка аккаунта."""
    user_id = await create_user()
    step = await accounts_login.start(
        user_id, PHONE, api_id=API_ID, api_hash=API_HASH
    )
    assert step.stage == "code"
    assert gateway["send"] == [LoginCreds(123456, API_HASH)]

    async with session_scope() as session:
        row = await repo.get_pending_login(session, user_id)
        assert row is not None and row.api_id == 123456
        assert decrypt_session(row.api_hash_encrypted or "") == API_HASH

    done = await accounts_login.submit_code(user_id, "12345")
    assert done.done
    assert gateway["code"] == [LoginCreds(123456, API_HASH)]
    assert gateway["check"] == [LoginCreds(123456, API_HASH)]
    async with session_scope() as session:
        accounts = await repo.list_accounts(session, user_id)
        assert accounts[0].api_id == 123456
        assert decrypt_session(accounts[0].api_hash_encrypted or "") == API_HASH


async def test_service_creds_stay_empty(create_user, ready, gateway):
    """Без своих ключей везде None — поведение как раньше."""
    user_id = await create_user()
    await accounts_login.start(user_id, PHONE)
    await accounts_login.submit_code(user_id, "12345")
    assert gateway["send"] == [None] and gateway["check"] == [None]
    async with session_scope() as session:
        accounts = await repo.list_accounts(session, user_id)
        assert accounts[0].api_id is None


async def test_bad_custom_keys_blame_keys(create_user, ready, monkeypatch):
    """Чужие ключи отклоняются словами про ключи, а не про сервис."""
    async def send_code(phone, creds=None):
        raise ApiIdInvalidError(request=None)

    monkeypatch.setattr(manager, "send_code", send_code)
    user_id = await create_user()
    with pytest.raises(ValidationError, match="эти ключи"):
        await accounts_login.start(user_id, PHONE, api_id=API_ID, api_hash=API_HASH)


async def test_qr_scan_finishes_login(create_user, ready, gateway, monkeypatch):
    """QR: код картинкой → сканирование → аккаунт подключён."""
    user_id = await create_user()
    client = FakeLoginClient()
    _login_client(monkeypatch, client)

    begun = await accounts_login.qr_start(user_id)
    assert begun["url"] == "tg://login?token=abc"
    assert begun["image"].startswith("data:image/png;base64,")
    assert begun["expires_in"] == accounts_login.QR_TTL_SECONDS

    status = await accounts_login.qr_status(user_id)
    assert status == {"stage": "waiting"}

    await asyncio.wait_for(accounts_login._qr_sessions[user_id].task, timeout=5)
    done = await accounts_login.qr_status(user_id)
    assert isinstance(done, accounts_login.LoginStep) and done.done
    assert client.disconnected
    async with session_scope() as session:
        accounts = await repo.list_accounts(session, user_id)
        assert len(accounts) == 1 and accounts[0].phone == "+79001234567"


async def test_qr_with_2fa_asks_password(create_user, ready, gateway, monkeypatch):
    """QR со 2FA: после сканирования — шаг пароля, потом готово."""
    user_id = await create_user()
    _login_client(monkeypatch, FakeLoginClient(qr=FakeQr(password=True)))

    await accounts_login.qr_start(user_id)
    await asyncio.wait_for(accounts_login._qr_sessions[user_id].task, timeout=5)
    assert await accounts_login.qr_status(user_id) == {"stage": "password"}

    done = await accounts_login.qr_password(user_id, "secret")
    assert done.done


async def test_qr_wrong_password_keeps_session(create_user, ready, gateway, monkeypatch):
    """Неверный QR-пароль — ошибка, но сессия жива для повтора."""
    user_id = await create_user()
    _login_client(
        monkeypatch,
        FakeLoginClient(qr=FakeQr(password=True), password_ok=False),
    )
    await accounts_login.qr_start(user_id)
    await asyncio.wait_for(accounts_login._qr_sessions[user_id].task, timeout=5)
    with pytest.raises(ValidationError, match="не подошёл"):
        await accounts_login.qr_password(user_id, "nope")
    assert await accounts_login.qr_status(user_id) == {"stage": "password"}


async def test_qr_expiry_says_restart(create_user, ready, gateway, monkeypatch):
    """Протухший QR — честное «начните заново», соединение закрыто."""
    user_id = await create_user()
    client = FakeLoginClient(qr=FakeQr(error=TimeoutError()))
    _login_client(monkeypatch, client)

    await accounts_login.qr_start(user_id)
    await asyncio.wait_for(accounts_login._qr_sessions[user_id].task, timeout=5)
    with pytest.raises(ConflictError, match="устарел"):
        await accounts_login.qr_status(user_id)
    assert client.disconnected


async def test_qr_cancel_and_single_login(create_user, ready, gateway, monkeypatch):
    """Отмена закрывает QR; номерной вход гасит QR и наоборот."""
    user_id = await create_user()
    client = FakeLoginClient(qr=FakeQr(error=asyncio.CancelledError()))
    _login_client(monkeypatch, client)

    await accounts_login.qr_start(user_id)
    assert await accounts_login.qr_cancel(user_id) is True
    assert await accounts_login.qr_cancel(user_id) is False

    await accounts_login.qr_start(user_id)
    await accounts_login.start(user_id, PHONE)
    assert user_id not in accounts_login._qr_sessions
    with pytest.raises(ConflictError):
        await accounts_login.qr_status(user_id)

    await accounts_login.qr_start(user_id)
    async with session_scope() as session:
        assert await repo.get_pending_login(session, user_id) is None


# ─────────────────────────── бот: разговор про QR ─────────────────────────────
#
# Сервис умеет ждать сканирования, но боту нечего опрашивать: вместо таймера —
# кнопка «Я отсканировал — проверить». Проверяем разговор: выбор способа, код
# фото, проверка до и после скана, пароль после скана, протухший код.

from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402

from app.bot.handlers import accounts as bot_accounts  # noqa: E402
from app.bot.states import LoginStates  # noqa: E402


class FakeBotMessage:
    """Сообщение меню в объёме QR-разговора: правка текста и фото с кодом."""

    def __init__(self) -> None:
        self.photo = None
        self.document = None
        self.video = None
        self.animation = None
        self.edited: list[str] = []
        self.photos: list[dict] = []
        self.replies: list[dict] = []

    async def edit_text(self, text, reply_markup=None, **_kwargs):
        self.edited.append(text)
        return self

    async def edit_caption(self, caption, reply_markup=None, **_kwargs):
        self.edited.append(caption)
        return self

    async def answer_photo(self, photo, caption=None, reply_markup=None, **_kwargs):
        self.photos.append({"photo": photo, "caption": caption, "markup": reply_markup})
        return self

    async def answer(self, text, reply_markup=None, **_kwargs):
        self.replies.append({"text": text, "markup": reply_markup})
        return self


class FakeCallback:
    """Нажатие кнопки: автор, данные и сообщение, откуда жали."""

    def __init__(self, user_id: int, data: str, message=None) -> None:
        self.from_user = SimpleNamespace(id=user_id)
        self.data = data
        self.message = message if message is not None else FakeBotMessage()
        self.answers: list[str] = []

    async def answer(self, text=None, show_alert=None, **_kwargs):
        self.answers.append(text or "")


def _fsm(user_id: int) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )


async def test_bot_offers_login_choice(create_user, ready):
    """Без начатого входа бот спрашивает способ, а не номер сразу."""
    user_id = await create_user()
    state = _fsm(user_id)

    text, _markup = await bot_accounts._open_login(user_id, state)

    assert await state.get_state() == LoginStates.choice.state
    assert "По номеру" in text and "QR-коду" in text


async def test_bot_qr_shows_photo_with_check_button(create_user, ready, gateway, monkeypatch):
    """Выбор QR: код уходит фото с подпиской-инструкцией и кнопкой проверки."""
    user_id = await create_user()
    _login_client(monkeypatch, FakeLoginClient())
    state = _fsm(user_id)
    callback = FakeCallback(user_id, "acc:method:qr")

    await bot_accounts.choose_qr_login(callback, state)

    assert await state.get_state() == LoginStates.qr.state
    assert len(callback.message.photos) == 1
    assert "Наведите камеру" in callback.message.photos[0]["caption"]
    assert "Связать устройство" in callback.message.photos[0]["caption"]
    assert callback.message.photos[0]["photo"].filename == "qr.png"


async def test_bot_qr_check_before_scan_keeps_waiting(create_user, ready, gateway, monkeypatch):
    """Проверку до скана бот переживает: остаёмся и жмём снова."""
    user_id = await create_user()
    _login_client(monkeypatch, FakeLoginClient(qr=FakeQr(error=asyncio.CancelledError())))
    state = _fsm(user_id)
    await bot_accounts.choose_qr_login(FakeCallback(user_id, "acc:method:qr"), state)
    # Сканирования нет: вотчер висит, соединение живо.
    status = await accounts_login.qr_status(user_id)
    assert status == {"stage": "waiting"}

    check = FakeCallback(user_id, "acc:qr:check")
    await bot_accounts.check_qr_login(check, state)

    assert await state.get_state() == LoginStates.qr.state
    assert "не вижу сканирования" in check.answers[-1]


async def test_bot_qr_check_after_scan_finishes(create_user, ready, gateway, monkeypatch):
    """Скан + проверка: аккаунт подключён, бот поздравляет с номером."""
    user_id = await create_user()
    _login_client(monkeypatch, FakeLoginClient())
    state = _fsm(user_id)
    await bot_accounts.choose_qr_login(FakeCallback(user_id, "acc:method:qr"), state)
    await asyncio.wait_for(accounts_login._qr_sessions[user_id].task, timeout=5)

    check = FakeCallback(user_id, "acc:qr:check")
    await bot_accounts.check_qr_login(check, state)

    assert await state.get_state() is None
    assert PHONE in check.message.edited[-1]
    assert "подключён" in check.message.edited[-1]


async def test_bot_qr_with_2fa_asks_password(create_user, ready, gateway, monkeypatch):
    """QR со 2FA: после скана бот просит облачный пароль и принимает его."""
    user_id = await create_user()
    _login_client(monkeypatch, FakeLoginClient(qr=FakeQr(password=True)))
    state = _fsm(user_id)
    await bot_accounts.choose_qr_login(FakeCallback(user_id, "acc:method:qr"), state)
    await asyncio.wait_for(accounts_login._qr_sessions[user_id].task, timeout=5)

    check = FakeCallback(user_id, "acc:qr:check")
    await bot_accounts.check_qr_login(check, state)
    assert await state.get_state() == LoginStates.qr_password.state
    assert "отсканирован" in check.message.edited[-1]

    answer_box = FakeBotMessage()
    password_message = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text="secret",
        delete=lambda: asyncio.sleep(0),
        answer=answer_box.answer,
    )
    await bot_accounts.process_qr_password(password_message, state)

    assert await state.get_state() is None
    assert "подключён" in answer_box.edited[-1]


async def test_bot_qr_expired_says_restart(create_user, ready, gateway, monkeypatch):
    """Протухший код: проверка честно отправляет за новым, а не виснет."""
    user_id = await create_user()
    _login_client(monkeypatch, FakeLoginClient(qr=FakeQr(error=TimeoutError())))
    state = _fsm(user_id)
    await bot_accounts.choose_qr_login(FakeCallback(user_id, "acc:method:qr"), state)
    await asyncio.wait_for(accounts_login._qr_sessions[user_id].task, timeout=5)

    check = FakeCallback(user_id, "acc:qr:check")
    await bot_accounts.check_qr_login(check, state)

    assert await state.get_state() is None
    assert "заново" in check.message.edited[-1]
