"""Парсер аудитории: режимы сбора, фильтры и лимиты.

Состав чата — это все, включая мёртвые души; авторы сообщений — только живые.
Проверяем оба режима, каждый фильтр отдельно и связку «просмотреть/сохранить»:
смотреть приходится больше, чем забираешь, иначе фильтры съедают результат.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.db import repo
from app.db.database import session_scope
from app.telegram_client import jobs
from app.telegram_client.filters import FilterConfig
from tests.helpers import TEST_USER_ID
from tests.test_delivery_fixes import _db_rule, _snapshot
from tests.test_oneshot_journal import OneShotClient, person
from tests.test_task_edit import make_task

# Фикстуры соседних файлов: «все ссылки находятся» и «разовые задачи не ходят
# в Telegram» нужны здесь ровно те же.
from tests.test_many_chats import (  # noqa: F401
    login_open,
    many_chats_resolved,
    one_shot_stubbed,
)


class UserStatusOnline:
    pass


class UserStatusRecently:
    pass


class UserStatusOffline:
    def __init__(self, was_online):
        self.was_online = was_online


class HistoryClient:
    """Чат для режима истории: сообщения с авторами и люди за ними."""

    def __init__(self, messages, users):
        self._messages = list(messages)
        self._users = dict(users)
        self.entity_calls = 0

    def iter_messages(self, chat_id, limit: int = 0):
        pool = self._messages[: limit or None]

        async def walk():
            for message in pool:
                yield message

        return walk()

    async def get_entity(self, user_id):
        self.entity_calls += 1
        return self._users[user_id]

    def iter_participants(self, chat_id, limit: int = 0, **kwargs):
        async def nobody():
            if False:
                yield None

        return nobody()


def message(sender_id):
    return SimpleNamespace(sender_id=sender_id)


async def test_parser_drops_users_without_username_by_default(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    client = OneShotClient(participants=[person(11), person(12, username="")])
    result = await jobs.run_parser(client, _snapshot(rule))
    assert result["ok"] is True
    assert result["collected"] == 1
    assert result["filtered"] == 1


async def test_parser_excludes_admins_by_default(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    admin = person(21)
    client = OneShotClient(participants=[admin, person(22)], admins=[admin])
    result = await jobs.run_parser(client, _snapshot(rule))
    assert result["collected"] == 1
    assert result["filtered"] == 1


async def test_parser_premium_and_photo_filters(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    plain = person(31)
    vip = person(32, premium=True, photo=SimpleNamespace(id=1))
    client = OneShotClient(participants=[plain, vip])
    conf = FilterConfig(only_premium=True, only_with_photo=True)
    result = await jobs.run_parser(client, _snapshot(rule, filters=conf))
    assert result["collected"] == 1


async def test_parser_active_only_keeps_recently_online(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    now = datetime.now(timezone.utc)
    online = person(41, status=UserStatusOnline())
    recent = person(42, status=UserStatusRecently())
    fresh = person(43, status=UserStatusOffline(now - timedelta(hours=5)))
    stale = person(44, status=UserStatusOffline(now - timedelta(days=10)))
    hidden = person(45)  # статус скрыт — когда был, неизвестно
    client = OneShotClient(participants=[online, recent, fresh, stale, hidden])
    result = await jobs.run_parser(
        client, _snapshot(rule, filters=FilterConfig(active_only=True))
    )
    assert result["collected"] == 3
    assert result["filtered"] == 2


async def test_parser_online_within_hours(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    now = datetime.now(timezone.utc)
    fresh = person(51, status=UserStatusOffline(now - timedelta(hours=2)))
    old = person(52, status=UserStatusOffline(now - timedelta(hours=30)))
    client = OneShotClient(participants=[fresh, old])
    result = await jobs.run_parser(
        client, _snapshot(rule, filters=FilterConfig(online_within_hours=24))
    )
    assert result["collected"] == 1


async def test_parser_history_collects_unique_authors(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    users = {61: person(61), 62: person(62), 63: person(63, username="")}
    client = HistoryClient(
        [message(61), message(61), message(62), message(63), message(None)],
        users,
    )
    result = await jobs.run_parser(
        client, _snapshot(rule, filters=FilterConfig(parser_mode="history"))
    )
    assert result["ok"] is True
    assert result["mode"] == "history"
    assert result["collected"] == 2
    # Повторный автор сущность не дёргает: его уже разбирали.
    assert client.entity_calls == 3
    async with session_scope() as session:
        assert await repo.count_collected_items(session, rule.id) == 2


async def test_parser_history_skips_bots_and_channels(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    users = {
        71: person(71, bot=True),
        72: SimpleNamespace(id=72, deleted=False, bot=False, broadcast=True),
        73: person(73),
    }
    client = HistoryClient([message(71), message(72), message(73)], users)
    result = await jobs.run_parser(
        client, _snapshot(rule, filters=FilterConfig(parser_mode="history"))
    )
    assert result["collected"] == 1
    assert result["filtered"] == 2


async def test_parser_scan_and_result_limits(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    client = OneShotClient(participants=[person(80 + i) for i in range(10)])
    # Просмотр короче результата: больше просмотренного не собрать.
    result = await jobs.run_parser(
        client, _snapshot(rule, filters=FilterConfig(scan_limit=3, limit=10))
    )
    assert result["collected"] == 3
    assert result["scanned"] == 3
    # Результат короче просмотра: лишнее не перебираем.
    client = OneShotClient(participants=[person(90 + i) for i in range(10)])
    result = await jobs.run_parser(
        client, _snapshot(rule, filters=FilterConfig(scan_limit=10, limit=4))
    )
    assert result["collected"] == 4
    assert result["scanned"] == 4


async def test_parser_api_stores_and_returns_settings(
    client, auth_headers, create_account, login_open, many_chats_resolved, one_shot_stubbed
):
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    task = await make_task(
        client, auth_headers, account_id,
        command="parser", source="@ch-1",
        parser_mode="history", scan=500, limit=50,
        require_username=False, only_premium=True, online_within_hours=48,
        api_delay=2,
    )
    edit = task["edit"]
    assert edit["parser_mode"] == "history"
    assert edit["scan"] == 500
    assert edit["limit"] == 50
    assert edit["require_username"] is False
    assert edit["exclude_admins"] is True
    assert edit["only_premium"] is True
    assert edit["online_within_hours"] == 48
    assert edit["api_delay"] == 2


async def test_parser_catalog_lists_new_fields(client, auth_headers):
    response = await client.get("/api/commands", headers=auth_headers)
    commands = {item["id"]: item for item in (await response.json())["commands"]}
    optional = commands["parser"]["optional"]
    for field in (
        "parser_mode", "scan", "limit", "require_username", "exclude_admins",
        "only_premium", "only_with_photo", "active_only",
        "online_within_hours", "api_delay",
    ):
        assert field in optional
