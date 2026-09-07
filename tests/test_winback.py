"""Возврат ушедших: «последний день» и письмо с промокодом через неделю.

Цепочка конца срока: «скоро конец» → «последний день» → «кончился» →
через неделю личный промокод на −5%. Проверяем:

* «последний день» уходит раз и только после первого письма;
* продление снимает все метки цепочки;
* возврат приходит с личным кодом, код рабочий;
* без задач и с замороженными днями писем нет;
* нулевой процент выключает возврат целиком.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import update

from app import promocode
from app.config import settings
from app.db import repo
from app.db.database import session_scope
from app.db.models import Rule, Subscription
from app.main import notify_last_day, notify_winback
from app.timeutil import utcnow


class FakeBot:
    def __init__(self) -> None:
        self.dms: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.dms.append((chat_id, text))


async def _sub(user_id: int, **fields):
    fields.setdefault("active_until", utcnow() + timedelta(days=30))
    async with session_scope() as session:
        session.add(Subscription(user_id=user_id, **fields))
        await session.commit()


async def _rule(user_id: int, create_account):
    account_id = await create_account(user_id)
    async with session_scope() as session:
        session.add(Rule(
            user_id=user_id, account_id=account_id,
            source_id=-100, target_id=-200,
        ))
        await session.commit()


async def _marks(user_id: int):
    async with session_scope() as session:
        sub = await session.get(Subscription, user_id)
        return (
            sub.reminded_at is not None,
            sub.lastday_notified_at is not None,
            sub.expired_notified_at is not None,
            sub.winback_notified_at is not None,
        )


async def test_last_day_sends_once_after_first_letter(create_user):
    """«Последний день»: раз, после первого письма, повтор — тихо."""
    user_id = await create_user()
    now = utcnow()
    await _sub(user_id, active_until=now + timedelta(hours=12), reminded_at=now)
    bot = FakeBot()
    await notify_last_day(bot)
    await notify_last_day(bot)
    assert len(bot.dms) == 1
    assert "Последний день" in bot.dms[0][1]
    assert await _marks(user_id) == (True, True, False, False)


async def test_last_day_skips_without_first_letter(create_user):
    """Без первого письма второго нет: цепочка по порядку."""
    user_id = await create_user()
    await _sub(user_id, active_until=utcnow() + timedelta(hours=12))
    bot = FakeBot()
    await notify_last_day(bot)
    assert bot.dms == []
    assert await _marks(user_id) == (False, False, False, False)


async def test_renewal_resets_the_chain(create_user):
    """Продление снимает все метки: следующему концу — новая цепочка."""
    user_id = await create_user()
    now = utcnow()
    await _sub(
        user_id, active_until=now - timedelta(days=8),
        reminded_at=now, lastday_notified_at=now,
        expired_notified_at=now, winback_notified_at=now,
    )
    async with session_scope() as session:
        await repo.activate_subscription(session, user_id, 1)
        await session.commit()
    assert await _marks(user_id) == (False, False, False, False)


async def test_winback_brings_a_working_code(create_user, create_account):
    """Возврат: личный код на −5%, код активируется."""
    user_id = await create_user()
    await _rule(user_id, create_account)
    await _rule(user_id, create_account)
    now = utcnow()
    await _sub(
        user_id, active_until=now - timedelta(days=8), expired_notified_at=now
    )
    bot = FakeBot()
    await notify_winback(bot)
    assert len(bot.dms) == 1
    assert "задач — 2" in bot.dms[0][1] and "−5%" in bot.dms[0][1]
    async with session_scope() as session:
        codes = await repo.owner_discount_codes(session, user_id)
        assert len(codes) == 1 and codes[0].code in bot.dms[0][1]
        outcome = await promocode.redeem(session, user_id, codes[0].code)
        await session.commit()
        assert outcome.granted
    assert await _marks(user_id) == (False, False, True, True)


async def test_winback_skips_without_tasks(create_user):
    """Без стоящих задач звать не с чем: метка — да, письма — нет."""
    user_id = await create_user()
    now = utcnow()
    await _sub(
        user_id, active_until=now - timedelta(days=8), expired_notified_at=now
    )
    bot = FakeBot()
    await notify_winback(bot)
    assert bot.dms == []
    assert await _marks(user_id) == (False, False, True, True)


async def test_winback_skips_banked_pause(create_user, create_account):
    """Замороженные дни — пауза, а не уход: возврат не дёргает."""
    user_id = await create_user()
    await _rule(user_id, create_account)
    now = utcnow()
    await _sub(
        user_id, active_until=now - timedelta(days=8),
        expired_notified_at=now, banked_days=5,
    )
    bot = FakeBot()
    await notify_winback(bot)
    assert bot.dms == []
    assert await _marks(user_id) == (False, False, True, True)


async def test_winback_off_at_zero_percent(create_user, create_account, monkeypatch):
    """Нулевой процент — возврата нет: без подарка это спам."""
    monkeypatch.setattr(settings, "winback_percent", 0)
    user_id = await create_user()
    await _rule(user_id, create_account)
    now = utcnow()
    await _sub(
        user_id, active_until=now - timedelta(days=8), expired_notified_at=now
    )
    bot = FakeBot()
    await notify_winback(bot)
    assert bot.dms == []
    async with session_scope() as session:
        assert await repo.owner_discount_codes(session, user_id) == []


async def test_winback_waits_a_week(create_user, create_account):
    """Кончился два дня назад — возврату рано."""
    user_id = await create_user()
    await _rule(user_id, create_account)
    now = utcnow()
    await _sub(
        user_id, active_until=now - timedelta(days=2), expired_notified_at=now
    )
    bot = FakeBot()
    await notify_winback(bot)
    assert bot.dms == []
