"""Клон канала: опись истории, порции от старых к новым, живой режим."""
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError

from app.db.database import session_scope
from app.db.models import Rule
from app.telegram_client import manager as manager_module
from app.telegram_client.filters import FilterConfig
from app.telegram_client.jobs import clone_backfill_tick, task_title
from app.telegram_client.manager import manager
from app.telegram_client.types import RuleSnapshot


class HistoryClient:
    """Источник с историей: помнит посты и записывает отправленное."""

    def __init__(self, posts: dict[int, str], fail_on: set[int] | None = None) -> None:
        self.posts = dict(posts)
        self.fail_on = set(fail_on or set())
        self.sent: list[tuple[int, str]] = []
        self.list_calls = 0

    def is_connected(self) -> bool:
        return True

    def _message(self, mid: int):
        return SimpleNamespace(id=mid, message=self.posts[mid], media=None)

    async def get_messages(self, entity, limit=None, ids=None):
        if ids is not None:
            if not isinstance(ids, list):
                ids = [ids]
            return [self._message(mid) for mid in ids if mid in self.posts]
        self.list_calls += 1
        newest = sorted(self.posts)[-limit:] if limit else sorted(self.posts)
        return [self._message(mid) for mid in reversed(newest)]

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if chat_id in self.fail_on:
            raise FloodWaitError(None, capture=30)
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent))


@pytest.fixture(autouse=True)
def clean_manager_state():
    yield
    manager._clone_rules = []
    manager._clone_state = {}


async def _clone_rule(user_id: int, account_id: int, filters: dict) -> RuleSnapshot:
    async with session_scope() as session:
        rule = Rule(
            user_id=user_id, account_id=account_id, source_id=-100, target_id=-200,
            kind="clone", mode="copy", enabled=True,
            source_title="src", target_title="dst", filters=filters,
        )
        session.add(rule)
        await session.flush()
        return RuleSnapshot(
            id=rule.id, user_id=user_id, target_id=-200, mode="copy",
            delay_seconds=0, account_id=account_id, kind="clone", source_id=-100,
            filters=FilterConfig.from_dict(dict(rule.filters or {})),
        )


async def _db_filters(rule_id: int) -> dict:
    async with session_scope() as session:
        rule = await session.get(Rule, rule_id)
        return dict(rule.filters or {})


async def test_backfill_lists_newest_and_sends_oldest_first(create_user, create_account):
    user_id = await create_user()
    account_id = await create_account(user_id)
    posts = {mid: f"пост {mid}" for mid in range(1, 11)}
    client = HistoryClient(posts)
    rule = await _clone_rule(user_id, account_id, {"clone_history": 5})

    assert await clone_backfill_tick(client, rule) == "done"
    # Забрали 5 последних (6–10) и ушли они от старого к новому.
    assert [text for _, text in client.sent] == [f"пост {mid}" for mid in range(6, 11)]
    assert client.list_calls == 1
    stored = await _db_filters(rule.id)
    assert stored["clone_done"] is True
    assert stored["clone_ids"] == []


async def test_backfill_goes_in_batches(create_user, create_account, monkeypatch):
    user_id = await create_user()
    account_id = await create_account(user_id)
    client = HistoryClient({mid: f"пост {mid}" for mid in range(1, 6)})
    rule = await _clone_rule(user_id, account_id, {"clone_history": 5})
    monkeypatch.setattr("app.telegram_client.jobs.CLONE_BATCH", 2)

    assert await clone_backfill_tick(client, rule) == "progress"
    assert [text for _, text in client.sent] == ["пост 1", "пост 2"]
    assert (await _db_filters(rule.id))["clone_ids"] == [3, 4, 5]
    assert await clone_backfill_tick(client, rule) == "progress"
    assert await clone_backfill_tick(client, rule) == "done"
    assert [text for _, text in client.sent] == [f"пост {mid}" for mid in range(1, 6)]


async def test_missing_and_service_posts_do_not_wedge_queue(create_user, create_account):
    user_id = await create_user()
    account_id = await create_account(user_id)
    client = HistoryClient({1: "раз", 3: "три"})
    rule = await _clone_rule(
        user_id, account_id,
        {"clone_history": 5, "clone_listed": True, "clone_ids": [1, 2, 3]},
    )
    assert await clone_backfill_tick(client, rule) == "done"
    assert [text for _, text in client.sent] == ["раз", "три"]


async def test_zero_history_finishes_without_fetch(create_user, create_account):
    user_id = await create_user()
    account_id = await create_account(user_id)
    client = HistoryClient({1: "раз"})
    rule = await _clone_rule(user_id, account_id, {"clone_history": 0})
    assert await clone_backfill_tick(client, rule) == "done"
    assert client.sent == [] and client.list_calls == 0
    assert (await _db_filters(rule.id))["clone_done"] is True


async def test_floodwait_keeps_consumed_prefix(create_user, create_account, monkeypatch):
    user_id = await create_user()
    account_id = await create_account(user_id)
    calls = {"n": 0}

    class FlakyClient(HistoryClient):
        async def send_message(self, chat_id: int, text: str, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise FloodWaitError(None, capture=30)
            return await super().send_message(chat_id, text, **kwargs)

    client = FlakyClient({mid: f"пост {mid}" for mid in range(1, 5)})
    rule = await _clone_rule(
        user_id, account_id,
        {"clone_history": 4, "clone_listed": True, "clone_ids": [1, 2, 3, 4]},
    )
    with pytest.raises(FloodWaitError):
        await clone_backfill_tick(client, rule)
    # Первый ушёл и в очередь не вернулся, остальные ждут повтора.
    assert [text for _, text in client.sent] == ["пост 1"]
    assert (await _db_filters(rule.id))["clone_ids"] == [2, 3, 4]
    assert await clone_backfill_tick(client, rule) == "done"
    assert [text for _, text in client.sent] == [f"пост {mid}" for mid in range(1, 5)]


async def test_clone_tick_honours_floodwait_pause(create_user, create_account, monkeypatch):
    user_id = await create_user()
    account_id = await create_account(user_id)
    client = HistoryClient({1: "раз"}, fail_on={-200})
    rule = await _clone_rule(user_id, account_id, {"clone_history": 1})
    manager._clone_rules = [rule]
    manager._clients[account_id] = client

    async def subscribed(uid):
        return True

    monkeypatch.setattr(manager_module, "subscription_active", subscribed)
    try:
        await manager._clone_tick()
        assert manager._clone_state[rule.id]["not_before"] > 0
        # Пока пауза не вышла — повторных попыток нет.
        await manager._clone_tick()
        assert client.sent == []
    finally:
        manager._clients.pop(account_id, None)


async def test_live_post_flows_as_copy(create_user, create_account, monkeypatch):
    from app.telegram_client import forwarder

    user_id = await create_user()
    account_id = await create_account(user_id)
    rule = await _clone_rule(
        user_id, account_id, {"clone_history": 0, "clone_done": True}
    )

    async def subscribed(uid):
        return True

    monkeypatch.setattr(forwarder, "subscription_active", subscribed)
    client = HistoryClient({})
    message = SimpleNamespace(id=99, message="свежий пост", media=None)
    await forwarder.deliver(client, message, rule)
    assert client.sent == [(-200, "свежий пост")]


def test_clone_title_shows_backfill_progress():
    rule = SimpleNamespace(
        kind="clone", source_title="src", target_title="dst",
        filters={"clone_history": 50, "clone_listed": True, "clone_ids": [1, 2]},
    )
    assert task_title(rule) == "Клон: src → dst · история 48/50"
    rule.filters["clone_done"] = True
    assert task_title(rule) == "Клон: src → dst"


@pytest.fixture
def login_open(monkeypatch):
    """Вход аккаунтов в тесте включён: без него создание задачи — 503."""
    from app import accounts_login

    monkeypatch.setattr(accounts_login, "require_enabled", lambda: None)


@pytest.fixture
def chats_resolved(monkeypatch):
    """Кабинет «находит» любые ссылки: каждой — свой id."""
    async def fake_resolve_many(account_id: int, queries):
        refs = [str(raw or "").strip() for raw in queries]
        return {ref: (-1000 - pos, ref) for pos, ref in enumerate(refs) if ref}

    monkeypatch.setattr(manager, "resolve_many", fake_resolve_many)


async def test_api_clone_roundtrip(
    client, auth_headers, create_account, login_open, chats_resolved
):
    from tests.helpers import TEST_USER_ID

    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    payload = {
        "command": "clone", "account_id": account_id,
        "source": "@src", "target": "@dst", "history": 30,
    }
    resp = await client.post("/api/tasks", json=payload, headers=auth_headers)
    assert resp.status == 201, await resp.text()
    task = (await resp.json())["task"]
    assert task["title"].startswith("Клон:")
    assert task["edit"]["history"] == 30
    assert task["clone_done"] is False
    # Просьбу «тысячу постов» тихо ужимаем до потолка.
    big = dict(payload, history=1000)
    resp = await client.post("/api/tasks", json=big, headers=auth_headers)
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["task"]["edit"]["history"] == 500
