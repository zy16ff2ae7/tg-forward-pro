"""Промокоды на дни абонемента: акции вида «код на выходных».

Один код — много человек (лимит — ``max_uses``), один человек — один раз на
код. Проверяем:

* создание и активацию: дни человеку, счётчик коду;
* ввод как угодно — код нормализуется (регистр и пробелы не важны);
* чужой код, выключенный, истёкший, разобранный и повторный — у каждого
  свой итог и свой текст, дни не начисляются;
* повторное создание того же кода — ошибка, а не двойник;
* эндпоинт кабинета разводит итоги по кодам.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app import promocode
from app.db import repo
from app.db.database import session_scope
from app.db.models import PromoCode
from app.timeutil import utcnow
from tests.helpers import TEST_USER_ID

FRIEND_ID = 768_000_101


async def test_activation_gives_days_and_counts_the_use(create_user):
    """Активация: дни человеку, счётчик коду."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        await repo.create_promo_code(session, "LETO", 7, max_uses=100)
        await session.commit()

    async with session_scope() as session:
        result = await promocode.redeem(session, TEST_USER_ID, "LETO")
        assert result.granted
        assert result.days == 7
        await session.commit()

    async with session_scope() as session:
        until = await repo.subscription_until(session, TEST_USER_ID)
        assert until is not None
        assert timedelta(days=6) < until - utcnow() <= timedelta(days=7)
        promo = await repo.get_promo_code(session, "LETO")
        assert promo is not None and promo.used_count == 1


async def test_code_is_case_and_space_insensitive(create_user):
    """« leto » и «LETO» — один код."""
    assert repo.normalize_promo_code("  le-to 2026\n") == "LE-TO2026"
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        await repo.create_promo_code(session, "ЛЕТО", 3)
        await session.commit()
    async with session_scope() as session:
        result = await promocode.redeem(session, TEST_USER_ID, "  лето ")
        await session.commit()
    assert result.granted


async def test_unknown_code_grants_nothing(create_user):
    """Чужого кода нет — так и говорим, дни не начисляем."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        result = await promocode.redeem(session, TEST_USER_ID, "NETAKOGO")
        await session.rollback()
    assert result.status == "unknown"
    async with session_scope() as session:
        assert await repo.subscription_until(session, TEST_USER_ID) is None


async def test_disabled_code_looks_unknown(create_user):
    """Выключенный код неотличим от несуществующего."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        promo = await repo.create_promo_code(session, "OFF", 3)
        promo.active = False
        await session.commit()
    async with session_scope() as session:
        result = await promocode.redeem(session, TEST_USER_ID, "OFF")
        await session.rollback()
    assert result.status == "unknown"


async def test_expired_code_grants_nothing(create_user):
    """Срок вышел — код мёртв."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        promo = await repo.create_promo_code(session, "OLD", 3, ttl_days=1)
        await session.execute(
            update(PromoCode)
            .where(PromoCode.id == promo.id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await session.commit()
    async with session_scope() as session:
        result = await promocode.redeem(session, TEST_USER_ID, "OLD")
        await session.rollback()
    assert result.status == "expired"


async def test_single_use_code_serves_only_the_first(create_user):
    """Лимит 1: первый забирает, второму — «разобрали»."""
    await create_user(id=TEST_USER_ID)
    await create_user(id=FRIEND_ID)
    async with session_scope() as session:
        await repo.create_promo_code(session, "ONE", 5, max_uses=1)
        await session.commit()
    async with session_scope() as session:
        first = await promocode.redeem(session, TEST_USER_ID, "ONE")
        assert first.granted
        await session.commit()
    async with session_scope() as session:
        second = await promocode.redeem(session, FRIEND_ID, "ONE")
        await session.rollback()
    assert second.status == "exhausted"
    async with session_scope() as session:
        assert await repo.subscription_until(session, FRIEND_ID) is None


async def test_same_person_redeems_once(create_user):
    """Один код — один раз на человека, хоть лимит и не выбран."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        await repo.create_promo_code(session, "TWICE", 2)
        await session.commit()
    async with session_scope() as session:
        first = await promocode.redeem(session, TEST_USER_ID, "TWICE")
        assert first.granted
        await session.commit()
    async with session_scope() as session:
        second = await promocode.redeem(session, TEST_USER_ID, "TWICE")
        await session.rollback()
    assert second.status == "already"


async def test_duplicate_code_is_rejected(create_user):
    """Второй код с тем же именем — ошибка, а не двойник."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        await repo.create_promo_code(session, "DUP", 1)
        await session.commit()
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            await repo.create_promo_code(session, "dup", 9)


async def test_every_outcome_has_its_own_words():
    """У каждого итога свой текст — человек понимает, что случилось."""
    assert "+7 дн" in promocode.message(promocode.Promo("granted", days=7))
    assert "один раз" in promocode.message(promocode.Promo("already"))
    assert "Проверьте" in promocode.message(promocode.Promo("unknown"))
    assert "Срок" in promocode.message(promocode.Promo("expired"))
    assert "разобрали" in promocode.message(promocode.Promo("exhausted"))


async def test_api_redeems_and_reports_outcomes(client, auth_headers, create_user):
    """Эндпоинт кабинета: выдача — 200, итоги — своими кодами."""
    await create_user(id=TEST_USER_ID)
    async with session_scope() as session:
        await repo.create_promo_code(session, "WEB", 4)
        await session.commit()

    response = await client.post(
        "/api/subscription/promo", json={"code": "web"}, headers=auth_headers
    )
    assert response.status == 200
    body = await response.json()
    assert body["granted"] is True and body["days"] == 4

    again = await client.post(
        "/api/subscription/promo", json={"code": "WEB"}, headers=auth_headers
    )
    assert again.status == 409
    assert (await again.json())["status"] == "already"

    missing = await client.post(
        "/api/subscription/promo", json={"code": "NET"}, headers=auth_headers
    )
    assert missing.status == 404

    empty = await client.post(
        "/api/subscription/promo", json={"code": "  "}, headers=auth_headers
    )
    assert empty.status == 400
