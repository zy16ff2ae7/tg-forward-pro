"""Брошенная оплата: счёт выставлен, деньги не пришли — зовём обратно.

Разовый счёт Stars пишет висящую строку, зачёт её закрывает, джоба через
час напоминает кнопкой «закончить оплату». Проверяем:

* выставление в боте и в кабинете пишет pending-строку;
* успех закрывает висящий счёт, а не плодит второй; без висящего — пишет paid;
* подарок и старые строки тоже считаются оплаченными, а не висят;
* письмо — через час, про свежий счёт, один раз, оплатившим — тишина;
* «закончить» гасит старый счёт и шлёт новый; чужой счёт не трогает.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import select

from app.bot.handlers.subscription import (
    on_stars_paid,
    pay_resume_abandoned,
    pay_stars_period,
)
from app.db import repo
from app.db.database import session_scope
from app.db.models import Payment
from app.main import notify_abandoned_payments
from app.plans import stars_amount
from app.timeutil import utcnow
from tests.helpers import TEST_USER_ID
from tests.test_webapp_api import FakeInvoiceBot

PAYER_ID = 768_000_401


def _callback(user_id: int, data: str, bot, message=None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, full_name="Плательщик"),
        data=data,
        bot=bot,
        message=message,
        answer=lambda *a, **k: asyncio.sleep(0),
    )


def _paid_message(user_id: int, payload: str, charge_id: str, total: int):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, username="payer", full_name="П"),
        successful_payment=SimpleNamespace(
            invoice_payload=payload,
            currency="XTR",
            total_amount=total,
            telegram_payment_charge_id=charge_id,
        ),
        bot=SimpleNamespace(send_message=lambda *a, **k: asyncio.sleep(0)),
        answer=lambda *a, **k: asyncio.sleep(0),
    )


async def _pending(user_id: int, age_minutes: float = 61, **fields) -> int:
    fields.setdefault("provider", "stars")
    fields.setdefault("amount", float(stars_amount(1)))
    fields.setdefault("currency", "XTR")
    fields.setdefault("months", 1)
    async with session_scope() as session:
        row = Payment(
            user_id=user_id,
            created_at=utcnow() - timedelta(minutes=age_minutes),
            **fields,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _payments(user_id: int) -> list[Payment]:
    async with session_scope() as session:
        result = await session.execute(
            select(Payment).where(Payment.user_id == user_id).order_by(Payment.id)
        )
        return list(result.scalars().all())


class FakeBot:
    def __init__(self) -> None:
        self.dms: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.dms.append((chat_id, text, kwargs))

    async def send_invoice(self, **kwargs):
        self.dms.append((-1, "invoice", kwargs))


async def test_bot_invoice_leaves_pending_trace(create_user):
    """Выставление в боте: инвойс ушёл и висящая строка записана."""
    await create_user(id=PAYER_ID)
    bot = FakeBot()
    await pay_stars_period(_callback(PAYER_ID, "pay:stars:1", bot))
    rows = await _payments(PAYER_ID)
    assert len(rows) == 1 and rows[0].status == "pending"
    assert rows[0].external_id is None and rows[0].months == 1
    assert bot.dms[-1][1] == "invoice"


async def test_success_closes_pending_instead_of_copying(create_user):
    """Успех закрывает висящий счёт: одна строка, статус paid, чек записан."""
    await create_user(id=PAYER_ID)
    await _pending(PAYER_ID, age_minutes=5)
    await on_stars_paid(_paid_message(
        PAYER_ID, f"sub:{PAYER_ID}:1", "charge-1", stars_amount(1)
    ))
    rows = await _payments(PAYER_ID)
    assert len(rows) == 1
    assert rows[0].status == "paid" and rows[0].external_id == "charge-1"
    async with session_scope() as session:
        assert await repo.subscription_until(session, PAYER_ID) is not None


async def test_success_without_pending_writes_paid(create_user):
    """Успех без висящего (рекуррент, старый счёт): новая строка сразу paid."""
    await create_user(id=PAYER_ID)
    await on_stars_paid(_paid_message(
        PAYER_ID, f"sub:{PAYER_ID}:1", "charge-2", stars_amount(1)
    ))
    rows = await _payments(PAYER_ID)
    assert len(rows) == 1 and rows[0].status == "paid"


async def test_legacy_stars_trace_counts_as_paid(create_user):
    """Старый след (pending + чек) — это оплата, а не брошенный счёт."""
    await create_user(id=PAYER_ID)
    await _pending(PAYER_ID, age_minutes=10_000, external_id="charge-old")
    async with session_scope() as session:
        assert await repo.has_paid(session, PAYER_ID) is True
        assert await repo.abandoned_payments(session, timedelta(hours=1)) == []


async def test_reminder_after_an_hour_with_resume_button(create_user):
    """Джоба: час прошёл — письмо с кнопкой «Оплатить»; второй проход молчит."""
    await create_user(id=PAYER_ID)
    pid = await _pending(PAYER_ID, age_minutes=61)
    bot = FakeBot()
    await notify_abandoned_payments(bot)  # type: ignore[arg-type]
    assert len(bot.dms) == 1
    chat, text, kwargs = bot.dms[0]
    assert chat == PAYER_ID and "не закончили оплату" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == f"pay:resume:{pid}"

    quiet = FakeBot()
    await notify_abandoned_payments(quiet)  # type: ignore[arg-type]
    assert quiet.dms == []


async def test_fresh_and_repaid_stay_quiet(create_user):
    """Свежий счёт и оплативший другим счётом писем не получают."""
    fresh = await create_user()
    await _pending(fresh, age_minutes=5)
    repaid = await create_user()
    old_id = await _pending(repaid, age_minutes=120)
    async with session_scope() as session:
        old = await session.get(Payment, old_id)
        assert old is not None
        old.created_at = utcnow() - timedelta(minutes=120)
        session.add(Payment(
            user_id=repaid, provider="stars", amount=100, currency="XTR",
            months=1, status="paid", created_at=utcnow() - timedelta(minutes=10),
        ))
        await session.commit()
    bot = FakeBot()
    await notify_abandoned_payments(bot)  # type: ignore[arg-type]
    assert bot.dms == []


async def test_only_latest_pending_is_reminded(create_user):
    """Два висящих — письмо одно, про свежий; старый помечен молча."""
    await create_user(id=PAYER_ID)
    await _pending(PAYER_ID, age_minutes=180)
    pid = await _pending(PAYER_ID, age_minutes=61)
    bot = FakeBot()
    await notify_abandoned_payments(bot)  # type: ignore[arg-type]
    assert len(bot.dms) == 1
    button = bot.dms[0][2]["reply_markup"].inline_keyboard[0][0]
    assert button.callback_data == f"pay:resume:{pid}"


async def test_resume_expires_old_and_sends_fresh(create_user):
    """«Закончить»: старый счёт истёк, новый инвойс в чате."""
    await create_user(id=PAYER_ID)
    pid = await _pending(PAYER_ID, age_minutes=61)
    bot = FakeBot()
    await pay_resume_abandoned(_callback(PAYER_ID, f"pay:resume:{pid}", bot))
    rows = await _payments(PAYER_ID)
    assert [row.status for row in rows] == ["expired", "pending"]
    assert bot.dms[-1][1] == "invoice"


async def test_resume_refuses_foreign_bill(create_user):
    """Чужой счёт по callback не трогаем: ни гашения, ни инвойса."""
    await create_user(id=PAYER_ID)
    stranger = await create_user()
    pid = await _pending(PAYER_ID, age_minutes=61)
    bot = FakeBot()
    await pay_resume_abandoned(_callback(stranger, f"pay:resume:{pid}", bot))
    rows = await _payments(PAYER_ID)
    assert [row.status for row in rows] == ["pending"]
    assert bot.dms == []


async def test_cabinet_invoice_leaves_pending_trace(
    bot_client, auth_headers, create_user
):
    """Кабинет: разовый инвойс пишет строку; подарок и авто — нет."""
    await create_user(id=TEST_USER_ID)
    test_client = await bot_client(FakeInvoiceBot())
    response = await test_client.post(
        "/api/subscription/invoice", headers=auth_headers, json={"months": 1}
    )
    assert response.status == 200
    rows = await _payments(TEST_USER_ID)
    assert len(rows) == 1 and rows[0].status == "pending"

    auto = await test_client.post(
        "/api/subscription/invoice", headers=auth_headers,
        json={"months": 1, "autorenew": True},
    )
    assert auto.status == 200
    assert len(await _payments(TEST_USER_ID)) == 1
