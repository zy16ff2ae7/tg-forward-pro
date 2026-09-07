"""Подарочный абонемент: месяц другу звёздами — в боте и в кабинете.

Даритель платит, абонемент включается другу. Друг должен быть в базе
(первый /start), себе дарить нельзя, автопродление в подарок не
заворачивается. Проверяем:

* бот спрашивает друга, незнакомца не принимает, себе отказывает;
* счёт несёт payload подарка, до списания его проверяют как свой;
* зачёт включает абонемент другу, обоих уведомляет, скидку дарителя гасит;
* подарок — не оплата друга: награды пригласившему он не даёт;
* кабинет выставляет подарочный инвойс тем же эндпоинтом.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import referral
from app.config import settings
from app.db import repo
from app.db.database import session_scope
from app.plans import apply_discount, stars_amount
from tests.helpers import TEST_USER_ID
from tests.test_webapp_api import FakeInvoiceBot

GIVER_ID = 768_000_301
FRIEND_ID = 768_000_302


class FakeState:
    """FSM без хранилища: состояние и данные — в памяти."""

    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, state=None):
        self.state = state

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def get_data(self):
        return dict(self.data)

    async def clear(self):
        self.state = None
        self.data = {}


def _message(user_id: int, text: str, sent: list):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, username=None, full_name="Даритель"),
        text=text,
        photo=None, document=None, video=None, animation=None,
        edit_text=lambda *a, **k: sent.append((a, k)) or asyncio.sleep(0),
        answer=lambda t, **k: sent.append(t) or asyncio.sleep(0),
    )


def _callback(user_id: int, data: str, bot, message=None, **kwargs):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, full_name="Даритель"),
        data=data,
        bot=bot,
        message=message,
        answer=lambda *a, **k: asyncio.sleep(0),
        **kwargs,
    )


async def test_gift_asks_for_friend(create_user):
    """Кнопка подарка: бот спрашивает, кому дарим."""
    from app.bot.handlers.subscription import pay_gift
    from app.bot.states import GiftStates

    await create_user(id=GIVER_ID)
    sent: list = []
    state = FakeState()
    await pay_gift(_callback(GIVER_ID, "pay:gift", None, _message(GIVER_ID, "", sent)), state)
    assert state.state == GiftStates.waiting_friend
    assert sent and "Пришлите" in str(sent[0])


async def test_unknown_friend_keeps_asking(create_user):
    """Незнакомцу дарить нечего: бот просит другого друга, состояние живёт."""
    from app.bot.handlers.subscription import gift_friend_entered
    from app.bot.states import GiftStates

    await create_user(id=GIVER_ID)
    sent: list = []
    state = FakeState()
    state.state = GiftStates.waiting_friend
    await gift_friend_entered(_message(GIVER_ID, "@nobody_at_all", sent), state)
    assert sent and "Не нашли" in sent[0]
    assert state.state == GiftStates.waiting_friend


async def test_self_gift_refused(create_user):
    """Себе дарить нельзя — состояние сбрасывается."""
    from app.bot.handlers.subscription import gift_friend_entered
    from app.bot.states import GiftStates

    await create_user(id=GIVER_ID)
    sent: list = []
    state = FakeState()
    state.state = GiftStates.waiting_friend
    await gift_friend_entered(_message(GIVER_ID, str(GIVER_ID), sent), state)
    assert sent and "Себе дарить" in sent[0]
    assert state.state is None and state.data == {}


async def test_known_friend_offers_periods(create_user):
    """Знакомый друг по @username — выбор срока с подарочными кнопками."""
    from app.bot.handlers.subscription import gift_friend_entered

    await create_user(id=GIVER_ID)
    await create_user(id=FRIEND_ID, username="drug")
    sent: list = []
    state = FakeState()
    captured: dict = {}

    async def answer(text, **kwargs):
        captured["text"] = text
        captured["markup"] = kwargs.get("reply_markup")

    message = _message(GIVER_ID, "@DRUG", sent)
    message.answer = answer
    await gift_friend_entered(message, state)
    assert state.data.get("gift_to") == FRIEND_ID
    assert "@drug" in captured["text"]
    callbacks = [
        button.callback_data
        for row in captured["markup"].inline_keyboard
        for button in row
    ]
    assert any(item.startswith("pay:gift:") for item in callbacks)
    assert not any(item == "pay:stars:auto" for item in callbacks)


async def test_gift_invoice_holds_gift_payload(create_user):
    """Счёт на подарок: payload ведёт к другу, а не к дарителю."""
    from app.bot.handlers.subscription import pay_gift_period

    await create_user(id=GIVER_ID)
    await create_user(id=FRIEND_ID)
    invoices: list[dict] = []

    async def send_invoice(**kwargs):
        invoices.append(kwargs)

    bot = SimpleNamespace(send_invoice=send_invoice)
    state = FakeState()
    state.data["gift_to"] = FRIEND_ID
    await pay_gift_period(_callback(GIVER_ID, "pay:gift:1", bot), state)
    assert len(invoices) == 1
    assert invoices[0]["payload"] == f"gift:{GIVER_ID}:{FRIEND_ID}:1"
    assert invoices[0]["prices"][0].amount == stars_amount(1)
    assert state.data == {}


def _checkout_query(user_id: int, payload: str, total: int, collected: list):
    return SimpleNamespace(
        invoice_payload=payload,
        from_user=SimpleNamespace(id=user_id),
        currency="XTR",
        total_amount=total,
        answer=lambda **kwargs: collected.append(kwargs) or asyncio.sleep(0),
    )


async def test_pre_checkout_checks_gifts(create_user):
    """До списания: честный подарок — да, себе/призраку/левый — нет."""
    from app.bot.handlers.subscription import on_pre_checkout

    await create_user(id=GIVER_ID)
    await create_user(id=FRIEND_ID)
    collected: list[dict] = []
    full = stars_amount(1)
    await on_pre_checkout(_checkout_query(
        GIVER_ID, f"gift:{GIVER_ID}:{FRIEND_ID}:1", full, collected))
    await on_pre_checkout(_checkout_query(
        GIVER_ID, f"gift:{GIVER_ID}:{GIVER_ID}:1", full, collected))
    await on_pre_checkout(_checkout_query(
        GIVER_ID, f"gift:{GIVER_ID}:777:1", full, collected))
    await on_pre_checkout(_checkout_query(
        GIVER_ID, f"gift:{GIVER_ID}:{FRIEND_ID}:1", 1, collected))
    assert [call["ok"] for call in collected] == [True, False, False, False]


def _paid_message(user_id: int, payload: str, charge_id: str, sent: list,
                  total: int, bot=None):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id, username="giver", full_name="Даритель"),
        successful_payment=SimpleNamespace(
            invoice_payload=payload,
            currency="XTR",
            total_amount=total,
            telegram_payment_charge_id=charge_id,
        ),
        bot=bot or SimpleNamespace(
            send_message=lambda *a, **k: asyncio.sleep(0)
        ),
        answer=lambda text, **kwargs: sent.append(text) or asyncio.sleep(0),
    )


async def test_success_grants_friend_and_dms_both(create_user):
    """Зачёт подарка: абонемент другу, вести — обоим."""
    from app.bot.handlers.subscription import on_stars_paid

    await create_user(id=GIVER_ID)
    await create_user(id=FRIEND_ID)
    dms: list[tuple[int, str]] = []

    async def send_message(chat_id, text, **kwargs):
        dms.append((chat_id, text))

    sent: list[str] = []
    await on_stars_paid(_paid_message(
        GIVER_ID, f"gift:{GIVER_ID}:{FRIEND_ID}:1", "charge-gift-1", sent,
        stars_amount(1), SimpleNamespace(send_message=send_message),
    ))
    async with session_scope() as session:
        assert await repo.subscription_until(session, FRIEND_ID) is not None
        assert await repo.subscription_until(session, GIVER_ID) is None
    assert sent and "Подарок оплачен" in sent[0]
    assert len(dms) == 1 and dms[0][0] == FRIEND_ID
    assert "подарили" in dms[0][1]


async def test_gift_consumes_giver_discount(create_user):
    """Скидка дарителя действует и на подарок — и гаснет зачётом."""
    from app.bot.handlers.subscription import on_stars_paid

    await create_user(id=GIVER_ID)
    await create_user(id=FRIEND_ID)
    cheap = int(apply_discount(stars_amount(1), settings.referral_discount_percent))
    async with session_scope() as session:
        promo = await repo.mint_personal_discount(
            session, GIVER_ID, settings.referral_discount_percent
        )
        from app import promocode

        assert (await promocode.redeem(session, GIVER_ID, promo.code)).granted
        await session.commit()

    sent: list[str] = []
    await on_stars_paid(_paid_message(
        GIVER_ID, f"gift:{GIVER_ID}:{FRIEND_ID}:1", "charge-gift-2", sent, cheap
    ))
    assert sent and "Подарок оплачен" in sent[0]
    async with session_scope() as session:
        assert await repo.pending_discount(session, GIVER_ID) is None


async def test_gift_gives_no_referral_reward(create_user):
    """Подарок — не оплата друга: награды пригласившему нет."""
    from app.bot.handlers.subscription import on_stars_paid

    await create_user(id=TEST_USER_ID)
    await create_user(id=GIVER_ID)
    await create_user(id=FRIEND_ID)
    async with session_scope() as session:
        result = await referral.apply(session, FRIEND_ID, TEST_USER_ID)
        assert result.granted
        await session.commit()
    sent: list[str] = []
    await on_stars_paid(_paid_message(
        GIVER_ID, f"gift:{GIVER_ID}:{FRIEND_ID}:1", "charge-gift-3", sent,
        stars_amount(1),
    ))
    async with session_scope() as session:
        assert await repo.subscription_until(session, TEST_USER_ID) is None
        assert await repo.count_active_referrals(session, TEST_USER_ID) == 0


async def test_api_invoice_builds_gift(bot_client, auth_headers, create_user):
    """Кабинет: инвойс с gift_to несёт payload подарка."""
    await create_user(id=TEST_USER_ID)
    await create_user(id=FRIEND_ID, username="drug")
    bot = FakeInvoiceBot()
    test_client = await bot_client(bot)
    response = await test_client.post(
        "/api/subscription/invoice",
        headers=auth_headers,
        json={"months": 1, "gift_to": "@drug"},
    )
    assert response.status == 200
    body = await response.json()
    assert body["gift_to"] == FRIEND_ID
    assert bot.calls[0]["payload"] == f"gift:{TEST_USER_ID}:{FRIEND_ID}:1"


async def test_api_invoice_refuses_bad_gifts(bot_client, auth_headers, create_user):
    """Кабинет: призрак, себе и подарок с автопродлением — отказ."""
    await create_user(id=TEST_USER_ID)
    bot = FakeInvoiceBot()
    test_client = await bot_client(bot)
    for payload in (
        {"months": 1, "gift_to": "@nobody_at_all"},
        {"months": 1, "gift_to": str(TEST_USER_ID)},
        {"months": 1, "gift_to": "123", "autorenew": True},
    ):
        response = await test_client.post(
            "/api/subscription/invoice", headers=auth_headers, json=payload
        )
        assert response.status == 400
    assert bot.calls == []
