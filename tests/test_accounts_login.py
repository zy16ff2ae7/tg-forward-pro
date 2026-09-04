"""Подключение аккаунта: общий сценарий и его HTTP-обёртка в кабинете.

Вход по номеру — самое хрупкое место сервиса: три шага, живой внешний API и
состояние, которое обязано переживать перезапуск. Сценарий
(``app/accounts_login.py``) и ручки кабинета проверяются в одном файле
намеренно: обёртка над сценарием тонкая до прозрачности, а фальшивый шлюз
нужен обеим половинам.

В Telegram здесь не ходит никто: ``manager`` подменяется ``FakeGateway``.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from app import accounts_login
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.errors import ConflictError, FeatureUnavailable, NotFoundError, ValidationError
from app.security import decrypt_session
from app.telegram_client.manager import manager
from app.timeutil import utcnow
from tests.helpers import TEST_USER_ID

PHONE = "+79001234567"
OTHER_PHONE = "+79007654321"
SESSION = "1AaBbCc-fake-session"
SIGNED = SESSION + "-signed"
AFTER_2FA = SESSION + "-2fa"

class FakeGateway:
    """MTProto-шлюз без Telegram: отвечает тем, что попросил тест.

    Заодно ведёт журнал вызовов — по нему видно не только результат, но и то,
    что сервис не пошёл в Telegram лишний раз (запросы кода на номер лимитированы).
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.codes: list[dict] = []
        self.passwords: list[str] = []
        self.checked: list[str] = []
        self.started: list[int] = []
        self.stopped: list[int] = []
        self.refreshed = 0
        self.send_error: Exception | None = None
        self.code_error: Exception | None = None
        self.password_error: Exception | None = None
        self.check_result: tuple[bool, str | None, str | None] = (True, "Тест Тестов", None)
        self.start_ok = True

    async def send_code(self, phone: str) -> tuple[str, str]:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(phone)
        return SESSION, "hash-" + phone[-4:]

    async def sign_in_code(
        self, phone: str, code: str, session_string: str, phone_code_hash: str
    ) -> str:
        self.codes.append({"phone": phone, "code": code, "hash": phone_code_hash})
        if self.code_error is not None:
            raise self.code_error
        return SIGNED

    async def sign_in_password(self, password: str, session_string: str) -> str:
        self.passwords.append(password)
        if self.password_error is not None:
            raise self.password_error
        return AFTER_2FA

    async def check_session(self, session_string: str) -> tuple[bool, str | None, str | None]:
        self.checked.append(session_string)
        return self.check_result

    async def start_account(self, account, session_string: str) -> bool:
        self.started.append(int(account.id))
        return self.start_ok

    async def stop_account(self, account_id: int) -> None:
        self.stopped.append(int(account_id))

    async def refresh_rules(self) -> None:
        self.refreshed += 1

    def is_online(self, account_id: int) -> bool:
        return account_id in self.started


@pytest.fixture
def gateway(monkeypatch) -> FakeGateway:
    """Живой шлюз и включённый вход — без этого сценарий отказывает на первом шаге.

    Готовность шлюза задаётся явно: она вычисляется из ключей MTProto, а те
    приходят из ``.env`` разработчика. Без подмены тест зависел бы от того, чей
    компьютер его запускает.
    """
    fake = FakeGateway()
    monkeypatch.setattr(type(settings), "mtproto_ready", property(lambda self: True))

    for name in (
        "send_code",
        "sign_in_code",
        "sign_in_password",
        "check_session",
        "start_account",
        "stop_account",
        "refresh_rules",
        "is_online",
    ):
        monkeypatch.setattr(manager, name, getattr(fake, name))
    return fake


@pytest.fixture
async def user(create_user) -> int:
    """Аккаунты ссылаются на users.id внешним ключом — пользователь нужен заранее."""
    return await create_user(id=TEST_USER_ID)


async def _pending(user_id: int):
    async with SessionLocal() as session:
        return await repo.get_pending_login(session, user_id)


async def _rewind_code(user_id: int, seconds: int) -> None:
    """Сдвигает время отправки кода в прошлое: иначе пауза не истечёт."""
    async with session_scope() as session:
        row = await repo.get_pending_login(session, user_id)
        row.created_at = utcnow() - timedelta(seconds=seconds)


async def _accounts(user_id: int) -> list:
    async with SessionLocal() as session:
        return list(await repo.list_accounts(session, user_id))

# ───────────────────────────── номер и доступность ────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("+79001234567", "+79001234567"),
        ("79001234567", "+79001234567"),          # «+» люди не набирают
        ("+7 900 123-45-67", "+79001234567"),     # пробелы и дефисы — не ошибка
        ("+7 (900) 123 45 67", "+79001234567"),
    ],
)
def test_normalize_phone_accepts_human_input(raw, expected):
    assert accounts_login.normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "телефон", "+7900", "+7900123456789012", "8-900-ТЕЛЕ"])
def test_normalize_phone_rejects_garbage(raw):
    """Мусор — отказ, а не молчаливая правка: иначе тратим лимит запросов номера."""
    with pytest.raises(ValidationError):
        accounts_login.normalize_phone(raw)


async def test_login_without_mtproto_is_feature_unavailable(monkeypatch):
    monkeypatch.setattr(type(settings), "mtproto_ready", property(lambda self: False))

    with pytest.raises(FeatureUnavailable) as info:
        await accounts_login.start(TEST_USER_ID, PHONE)

    assert info.value.feature == "account_login"
    # status у исключения — HTTP-код; публичный статус функции лежит отдельно.
    assert info.value.status == 503
    assert info.value.feature_status == "setup_required"


# ────────────────────────────── шаг 1: номер и код ────────────────────────────


async def test_start_sends_code_and_remembers_step(gateway, user):
    step = await accounts_login.start(user, "+7 900 123-45-67")

    assert (step.stage, step.phone) == ("code", PHONE)
    assert step.attempts_left == accounts_login.MAX_CODE_ATTEMPTS
    assert gateway.sent == [PHONE]

    row = await _pending(user)
    assert (row.phone, row.stage, row.attempts) == (PHONE, "waiting_code", 0)
    # Временная сессия — тоже доступ к аккаунту, в БД она только шифром.
    assert row.session_encrypted != SESSION
    assert decrypt_session(row.session_encrypted) == SESSION

async def test_repeat_start_on_same_phone_waits_out_the_cooldown(gateway, user):
    """Двойное нажатие не должно превращаться во второй запрос кода.

    Telegram отвечает на такие запросы флудом на сам номер, поэтому дешевле
    отказать сразу и попросить ввести уже отправленный код.
    """
    await accounts_login.start(user, PHONE)

    with pytest.raises(ConflictError) as info:
        await accounts_login.start(user, PHONE)

    assert "уже отправлен" in info.value.message
    assert gateway.sent == [PHONE]  # в Telegram не пошли


async def test_start_sends_new_code_after_cooldown(gateway, user):
    await accounts_login.start(user, PHONE)
    await _rewind_code(user, accounts_login.RESEND_COOLDOWN_SECONDS + 5)

    step = await accounts_login.start(user, PHONE)

    assert step.stage == "code"
    assert gateway.sent == [PHONE, PHONE]


async def test_start_on_another_phone_replaces_pending(gateway, user):
    """Человек ошибся номером — пауза здесь не при чём, начинаем заново."""
    await accounts_login.start(user, PHONE)

    step = await accounts_login.start(user, OTHER_PHONE)

    assert step.phone == OTHER_PHONE
    row = await _pending(user)
    assert row.phone == OTHER_PHONE
    assert gateway.sent == [PHONE, OTHER_PHONE]


@pytest.mark.parametrize(
    "error,expected",
    [
        (PhoneNumberInvalidError(request=None), "Telegram не знает такой номер"),
        (PhoneNumberBannedError(request=None), "заблокирован в Telegram"),
    ],
)
async def test_start_explains_telegram_refusal(gateway, user, error, expected):
    gateway.send_error = error

    with pytest.raises(ValidationError) as info:
        await accounts_login.start(user, PHONE)

    assert expected in info.value.message
    assert await _pending(user) is None  # незавершённого входа не осталось

# ─────────────────────────────── шаг 2: код ───────────────────────────────────


async def test_code_without_pending_login_is_conflict(gateway, user):
    with pytest.raises(ConflictError) as info:
        await accounts_login.submit_code(user, "11111")

    assert "Незавершённого входа нет" in info.value.message


async def test_code_keeps_only_digits(gateway, user):
    """«1 23-45» — тот же код: люди копируют его из сообщения вместе с пробелами."""
    await accounts_login.start(user, PHONE)

    await accounts_login.submit_code(user, "1 23-45")

    assert gateway.codes[0]["code"] == "12345"


async def test_code_without_digits_is_validation_error(gateway, user):
    await accounts_login.start(user, PHONE)

    with pytest.raises(ValidationError):
        await accounts_login.submit_code(user, "код")

    assert gateway.codes == []


async def test_wrong_code_forgives_a_typo(gateway, user):
    """Опечатка в цифре не должна убивать вход: код ещё действует."""
    await accounts_login.start(user, PHONE)
    gateway.code_error = PhoneCodeInvalidError(request=None)

    with pytest.raises(ValidationError) as info:
        await accounts_login.submit_code(user, "11111")

    assert "Осталось попыток: 4" in info.value.message
    row = await _pending(user)
    assert row is not None and row.attempts == 1
    assert row.stage == "waiting_code"


async def test_attempts_run_out_and_reset_the_login(gateway, user):
    await accounts_login.start(user, PHONE)
    gateway.code_error = PhoneCodeInvalidError(request=None)

    for left in (4, 3, 2, 1):
        with pytest.raises(ValidationError) as info:
            await accounts_login.submit_code(user, "11111")
        assert f"Осталось попыток: {left}" in info.value.message

    # Пятая — уже похоже на перебор: вход сбрасывается, нужен новый код.
    with pytest.raises(ConflictError) as info:
        await accounts_login.submit_code(user, "11111")

    assert "слишком много раз" in info.value.message
    assert await _pending(user) is None


async def test_expired_code_resets_the_login(gateway, user):
    await accounts_login.start(user, PHONE)
    gateway.code_error = PhoneCodeExpiredError(request=None)

    with pytest.raises(ConflictError) as info:
        await accounts_login.submit_code(user, "11111")

    assert "устарел" in info.value.message
    assert await _pending(user) is None


async def test_two_factor_moves_step_to_password(gateway, user):
    await accounts_login.start(user, PHONE)
    gateway.code_error = SessionPasswordNeededError(request=None)

    step = await accounts_login.submit_code(user, "11111")

    assert (step.stage, step.phone) == ("password", PHONE)
    row = await _pending(user)
    assert row.stage == "waiting_password"
    assert row.attempts == 0  # счётчик кода к паролю отношения не имеет


async def test_code_without_two_factor_connects_account(gateway, user):
    await accounts_login.start(user, PHONE)

    step = await accounts_login.submit_code(user, "11111")

    assert step.stage == "done"
    assert step.name == "Тест Тестов"
    assert await _pending(user) is None

    accounts = await _accounts(user)
    assert len(accounts) == 1
    assert accounts[0].phone == PHONE
    assert decrypt_session(accounts[0].session_encrypted) == SIGNED
    assert step.account_id == accounts[0].id
    # Клиент поднят сразу, правила перечитаны — иначе задачи заработают
    # только после следующего перезапуска сервиса.
    assert gateway.started == [accounts[0].id]
    assert gateway.refreshed == 1

# ────────────────────────────── шаг 3: пароль 2FA ─────────────────────────────


async def _reach_password(user_id: int, gateway: FakeGateway) -> None:
    """Доводит вход до шага пароля и возвращает шлюз в обычный режим."""
    await accounts_login.start(user_id, PHONE)
    gateway.code_error = SessionPasswordNeededError(request=None)
    await accounts_login.submit_code(user_id, "11111")
    gateway.code_error = None


async def test_password_connects_account(gateway, user):
    await _reach_password(user, gateway)

    step = await accounts_login.submit_password(user, "секрет")

    assert step.stage == "done"
    assert gateway.passwords == ["секрет"]
    accounts = await _accounts(user)
    assert decrypt_session(accounts[0].session_encrypted) == AFTER_2FA
    assert await _pending(user) is None


async def test_wrong_password_keeps_the_login_alive(gateway, user):
    """Пароль набирают вслепую — с первой попытки ошибаются часто."""
    await _reach_password(user, gateway)
    gateway.password_error = RuntimeError("PasswordHashInvalidError")

    with pytest.raises(ValidationError) as info:
        await accounts_login.submit_password(user, "мимо")

    assert "Попробуйте снова" in info.value.message
    row = await _pending(user)
    assert row is not None and row.stage == "waiting_password"

    gateway.password_error = None
    assert (await accounts_login.submit_password(user, "секрет")).stage == "done"


async def test_empty_password_does_not_reach_telegram(gateway, user):
    await _reach_password(user, gateway)

    with pytest.raises(ValidationError):
        await accounts_login.submit_password(user, "   ")

    assert gateway.passwords == []


async def test_password_on_code_step_is_conflict(gateway, user):
    await accounts_login.start(user, PHONE)

    with pytest.raises(ConflictError) as info:
        await accounts_login.submit_password(user, "секрет")

    assert "код из Telegram" in info.value.message

# ──────────────────────── завершение входа и обслуживание ─────────────────────


async def test_relogin_replaces_session_instead_of_second_account(gateway, user):
    """Тот же номер второй раз — замена сессии, а не второй аккаунт.

    Две строки с одним телефоном означали бы два клиента на одной сессии
    Telegram, то есть AuthKeyDuplicated и потерю доступа к аккаунту вообще.
    """
    await accounts_login.start(user, PHONE)
    first = await accounts_login.submit_code(user, "11111")

    async with session_scope() as session:
        account = await repo.get_account(session, first.account_id, user)
        account.is_active = False
        account.last_error = "Не запустился"

    # Успешный вход убрал pending, поэтому пауза на повторный код не мешает.
    await accounts_login.start(user, PHONE)
    second = await accounts_login.submit_code(user, "22222")

    assert second.account_id == first.account_id
    accounts = await _accounts(user)
    assert len(accounts) == 1
    assert accounts[0].is_active is True
    assert accounts[0].last_error is None


async def test_unconfirmed_session_does_not_create_account(gateway, user):
    gateway.check_result = (False, None, "AuthKeyUnregisteredError")
    await accounts_login.start(user, PHONE)

    with pytest.raises(ConflictError) as info:
        await accounts_login.submit_code(user, "11111")

    assert "не подтверждён" in info.value.message
    assert await _accounts(user) == []
    assert await _pending(user) is None


async def test_broken_temporary_session_resets_the_login(gateway, user):
    """Сменился SECRET_KEY — расшифровать нечем, дальше идти бессмысленно."""
    await accounts_login.start(user, PHONE)
    async with session_scope() as session:
        row = await repo.get_pending_login(session, user)
        row.session_encrypted = "не-шифр"

    with pytest.raises(ConflictError) as info:
        await accounts_login.submit_code(user, "11111")

    assert "восстановить временную сессию" in info.value.message
    assert await _pending(user) is None

async def test_pending_view_shows_step_and_attempts(gateway, user):
    assert await accounts_login.pending(user) is None

    await accounts_login.start(user, PHONE)
    gateway.code_error = PhoneCodeInvalidError(request=None)
    with pytest.raises(ValidationError):
        await accounts_login.submit_code(user, "11111")

    view = await accounts_login.pending(user)
    assert view.as_dict() == {
        "exists": True,
        "phone": PHONE,
        "stage": "code",
        "attempts_left": accounts_login.MAX_CODE_ATTEMPTS - 1,
    }


async def test_cancel_reports_whether_there_was_anything(gateway, user):
    assert await accounts_login.cancel(user) is False

    await accounts_login.start(user, PHONE)
    assert await accounts_login.cancel(user) is True
    assert await _pending(user) is None


async def test_disconnect_removes_account_and_stops_client(gateway, user):
    await accounts_login.start(user, PHONE)
    step = await accounts_login.submit_code(user, "11111")

    phone = await accounts_login.disconnect(user, step.account_id)

    assert phone == PHONE
    assert await _accounts(user) == []
    assert gateway.stopped == [step.account_id]
    assert gateway.refreshed == 2  # подключение и отключение


async def test_disconnect_of_foreign_account_is_not_found(gateway, user, create_user, create_account):
    stranger = await create_user()
    foreign_id = await create_account(stranger, phone="+79990001122")

    with pytest.raises(NotFoundError):
        await accounts_login.disconnect(user, foreign_id)

    with pytest.raises(NotFoundError):
        await accounts_login.disconnect(user, 424242)

    assert len(await _accounts(stranger)) == 1  # чужой аккаунт на месте

# ─────────────────────────── ручки кабинета (HTTP) ────────────────────────────
#
# Кабинет проходит вход целиком сам: раньше кнопка «Подключить аккаунт» умела
# только открыть чат с ботом, и человек уходил из мини-аппа на середине.

START = "/api/accounts/login/start"
CODE = "/api/accounts/login/code"
PASSWORD = "/api/accounts/login/password"
CANCEL = "/api/accounts/login/cancel"

STEP_ENDPOINTS = [(START, {"phone": PHONE}), (CODE, {"code": "11111"}), (PASSWORD, {"password": "x"})]


@pytest.mark.parametrize("path,payload", STEP_ENDPOINTS + [(CANCEL, {})])
async def test_login_endpoints_require_auth(client, path, payload):
    assert (await client.post(path, json=payload)).status == 401


async def test_delete_account_requires_auth(client):
    assert (await client.delete("/api/accounts/1")).status == 401


@pytest.mark.parametrize("path,payload", STEP_ENDPOINTS)
async def test_login_endpoints_are_503_without_mtproto(
    client, auth_headers, monkeypatch, path, payload
):
    """Отказ с маркером функции: кабинет по нему показывает объяснение, а не ошибку."""
    monkeypatch.setattr(type(settings), "mtproto_ready", property(lambda self: False))

    response = await client.post(path, json=payload, headers=auth_headers)

    assert response.status == 503
    body = await response.json()
    assert body["feature"] == "account_login"
    assert body["status"] == "setup_required"


async def test_login_start_rejects_bad_phone(client, auth_headers, gateway, user):
    response = await client.post(START, json={"phone": "телефон"}, headers=auth_headers)

    assert response.status == 400
    assert "+79001234567" in (await response.json())["error"]
    assert gateway.sent == []


async def test_login_start_requires_json_body(client, auth_headers, gateway, user):
    response = await client.post(START, data="не json", headers=auth_headers)

    assert response.status == 400

async def _accounts_payload(client, auth_headers) -> dict:
    response = await client.get("/api/accounts", headers=auth_headers)
    assert response.status == 200
    return await response.json()


async def test_cabinet_walks_the_whole_login(client, auth_headers, gateway, user):
    """Номер → код → пароль 2FA, не выходя из кабинета."""
    started = await client.post(START, json={"phone": "+7 900 123-45-67"}, headers=auth_headers)
    assert started.status == 200
    assert (await started.json())["stage"] == "code"

    payload = await _accounts_payload(client, auth_headers)
    assert payload["pending_login"] == {
        "exists": True,
        "phone": PHONE,
        "stage": "waiting_code",
        # Короткое имя шага — то же, что отдают ручки входа: кабинет не должен
        # знать про «waiting_*» из БД.
        "step": "code",
        "attempts_left": accounts_login.MAX_CODE_ATTEMPTS,
    }

    gateway.code_error = SessionPasswordNeededError(request=None)
    coded = await client.post(CODE, json={"code": "11111"}, headers=auth_headers)
    assert coded.status == 200
    assert (await coded.json())["stage"] == "password"
    assert (await _accounts_payload(client, auth_headers))["pending_login"]["step"] == "password"

    gateway.code_error = None
    done = await client.post(PASSWORD, json={"password": "секрет"}, headers=auth_headers)
    assert done.status == 200
    body = await done.json()
    assert body["stage"] == "done"
    assert body["name"] == "Тест Тестов"

    payload = await _accounts_payload(client, auth_headers)
    assert [item["phone"] for item in payload["accounts"]] == [PHONE]
    assert payload["accounts"][0]["online"] is True
    assert payload["pending_login"]["exists"] is False


async def test_wrong_code_is_400_and_keeps_the_step(client, auth_headers, gateway, user):
    await client.post(START, json={"phone": PHONE}, headers=auth_headers)
    gateway.code_error = PhoneCodeInvalidError(request=None)

    response = await client.post(CODE, json={"code": "00000"}, headers=auth_headers)

    assert response.status == 400
    body = await response.json()
    assert "Осталось попыток: 4" in body["error"]
    # Число в теле отказа, а не только в тексте: подсказка под полем в кабинете
    # обновляется из него, иначе она осталась бы с прежним «Осталось попыток: 5».
    assert body["attempts_left"] == 4
    payload = await _accounts_payload(client, auth_headers)
    assert payload["pending_login"]["attempts_left"] == 4
    assert payload["pending_login"]["step"] == "code"


async def test_exhausted_code_attempts_are_409(client, auth_headers, gateway, user):
    """409 — сигнал кабинету, что шторку надо вернуть к вводу номера."""
    await client.post(START, json={"phone": PHONE}, headers=auth_headers)
    gateway.code_error = PhoneCodeInvalidError(request=None)

    for _ in range(accounts_login.MAX_CODE_ATTEMPTS - 1):
        assert (await client.post(CODE, json={"code": "00000"}, headers=auth_headers)).status == 400

    response = await client.post(CODE, json={"code": "00000"}, headers=auth_headers)

    assert response.status == 409
    assert (await _accounts_payload(client, auth_headers))["pending_login"]["exists"] is False


async def test_cancel_endpoint_reports_whether_it_dropped_anything(
    client, auth_headers, gateway, user
):
    await client.post(START, json={"phone": PHONE}, headers=auth_headers)

    first = await client.post(CANCEL, headers=auth_headers)
    second = await client.post(CANCEL, headers=auth_headers)

    assert await first.json() == {"ok": True, "dropped": True}
    assert (await second.json())["dropped"] is False


async def test_delete_account_endpoint_removes_session(client, auth_headers, gateway, user):
    await client.post(START, json={"phone": PHONE}, headers=auth_headers)
    account_id = (await (await client.post(CODE, json={"code": "11111"}, headers=auth_headers)).json())[
        "account_id"
    ]

    response = await client.delete(f"/api/accounts/{account_id}", headers=auth_headers)

    assert response.status == 200
    assert (await response.json())["phone"] == PHONE
    assert (await _accounts_payload(client, auth_headers))["accounts"] == []
    assert gateway.stopped == [account_id]
    # Повторное удаление и мусор в пути — 404, а не 500.
    assert (await client.delete(f"/api/accounts/{account_id}", headers=auth_headers)).status == 404
    assert (await client.delete("/api/accounts/мусор", headers=auth_headers)).status == 404
