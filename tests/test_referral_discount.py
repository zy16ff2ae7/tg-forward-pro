"""Скидка за друга: другу — код за приход, пригласившему — за оплату.

Друг за приход получает личный промокод на −5% к первой оплате, а
пригласивший — дни и свой код, когда друг оплатит первый абонемент.
Код активируется как обычный промокод, но вместо дней встаёт в ожидание
и дешевле делает ближайший разовый счёт. Проверяем:

* заход дарит код только другу, дней нет; оплата — код и дни пригласившему;
* чужой код неотличим от несуществующего;
* активация ставит скидку в ожидание, а не продлевает абонемент;
* вторая скидка ждёт своей очереди (deferred), пока первая не потрачена;
* зачёт дешевле тарифа гасит скидку, полный тариф и ручная выдача — нет;
* погашенный код второй раз не активируется;
* звёзды принимают цену со скидкой и до, и после списания, левую — нет;
* разовый инвойс дешевле, автопродление — по полному тарифу;
* ``/api/me`` отдаёт коды и ожидание для карточки в кабинете.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import promocode, referral
from app.config import settings
from app.db import repo
from app.db.database import session_scope
from app.plans import apply_discount, rub_amount, stars_amount
from tests.helpers import TEST_USER_ID
from tests.test_webapp_api import FakeInvoiceBot

FRIEND_ID = 768_000_201
OTHER_ID = 768_000_202


async def _join(create_user):
    await create_user(id=TEST_USER_ID)
    await create_user(id=FRIEND_ID)
    async with session_scope() as session:
        result = await referral.apply(session, FRIEND_ID, TEST_USER_ID)
        assert result.granted
        await session.commit()
    return result


async def _first_payment(user_id: int):
    """Первый оплаченный месяц картой — с наградой пригласившему."""
    async with session_scope() as session:
        payment = await repo.create_payment(
            session, user_id=user_id, provider="yookassa",
            amount=float(rub_amount(1)), currency="RUB", months=1,
        )
        await session.flush()
        assert await repo.claim_payment(session, payment) is True
        await repo.activate_subscription(session, user_id, 1)
        reward = await repo.reward_referrer(
            session, user_id, settings.referral_days, referral.discount_percent()
        )
        await session.commit()
    return reward


async def _pending(user_id: int):
    async with session_scope() as session:
        promo = await repo.pending_discount(session, user_id)
        return promo.code if promo else None


async def test_join_gives_code_only_to_friend(create_user):
    """Заход: код только другу, дней никому; оплата: код и дни пригласившему."""
    result = await _join(create_user)
    assert result.friend_code
    async with session_scope() as session:
        friend = await repo.get_promo_code(session, result.friend_code)
        assert friend.percent == settings.referral_discount_percent
        assert friend.owner_id == FRIEND_ID
        assert friend.days == 0  # скидочный код дней не даёт
        assert await repo.subscription_until(session, FRIEND_ID) is None
        assert await repo.subscription_until(session, TEST_USER_ID) is None

    reward = await _first_payment(FRIEND_ID)
    assert reward is not None
    referrer_id, code = reward
    assert referrer_id == TEST_USER_ID and code and code != result.friend_code
    async with session_scope() as session:
        assert await repo.subscription_until(session, TEST_USER_ID) is not None


async def test_messages_hold_the_codes(create_user):
    """Друг видит код в приветствии, пригласивший — в вести об оплате."""
    result = await _join(create_user)
    assert result.friend_code in referral.message(result, "Иван")
    pending = referral.referrer_pending_message("Пётр")
    assert "REF-" not in pending and "первый абонемент" in pending
    reward = await _first_payment(FRIEND_ID)
    assert reward is not None
    text = referral.referrer_reward_message("Пётр", settings.referral_days, reward[1])
    assert reward[1] in text and "Промокод" in text


async def test_stranger_cannot_use_personal_code(create_user):
    """Чужой личный код — как несуществующий: ни перехвата, ни перебора."""
    result = await _join(create_user)
    await create_user(id=OTHER_ID)
    async with session_scope() as session:
        outcome = await promocode.redeem(session, OTHER_ID, result.friend_code)
        await session.rollback()
    assert outcome.status == "unknown"
    assert await _pending(OTHER_ID) is None


async def test_owner_activation_parks_the_discount(create_user):
    """Активация ставит скидку в ожидание, абонемент не трогает."""
    result = await _join(create_user)
    async with session_scope() as session:
        outcome = await promocode.redeem(session, FRIEND_ID, result.friend_code)
        assert outcome.granted and outcome.days == 0
        assert outcome.percent == settings.referral_discount_percent
        await session.commit()
    assert await _pending(FRIEND_ID) == result.friend_code
    assert "Скидка" in promocode.message(outcome)


async def test_second_discount_waits_its_turn(create_user):
    """Вторая скидка ждёт, пока первая не потрачена."""
    result = await _join(create_user)
    async with session_scope() as session:
        first = await promocode.redeem(session, FRIEND_ID, result.friend_code)
        assert first.granted
        second_code = (
            await repo.mint_personal_discount(session, FRIEND_ID, 5)
        ).code
        await session.commit()
    async with session_scope() as session:
        outcome = await promocode.redeem(session, FRIEND_ID, second_code)
        await session.rollback()
    assert outcome.status == "deferred"
    assert "уже ждёт" in promocode.message(outcome)


async def test_discounted_claim_consumes_the_code(create_user):
    """Зачёт дешевле тарифа: скидка гаснет, код — одноразовый."""
    result = await _join(create_user)
    cheap = int(apply_discount(rub_amount(1), settings.referral_discount_percent))
    async with session_scope() as session:
        activated = await promocode.redeem(session, FRIEND_ID, result.friend_code)
        assert activated.granted
        payment = await repo.create_payment(
            session, user_id=FRIEND_ID, provider="yookassa",
            amount=float(cheap), currency="RUB", months=1,
        )
        await session.flush()
        assert await repo.claim_payment(session, payment) is True
        await session.commit()
    assert await _pending(FRIEND_ID) is None
    async with session_scope() as session:
        promo = await repo.get_promo_code(session, result.friend_code)
        assert promo.active is False
        outcome = await promocode.redeem(session, FRIEND_ID, result.friend_code)
        await session.rollback()
    assert outcome.status == "unknown"


async def test_full_price_claim_keeps_the_discount(create_user):
    """Зачёт по полному тарифу чужую (будущую) скидку не трогает."""
    result = await _join(create_user)
    async with session_scope() as session:
        payment = await repo.create_payment(
            session, user_id=FRIEND_ID, provider="yookassa",
            amount=float(rub_amount(1)), currency="RUB", months=1,
        )
        await session.flush()
        assert await repo.claim_payment(session, payment) is True
        await session.commit()
    # Скидка не ждала — код цел, активируется как раньше.
    async with session_scope() as session:
        outcome = await promocode.redeem(session, FRIEND_ID, result.friend_code)
        await session.commit()
    assert outcome.granted


async def test_manual_claim_keeps_the_discount(create_user):
    """Ручная выдача скидок не касается: там платит не человек."""
    result = await _join(create_user)
    async with session_scope() as session:
        activated = await promocode.redeem(session, FRIEND_ID, result.friend_code)
        assert activated.granted
        payment = await repo.create_payment(
            session, user_id=FRIEND_ID, provider="manual",
            amount=0.0, currency="RUB", months=1,
        )
        await session.flush()
        assert await repo.claim_payment(session, payment) is True
        await session.commit()
    assert await _pending(FRIEND_ID) == result.friend_code


async def test_no_codes_when_percent_zero(create_user, monkeypatch):
    """Ноль процентов — только дни за оплату: кодов нет, тексты короткие."""
    monkeypatch.setattr(settings, "referral_discount_percent", 0)
    result = await _join(create_user)
    assert result.friend_code == ""
    assert "промокод" not in referral.message(result).lower()
    reward = await _first_payment(FRIEND_ID)
    assert reward is not None and reward[1] == ""
    async with session_scope() as session:
        assert await repo.subscription_until(session, TEST_USER_ID) is not None


async def test_api_me_reports_codes_and_pending(client, auth_headers, create_user):
    """/api/me: карточка видит коды, ожидание и размер скидки."""
    await _join(create_user)
    response = await client.get("/api/me", headers=auth_headers)
    block = (await response.json())["referral"]
    assert block["discount_percent"] == settings.referral_discount_percent
    assert block["discount_codes"] == [] and block["pending_discount"] == 0

    reward = await _first_payment(FRIEND_ID)
    assert reward is not None
    again = await client.get("/api/me", headers=auth_headers)
    block = (await again.json())["referral"]
    assert reward[1] in block["discount_codes"]

    async with session_scope() as session:
        activated = await promocode.redeem(session, TEST_USER_ID, reward[1])
        assert activated.granted
        await session.commit()
    third = await client.get("/api/me", headers=auth_headers)
    assert (await third.json())["referral"]["pending_discount"] == (
        settings.referral_discount_percent
    )


async def test_api_promo_redeems_discount(client, auth_headers, create_user):
    """Кабинет активирует скидку тем же эндпоинтом — 200 и процент."""
    await _join(create_user)
    reward = await _first_payment(FRIEND_ID)
    assert reward is not None
    response = await client.post(
        "/api/subscription/promo",
        json={"code": reward[1]},
        headers=auth_headers,
    )
    assert response.status == 200
    body = await response.json()
    assert body["granted"] is True and body["days"] == 0
    assert body["percent"] == settings.referral_discount_percent
    assert "Скидка" in body["message"]
    assert await _pending(TEST_USER_ID) == reward[1]


def _paid_message(user_id: int, charge_id: str, sent: list, total: int):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        successful_payment=SimpleNamespace(
            invoice_payload=f"sub:{user_id}:1",
            currency="XTR",
            total_amount=total,
            telegram_payment_charge_id=charge_id,
        ),
        answer=lambda text, **kwargs: sent.append(text) or asyncio.sleep(0),
    )


async def test_stars_success_consumes_discount(create_user):
    """Звёзды со скидкой: абонемент плюс, скидка погашена."""
    from app.bot.handlers.subscription import on_stars_paid

    result = await _join(create_user)
    cheap = int(apply_discount(stars_amount(1), settings.referral_discount_percent))
    async with session_scope() as session:
        activated = await promocode.redeem(session, FRIEND_ID, result.friend_code)
        assert activated.granted
        await session.commit()

    sent: list[str] = []
    message = _paid_message(FRIEND_ID, "charge-deal-1", sent, cheap)
    # Награды пригласившему тут нет: друг платит, а bring_to... — нет бота для
    # вести, поэтому друг без пригласившего. Отвязываем, чтобы не слать в пустоту.
    message.bot = SimpleNamespace(send_message=lambda *a, **k: asyncio.sleep(0))
    await on_stars_paid(message)
    async with session_scope() as session:
        assert await repo.subscription_until(session, FRIEND_ID) is not None
    assert sent and "Оплата прошла" in sent[0]
    assert await _pending(FRIEND_ID) is None


async def test_stars_success_rejects_wrong_amount(create_user):
    """Звёзды с левой суммой: как раньше — предупреждение, не доступ."""
    from app.bot.handlers.subscription import on_stars_paid

    user_id = await create_user()
    sent: list[str] = []
    await on_stars_paid(_paid_message(user_id, "charge-odd-1", sent, 1))
    async with session_scope() as session:
        assert await repo.subscription_until(session, user_id) is None
    assert sent and "не совпал" in sent[0]


def _checkout_query(user_id: int, total: int, collected: list):
    return SimpleNamespace(
        invoice_payload=f"sub:{user_id}:1",
        from_user=SimpleNamespace(id=user_id),
        currency="XTR",
        total_amount=total,
        answer=lambda **kwargs: collected.append(kwargs) or asyncio.sleep(0),
    )


async def test_pre_checkout_accepts_discounted_amount(create_user):
    """До списания: полный тариф и цена со скидкой — да, левая — нет."""
    from app.bot.handlers.subscription import on_pre_checkout

    result = await _join(create_user)
    async with session_scope() as session:
        activated = await promocode.redeem(session, FRIEND_ID, result.friend_code)
        assert activated.granted
        await session.commit()

    cheap = int(apply_discount(stars_amount(1), settings.referral_discount_percent))
    collected: list[dict] = []
    await on_pre_checkout(_checkout_query(FRIEND_ID, cheap, collected))
    await on_pre_checkout(_checkout_query(FRIEND_ID, stars_amount(1), collected))
    await on_pre_checkout(_checkout_query(FRIEND_ID, 1, collected))
    assert [call["ok"] for call in collected] == [True, True, False]


async def test_invoice_endpoint_discounts_one_time(bot_client, auth_headers, create_user):
    """Кабинет: разовый инвойс дешевле, размер скидки — в ответе."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        promo = await repo.mint_personal_discount(
            session, TEST_USER_ID, settings.referral_discount_percent
        )
        activated = await promocode.redeem(session, TEST_USER_ID, promo.code)
        assert activated.granted
        await session.commit()

    bot = FakeInvoiceBot()
    test_client = await bot_client(bot)
    response = await test_client.post(
        "/api/subscription/invoice",
        headers=auth_headers,
        json={"months": 1},
    )
    assert response.status == 200
    body = await response.json()
    assert body["amount"] == int(
        apply_discount(stars_amount(1), settings.referral_discount_percent)
    )
    assert body["discount_percent"] == settings.referral_discount_percent
    assert "Скидка" in bot.calls[0]["description"]


async def test_invoice_endpoint_keeps_autorenew_full(
    bot_client, auth_headers, create_user
):
    """Кабинет: автопродление — по полному тарифу, скидка ждёт дальше."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        promo = await repo.mint_personal_discount(
            session, TEST_USER_ID, settings.referral_discount_percent
        )
        activated = await promocode.redeem(session, TEST_USER_ID, promo.code)
        assert activated.granted
        await session.commit()

    bot = FakeInvoiceBot()
    test_client = await bot_client(bot)
    response = await test_client.post(
        "/api/subscription/invoice",
        headers=auth_headers,
        json={"months": 1, "autorenew": True},
    )
    assert response.status == 200
    body = await response.json()
    assert body["amount"] == stars_amount(1)
    assert body["discount_percent"] == 0
    assert await _pending(TEST_USER_ID) == promo.code
