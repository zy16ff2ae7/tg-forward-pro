"""Акционные промокоды на скидку: /promo_new КОД 20% [ЛИМИТ] [СРОК].

Владелец объявляет акцию в канале общим кодом на −N%: кто успел
активировать, тот платит дешевле. Проверяем:

* команда создаёт общий код на скидку с лимитом и сроком;
* дни командой без процента — как раньше;
* мусор вместо процента — подсказка, а не код;
* лимит держит число активаций: лишний получает exhausted;
* чужой deferred места в лимите не занимает.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import promocode
from app.config import settings
from app.db import repo
from app.db.database import session_scope

ADMIN_ID = 900_001


def _admin_message(text: str, sent: list):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=ADMIN_ID),
        text=text,
        answer=lambda t, **k: sent.append(t) or asyncio.sleep(0),
    )


async def _run(text: str, monkeypatch):
    from app.bot.handlers.admin import promo_new

    monkeypatch.setattr(settings, "admin_ids", [ADMIN_ID])
    sent: list[str] = []
    await promo_new(_admin_message(text, sent))
    return sent


async def test_percent_code_is_created_with_limit_and_ttl(create_user, monkeypatch):
    """/promo_new SALE 20% 500 3 — общий код на −20% с лимитом и сроком."""
    await create_user(id=ADMIN_ID)
    sent = await _run("/promo_new SALE 20% 500 3", monkeypatch)
    assert sent and "−20%" in sent[0]
    async with session_scope() as session:
        promo = await repo.get_promo_code(session, "sale")
        assert promo is not None
        assert promo.percent == 20 and promo.days == 0
        assert promo.owner_id is None
        assert promo.max_uses == 500
        assert promo.expires_at is not None


async def test_days_code_still_works(create_user, monkeypatch):
    """/promo_new LETO 7 — код на дни, как раньше."""
    await create_user(id=ADMIN_ID)
    sent = await _run("/promo_new LETO 7", monkeypatch)
    assert sent and "7 дн" in sent[0]
    async with session_scope() as session:
        promo = await repo.get_promo_code(session, "LETO")
        assert promo.days == 7 and promo.percent == 0


async def test_garbage_percent_shows_usage(create_user, monkeypatch):
    """Мусор вместо процента — подсказка, код не создаётся."""
    await create_user(id=ADMIN_ID)
    for bad in ("0%", "95%", "abc", "-7"):
        sent = await _run(f"/promo_new X{bad} {bad}", monkeypatch)
        assert sent and "Использование" in sent[0]
    async with session_scope() as session:
        assert await repo.get_promo_code(session, "X0%") is None


async def test_limit_holds_the_number_of_activations(create_user):
    """Лимит 2 — двое активируют, третий получает exhausted."""
    users = [await create_user() for _ in range(3)]
    async with session_scope() as session:
        await repo.create_promo_code(
            session, "FLASH", 0, max_uses=2, percent=15, created_by=ADMIN_ID
        )
        await session.commit()
    outcomes = []
    for user_id in users:
        async with session_scope() as session:
            outcome = await promocode.redeem(session, user_id, "flash")
            await session.commit()
            outcomes.append(outcome.status)
    assert outcomes == ["granted", "granted", "exhausted"]
    async with session_scope() as session:
        promo = await repo.get_promo_code(session, "FLASH")
        assert promo.used_count == 2


async def test_deferred_does_not_take_a_slot(create_user):
    """Отказ «сначала потратьте скидку» места в лимите не занимает."""
    first, second = await create_user(), await create_user()
    async with session_scope() as session:
        personal = await repo.mint_personal_discount(session, first, 5)
        await repo.create_promo_code(
            session, "FLASH2", 0, max_uses=1, percent=10, created_by=ADMIN_ID
        )
        await session.commit()
    async with session_scope() as session:
        assert (await promocode.redeem(session, first, personal.code)).granted
        await session.commit()
    async with session_scope() as session:
        busy = await promocode.redeem(session, first, "FLASH2")
        await session.rollback()
        assert busy.status == "deferred"
    async with session_scope() as session:
        lucky = await promocode.redeem(session, second, "FLASH2")
        await session.commit()
        assert lucky.granted
