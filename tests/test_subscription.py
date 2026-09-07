"""Абонемент и копилка дней: деньги пользователя, считать надо точно."""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import settings
from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.db.models import Subscription
from app.timeutil import utcnow


@pytest.fixture
async def user_id(create_user) -> int:
    return await create_user(username="tester", full_name="Тестовый Пользователь")


async def _activate(user_id: int, days: int) -> None:
    """Прямая выдача подписки на нужное число дней вперёд.

    Лишний час — чтобы ``int(remaining_days)`` внутри bank_days давал ровно
    ``days``, а не срезал сутки из-за микросекунд между записью и проверкой.
    """
    async with session_scope() as session:
        await repo.activate_subscription(session, user_id, months=1)
        sub = await session.get(Subscription, user_id)
        sub.active_until = utcnow() + timedelta(days=days, hours=1)
        sub.banked_days = 0


def _days_left(until) -> int:
    """Полных дней до даты. Округление — иначе «почти 2 дня» даёт 1 из-за микросекунд."""
    return round((until - utcnow()).total_seconds() / 86400)


async def _read(user_id: int):
    """Читает состояние подписки в новой сессии — так видно, что записано на самом деле."""
    async with SessionLocal() as session:
        sub = await repo.get_subscription(session, user_id)
        return sub.active_until, sub.banked_days


async def test_activate_subscription_creates_and_extends(user_id):
    async with session_scope() as session:
        first = await repo.activate_subscription(session, user_id, months=1)
        second = await repo.activate_subscription(session, user_id, months=1)

    assert second - first == timedelta(days=30)


async def test_activate_subscription_extends_from_future_date(user_id):
    """Продление действующего абонемента считается от его конца, а не от сегодня."""
    async with session_scope() as session:
        await repo.activate_subscription(session, user_id, months=1)
        sub = await session.get(Subscription, user_id)
        sub.active_until = utcnow() + timedelta(days=100)
        await session.flush()

    async with session_scope() as session:
        until = await repo.activate_subscription(session, user_id, months=1)

    assert _days_left(until) == 130


async def test_grant_trial_only_once(user_id, monkeypatch):
    # Рубильник TRIAL_DAYS в продукте выключен — механику проверяем
    # с явно включённым.
    monkeypatch.setattr(settings, "trial_days", 3)
    async with session_scope() as session:
        first = await repo.grant_trial(session, user_id)
        second = await repo.grant_trial(session, user_id)

    assert first is not None
    assert _days_left(first) == 3
    assert second is None


async def test_grant_trial_disabled_by_default(user_id):
    """Пробного «просто так» нет: при TRIAL_DAYS=0 выдача — no-op."""
    async with session_scope() as session:
        assert await repo.grant_trial(session, user_id) is None
        assert await session.get(Subscription, user_id) is None


async def test_bank_days_moves_days_to_piggy_bank(user_id):
    await _activate(user_id, 10)
    async with session_scope() as session:
        moved = await repo.bank_days(session, user_id, 4)

    until, banked = await _read(user_id)
    assert moved == 4
    assert banked == 4
    assert _days_left(until) == 6


async def test_bank_days_never_freezes_last_day(user_id):
    """Иначе абонемент выключался бы в сам момент заморозки."""
    await _activate(user_id, 10)
    async with session_scope() as session:
        moved = await repo.bank_days(session, user_id, 50)

    _, banked = await _read(user_id)
    assert moved == 9
    assert banked == 9


async def test_bank_days_rejects_non_positive(user_id):
    await _activate(user_id, 10)
    async with session_scope() as session:
        assert await repo.bank_days(session, user_id, 0) == 0
        assert await repo.bank_days(session, user_id, -3) == 0

    _, banked = await _read(user_id)
    assert banked == 0


async def test_bank_days_on_expired_subscription_is_noop(user_id):
    await _activate(user_id, 1)
    async with session_scope() as session:
        sub = await session.get(Subscription, user_id)
        sub.active_until = utcnow() - timedelta(days=1)
        await session.flush()

    async with session_scope() as session:
        assert await repo.bank_days(session, user_id, 5) == 0

    _, banked = await _read(user_id)
    assert banked == 0


async def test_bank_days_unknown_user_is_noop():
    async with session_scope() as session:
        assert await repo.bank_days(session, 42, 5) == 0


async def test_round_trip_bank_then_unbank(user_id):
    await _activate(user_id, 10)
    async with session_scope() as session:
        moved = await repo.bank_days(session, user_id, 6)
    async with session_scope() as session:
        back = await repo.unbank_days(session, user_id, 6)

    until, banked = await _read(user_id)
    assert moved == back == 6
    assert banked == 0
    assert _days_left(until) == 10


async def test_unbank_all_when_days_not_positive(user_id):
    await _activate(user_id, 5)
    async with session_scope() as session:
        await repo.bank_days(session, user_id, 4)
    async with session_scope() as session:
        back = await repo.unbank_days(session, user_id, 0)

    _, banked = await _read(user_id)
    assert back == 4
    assert banked == 0


async def test_unbank_caps_at_banked_amount(user_id):
    await _activate(user_id, 5)
    async with session_scope() as session:
        await repo.bank_days(session, user_id, 4)
    async with session_scope() as session:
        back = await repo.unbank_days(session, user_id, 99)

    _, banked = await _read(user_id)
    assert back == 4
    assert banked == 0


async def test_unbank_extends_from_today_when_subscription_expired(user_id):
    """Истёкший абонемент продлевается от сейчас, а не от даты в прошлом."""
    await _activate(user_id, 10)
    async with session_scope() as session:
        await repo.bank_days(session, user_id, 9)

    async with session_scope() as session:
        sub = await session.get(Subscription, user_id)
        sub.active_until = utcnow() - timedelta(days=3)
        await session.flush()

    async with session_scope() as session:
        await repo.unbank_days(session, user_id, 2)

    until, _ = await _read(user_id)
    assert _days_left(until) == 2


async def test_has_active_subscription_after_expiry(user_id):
    await _activate(user_id, 1)
    async with session_scope() as session:
        sub = await session.get(Subscription, user_id)
        sub.active_until = utcnow() - timedelta(seconds=1)
        await session.flush()

    async with SessionLocal() as session:
        assert await repo.has_active_subscription(session, user_id) is False
        assert await repo.subscription_until(session, user_id) is None
