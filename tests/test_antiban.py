"""Антибан-пакет: джиттер пауз, дневной лимит и прогрев новичков.

Джиттер только прибавляется — задержка из правила остаётся минимумом.
Лимит считается по реальным отправкам, а новички неделю шлют по нарастающей:
свежий аккаунт с тысячей сообщений в первый день живёт недолго.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog
from app.telegram_client import forwarder
from app.telegram_client.filters import FilterConfig
from app.telegram_client.manager import manager
from app.telegram_client.queue import DeliveryQueue
from app.telegram_client.types import SKIP_DAILY_CAP, RuleSnapshot
from tests.test_pin import _forward_rule


def _aged(days: int):
    return repo.utcnow() - timedelta(days=days)


# ────────────────────────── лимит: свой или прогрев ───────────────────────────


def test_override_wins_over_warmup():
    """Свой лимит из задачи перекрывает прогрев полностью."""
    assert forwarder.send_cap_for(_aged(0), 5) == 5
    assert forwarder.send_cap_for(_aged(30), 2000) == 2000


def test_warmup_ramps_by_age():
    """Новичок — 50, трёхдневка — 150, неделя — 400, дальше — 1000."""
    assert forwarder.send_cap_for(_aged(0)) == 50
    assert forwarder.send_cap_for(_aged(1)) == 50
    assert forwarder.send_cap_for(_aged(2)) == 150
    assert forwarder.send_cap_for(_aged(5)) == 400
    assert forwarder.send_cap_for(_aged(8)) == 1000
    assert forwarder.send_cap_for(_aged(365)) == 1000


def test_unknown_age_is_mature():
    """Возраст не знаем (строка без даты) — не душим: полный лимит."""
    assert forwarder.send_cap_for(None) == 1000


# ─────────────────────────────── счётчик отправок ─────────────────────────────


async def test_counter_counts_today(create_user, create_account):
    """Счётчик копится за сутки и читается обратно."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        assert await repo.send_count_today(session, account_id) == 0
        assert await repo.bump_send_count(session, account_id) == 1
        assert await repo.bump_send_count(session, account_id, 4) == 5
        assert await repo.send_count_today(session, account_id) == 5


async def test_counter_purges_yesterday(create_user, create_account):
    """Вчерашняя строка стирается при первой сегодняшней записи."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        session.add(
            repo.SendCounter(
                account_id=account_id,
                day=(repo.utcnow() - timedelta(days=1)).date(),
                count=999,
            )
        )
        await session.flush()
        assert await repo.bump_send_count(session, account_id) == 1
        assert await repo.send_count_today(session, account_id) == 1


# ────────────────────────── джиттер задержки пересылки ────────────────────────


def _snapshot(**filters) -> RuleSnapshot:
    return RuleSnapshot(
        id=1, user_id=100, target_id=200, mode="copy", delay_seconds=5,
        account_id=1, filters=FilterConfig(**filters),
    )


def test_jitter_adds_within_spread():
    """Джиттер 0..10 к задержке 5: всегда от 5 до 15, ниже базы — никогда."""
    rule = _snapshot(delay_jitter=10)
    for _ in range(50):
        assert 5 <= DeliveryQueue._delay_for(rule) <= 15


def test_no_jitter_keeps_exact_delay():
    """Без джиттера задержка точная, как раньше."""
    assert DeliveryQueue._delay_for(_snapshot()) == 5


def test_moderation_kinds_skip_jitter():
    """Мут не ждёт лишнего: джиттер — только для отправки постов."""
    rule = _snapshot(delay_jitter=100)
    rule.kind = "mute"
    assert DeliveryQueue._delay_for(rule) == 5


# ──────────────────────── пересылка встаёт по лимиту ──────────────────────────


class CapClient:
    def __init__(self) -> None:
        self.sent: list[int] = []

    def is_connected(self) -> bool:
        return True

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append(chat_id)
        return SimpleNamespace(id=60)


async def _journal_errors(rule_id: int) -> list[str]:
    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ForwardLog.error).where(
                    ForwardLog.rule_id == rule_id,
                    ForwardLog.status == "error",
                )
            )
        ).scalars().all()
        return [str(error) for error in rows]


async def test_forward_stops_at_cap_and_tells_once(
    create_user, create_account, monkeypatch
):
    """Лимит исчерпан: пропуск с причиной, а в журнале одна строка на день."""
    async def subscribed(uid: int) -> bool:
        return True

    monkeypatch.setattr(forwarder, "subscription_active", subscribed)
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule = await _forward_rule(user_id, account_id, {"daily_cap": 1})
    async with session_scope() as session:
        await repo.bump_send_count(session, account_id)
    client = CapClient()
    message = SimpleNamespace(id=11, message="пост", media=None)

    first = await forwarder.deliver(client, message, rule)
    second = await forwarder.deliver(client, message, rule)

    assert first.reason == SKIP_DAILY_CAP and not first.sent
    assert second.reason == SKIP_DAILY_CAP
    assert client.sent == []
    errors = await _journal_errors(rule.id)
    assert len(errors) == 1
    assert "Дневной лимит" in errors[0]


async def test_forward_counts_send(create_user, create_account, monkeypatch):
    """Удачная отправка прибавляет счётчик аккаунта."""
    async def subscribed(uid: int) -> bool:
        return True

    monkeypatch.setattr(forwarder, "subscription_active", subscribed)
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule = await _forward_rule(user_id, account_id, {})

    await forwarder.deliver(
        CapClient(), SimpleNamespace(id=12, message="пост", media=None), rule
    )

    async with session_scope() as session:
        assert await repo.send_count_today(session, account_id) == 1


# ───────────────────────── рассылка ждёт полуночи ─────────────────────────────


async def test_mailing_stands_until_midnight(create_user, create_account):
    """Рассылка при исчерпанном лимите молчит и ставит паузу до завтра."""
    from tests.test_mailing import FakeClient, make_mailing

    _rule_id, _user_id, account_id = await make_mailing(
        create_user, create_account,
        targets=[-1001], texts=["всем привет"], daily_cap=1,
    )
    async with session_scope() as session:
        await repo.bump_send_count(session, account_id)
    client = FakeClient()
    manager._clients[account_id] = client
    try:
        import time as time_module

        await manager._mailing_tick()
        await manager._mailing_tick()
        states = list(manager._mailing_state.values())
    finally:
        manager._mailing_rules = []
        manager._mailing_state.clear()
        manager._clients.clear()

    assert client.sent == []
    assert len(states) == 1 and states[0]["not_before"] > time_module.time()


async def test_mailing_send_counts(create_user, create_account):
    """Отправка рассылки видна счётчику — лимит общий на аккаунт."""
    from tests.test_mailing import FakeClient, make_mailing

    _rule_id, _user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001], texts=["всем привет"]
    )
    manager._clients[account_id] = FakeClient()
    try:
        await manager._mailing_tick()
    finally:
        manager._mailing_rules = []
        manager._mailing_state.clear()
        manager._clients.clear()

    async with session_scope() as session:
        assert await repo.send_count_today(session, account_id) == 1
