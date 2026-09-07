"""Панель владельца: состав кнопок, гейт доступа, выдача и рассылка."""
from __future__ import annotations

from types import SimpleNamespace

from app.bot.handlers import admin as owner
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal, session_scope
from tests.helpers import (
    TEST_USER_ID,
    FakeCallback,
    FakeMessage,
    RecordingBot,
    add_rule,
    button_labels,
)


async def _with_admin(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", [TEST_USER_ID])


def _panel_buttons(markup) -> list[str]:
    return button_labels(markup)


async def test_panel_shows_all_sections(create_user, monkeypatch):
    await _with_admin(monkeypatch)
    await create_user(id=TEST_USER_ID)
    callback = FakeCallback("menu:admin", RecordingBot())

    await owner.show_admin(callback)

    text, markup = callback.message.edits[-1]
    assert "Панель владельца" in text
    labels = _panel_buttons(markup)
    for expected in (
        "Статистика",
        "Пользователи",
        "Выдать абонемент",
        "Рассылка",
        "Перезапустить аккаунты",
    ):
        assert any(expected in label for label in labels), labels


async def test_stranger_gets_no_panel(monkeypatch):
    await _with_admin(monkeypatch)
    callback = FakeCallback("menu:admin", RecordingBot(), user_id=999_999_999)

    await owner.show_admin(callback)

    assert "Нет доступа" in callback.alerts
    assert callback.message.edits == []


async def test_stats_counts_everything(create_user, create_account, monkeypatch):
    await _with_admin(monkeypatch)
    await create_user(id=TEST_USER_ID)
    friend = await create_user()
    account_id = await create_account(TEST_USER_ID)
    await add_rule(TEST_USER_ID, account_id)
    async with session_scope() as session:
        await repo.add_subscription_days(session, friend, 30)
        await session.commit()

    await owner.admin_stats(FakeCallback("admin:stats", RecordingBot()))

    text = owner  # silence linters about unused import in refactors
    del text


async def test_stats_text(create_user, create_account, monkeypatch):
    await _with_admin(monkeypatch)
    await create_user(id=TEST_USER_ID)
    friend = await create_user()
    account_id = await create_account(TEST_USER_ID)
    await add_rule(TEST_USER_ID, account_id)
    async with session_scope() as session:
        await repo.add_subscription_days(session, friend, 30)
        await session.commit()
    callback = FakeCallback("admin:stats", RecordingBot())

    await owner.admin_stats(callback)

    text, _markup = callback.message.edits[-1]
    assert "Пользователей: <b>2</b>" in text
    assert "Активных абонементов: <b>1</b>" in text
    assert "Задач: <b>1</b>" in text
    assert "Аккаунтов: <b>1</b>" in text


async def test_users_list_shows_names_and_subs(create_user, monkeypatch):
    await _with_admin(monkeypatch)
    await create_user(id=TEST_USER_ID, username="owner")
    await create_user(username="friend")
    async with session_scope() as session:
        users = list(await repo.recent_users(session, 10))
        friend_id = next(user.id for user in users if user.username == "friend")
        await repo.add_subscription_days(session, friend_id, 30)
        await session.commit()
    callback = FakeCallback("admin:users", RecordingBot())

    await owner.admin_users(callback)

    text, _markup = callback.message.edits[-1]
    assert "(всего: 2)" in text
    assert "@friend" in text and "@owner" in text
    assert f"<code>{friend_id}</code>" in text


def _grant_message(text: str, bot=None) -> FakeMessage:
    message = FakeMessage(text)
    message.from_user = SimpleNamespace(id=TEST_USER_ID)
    message.bot = bot or RecordingBot()
    return message


async def test_grant_command_issues_and_notifies(monkeypatch):
    await _with_admin(monkeypatch)
    bot = RecordingBot()
    message = _grant_message("/grant 555001 3", bot)

    await owner.grant_access(message)

    assert "555001" in message.edits[-1][0]
    async with session_scope() as session:
        assert await repo.has_active_subscription(session, 555001)
    assert bot.messages and bot.messages[-1][0] == 555001


async def test_grant_command_rejects_bad_period(monkeypatch):
    await _with_admin(monkeypatch)
    message = _grant_message("/grant 555001 5")

    await owner.grant_access(message)

    assert "1, 3, 6 или 12" in message.edits[-1][0]
    async with session_scope() as session:
        assert not await repo.has_active_subscription(session, 555001)


async def test_grant_command_ignores_stranger(monkeypatch):
    await _with_admin(monkeypatch)
    message = _grant_message("/grant 555001 1")
    message.from_user = SimpleNamespace(id=999_999_999)

    await owner.grant_access(message)

    assert message.edits == []


async def test_do_grant_reports_blocked_bot():
    bot = RecordingBot(fail_for={777001})
    until, notified = await owner._do_grant(bot, 777001, 1)
    assert notified is False
    assert until is not None
    async with session_scope() as session:
        assert await repo.has_active_subscription(session, 777001)


async def test_broadcast_reaches_everyone_except_blocked():
    bot = RecordingBot(fail_for={3})
    sent, failed = await owner._send_broadcast(bot, [1, 2, 3], "новость")
    assert (sent, failed) == (2, 1)
    assert [chat for chat, _text in bot.messages] == [1, 2]


async def test_broadcast_skips_banned(create_user, monkeypatch):
    await _with_admin(monkeypatch)
    await create_user(id=TEST_USER_ID)
    banned = await create_user()
    async with session_scope() as session:
        user = await repo.get_user(session, banned)
        assert user is not None
        user.is_banned = True
        await session.commit()
    async with SessionLocal() as session:
        ids = list(await repo.list_user_ids(session))
    assert banned not in ids and TEST_USER_ID in ids


def test_parse_user_id():
    assert owner._parse_user_id(" 123456789 ") == 123456789
    assert owner._parse_user_id("@username") is None
    assert owner._parse_user_id("") is None
