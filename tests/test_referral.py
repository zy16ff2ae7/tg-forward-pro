"""Реферальная программа: друг пришёл по ссылке — дни обоим.

Дарят именно приход новичка, а не открытие ссылки: себе, со старого аккаунта
и по второму кругу дни не начисляются. Проверяем:

* новичок по чужой ссылке — оба получают дни, пригласивший записывается;
* своя ссылка, несуществующий пригласивший, старый аккаунт, повторная
  ссылка — дни не начисляются, у каждого отказа свой итог;
* выключенная программа (ноль дней) не дарит и ссылку не собирает;
* ``/api/me`` отдаёт ссылку и счёт для карточки в кабинете.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import update

from app import referral
from app.config import settings
from app.db import repo
from app.db.database import session_scope
from app.db.models import User
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


async def test_the_link_gives_days_to_both(create_user):
    """Новичок по чужой ссылке: дни обоим, пригласивший записан."""
    await create_user(id=TEST_USER_ID)
    await create_user(id=FRIEND_ID)

    async with session_scope() as session:
        result = await referral.apply(session, FRIEND_ID, TEST_USER_ID)
        assert result.granted
        await session.commit()

    assert await _referred_by(FRIEND_ID) == TEST_USER_ID
    days = settings.referral_days
    for user_id in (TEST_USER_ID, FRIEND_ID):
        until = await _until(user_id)
        assert until is not None
        assert timedelta(days=days - 1) < until - utcnow() <= timedelta(days=days)
    async with session_scope() as session:
        assert await repo.count_referrals(session, TEST_USER_ID) == 1


async def test_own_link_gives_nothing(create_user):
    """Своя ссылка себе дни не начисляет."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        result = await referral.apply(session, TEST_USER_ID, TEST_USER_ID)
        await session.commit()
    assert result.status == "self"
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
    """Старый аккаунт по чужой ссылке дней не получает: дар — новичкам."""
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
    assert result.status == "stale"
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
    assert "обоим" in referral.message(referral.Referral("granted", days=7))
    assert "собственная" in referral.message(referral.Referral("self"))
    assert "один раз" in referral.message(referral.Referral("already"))
    assert "новичков" in referral.message(referral.Referral("stale"))
    assert "выключена" in referral.message(referral.Referral("disabled"))
    assert "не найден" in referral.message(referral.Referral("stranger"))


async def test_api_me_reports_the_link_and_the_score(
    client, auth_headers, create_user, monkeypatch
):
    """/api/me отдаёт ссылку-приглашение и счёт для карточки кабинета."""
    monkeypatch.setattr(settings, "bot_username", "docha_test_bot")
    await create_user(id=TEST_USER_ID)
    await create_user(id=FRIEND_ID)
    async with session_scope() as session:
        result = await referral.apply(session, FRIEND_ID, TEST_USER_ID)
        assert result.granted
        await session.commit()

    response = await client.get("/api/me", headers=auth_headers)
    assert response.status == 200
    block = (await response.json())["referral"]
    assert block["enabled"] is True
    assert block["link"] == f"https://t.me/docha_test_bot?start=ref_{TEST_USER_ID}"
    assert block["code"] == f"ref_{TEST_USER_ID}"
    assert (block["invited"], block["earned_days"]) == (1, settings.referral_days)
