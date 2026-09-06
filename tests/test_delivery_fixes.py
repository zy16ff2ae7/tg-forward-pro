"""Доставка без потерь: гейт абонемента, FloodWait в рассылке, парсер и лимит.

* Ручной запуск без подписки упирается в абонемент — до проверки аккаунта.
* FloodWait из рассылки «в несколько чатов» уходит на повтор в очередь,
  а не глотается с потерей оставшихся чатов.
* Парсер при FloodWait сохраняет уже собранное, а не выбрасывает.
* Больше лимита записей на правило не хранится — таблица не растёт бесконечно.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError

from app.db import repo
from app.db.database import session_scope
from app.db.models import Rule
from app.telegram_client import jobs
from app.telegram_client.manager import manager
from app.telegram_client.types import RuleSnapshot
from tests.test_mailing import clean_manager  # noqa: F401
from tests.test_oneshot_journal import person  # noqa: F401 — участник чата


async def _db_rule(create_user, create_account, *, kind="parser", with_trial=False) -> Rule:
    user_id = await create_user()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        if with_trial:
            await repo.grant_trial(session, user_id)
        rule = Rule(
            user_id=user_id,
            account_id=account_id,
            source_id=-100100,
            source_title="Источник",
            target_id=-100200,
            target_title="Приёмник",
            kind=kind,
            enabled=True,
        )
        session.add(rule)
        await session.flush()
        await session.refresh(rule)
        return rule


def _snapshot(rule: Rule, **kwargs) -> RuleSnapshot:
    params = {
        "id": rule.id,
        "user_id": rule.user_id,
        "target_id": rule.target_id,
        "account_id": rule.account_id,
        "mode": "copy",
        "delay_seconds": 0,
        "kind": rule.kind,
        "source_id": rule.source_id,
    }
    params.update(kwargs)
    return RuleSnapshot(**params)


async def test_run_without_subscription_is_blocked(create_user, create_account):
    rule = await _db_rule(create_user, create_account)
    result = await manager.run_task_now(rule)
    assert result["ok"] is False
    assert result["need_subscription"] is True
    assert "абонемент" in result["error"]


async def test_broadcast_floodwait_propagates_to_queue(create_user, create_account):
    """FloodWait из рассылки долетает до очереди — она подождёт и повторит всё."""
    rule = await _db_rule(create_user, create_account, kind="broadcast", with_trial=True)

    async def boom(*args, **kwargs):
        raise FloodWaitError(None, capture=30)

    client = SimpleNamespace(send_message=boom)
    message = SimpleNamespace(id=7, message="пост", media=None, action=None)
    with pytest.raises(FloodWaitError):
        await jobs.run_job(client, message, _snapshot(rule))


class PartialClient:
    """Отдаёт двух участников и падает FloodWait — как живой чат под нагрузкой."""

    def __init__(self):
        self.calls = 0

    def iter_participants(self, chat_id, limit: int = 0, **kwargs):
        async def walk():
            yield person(501, "Первый")
            yield person(502, "Второй")
            raise FloodWaitError(None, capture=30)

        return walk()


async def test_parser_keeps_partial_results(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    result = await jobs.run_parser(PartialClient(), _snapshot(rule))
    assert result["ok"] is False
    assert result["collected"] == 2
    assert result["partial"] is True
    async with session_scope() as session:
        assert await repo.count_collected_items(session, rule.id) == 2


async def test_store_caps_results_per_rule(create_user, create_account, monkeypatch):
    monkeypatch.setattr(jobs, "MAX_PARSER_LIMIT", 5)
    rule = await _db_rule(create_user, create_account, with_trial=True)
    async with session_scope() as session:
        await repo.add_collected_items(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            kind="parser",
            payloads=[{"user_id": 100 + i} for i in range(4)],
        )
        await session.commit()

    async def walk():
        for uid in (201, 202, 203):
            yield person(uid)

    async def nobody():
        if False:
            yield None

    # Запрос списка админов (с filter) — пустой: иначе все трое посчитаются
    # админами и фильтр «не брать админов» отсечёт их раньше лимита.
    client = SimpleNamespace(
        iter_participants=lambda chat_id, limit=0, **kwargs: (
            nobody() if "filter" in kwargs else walk()
        )
    )
    result = await jobs.run_parser(client, _snapshot(rule))
    assert result["ok"] is True
    assert result["collected"] == 1
    assert result["capped"] is True
    async with session_scope() as session:
        assert await repo.count_collected_items(session, rule.id) == 5
