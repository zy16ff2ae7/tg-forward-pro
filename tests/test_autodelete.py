"""Автоудаление: отправленное живёт часы из настройки — и сносится.

Время сноса лежит в БД, а не в памяти: перезапуск уборщика не обнуляет.
Уборщик ходит кругом постера (раз в 20 секунд) — точности минутной ему
за глаза хватает при жизни поста в часах.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from telethon.errors import ChannelPrivateError, MessageIdInvalidError

from app.db import repo
from app.db.database import session_scope
from app.telegram_client import forwarder, jobs
from app.telegram_client.filters import autodelete_hours
from app.telegram_client.manager import manager
from tests.test_pin import _forward_rule


class DeleteClient:
    """Клиент со сносом: помнит, что снесли, умеет ломаться по заказу."""

    def __init__(self, *, error: BaseException | None = None, connected: bool = True) -> None:
        self.deleted: list[tuple[int, int]] = []
        self.error = error
        self.connected = connected

    def is_connected(self) -> bool:
        return self.connected

    async def send_message(self, chat_id: int, text: str, **kwargs):
        return SimpleNamespace(id=500)

    async def delete_messages(self, chat_id: int, msg_id: int, **kwargs):
        if self.error is not None:
            raise self.error
        self.deleted.append((chat_id, msg_id))


@pytest.fixture
def clean_autodelete():
    yield
    manager._clients.clear()


async def _schedule(user_id, account_id, *, hours_ago=2, rule_id=7):
    async with session_scope() as session:
        return await repo.schedule_delete(
            session,
            rule_id=rule_id,
            user_id=user_id,
            account_id=account_id,
            chat_id=-200,
            msg_id=500,
            delete_at=repo.utcnow() - timedelta(hours=hours_ago),
        )


async def _rows():
    async with session_scope() as session:
        return await repo.due_deletes(session, repo.utcnow() + timedelta(days=1))


# ───────────────────────────── часы из настройки ─────────────────────────────


def test_zero_means_keep_forever():
    assert autodelete_hours(SimpleNamespace()) == 0
    assert autodelete_hours(SimpleNamespace(autodelete_hours=0)) == 0
    assert autodelete_hours(SimpleNamespace(autodelete_hours=-5)) == 0
    assert autodelete_hours(SimpleNamespace(autodelete_hours="ерунда")) == 0


def test_hours_pass_through_capped_at_month():
    assert autodelete_hours(SimpleNamespace(autodelete_hours=2.5)) == 2.5
    assert autodelete_hours(SimpleNamespace(autodelete_hours=100000)) == 24 * 30


# ─────────────────────────────── уборщик сносит ───────────────────────────────


async def test_sweeper_deletes_due(create_user, create_account, clean_autodelete):
    """Время вышло — сообщение снесено, строка убрана."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    await _schedule(user_id, account_id)
    client = DeleteClient()
    manager._clients[account_id] = client

    await manager._autodelete_tick()

    assert client.deleted == [(-200, 500)]
    assert await _rows() == []


async def test_sweeper_skips_not_due(create_user, create_account, clean_autodelete):
    """Время не вышло — не трогаем, строка ждёт."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        await repo.schedule_delete(
            session, rule_id=7, user_id=user_id, account_id=account_id,
            chat_id=-200, msg_id=500,
            delete_at=repo.utcnow() + timedelta(hours=5),
        )
    client = DeleteClient()
    manager._clients[account_id] = client

    await manager._autodelete_tick()

    assert client.deleted == []
    assert len(await _rows()) == 1


async def test_sweeper_waits_for_offline_account(create_user, create_account, clean_autodelete):
    """Аккаунта нет на связи — строка не сгорает, попробуем следующим кругом."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    await _schedule(user_id, account_id)

    await manager._autodelete_tick()

    assert len(await _rows()) == 1


async def test_gone_message_drops_row(create_user, create_account, clean_autodelete):
    """Сообщения уже нет — горевать не о чем, строку убираем сразу."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    await _schedule(user_id, account_id)
    manager._clients[account_id] = DeleteClient(error=MessageIdInvalidError(None))

    await manager._autodelete_tick()

    assert await _rows() == []


async def test_dead_chat_drops_row(create_user, create_account, clean_autodelete):
    """Чат умер — долбить снос нечего, строку убираем."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    await _schedule(user_id, account_id)
    manager._clients[account_id] = DeleteClient(error=ChannelPrivateError(None))

    await manager._autodelete_tick()

    assert await _rows() == []


async def test_transient_error_retries_then_gives_up(
    create_user, create_account, clean_autodelete, monkeypatch
):
    """Сеть упала — считаем попытки; исчерпали — сдаёмся и убираем строку."""
    monkeypatch.setattr(jobs, "AUTODELETE_ATTEMPTS", 2)
    user_id = await create_user()
    account_id = await create_account(user_id)
    delete_id = await _schedule(user_id, account_id)
    client = DeleteClient(error=ConnectionError("сеть"))
    manager._clients[account_id] = client

    await manager._autodelete_tick()
    assert len(await _rows()) == 1
    async with session_scope() as session:
        assert (await session.get(repo.ScheduledDelete, delete_id)).attempts == 1

    client.error = None
    await manager._autodelete_tick()
    assert client.deleted == [(-200, 500)]
    assert await _rows() == []


async def test_attempts_cap_drops_row(
    create_user, create_account, clean_autodelete, monkeypatch
):
    """Десять неудач подряд — строка мёртвая, дальше не долбим."""
    monkeypatch.setattr(jobs, "AUTODELETE_ATTEMPTS", 2)
    user_id = await create_user()
    account_id = await create_account(user_id)
    await _schedule(user_id, account_id)
    manager._clients[account_id] = DeleteClient(error=ConnectionError("сеть"))

    await manager._autodelete_tick()
    await manager._autodelete_tick()

    assert await _rows() == []


# ─────────────────────────── отправки планируют снос ──────────────────────────


async def test_forward_schedules_delete(create_user, create_account, monkeypatch):
    """Пересылка с часами жизни оставляет строку сноса."""
    from app.telegram_client import forwarder as fwd

    async def subscribed(uid: int) -> bool:
        return True

    monkeypatch.setattr(fwd, "subscription_active", subscribed)
    user_id = await create_user()
    rule = await _forward_rule(
        user_id, await create_account(user_id), {"autodelete_hours": 2}
    )

    await fwd.deliver(
        DeleteClient(), SimpleNamespace(id=9, message="пост", media=None), rule
    )

    rows = await _rows()
    assert len(rows) == 1
    assert (rows[0].chat_id, rows[0].msg_id) == (-200, 500)
    assert rows[0].delete_at > repo.utcnow() + timedelta(hours=1)


async def test_forward_without_hours_schedules_nothing(
    create_user, create_account, monkeypatch
):
    """Без часов жизни строк сноса нет."""
    from app.telegram_client import forwarder as fwd

    async def subscribed(uid: int) -> bool:
        return True

    monkeypatch.setattr(fwd, "subscription_active", subscribed)
    user_id = await create_user()
    rule = await _forward_rule(user_id, await create_account(user_id), {})

    await fwd.deliver(
        DeleteClient(), SimpleNamespace(id=9, message="пост", media=None), rule
    )

    assert await _rows() == []


async def test_mailing_tick_schedules_delete(create_user, create_account, clean_autodelete):
    """Рассылка с часами жизни: отправила — запланировала снос."""
    from tests.test_mailing import make_mailing

    _rule_id, _user_id, account_id = await make_mailing(
        create_user, create_account,
        targets=[-1001], texts=["всем привет"], autodelete_hours=3,
    )
    manager._clients[account_id] = DeleteClient()
    try:
        await manager._mailing_tick()
    finally:
        manager._mailing_rules = []
        manager._mailing_state.clear()

    rows = await _rows()
    assert len(rows) == 1
    assert (rows[0].chat_id, rows[0].msg_id) == (-1001, 500)
