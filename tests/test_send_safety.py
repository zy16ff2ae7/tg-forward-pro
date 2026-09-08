"""Общая безопасность отправок: темп, потолки, живые тексты.

Три рассылки на одном номере больше не дают три сообщения в секунду (общий
слот аккаунта), дневной лимит не снимается одной цифрой, а тексты не идут
байт-в-байт (спинтакс, случайный выбор, перевод и уникализация у рассылки).
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import pytest

from app.db import repo
from app.db.database import session_scope
from app.db.models import SendCounter
from app.telegram_client.filters import FilterConfig, spin_text, transform_text
from app.telegram_client.forwarder import check_send_cap
from app.telegram_client.manager import manager
from app.telegram_client.types import RuleSnapshot
from tests.helpers import TEST_USER_ID
from tests.test_mailing import (  # noqa: F401
    FakeClient,
    clean_manager,
    login_open,
    make_mailing,
    no_pauses,
    resolved_chats,
)


@pytest.fixture(autouse=True)
def _clean_gate():
    yield
    manager._last_send_at.clear()


# ────────────────────────── общий слот аккаунта ──────────────────────────


async def test_send_slot_spaces_sends():
    """Два слота подряд — второй ждёт паузу, а не влетает следом."""
    manager._last_send_at.clear()
    started = time.monotonic()
    async with manager.account_send_slot(4242, gap=0.05):
        pass
    async with manager.account_send_slot(4242, gap=0.05):
        pass
    assert time.monotonic() - started >= 0.05


async def test_send_slot_serializes_concurrent():
    """Два отправителя в одну секунду уходят друг за другом через паузу."""
    manager._last_send_at.clear()
    order: list[str] = []

    async def sender(name: str):
        async with manager.account_send_slot(4343, gap=0.05):
            order.append(name)

    started = time.monotonic()
    await asyncio.gather(sender("a"), sender("b"))
    assert sorted(order) == ["a", "b"]
    assert time.monotonic() - started >= 0.05


async def test_mailing_tick_marks_the_ledger(create_user, create_account, no_pauses):
    """Тик рассылки отмечается в журнале темпа — слот реально используется."""
    _, _, account_id = await make_mailing(
        create_user, create_account, targets=[-1001], texts=["всем привет"]
    )
    manager._clients[account_id] = FakeClient()
    manager._last_send_at.clear()

    await manager._mailing_tick()

    assert account_id in manager._last_send_at


# ─────────────────────────────── живые тексты ───────────────────────────────


def test_spintax_picks_variants():
    """{a|b} тасуется: за 20 показов видно оба варианта."""
    seen = {spin_text("{первое|второе}") for _ in range(20)}
    assert seen == {"первое", "второе"}
    assert spin_text("без скобок") == "без скобок"


def test_transform_applies_spintax():
    """Спинтакс раскрывается в общем конвейере текста."""
    conf = FilterConfig.from_dict({})
    seen = {transform_text("{a|b}!", conf) for _ in range(20)}
    assert seen == {"a!", "b!"}


def test_random_pick_default_true_explicit_false_kept():
    """По умолчанию — наугад; явный False из старых задач уважается."""
    assert FilterConfig.from_dict({}).random_pick is True
    assert FilterConfig.from_dict({"random_pick": False}).random_pick is False


async def test_create_defaults_random_pick_true(
    client, auth_headers, create_account, login_open, resolved_chats
):
    """Форма без галочки — сервер включает «наугад» сам."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    response = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@a"],
            "message": "раз",
            "gap": 70,
        },
        headers=auth_headers,
    )
    assert response.status == 201, await response.text()
    assert (await response.json())["task"]["edit"]["random_pick"] is True


async def test_translate_uniquify_opened_for_mailing(
    client, auth_headers, create_account, login_open, resolved_chats
):
    """Рассылке доступны перевод и уникализация — как вееру."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    response = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@a"],
            "message": "раз",
            "gap": 70,
            "translate_to": "en",
            "uniquify": True,
        },
        headers=auth_headers,
    )
    assert response.status == 201, await response.text()
    edit = (await response.json())["task"]["edit"]
    assert edit["translate_to"] == "en"
    assert edit["uniquify"] is True


# ─────────────────────────── потолки и активность ───────────────────────────


async def test_daily_cap_clamped_at_create(
    client, auth_headers, create_account, login_open, resolved_chats
):
    """Цифра 999999 на записи жмётся к потолку — предохранитель не снять."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    response = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@a"],
            "message": "раз",
            "gap": 70,
            "daily_cap": 999999,
        },
        headers=auth_headers,
    )
    assert response.status == 201, await response.text()
    assert (await response.json())["task"]["edit"]["daily_cap"] == 1000


def _snapshot(rule_id: int, user_id: int, account_id: int, kind: str) -> RuleSnapshot:
    return RuleSnapshot(
        id=rule_id, user_id=user_id, target_id=-1, account_id=account_id,
        mode="copy", delay_seconds=0, kind=kind,
    )


async def test_quiet_veteran_capped_by_activity(create_user, create_account):
    """Ветеран по дате, но тихий по жизни — жмётся к новичку."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        from app.db.models import TelegramAccount

        account = await session.get(TelegramAccount, account_id)
        assert account is not None
        account.created_at = repo.utcnow() - timedelta(days=30)
        await session.commit()

    hit, used, cap = await check_send_cap(_snapshot(1, user_id, account_id, "forward"))

    assert (hit, used, cap) == (False, 0, 50)


async def test_working_veteran_keeps_full_cap(create_user, create_account):
    """Ветеран, который реально слал, — полный лимит, активность не душит."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        from app.db.models import TelegramAccount

        account = await session.get(TelegramAccount, account_id)
        assert account is not None
        account.created_at = repo.utcnow() - timedelta(days=30)
        session.add(SendCounter(
            account_id=account_id,
            day=(repo.utcnow() - timedelta(days=1)).date(),
            count=500,
        ))
        await session.commit()

    hit, used, cap = await check_send_cap(_snapshot(1, user_id, account_id, "forward"))

    assert (hit, used, cap) == (False, 0, 1000)


# ─────────────────────────── живучесть кругов ───────────────────────────


class FlakyChatClient(FakeClient):
    """Один чат всегда ломается небезнадёжно — остальные принимают."""

    def __init__(self, dead_chat: int) -> None:
        super().__init__()
        self.dead_chat = dead_chat

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if chat_id == self.dead_chat:
            raise RuntimeError("сеть моргнула")
        return await super().send_message(chat_id, text, **kwargs)


async def test_stuck_chat_skipped_after_three_strikes(
    create_user, create_account, no_pauses
):
    """«Полуживой» чат не держит рассылку вечно: три сбоя — пропуск."""
    from tests.test_task_health import logged

    rule_id, _, account_id = await make_mailing(
        create_user, create_account, targets=[-1001, -1002], texts=["всем привет"]
    )
    client = FlakyChatClient(dead_chat=-1001)
    manager._clients[account_id] = client

    for _ in range(3):
        await manager._mailing_tick()
        manager._mailing_state[rule_id]["not_before"] = 0.0
    await manager._mailing_tick()

    assert -1002 in client.recipients, "после трёх сбоев круг пошёл дальше"
    journal = await logged(rule_id)
    assert any("пропускаем" in line for _, line in journal)


class FanClient:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append(chat_id)
        from types import SimpleNamespace as NS

        return NS(id=len(self.sent))


async def test_broadcast_stops_at_cap_mid_round(create_user, create_account):
    """Веер встаёт ровно на лимите, а не перелетает его на размер веера."""
    from app.db.models import TelegramAccount

    user_id = await create_user()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        account = await session.get(TelegramAccount, account_id)
        assert account is not None
        account.created_at = repo.utcnow() - timedelta(days=30)
        session.add(SendCounter(
            account_id=account_id,
            day=(repo.utcnow() - timedelta(days=1)).date(),
            count=500,
        ))
        await session.commit()
    async with session_scope() as session:
        from app.db.models import Rule

        rule = Rule(
            user_id=user_id, account_id=account_id, source_id=-100,
            target_id=-1, kind="broadcast", enabled=True,
            filters={"targets": [-2, -3], "daily_cap": 2},
        )
        session.add(rule)
        await session.flush()
        rule_id = rule.id

    from app.telegram_client import jobs
    from tests.test_task_health import logged
    from types import SimpleNamespace as NS

    snapshot = _snapshot(rule_id, user_id, account_id, "broadcast")
    snapshot.filters = FilterConfig.from_dict({"targets": [-2, -3], "daily_cap": 2})
    client = FanClient()
    await jobs._broadcast(client, NS(id=7, message="пост", media=None), snapshot)

    assert client.sent == [-1, -2], "третий чат за лимитом — не ушёл"
    journal = await logged(rule_id)
    assert any("исчерпан" in line for _, line in journal)


async def test_streaming_autosubscribe_dedups_attempts(create_user, create_account, monkeypatch):
    """Одни ссылки при каждом посте: второй заход в тот же день — мимо."""
    from app.telegram_client import jobs
    from tests.test_oneshot_journal import OneShotClient
    from types import SimpleNamespace as NS

    monkeypatch.setattr(jobs, "JOIN_MIN_GAP", 0)
    user_id = await create_user()
    account_id = await create_account(user_id)
    snapshot = _snapshot(1201, user_id, account_id, "autosubscribe")
    snapshot.filters = FilterConfig.from_dict({"join_gap": 0, "daily_join_limit": 0})
    message = NS(id=9, message="вступайте https://t.me/somechannel", media=None)
    client = OneShotClient()

    await jobs._autosubscribe(client, message, snapshot)
    await jobs._autosubscribe(client, message, snapshot)

    assert client.tried == ["somechannel"]
