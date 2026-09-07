"""Аналитика промокодов: какой код приводит деньги.

Зачёт помечает платёж сработавшим кодом, сводка считает по каждому коду
«активации → платящие», у скидочных — выручку. Личные коды идут одной
строкой: их десятки. Проверяем:

* платёж со скидкой помечается кодом;
* код на дни считает плативших после активации;
* экран показывает общие построчно, личные — сводкой, пустоту — честно.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app import promocode
from app.bot.handlers import admin as owner
from app.bot.handlers.subscription import on_stars_paid
from app.config import settings
from app.db import repo
from app.db.database import session_scope
from app.db.models import Payment, PromoCode
from app.plans import apply_discount, stars_amount
from app.timeutil import utcnow
from tests.helpers import FakeCallback, RecordingBot, TEST_USER_ID


def _paid_message(user_id: int, payload: str, charge_id: str, total: int):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, username="p", full_name="П"),
        successful_payment=SimpleNamespace(
            invoice_payload=payload, currency="XTR", total_amount=total,
            telegram_payment_charge_id=charge_id,
        ),
        bot=SimpleNamespace(send_message=lambda *a, **k: asyncio.sleep(0)),
        answer=lambda *a, **k: asyncio.sleep(0),
    )


async def _with_admin(monkeypatch) -> None:
    monkeypatch.setattr(settings, "admin_ids", [TEST_USER_ID])


async def test_discount_payment_is_tagged_with_code(create_user):
    """Платёж со скидкой несёт код: аналитика знает, чей он."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        await repo.create_promo_code(session, "SALE", 0, percent=20)
        result = await promocode.redeem(session, TEST_USER_ID, "sale")
        assert result.granted
        await session.commit()
    cheap = int(apply_discount(stars_amount(1), 20))
    await on_stars_paid(_paid_message(
        TEST_USER_ID, f"sub:{TEST_USER_ID}:1", "charge-sale", cheap
    ))
    async with session_scope() as session:
        rows = list((await session.execute(
            select(Payment).where(Payment.user_id == TEST_USER_ID)
        )).scalars().all())
        assert len(rows) == 1 and rows[0].promo_code == "SALE"


async def test_days_code_counts_later_payers(create_user):
    """Код на дни: платил после активации — в счёт; молчун — нет."""
    payer = await create_user()
    silent = await create_user()
    async with session_scope() as session:
        await repo.create_promo_code(session, "LETO", 7)
        assert (await promocode.redeem(session, payer, "LETO")).granted
        assert (await promocode.redeem(session, silent, "LETO")).granted
        session.add(Payment(
            user_id=payer, provider="stars", amount=100, currency="XTR",
            months=1, status="paid", created_at=utcnow() + timedelta(seconds=1),
        ))
        await session.commit()
        rows = await repo.promo_stats(session)
    leto = next(row for row in rows if row["code"] == "LETO")
    assert leto["used"] == 2 and leto["payers"] == 1


async def test_screen_lists_public_and_rolls_up_personal(create_user, monkeypatch):
    """Экран: общие построчно, личные — одной строкой."""
    await _with_admin(monkeypatch)
    await create_user(id=TEST_USER_ID)
    friend = await create_user()
    async with session_scope() as session:
        await repo.create_promo_code(session, "SALE", 0, percent=20, max_uses=500)
        await repo.mint_personal_discount(session, friend, 5)
        await session.commit()
        personal = (await session.execute(
            select(PromoCode).where(PromoCode.owner_id == friend)
        )).scalar_one()
        assert (await promocode.redeem(session, TEST_USER_ID, "SALE")).granted
        assert (await promocode.redeem(session, friend, personal.code)).granted
        await session.commit()
    callback = FakeCallback("admin:promo", RecordingBot())
    await owner.admin_promo(callback)
    text, _markup = callback.message.edits[-1]
    assert "<code>SALE</code> (−20%)" in text
    assert "активаций <b>1/500</b> → платят <b>0</b>" in text
    assert "личные: <b>1</b> шт" in text


async def test_empty_screen_is_honest(create_user, monkeypatch):
    """Без кодов — «пока нет», а не пустой экран."""
    await _with_admin(monkeypatch)
    await create_user(id=TEST_USER_ID)
    callback = FakeCallback("admin:promo", RecordingBot())
    await owner.admin_promo(callback)
    text, _markup = callback.message.edits[-1]
    assert "Пока нет ни одного кода" in text
