"""Онбординг бонусника: подарок → первая задача → оплата до конца бонуса.

Человек брал три дня за канал и терялся — самый дешёвый трафик сгорал молча.
Теперь его ведут три письма: день 0 — «подарок активен», день 1 — «задач всё
нет», день 2 — «бонус кончается завтра». Проверяем:

* приветствие уходит сразу после подарка;
* напоминание дня 1 — только тем, кто так ничего и не создал;
* «кончается завтра» — только неплатившим с живым сроком;
* бонусники без оплат не получают общих писем платящим;
* повторный проход молчит — каждое письмо уходит один раз.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import update

from app.config import settings
from app.db import repo
from app.db.database import session_scope
from app.db.models import Payment, Subscription, User
from app.main import notify_onboarding
from app.timeutil import utcnow
from tests.helpers import add_rule


class FakeBot:
    def __init__(self) -> None:
        self.dms: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.dms.append((chat_id, text))


async def _grant_bonus(user_id: int, days_ago: float = 0) -> None:
    async with session_scope() as session:
        await session.execute(
            update(User)
            .where(User.id == user_id)
            .values(channel_bonus_at=utcnow() - timedelta(days=days_ago))
        )
        await session.commit()


async def _sub(user_id: int, active_days: float = 3) -> None:
    async with session_scope() as session:
        session.add(
            Subscription(
                user_id=user_id,
                active_until=utcnow() + timedelta(days=active_days),
            )
        )
        await session.commit()


async def _pay(user_id: int) -> None:
    async with session_scope() as session:
        session.add(
            Payment(
                user_id=user_id, provider="stars",
                amount=100, currency="XTR", status="paid",
            )
        )
        await session.commit()


async def _marks(user_id: int) -> tuple:
    async with session_scope() as session:
        user = await session.get(User, user_id)
        assert user is not None
        return (user.onboard_day0_at, user.onboard_day1_at, user.onboard_day2_at)


async def test_welcome_follows_the_gift(create_user):
    """День 0: подарок забран — приветствие с зовом в кабинет уже летит."""
    user_id = await create_user()
    await _grant_bonus(user_id)
    bot = FakeBot()
    await notify_onboarding(bot)  # type: ignore[arg-type]
    assert len(bot.dms) == 1
    assert "Подарок активен" in bot.dms[0][1]
    assert f"{settings.bonus_days} дн" in bot.dms[0][1]
    assert (await _marks(user_id))[0] is not None


async def test_day_one_nudges_only_the_empty(create_user, create_account):
    """День 1: «задач нет» — только тем, у кого их правда нет."""
    busy = await create_user()
    await _grant_bonus(busy, days_ago=1.5)
    async with session_scope() as session:
        await repo.mark_onboarded(session, busy, 0)
        await session.commit()
    await add_rule(busy, await create_account(busy))

    idle = await create_user()
    await _grant_bonus(idle, days_ago=1.5)
    async with session_scope() as session:
        await repo.mark_onboarded(session, idle, 0)
        await session.commit()

    bot = FakeBot()
    await notify_onboarding(bot)  # type: ignore[arg-type]
    assert [chat for chat, _ in bot.dms] == [idle]
    assert "нет ни одной задачи" in bot.dms[0][1]
    assert (await _marks(busy))[1] is not None  # занятой помечен молча


async def test_day_two_warns_only_unpaid_with_live_term(create_user):
    """День 2: «кончается завтра» — неплатившим; плативший идёт мимо."""
    bonus_only = await create_user()
    await _grant_bonus(bonus_only, days_ago=2.5)
    await _sub(bonus_only, active_days=0.5)
    async with session_scope() as session:
        await repo.mark_onboarded(session, bonus_only, 0)
        await repo.mark_onboarded(session, bonus_only, 1)
        await session.commit()

    payer = await create_user()
    await _grant_bonus(payer, days_ago=2.5)
    await _sub(payer, active_days=30)
    await _pay(payer)
    async with session_scope() as session:
        await repo.mark_onboarded(session, payer, 0)
        await repo.mark_onboarded(session, payer, 1)
        await session.commit()

    bot = FakeBot()
    await notify_onboarding(bot)  # type: ignore[arg-type]
    assert [chat for chat, _ in bot.dms] == [bonus_only]
    assert "кончается завтра" in bot.dms[0][1]


async def test_bonus_users_skip_payers_letters(create_user):
    """Бонусник без оплат не получает «абонемент заканчивается» в первую же
    минуту после подарка — его ведёт онбординг, а не общая цепочка."""
    user_id = await create_user()
    await _grant_bonus(user_id)
    await _sub(user_id, active_days=2)
    async with session_scope() as session:
        soon = await repo.expiring_soon(session)
        assert [sub.user_id for sub in soon] == []


async def test_each_letter_goes_once(create_user):
    """Повторный проход молчит: метки уже стоят."""
    user_id = await create_user()
    await _grant_bonus(user_id)
    bot = FakeBot()
    await notify_onboarding(bot)  # type: ignore[arg-type]
    await notify_onboarding(bot)  # type: ignore[arg-type]
    assert len(bot.dms) == 1
