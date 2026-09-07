"""Автопродление за Stars: Telegram списывает месяц сам.

Подписочный счёт отличается от разового одним параметром, а продлевает его
тот же хендлер successful_payment. Проверяем:

* рекуррентное списание продлевает абонемент и взводит флаг;
* разовый платёж флага не касается и звучит иначе;
* истёкший срок флаг снимает: списания нет — продления нет;
* кабинет просит подписочный инвойс одним флагом, но только на месяц;
* ``/api/me`` и ``/api/accounts`` отдают флаг для кнопки и строки.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from app.db import repo
from app.db.database import session_scope
from app.plans import STARS_SUBSCRIPTION_PERIOD, stars_amount
from app.timeutil import utcnow
from tests.helpers import TEST_USER_ID
from tests.test_webapp_api import FakeInvoiceBot


def _paid_message(user_id: int, charge_id: str, sent: list, **flags):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        successful_payment=SimpleNamespace(
            invoice_payload=f"sub:{user_id}:1",
            currency="XTR",
            total_amount=stars_amount(1),
            telegram_payment_charge_id=charge_id,
            **flags,
        ),
        answer=lambda text, **kwargs: sent.append(text) or asyncio.sleep(0),
    )


async def test_recurring_charge_extends_and_marks_autorenew(create_user):
    """Рекуррентное списание: месяц плюс, флаг взведён, текст свой."""
    from app.bot.handlers.subscription import on_stars_paid

    user_id = await create_user()
    sent: list[str] = []
    await on_stars_paid(
        _paid_message(user_id, "charge-sub-1", sent, is_recurring=True)
    )

    async with session_scope() as session:
        until = await repo.subscription_until(session, user_id)
        assert until is not None
        assert timedelta(days=27) < until - utcnow() <= timedelta(days=32)
        assert await repo.stars_autorenew(session, user_id) is True
    assert sent and "Автопродление" in sent[0]


async def test_one_time_payment_does_not_touch_the_flag(create_user):
    """Разовый платёж: месяц плюс, флаг не тронут, текст обычный."""
    from app.bot.handlers.subscription import on_stars_paid

    user_id = await create_user()
    sent: list[str] = []
    await on_stars_paid(_paid_message(user_id, "charge-once-1", sent))

    async with session_scope() as session:
        assert await repo.subscription_until(session, user_id) is not None
        assert await repo.stars_autorenew(session, user_id) is False
    assert sent and "Оплата прошла" in sent[0]


async def test_expired_subscription_drops_the_flag(create_user):
    """Срок истёк — флаг снят: отмены в Telegram боту никто не сообщает."""
    user_id = await create_user()
    async with session_scope() as session:
        await repo.activate_subscription(session, user_id, 1)
        await repo.set_stars_autorenew(session, user_id, True)
        await session.commit()
        assert await repo.stars_autorenew(session, user_id) is True
        sub = await repo.get_subscription(session, user_id)
        assert sub is not None
        sub.active_until = utcnow() - timedelta(seconds=1)
        await repo.mark_expiry_notified(session, sub)
        await session.commit()
        assert await repo.stars_autorenew(session, user_id) is False


async def test_invoice_endpoint_builds_a_subscription_on_request(
    bot_client, auth_headers
):
    """Кабинет просит подписочный инвойс — боту уходит subscription_period."""
    bot = FakeInvoiceBot()
    test_client = await bot_client(bot)
    response = await test_client.post(
        "/api/subscription/invoice",
        headers=auth_headers,
        json={"months": 1, "autorenew": True},
    )
    assert response.status == 200
    assert (await response.json())["autorenew"] is True
    assert len(bot.calls) == 1
    assert bot.calls[0].get("subscription_period") == STARS_SUBSCRIPTION_PERIOD
    assert bot.calls[0]["payload"] == f"sub:{TEST_USER_ID}:1"


async def test_invoice_endpoint_refuses_autorenew_for_other_periods(
    bot_client, auth_headers
):
    """Автопродление — только на месяц: период подписки всегда 30 дней."""
    bot = FakeInvoiceBot()
    test_client = await bot_client(bot)
    response = await test_client.post(
        "/api/subscription/invoice",
        headers=auth_headers,
        json={"months": 3, "autorenew": True},
    )
    assert response.status == 400
    assert bot.calls == []


async def test_one_time_invoice_has_no_subscription_period(bot_client, auth_headers):
    """Разовый счёт — без subscription_period: списания не повторятся."""
    bot = FakeInvoiceBot()
    test_client = await bot_client(bot)
    response = await test_client.post(
        "/api/subscription/invoice",
        headers=auth_headers,
        json={"months": 3},
    )
    assert response.status == 200
    assert (await response.json())["autorenew"] is False
    assert "subscription_period" not in bot.calls[0]


async def test_api_reports_the_flag(client, auth_headers, create_user):
    """/api/me и /api/accounts отдают флаг для кнопки и строки."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        await repo.activate_subscription(session, TEST_USER_ID, 1)
        await repo.set_stars_autorenew(session, TEST_USER_ID, True)
        await session.commit()

    me = await client.get("/api/me", headers=auth_headers)
    assert (await me.json())["subscription"]["autorenew"] is True

    accounts = await client.get("/api/accounts", headers=auth_headers)
    assert (await accounts.json())["subscription"]["autorenew"] is True
