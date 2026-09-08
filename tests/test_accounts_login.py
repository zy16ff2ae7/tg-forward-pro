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
    ApiIdPublishedFloodError,
    FloodWaitError,
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
from app.db.models import PhoneCodeSend
from app.errors import ConflictError, FeatureUnavailable, NotFoundError, ValidationError
from app.security import decrypt_session
from app.telegram_client.manager import SESSION_REVOKED, manager
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
        self.creds_seen: list = []
        self.resent: list[dict] = []
        self.codes: list[dict] = []
        self.passwords: list[str] = []
        self.checked: list[str] = []
        self.started: list[int] = []
        self.stopped: list[int] = []
        self.refreshed = 0
        self.send_error: Exception | None = None
        self.resend_error: Exception | None = None
        self.code_error: Exception | None = None
        self.password_error: Exception | None = None
        self.check_result: tuple[bool, str | None, str | None] = (True, "Тест Тестов", None)
        self.start_ok = True
        self.retried: list[int] = []
        # Что ответит повтор по кнопке «Попробовать снова»: вышел на связь или нет.
        self.retry_result: tuple[bool, str | None] = (True, None)
        # Куда «Telegram» положил код: журнал тянет это из ответа шлюза.
        self.delivery: dict = {"via": "app", "next": "sms", "timeout": 60}

    async def send_code(self, phone: str, creds=None) -> tuple[str, str, dict]:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append(phone)
        self.creds_seen.append(creds)
        return SESSION, "hash-" + phone[-4:], dict(self.delivery)

    async def resend_code(
        self, phone: str, session_string: str, phone_code_hash: str, creds=None
    ) -> tuple[str, str, dict]:
        if self.resend_error is not None:
            raise self.resend_error
        self.resent.append({"phone": phone, "hash": phone_code_hash})
        return SESSION, "resend-" + phone[-4:], dict(self.delivery)

    async def sign_in_code(
        self, phone: str, code: str, session_string: str, phone_code_hash: str,
        creds=None,
    ) -> str:
        self.codes.append({"phone": phone, "code": code, "hash": phone_code_hash})
        if self.code_error is not None:
            raise self.code_error
        return SIGNED

    async def sign_in_password(
        self, password: str, session_string: str, creds=None, fingerprint_seed=None
    ) -> str:
        self.passwords.append(password)
        if self.password_error is not None:
            raise self.password_error
        return AFTER_2FA

    async def check_session(self, session_string: str, creds=None) -> tuple[bool, str | None, str | None]:
        self.checked.append(session_string)
        return self.check_result

    async def start_account(self, account, session_string: str) -> bool:
        self.started.append(int(account.id))
        return self.start_ok

    async def retry_account(self, account_id: int) -> tuple[bool, str | None]:
        self.retried.append(int(account_id))
        if self.retry_result[0]:
            self.started.append(int(account_id))
        return self.retry_result

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
        "resend_code",
        "sign_in_code",
        "sign_in_password",
        "check_session",
        "start_account",
        "retry_account",
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


async def _rewind_code(user_id: int, seconds: int, phone: str = PHONE) -> None:
    """Сдвигает время отправки кода в прошлое: иначе пауза не истечёт.

    Точек отсчёта две — метка номера и время начатого входа, — а сервис берёт
    самую свежую. Значит, сдвигать надо обе; какой-то из них может и не быть
    (после удачного входа pending уже удалён). Подробнее о самой паузе —
    ``tests/test_login_code_pause.py``.
    """
    async with session_scope() as session:
        past = utcnow() - timedelta(seconds=seconds)
        mark = await session.get(PhoneCodeSend, phone)
        if mark is not None:
            mark.sent_at = past
        row = await repo.get_pending_login(session, user_id)
        if row is not None:
            row.created_at = past


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


async def test_start_reports_delivery(gateway, user):
    """Шаг кода говорит, куда Telegram положил код: иначе «не пришло» гадается."""
    gateway.delivery = {"via": "sms", "next": "call", "timeout": 120}

    step = await accounts_login.start(user, PHONE)

    assert step.delivery == {"via": "sms", "next": "call", "timeout": 120}
    assert step.as_dict()["delivery"] == step.delivery


# ─────────────────── повтор кода («Прислать ещё раз») ────────────────────────


async def test_resend_uses_resend_not_new_send(gateway, user):
    """Повтор продолжает ту же попытку: в Telegram — ResendCode, а не новый код."""
    await accounts_login.start(user, PHONE)

    step = await accounts_login.start(user, PHONE, resend=True)

    assert step.stage == "code"
    assert gateway.sent == [PHONE]  # нового запроса кода не было
    assert [item["phone"] for item in gateway.resent] == [PHONE]
    assert gateway.resent[0]["hash"] == "hash-4567"  # хэш первой попытки
    row = await _pending(user)
    assert row.phone_code_hash == "resend-4567"  # дальше входим по новому хэшу
    assert row.attempts == 0  # код новый — счётчик опечаток сброшен


async def test_resend_without_pending_is_plain_start(gateway, user):
    """Повторять нечего — вырождается в обычный новый запрос."""
    step = await accounts_login.start(user, PHONE, resend=True)

    assert step.stage == "code"
    assert gateway.sent == [PHONE]
    assert gateway.resent == []


async def test_resend_on_another_phone_is_plain_start(gateway, user):
    await accounts_login.start(user, PHONE)

    step = await accounts_login.start(user, OTHER_PHONE, resend=True)

    assert step.phone == OTHER_PHONE
    assert gateway.sent == [PHONE, OTHER_PHONE]
    assert gateway.resent == []


async def test_resend_reports_flood_wait_and_stays_on_code(gateway, user):
    """Повтор ещё недоступен — называем срок, вход не рушим."""
    await accounts_login.start(user, PHONE)
    gateway.resend_error = FloodWaitError(request=None)
    gateway.resend_error.seconds = 120

    with pytest.raises(ConflictError) as info:
        await accounts_login.start(user, PHONE, resend=True)

    assert "2 мин" in info.value.message
    assert info.value.details["stage"] == "code"  # кабинет и бот остаются на коде
    row = await _pending(user)
    assert row is not None and row.stage == "waiting_code"


async def test_resend_after_expiry_restarts_login(gateway, user):
    """Попытка целиком протухла — повторять нечего, нужен новый код."""
    await accounts_login.start(user, PHONE)
    gateway.resend_error = PhoneCodeExpiredError(request=None)

    with pytest.raises(ConflictError) as info:
        await accounts_login.start(user, PHONE, resend=True)

    assert "устарел" in info.value.message
    assert await _pending(user) is None


async def test_resend_unavailable_explains_no_sms(gateway, user):
    """Telegram не даёт другого способа доставки — говорим, где код, вход жив."""
    from telethon.errors import SendCodeUnavailableError

    await accounts_login.start(user, PHONE)
    gateway.resend_error = SendCodeUnavailableError(request=None)

    with pytest.raises(ConflictError) as info:
        await accounts_login.start(user, PHONE, resend=True)

    assert "только в приложение" in info.value.message
    assert info.value.details["stage"] == "code"
    row = await _pending(user)
    assert row is not None and row.stage == "waiting_code"


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


async def test_start_blames_service_keys_not_the_phone(gateway, user):
    """Опубликованные api_id/api_hash — беда сервиса, а не номера.

    Текст про «проверьте номер» здесь врал бы: человек сменит номер, получит тот
    же отказ и уйдёт. Отвечаем как о недоступной возможности (кабинет на 503
    закрывает шторку входа) и подсказываем владельцу, что делать.
    """
    gateway.send_error = ApiIdPublishedFloodError(request=None)

    with pytest.raises(FeatureUnavailable) as info:
        await accounts_login.start(user, PHONE)

    assert "my.telegram.org" in info.value.message
    assert info.value.feature_status == "api_keys_public"
    assert gateway.sent == []
    assert await _pending(user) is None


async def test_start_reports_exact_flood_wait(gateway, user):
    """Срок ожидания называем: «попробуйте позже» превращается в долбёж кнопки."""
    gateway.send_error = FloodWaitError(request=None)
    gateway.send_error.seconds = 300

    with pytest.raises(ConflictError) as info:
        await accounts_login.start(user, PHONE)

    assert "5 мин" in info.value.message
    assert await _pending(user) is None

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

    # Пауза перед новым кодом висит на номере и удачный вход её не снимает:
    # запрос кода — он и есть то, что лимитирует Telegram. Ждём её и входим снова.
    await _rewind_code(user, accounts_login.RESEND_COOLDOWN_SECONDS + 5)
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


# ───────────────────── «Попробовать снова» для аккаунта ───────────────────────
#
# Аккаунт мог не выйти на связь из-за сети или молчания Telegram. Сервис
# вернётся к нему сам, но человеку, который смотрит на «офлайн», ждать незачем.


async def test_retry_reports_that_the_account_is_back(gateway, user, create_account):
    account_id = await create_account(user, phone=PHONE)

    result = await accounts_login.retry(user, account_id)

    assert result == {"phone": PHONE, "online": True, "error": None}
    assert gateway.retried == [account_id]
    # Правила перечитываем: пока аккаунт лежал, его задачи никто не обслуживал.
    assert gateway.refreshed == 1


async def test_retry_passes_on_the_reason(gateway, user, create_account):
    """Не вышло — причина уходит в кабинет как есть, без «попробуйте позже»."""
    account_id = await create_account(user, phone=PHONE)
    gateway.retry_result = (False, "Аккаунт вышел из Telegram — подключите номер заново")

    result = await accounts_login.retry(user, account_id)

    assert result["online"] is False
    assert result["error"] == "Аккаунт вышел из Telegram — подключите номер заново"
    assert gateway.refreshed == 0, "поднимать нечего — правила перечитывать незачем"


async def test_retry_of_a_foreign_account_is_not_found(gateway, user, create_user, create_account):
    """Чужой аккаунт нельзя даже подёргать: id в пути ничего не доказывает."""
    stranger = await create_user()
    foreign_id = await create_account(stranger, phone="+79990001122")

    with pytest.raises(NotFoundError):
        await accounts_login.retry(user, foreign_id)

    assert gateway.retried == []

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


async def test_login_resend_endpoint_repeats_code(client, auth_headers, gateway, user):
    """Кабинет: «Прислать ещё раз» идёт повтором, пауза в минуту его не держит."""
    started = await client.post(START, json={"phone": PHONE}, headers=auth_headers)
    assert started.status == 200
    assert (await started.json())["delivery"]["via"] == "app"

    response = await client.post(
        START, json={"phone": PHONE, "resend": True}, headers=auth_headers
    )

    assert response.status == 200
    body = await response.json()
    assert body["stage"] == "code"
    assert gateway.sent == [PHONE]
    assert len(gateway.resent) == 1


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


async def test_retry_endpoint_brings_the_account_back(
    client, auth_headers, gateway, user, create_account
):
    """Кнопка кабинета: одна ручка, ответ — вышел ли аккаунт на связь."""
    account_id = await create_account(user, phone=PHONE)

    response = await client.post(f"/api/accounts/{account_id}/retry", headers=auth_headers)

    assert response.status == 200
    assert await response.json() == {
        "ok": True,
        "phone": PHONE,
        "online": True,
        "error": None,
    }
    assert gateway.retried == [account_id]


async def test_retry_endpoint_says_why_it_failed(
    client, auth_headers, gateway, user, create_account
):
    """Отказ — это 200 с причиной: беда не в запросе, и кабинету есть что сказать."""
    account_id = await create_account(user, phone=PHONE)
    gateway.retry_result = (False, "OSError: network is unreachable")

    response = await client.post(f"/api/accounts/{account_id}/retry", headers=auth_headers)

    assert response.status == 200
    body = await response.json()
    assert (body["online"], body["error"]) == (False, "OSError: network is unreachable")


async def test_retry_endpoint_guards_the_account(client, auth_headers, gateway, user):
    """Без подписи — 401, чужой или выдуманный id — 404."""
    assert (await client.post("/api/accounts/1/retry")).status == 401
    assert (await client.post("/api/accounts/424242/retry", headers=auth_headers)).status == 404
    assert (await client.post("/api/accounts/мусор/retry", headers=auth_headers)).status == 404


async def test_accounts_payload_tells_when_a_new_login_is_needed(
    client, auth_headers, gateway, user, create_account
):
    """Кабинету нужно знать, что предлагать: повтор или вход по номеру заново.

    Разбирать текст ошибки на стороне кабинета нельзя — от правки формулировки
    кнопка молча стала бы неправильной.
    """
    account_id = await create_account(user, phone=PHONE)
    async with session_scope() as session:
        row = await repo.get_account(session, account_id, user)
        await repo.set_account_error(session, row, SESSION_REVOKED)

    item = (await _accounts_payload(client, auth_headers))["accounts"][0]

    assert item["needs_login"] is True
    assert item["last_error"] == SESSION_REVOKED
    assert item["online"] is False

    async with session_scope() as session:
        row = await repo.get_account(session, account_id, user)
        await repo.note_account_trouble(session, row, "TimeoutError: Telegram молчит")

    item = (await _accounts_payload(client, auth_headers))["accounts"][0]
    assert item["needs_login"] is False, "сеть чинится повтором, вход тут не нужен"
    assert item["is_active"] is True


# ── Контракт настоящего шлюза ────────────────────────────────────────────────
# Всё выше проверено на FakeGateway, а он ловит SessionPasswordNeededError из
# sign_in_code. Настоящий ClientManager эту ошибку однажды глотал и возвращал
# сессию, как при удачном входе: сценарий считал код принятым целиком, шаг с
# облачным паролем не наступал никогда, а неавторизованная сессия падала на
# итоговой проверке — «Аккаунт не подтверждён: Не удалось получить данные
# аккаунта». Фальшивый шлюз такого не покажет, поэтому контракт закреплён здесь.


class FakeTelethonClient:
    """Клиент Telethon без сети: отвечает на sign_in тем, что попросил тест."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.connected = False
        self.disconnected = False
        self.session = type("FakeSession", (), {"save": staticmethod(lambda: SIGNED)})()

    async def connect(self) -> None:
        self.connected = True

    async def sign_in(self, **kwargs) -> None:
        if self.error is not None:
            raise self.error

    async def disconnect(self) -> None:
        self.disconnected = True


async def test_gateway_lets_2fa_request_through(monkeypatch) -> None:
    """Требование облачного пароля обязано дойти до сценария, а не пропасть."""
    client = FakeTelethonClient(SessionPasswordNeededError(request=None))
    monkeypatch.setattr(manager, "_new_client", lambda session_string="", creds=None, **kwargs: client)

    with pytest.raises(SessionPasswordNeededError):
        await manager.sign_in_code(
            phone=PHONE, code="11111", session_string=SESSION, phone_code_hash="hash"
        )

    # Соединение закрывается и на ошибке: иначе висит сокет на каждый вход.
    assert client.disconnected


async def test_gateway_returns_session_when_code_is_enough(monkeypatch) -> None:
    """Без 2FA шлюз отдаёт сохранённую сессию — её сценарий и записывает."""
    client = FakeTelethonClient()
    monkeypatch.setattr(manager, "_new_client", lambda session_string="", creds=None, **kwargs: client)

    saved = await manager.sign_in_code(
        phone=PHONE, code="11111", session_string=SESSION, phone_code_hash="hash"
    )

    assert saved == SIGNED
    assert client.connected and client.disconnected


# ── Контракт доставки кода ───────────────────────────────────────────────────
# «Код не пришёл» чинится только знанием, куда Telegram его положил. Тип
# доставки тянем из ответа SendCode/ResendCode здесь — фальшивый клиент ниже
# отвечает тем типом, что попросил тест.


def _code_type(name: str):
    """Класс с именем типа Telegram: _delivery_info смотрит только на него."""
    return type(name, (), {})()


class FakeSentCode:
    def __init__(self, via: str = "SentCodeTypeApp", next_via=None, timeout=60) -> None:
        self.type = _code_type(via)
        self.next_type = _code_type(next_via) if next_via else None
        self.timeout = timeout
        self.phone_code_hash = "hash-" + via


class FakeCodeClient(FakeTelethonClient):
    """Отвечает на запрос и повтор кода тем типом доставки, что дали."""

    def __init__(self, sent: FakeSentCode, resent: FakeSentCode | None = None) -> None:
        super().__init__()
        self.sent_result = sent
        self.resent_result = resent or sent
        self.requests: list = []

    async def send_code_request(self, phone: str) -> FakeSentCode:
        return self.sent_result

    async def __call__(self, request) -> FakeSentCode:
        self.requests.append(request)
        return self.resent_result


def test_delivery_info_names_telegram_types():
    from app.telegram_client.manager import _delivery_info

    info = _delivery_info(FakeSentCode("SentCodeTypeApp", "SentCodeTypeSms", 60))

    assert info == {"via": "app", "next": "sms", "timeout": 60}


def test_delivery_info_survives_unknown_types():
    """Будущий тип Telegram — "other", а не рухнувший вход."""
    from app.telegram_client.manager import _delivery_info

    info = _delivery_info(FakeSentCode("SentCodeTypeCarrierPigeon"))

    assert info["via"] == "other"
    assert info["next"] is None


async def test_gateway_send_code_returns_delivery(monkeypatch) -> None:
    client = FakeCodeClient(FakeSentCode("SentCodeTypeSms", "SentCodeTypeCall", 120))
    monkeypatch.setattr(manager, "_new_client", lambda session_string="", creds=None, **kwargs: client)

    session, code_hash, delivery = await manager.send_code(PHONE)

    assert session == SIGNED
    assert code_hash == "hash-SentCodeTypeSms"
    assert delivery == {"via": "sms", "next": "call", "timeout": 120}
    assert client.disconnected


async def test_gateway_resend_uses_resend_request(monkeypatch) -> None:
    """Повтор — это auth.ResendCode с хэшем первой попытки, а не новый SendCode."""
    from telethon.tl import functions

    client = FakeCodeClient(
        FakeSentCode("SentCodeTypeApp"),
        FakeSentCode("SentCodeTypeSms", "SentCodeTypeCall", 60),
    )
    monkeypatch.setattr(manager, "_new_client", lambda session_string="", creds=None, **kwargs: client)

    _, code_hash, delivery = await manager.resend_code(
        phone=PHONE, session_string=SESSION, phone_code_hash="old-hash"
    )

    assert len(client.requests) == 1
    request = client.requests[0]
    assert isinstance(request, functions.auth.ResendCodeRequest)
    assert (request.phone_number, request.phone_code_hash) == (PHONE, "old-hash")
    assert code_hash == "hash-SentCodeTypeSms"
    assert delivery["via"] == "sms"
    assert client.disconnected
