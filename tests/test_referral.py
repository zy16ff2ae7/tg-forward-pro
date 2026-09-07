"""Реферальная программа: друг пришёл — ему скидка, оплатил — вам дни.

За саму регистрацию дней не даётся никому: их фармили пачками фейков, и они
ломали правило «бесплатно — только за подписку на канал». Проверяем:

* новичок по чужой ссылке — привязывается и получает код на скидку, дней нет;
* своя ссылка, несуществующий пригласивший, старый аккаунт, повторная
  ссылка — у каждого отказа свой итог, кодов и дней нет;
* первый оплаченный абонемент друга — пригласившему дни и код, второй — нет;
* платёж без пригласившего и ручная выдача награды не дают;
* выключенная программа (ноль дней) не дарит и ссылку не собирает;
* ``/api/me`` отдаёт ссылку и честный счёт для карточки в кабинете.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import update

from app import referral
from app.config import settings
from app.db import database, repo
from app.db.database import session_scope
from app.db.models import User
from app.plans import rub_amount
from app.timeutil import utcnow
from tests.helpers import TEST_USER_ID

FRIEND_ID = 768_000_101
OTHER_ID = 768_000_102


async def _until(user_id: int):
    async with session_scope() as session:
        return await repo.subscription_until(session, user_id)


async def _referred_by(user_id: int):
    async with session_scope() as session:
        user = await repo.get_user(session, user_id)
        return user.referred_by if user else None


async def _join(create_user):
    await create_user(id=TEST_USER_ID)
    await create_user(id=FRIEND_ID)
    async with session_scope() as session:
        result = await referral.apply(session, FRIEND_ID, TEST_USER_ID)
        assert result.granted
        await session.commit()
    return result


async def _pay(user_id: int, charge: str = "ch"):
    """Первый (или очередной) оплаченный месяц картой — как кнопка «Я оплатил»."""
    async with session_scope() as session:
        payment = await repo.create_payment(
            session, user_id=user_id, provider="yookassa",
            amount=float(rub_amount(1)), currency="RUB", months=1,
            external_id=f"ext-{charge}-{user_id}",
        )
        await session.flush()
        assert await repo.claim_payment(session, payment) is True
        await repo.activate_subscription(session, user_id, 1)
        reward = await repo.reward_referrer(
            session, user_id, settings.referral_days, referral.discount_percent()
        )
        await session.commit()
    return reward


async def test_the_link_binds_and_gives_a_code_but_no_days(create_user):
    """Новичок по ссылке: привязка и код другу, дней — никому."""
    result = await _join(create_user)
    assert result.friend_code
    assert await _referred_by(FRIEND_ID) == TEST_USER_ID
    assert await _until(FRIEND_ID) is None
    assert await _until(TEST_USER_ID) is None
    async with session_scope() as session:
        assert await repo.count_referrals(session, TEST_USER_ID) == 1
        assert await repo.count_active_referrals(session, TEST_USER_ID) == 0


async def test_first_payment_rewards_the_referrer(create_user):
    """Первый абонемент друга: пригласившему дни и код."""
    await _join(create_user)
    reward = await _pay(FRIEND_ID, "first")
    assert reward is not None
    referrer_id, code = reward
    assert referrer_id == TEST_USER_ID and code

    days = settings.referral_days
    until = await _until(TEST_USER_ID)
    assert until is not None
    assert timedelta(days=days - 1) < until - utcnow() <= timedelta(days=days)
    async with session_scope() as session:
        assert await repo.count_active_referrals(session, TEST_USER_ID) == 1
        promo = await repo.get_promo_code(session, code)
        assert promo.owner_id == TEST_USER_ID and promo.percent > 0


async def test_second_payment_gives_nothing_more(create_user):
    """Второй абонемент того же друга: один друг — одна награда."""
    await _join(create_user)
    assert await _pay(FRIEND_ID, "one") is not None
    assert await _pay(FRIEND_ID, "two") is None
    async with session_scope() as session:
        codes = await repo.owner_discount_codes(session, TEST_USER_ID)
        assert len(codes) == 1


async def test_payment_without_referrer_gives_nothing(create_user):
    """Платёж без пригласившего — обычная оплата, награды нет."""
    user_id = await create_user()
    async with session_scope() as session:
        reward = await repo.reward_referrer(
            session, user_id, settings.referral_days, referral.discount_percent()
        )
        await session.commit()
    assert reward is None


async def test_manual_grant_gives_nothing(create_user):
    """Ручная выдача админа — не оплата: награды пригласившему нет."""
    await _join(create_user)
    # _do_grant зовёт только activate_subscription — без reward_referrer.
    async with session_scope() as session:
        await repo.activate_subscription(session, FRIEND_ID, 1)
        await session.commit()
    assert await _until(TEST_USER_ID) is None
    async with session_scope() as session:
        assert await repo.count_active_referrals(session, TEST_USER_ID) == 0


async def test_stars_payment_rewards_and_notifies(create_user):
    """Звёзды: первый платёж награждает и шлёт весть пригласившему."""
    from app.bot.handlers.subscription import on_stars_paid

    await _join(create_user)
    from app.plans import stars_amount

    dms: list[tuple[int, str]] = []

    async def send_message(chat_id, text, **kwargs):
        dms.append((chat_id, text))

    sent: list[str] = []
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=FRIEND_ID, full_name="Друг"),
        successful_payment=SimpleNamespace(
            invoice_payload=f"sub:{FRIEND_ID}:1",
            currency="XTR",
            total_amount=stars_amount(1),
            telegram_payment_charge_id="charge-ref-1",
        ),
        bot=SimpleNamespace(send_message=send_message),
        answer=lambda text, **kwargs: sent.append(text) or asyncio.sleep(0),
    )
    await on_stars_paid(message)
    assert await _until(TEST_USER_ID) is not None
    assert len(dms) == 1 and dms[0][0] == TEST_USER_ID
    assert "оформил абонемент" in dms[0][1]


async def test_old_links_count_as_settled():
    """Старые связи закрыты: доливка ставит флаг награды всем прошлым строкам."""
    assert (
        database.ADDED_COLUMNS["users"]["referred_rewarded"]
        == "BOOLEAN NOT NULL DEFAULT 1"
    )


async def test_own_link_gives_nothing(create_user):
    """Своя ссылка себе ничего не даёт."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        result = await referral.apply(session, TEST_USER_ID, TEST_USER_ID)
        await session.commit()
    assert result.status == "self" and result.friend_code == ""
    assert await _until(TEST_USER_ID) is None


async def test_unknown_referrer_gives_nothing(create_user):
    """Пригласившего нет в базе — ссылка молча не срабатывает."""
    await create_user(id=FRIEND_ID)
    async with session_scope() as session:
        result = await referral.apply(session, FRIEND_ID, 999_888_777)
        await session.commit()
    assert result.status == "stranger"
    assert await _until(FRIEND_ID) is None


async def test_old_account_is_not_a_newcomer(create_user):
    """Старый аккаунт по чужой ссылке: дар — новичкам."""
    await create_user(id=TEST_USER_ID)
    await create_user(id=FRIEND_ID)
    async with session_scope() as session:
        await session.execute(
            update(User)
            .where(User.id == FRIEND_ID)
            .values(created_at=utcnow() - timedelta(days=30))
        )
        await session.commit()
    async with session_scope() as session:
        result = await referral.apply(session, FRIEND_ID, TEST_USER_ID)
        await session.commit()
    assert result.status == "stale" and result.friend_code == ""
    assert await _until(FRIEND_ID) is None


async def test_second_link_does_not_rebind(create_user):
    """Пригласивший фиксируется первым: вторая ссылка не перепривязывает."""
    await create_user(id=TEST_USER_ID)
    await create_user(id=OTHER_ID)
    await create_user(id=FRIEND_ID)
    async with session_scope() as session:
        first = await referral.apply(session, FRIEND_ID, TEST_USER_ID)
        assert first.granted
        await session.commit()
    async with session_scope() as session:
        second = await referral.apply(session, FRIEND_ID, OTHER_ID)
        await session.commit()
    assert second.status == "already"
    assert await _referred_by(FRIEND_ID) == TEST_USER_ID
    async with session_scope() as session:
        assert await repo.count_referrals(session, OTHER_ID) == 0


async def test_disabled_program_grants_nothing(create_user, monkeypatch):
    """Ноль дней — программа выключена: ни выдачи, ни ссылки."""
    monkeypatch.setattr(settings, "referral_days", 0)
    assert referral.enabled() is False
    assert referral.link(TEST_USER_ID) == ""
    await create_user(id=TEST_USER_ID)
    await create_user(id=FRIEND_ID)
    async with session_scope() as session:
        result = await referral.apply(session, FRIEND_ID, TEST_USER_ID)
        await session.commit()
    assert result.status == "disabled"
    assert await _until(FRIEND_ID) is None


async def test_every_outcome_has_its_own_words():
    """У каждого итога свой текст — человек понимает, что случилось."""
    granted = referral.message(referral.Referral("granted", days=5, friend_code="REF-X"))
    assert "REF-X" in granted and "+5 дн" in granted
    assert "собственная" in referral.message(referral.Referral("self"))
    assert "один раз" in referral.message(referral.Referral("already"))
    assert "новичков" in referral.message(referral.Referral("stale"))
    assert "выключена" in referral.message(referral.Referral("disabled"))
    assert "не найден" in referral.message(referral.Referral("stranger"))
    pending = referral.referrer_pending_message("Пётр")
    assert "Пётр" in pending and "первый абонемент" in pending
    rewarded = referral.referrer_reward_message("Пётр", 5, "REF-Y")
    assert "REF-Y" in rewarded and "+5 дн" in rewarded


async def test_api_me_reports_the_link_and_the_honest_score(
    client, auth_headers, create_user, monkeypatch
):
    """/api/me: ссылка, счёт приходов и наград — для карточки кабинета."""
    monkeypatch.setattr(settings, "bot_username", "docha_test_bot")
    await _join(create_user)

    response = await client.get("/api/me", headers=auth_headers)
    assert response.status == 200
    block = (await response.json())["referral"]
    assert block["enabled"] is True
    assert block["link"] == f"https://t.me/docha_test_bot?start=ref_{TEST_USER_ID}"
    assert block["code"] == f"ref_{TEST_USER_ID}"
    assert (block["invited"], block["rewarded"], block["earned_days"]) == (1, 0, 0)

    assert await _pay(FRIEND_ID, "api") is not None
    again = await client.get("/api/me", headers=auth_headers)
    block = (await again.json())["referral"]
    assert (block["invited"], block["rewarded"]) == (1, 1)
    assert block["earned_days"] == settings.referral_days
