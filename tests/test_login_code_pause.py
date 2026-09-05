"""Пауза перед новым кодом принадлежит номеру, а не попытке входа.

В боевом журнале один номер получил три кода за 43 секунды: 14:02:32, 14:02:56,
14:03:15 — при том, что пауза между запросами кода в коде стояла минутная.
Причина: пауза считалась по строке ``pending_logins``, а «Отмена» (в боте) и
«Другой номер» (в кабинете) эту строку удаляют — следующий запрос уходил в
Telegram сразу. Так и получают ``FloodWaitError`` на сам номер: вход на него
закрывается на часы, и человек не может подключить аккаунт вообще.

Здесь проверяем, что:

* повтор раньше минуты в Telegram не идёт, и отказ называет остаток секунд;
* пауза переживает отмену входа и перезапуск сервиса (она в БД, по номеру);
* другой номер ждать не заставляют, а после паузы код уходит снова;
* неудачная отправка паузу не накладывает — там кода не было;
* отказ говорит, где человек теперь стоит, чтобы его не выкидывали на начало,
  и бот по этому указанию оставляет человека внутри входа.

В Telegram здесь не ходит никто: шлюз подменён ``FakeGateway``.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app import accounts_login
from app.bot.handlers import accounts as bot_accounts
from app.bot.states import LoginStates
from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.db.models import PhoneCodeSend
from app.errors import ConflictError, ValidationError
from app.timeutil import utcnow
from tests.helpers import TEST_USER_ID
from tests.test_accounts_login import (  # общий фальшивый шлюз
    OTHER_PHONE,
    PHONE,
    FakeGateway,
    gateway,
    user,
)

__all__ = ["FakeGateway", "gateway", "user"]  # фикстуры импортированы, а не забыты


async def rewind(phone: str, seconds: int) -> None:
    """Сдвигает метку номера в прошлое: иначе пауза в тесте не истечёт."""
    async with session_scope() as session:
        mark = await session.get(PhoneCodeSend, phone)
        mark.sent_at = utcnow() - timedelta(seconds=seconds)


async def mark_of(phone: str):
    async with SessionLocal() as session:
        return await repo.code_sent_at(session, phone)


# ────────────────────────── пауза после отправки кода ─────────────────────────


async def test_a_second_request_within_a_minute_is_refused(gateway, user):
    """Второй запрос кода в Telegram не идёт, а отказ называет остаток."""
    await accounts_login.start(user, PHONE)

    with pytest.raises(ConflictError) as info:
        await accounts_login.start(user, PHONE)

    assert gateway.sent == [PHONE], "второй запрос кода всё-таки ушёл в Telegram"
    assert 0 < info.value.details["wait"] <= accounts_login.RESEND_COOLDOWN_SECONDS
    assert str(info.value.details["wait"]) in info.value.message


async def test_the_pause_survives_a_cancel(gateway, user):
    """Та самая боевая осечка: «Отмена» больше не обнуляет паузу."""
    await accounts_login.start(user, PHONE)
    assert await accounts_login.cancel(user) is True

    with pytest.raises(ConflictError) as info:
        await accounts_login.start(user, PHONE)

    assert gateway.sent == [PHONE], "после отмены код ушёл второй раз"
    # Незавершённого входа больше нет, поэтому текст другой: ждать, а не вводить.
    assert "меньше минуты назад" in info.value.message
    assert info.value.details["stage"] == "phone"


async def test_the_pause_outlives_the_process(gateway, user):
    """Метка лежит в БД: перезапуск сервиса паузу не снимает.

    Отдельная сессия БД здесь и есть проверка — в памяти процесса ничего нет.
    """
    await accounts_login.start(user, PHONE)

    assert await mark_of(PHONE) is not None

    async with SessionLocal() as session:
        # Ровно то, что сделает свежий процесс: прочитает метку и посчитает паузу.
        left = await accounts_login._pause_left(session, PHONE, None)
    assert left > 0


async def test_a_cancelled_login_keeps_only_the_mark(gateway, user):
    """Отмена стирает шаг входа, но не память о том, что код уходил."""
    await accounts_login.start(user, PHONE)
    await accounts_login.cancel(user)

    async with SessionLocal() as session:
        assert await repo.get_pending_login(session, user) is None
    assert await mark_of(PHONE) is not None


async def test_another_number_is_not_made_to_wait(gateway, user):
    """Пауза на одном номере не мешает подключить другой."""
    await accounts_login.start(user, PHONE)
    await accounts_login.cancel(user)

    step = await accounts_login.start(user, OTHER_PHONE)

    assert step.phone == OTHER_PHONE
    assert gateway.sent == [PHONE, OTHER_PHONE]


async def test_after_the_pause_a_new_code_goes_out(gateway, user):
    """Минута прошла — повтор разрешён: это и есть «запросить новый код»."""
    await accounts_login.start(user, PHONE)
    await accounts_login.cancel(user)
    await rewind(PHONE, accounts_login.RESEND_COOLDOWN_SECONDS + 5)

    step = await accounts_login.start(user, PHONE)

    assert (step.stage, step.phone) == ("code", PHONE)
    assert gateway.sent == [PHONE, PHONE]


async def test_a_refused_send_does_not_impose_a_pause(gateway, user):
    """Кода не было — ждать нечего: мусорный номер паузу не ставит."""
    with pytest.raises(ValidationError):
        await accounts_login.start(user, "телефон")

    assert await mark_of(PHONE) is None
    step = await accounts_login.start(user, PHONE)
    assert step.phone == PHONE


async def test_a_failed_send_leaves_the_number_free(gateway, user):
    """Telegram не принял номер — на этом номере паузы нет."""
    from telethon.errors import PhoneNumberInvalidError

    gateway.send_error = PhoneNumberInvalidError(request=None)
    with pytest.raises(ValidationError):
        await accounts_login.start(user, PHONE)

    assert await mark_of(PHONE) is None


# ─────────────────────── отказ говорит, где человек стоит ─────────────────────


async def test_the_refusal_points_at_the_code_step(gateway, user):
    """Код уже у человека: отказ ведёт на ввод кода, а не на ввод номера."""
    await accounts_login.start(user, PHONE)

    with pytest.raises(ConflictError) as info:
        await accounts_login.start(user, PHONE)

    details = info.value.details
    assert details["stage"] == "code"
    assert details["phone"] == PHONE
    assert details["attempts_left"] == accounts_login.MAX_CODE_ATTEMPTS
    assert "уже отправлен" in info.value.message


async def test_the_refusal_counts_spent_attempts(gateway, user):
    """Остаток попыток в отказе честный: человек уже ошибался с кодом."""
    from telethon.errors import PhoneCodeInvalidError

    await accounts_login.start(user, PHONE)
    gateway.code_error = PhoneCodeInvalidError(request=None)
    with pytest.raises(ValidationError):
        await accounts_login.submit_code(user, "00000")

    with pytest.raises(ConflictError) as info:
        await accounts_login.start(user, PHONE)

    assert info.value.details["attempts_left"] == accounts_login.MAX_CODE_ATTEMPTS - 1


async def test_the_pause_holds_for_another_person(gateway, create_user):
    """Лимит висит на номере: второй человек тем же номером его не обходит."""
    first = await create_user(id=TEST_USER_ID)
    second = await create_user(id=TEST_USER_ID + 1)

    await accounts_login.start(first, PHONE)

    with pytest.raises(ConflictError) as info:
        await accounts_login.start(second, PHONE)

    assert gateway.sent == [PHONE]
    assert info.value.details["stage"] == "phone", "чужой шаг входа показывать нельзя"


# ──────────────────────────── метки не копятся ────────────────────────────────


async def test_old_marks_are_swept(gateway, user):
    """Таблица нужна на минуту: старые метки уходят при следующей записи."""
    await accounts_login.start(user, PHONE)
    await rewind(PHONE, int(repo.CODE_MARK_TTL.total_seconds()) + 60)

    await accounts_login.start(user, OTHER_PHONE)

    assert await mark_of(PHONE) is None, "метка старше часа осталась в БД"
    assert await mark_of(OTHER_PHONE) is not None


async def test_a_repeat_moves_the_mark_forward(gateway, user):
    """Новый код — новая точка отсчёта, иначе пауза считалась бы от первой."""
    await accounts_login.start(user, PHONE)
    await accounts_login.cancel(user)
    await rewind(PHONE, accounts_login.RESEND_COOLDOWN_SECONDS + 5)
    before = await mark_of(PHONE)

    await accounts_login.start(user, PHONE)

    assert await mark_of(PHONE) > before


# ──────────────────── бот: отказ не выкидывает из входа ───────────────────────
#
# Раньше любой отказ на шаге номера чистил FSM: человек оставался в чате с
# ботом, введённый следом код улетал в никуда, а вход приходилось начинать
# заново — и с новым кодом, то есть снова к тому же лимиту Telegram.


class Reply:
    """Ответ «⏳ Отправляю код…», который хендлер потом правит на результат."""

    def __init__(self, log: list[dict]) -> None:
        self.log = log

    async def edit_text(self, text, reply_markup=None, **_kwargs):
        self.log.append({"text": text, "markup": reply_markup})


class FakeMessage:
    """Сообщение в объёме шага входа: автор, текст и ответ, который правят."""

    def __init__(self, text: str, user_id: int) -> None:
        self.text = text
        self.from_user = SimpleNamespace(
            id=user_id, username="doch", full_name="Иван Петров", is_bot=False
        )
        self.replies: list[dict] = []

    async def answer(self, text, reply_markup=None, **_kwargs):
        self.replies.append({"text": text, "markup": reply_markup})
        return Reply(self.replies)


async def phone_step(user_id: int, phone: str) -> tuple[FSMContext, FakeMessage]:
    """Ставит человека на шаг номера и отдаёт боту введённый номер."""
    state = FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id),
    )
    await state.set_state(LoginStates.phone)
    message = FakeMessage(phone, user_id)
    await bot_accounts.process_phone(message, state)
    return state, message


async def test_the_bot_leads_to_the_code_after_such_a_refusal(gateway, user):
    """Код на номер уже ушёл — бот ждёт код, а не номер по второму разу."""
    await accounts_login.start(user, PHONE)

    state, message = await phone_step(user, PHONE)

    assert await state.get_state() == LoginStates.code.state
    assert "уже отправлен" in message.replies[-1]["text"]
    assert gateway.sent == [PHONE]


async def test_the_bot_keeps_the_person_on_the_phone_step(gateway, user):
    """Вход отменён, но пауза идёт: остаёмся на шаге номера, а не в меню."""
    await accounts_login.start(user, PHONE)
    await accounts_login.cancel(user)

    state, message = await phone_step(user, PHONE)

    assert await state.get_state() == LoginStates.phone.state
    assert "меньше минуты назад" in message.replies[-1]["text"]


async def test_the_bot_gives_up_when_telegram_refuses_the_number(gateway, user):
    """Номер заблокирован — продолжать нечего, но и это остаётся шагом номера."""
    from telethon.errors import PhoneNumberBannedError

    gateway.send_error = PhoneNumberBannedError(request=None)

    state, message = await phone_step(user, PHONE)

    # ValidationError — ввод не подошёл: человек вправе назвать другой номер.
    assert await state.get_state() == LoginStates.phone.state
    assert "заблокирован" in message.replies[-1]["text"]

