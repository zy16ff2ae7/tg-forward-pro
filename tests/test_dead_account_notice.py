"""Аккаунт выпал — владельцу говорят об этом сами, а не по его догадке.

В боевом журнале сессия аккаунта +79013533606 перестала работать в 08:27
(«Сессия ... больше не действует — нужен повторный вход»), а владелец отключил
мёртвый аккаунт в 16:49 — и только потому, что сам зашёл в кабинет. Восемь с
лишним часов задачи на этом номере молча ничего не пересылали: сервис знал
причину, кабинет её показывал, но человеку никто не сказал.

Здесь проверяем, что:

* про выпавший аккаунт приходит одно сообщение — с номером, ценой простоя и
  кнопкой «Подключить заново»;
* оно не повторяется каждые пять минут, но после возвращения аккаунта в работу
  о следующем таком случае скажут снова;
* беда, которая проходит сама (сеть, таймаут), сообщением не будит;
* сообщение уходит владельцу номера, а не всем подряд;
* заблокированный бот не мешает остальным и не роняет проход.

Бот здесь подменён ``RecordingBot``: в Telegram не ходит никто.
"""
from __future__ import annotations

from sqlalchemy import text

from app import main
from app.db import repo
from app.db.database import SessionLocal, engine, ensure_schema, session_scope
from app.db.models import TelegramAccount
from app.telegram_client.manager import SESSION_REVOKED, SESSION_UNREADABLE
from tests.helpers import RecordingBot, add_rule

PHONE = "+79013533606"


async def kill_session(account_id: int, reason: str = SESSION_REVOKED) -> None:
    """Ровно то, что делает пул, когда Telegram отказал в сохранённой сессии."""
    async with session_scope() as session:
        account = await session.get(TelegramAccount, account_id)
        await repo.set_account_error(session, account, reason)


async def notified_at(account_id: int):
    async with SessionLocal() as session:
        account = await session.get(TelegramAccount, account_id)
        return account.error_notified_at


# ─────────────────────────── сообщение о выпадении ────────────────────────────


async def test_the_owner_is_told_the_account_fell_out(create_user, create_account):
    """Тот самый пропущенный час: теперь про мёртвую сессию говорят сразу."""
    user_id = await create_user()
    account_id = await create_account(user_id, PHONE)
    await kill_session(account_id)
    bot = RecordingBot()

    await main.notify_dead_accounts(bot)

    assert bot.recipients == [user_id]
    text = bot.messages[0][1]
    assert "+79013533606" in text
    assert "подключите номер заново" in text.lower()
    assert await notified_at(account_id) is not None


async def test_the_notice_names_the_price_of_the_silence(create_user, create_account):
    """Сколько задач встало — это и есть цена мёртвой сессии."""
    user_id = await create_user()
    account_id = await create_account(user_id, PHONE)
    await add_rule(user_id, account_id)
    await add_rule(user_id, account_id)
    await kill_session(account_id)
    bot = RecordingBot()

    await main.notify_dead_accounts(bot)

    assert "задач — 2" in bot.messages[0][1]


async def test_stopped_tasks_are_counted_honestly(create_user, create_account):
    """Выключенная и убранная в архив задача и так ничего не делала."""
    user_id = await create_user()
    account_id = await create_account(user_id, PHONE)
    await add_rule(user_id, account_id)
    await add_rule(user_id, account_id, enabled=False)
    await add_rule(user_id, account_id, archived=True)
    await kill_session(account_id)
    bot = RecordingBot()

    await main.notify_dead_accounts(bot)

    assert "задач — 1" in bot.messages[0][1]


async def test_an_account_without_tasks_does_not_claim_a_stoppage(create_user, create_account):
    """Пересылке нечего было останавливать — так и говорим."""
    user_id = await create_user()
    account_id = await create_account(user_id, PHONE)
    await kill_session(account_id)
    bot = RecordingBot()

    await main.notify_dead_accounts(bot)

    text = bot.messages[0][1]
    assert "Задач на нём пока нет" in text
    assert "стоит" not in text


async def test_the_notice_carries_the_reason_the_pool_wrote(create_user, create_account):
    """Причины разные, и подменять их одной общей — значит соврать в одной из них."""
    user_id = await create_user()
    account_id = await create_account(user_id, PHONE)
    await kill_session(account_id, SESSION_UNREADABLE)
    bot = RecordingBot()

    await main.notify_dead_accounts(bot)

    text = bot.messages[0][1]
    assert "Сохранённая сессия не читается" in text
    assert "вышел из Telegram" not in text, "причина подменена чужой"
    assert ".." not in text, "точка причины и точка сервиса сложились"


async def test_the_notice_offers_the_way_back(create_user, create_account):
    """Кнопка ведёт на вход: искать его в меню после такого письма — лишнее."""
    user_id = await create_user()
    account_id = await create_account(user_id, PHONE)
    await kill_session(account_id)
    bot = RecordingBot()

    await main.notify_dead_accounts(bot)

    buttons = [
        button.callback_data
        for row in bot.markups[0].inline_keyboard
        for button in row
    ]
    assert "acc:add" in buttons


# ──────────────────────────── одно письмо на беду ─────────────────────────────


async def test_the_notice_is_not_repeated_every_five_minutes(create_user, create_account):
    """Фоновый цикл ходит каждые пять минут — письмо всё равно одно."""
    user_id = await create_user()
    account_id = await create_account(user_id, PHONE)
    await kill_session(account_id)
    bot = RecordingBot()

    await main.notify_dead_accounts(bot)
    await main.notify_dead_accounts(bot)
    await main.notify_dead_accounts(bot)

    assert bot.recipients == [user_id]


async def test_a_second_fall_is_reported_again(create_user, create_account):
    """Аккаунт вернулся и снова выпал — это новая беда, а не та же самая."""
    user_id = await create_user()
    account_id = await create_account(user_id, PHONE)
    await kill_session(account_id)
    bot = RecordingBot()
    await main.notify_dead_accounts(bot)

    async with session_scope() as session:  # удачный вход снимает причину
        account = await session.get(TelegramAccount, account_id)
        await repo.set_account_error(session, account, None)
    assert await notified_at(account_id) is None

    await kill_session(account_id)
    await main.notify_dead_accounts(bot)

    assert bot.recipients == [user_id, user_id]


async def test_a_blocked_bot_does_not_break_the_pass(create_user, create_account):
    """Человек заблокировал бота: сказать нельзя, но проход обязан дойти до конца."""
    blocked = await create_user()
    blocked_account = await create_account(blocked, "+79005550001")
    other = await create_user(id=blocked + 1)
    account_id = await create_account(other, "+79005550002")
    await kill_session(blocked_account)
    await kill_session(account_id)
    bot = RecordingBot(fail_for={blocked})

    await main.notify_dead_accounts(bot)

    assert bot.recipients == [other], "письмо второму владельцу не ушло"
    assert await notified_at(account_id) is not None
    # Попытка одна на беду: заблокировавшему бота сказать нечем, а долбить его
    # каждые пять минут — только тратить проходы. Причина ждёт его в кабинете.
    assert await notified_at(blocked_account) is not None


async def test_the_pass_is_wired_into_the_background_loop(create_user, create_account):
    """Проверка, которой нет в фоновом цикле, не сработает никогда."""
    user_id = await create_user()
    await kill_session(await create_account(user_id, PHONE))
    bot = RecordingBot()

    await main.run_background_checks(bot)

    assert bot.recipients == [user_id]


# ─────────────────────── о чём говорить не надо ───────────────────────────────


async def test_a_passing_trouble_does_not_wake_anyone(create_user, create_account):
    """Сеть отвалилась — аккаунт останется в работе, будить человека не за что."""
    user_id = await create_user()
    account_id = await create_account(user_id, PHONE)
    async with session_scope() as session:
        account = await session.get(TelegramAccount, account_id)
        await repo.note_account_trouble(session, account, "TimeoutError: нет сети")
    bot = RecordingBot()

    await main.notify_dead_accounts(bot)

    assert bot.messages == []
    assert await notified_at(account_id) is None


async def test_a_working_account_is_left_alone(create_user, create_account):
    user_id = await create_user()
    await create_account(user_id, PHONE)
    bot = RecordingBot()

    await main.notify_dead_accounts(bot)

    assert bot.messages == []


async def test_the_notice_goes_to_the_owner_of_the_number(create_user, create_account):
    """Чужой номер — чужая беда: списка аккаунтов другому человеку не видно."""
    owner = await create_user()
    stranger = await create_user(id=owner + 1)
    account_id = await create_account(owner, PHONE)
    await create_account(stranger, "+79005550003")
    await kill_session(account_id)
    bot = RecordingBot()

    await main.notify_dead_accounts(bot)

    assert bot.recipients == [owner]


# ─────────────────────────── база старше правки ───────────────────────────────


async def test_an_old_database_gets_the_mark_column(create_user, create_account):
    """Боевая БД старше этой правки: колонку доливает старт, руками — ничего.

    ``create_all`` дополняет только новые таблицы, поэтому колонка в живой базе
    появляется через ``ADDED_COLUMNS``. Без неё первый же фоновый проход упал бы
    на «no such column», и сообщение не ушло бы никому.
    """
    user_id = await create_user()
    account_id = await create_account(user_id, PHONE)
    await kill_session(account_id)

    async with engine.begin() as conn:  # база, какой она была до правки
        await conn.execute(
            text('ALTER TABLE telegram_accounts DROP COLUMN "error_notified_at"')
        )
        rows = await conn.execute(text('PRAGMA table_info("telegram_accounts")'))
        assert "error_notified_at" not in {row[1] for row in rows}

    await ensure_schema()

    bot = RecordingBot()
    await main.notify_dead_accounts(bot)

    assert bot.recipients == [user_id]
    assert await notified_at(account_id) is not None
