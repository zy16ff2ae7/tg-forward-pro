"""Мастер промокодов кнопками: владелец собирает код без команды с аргументами.

Панель показывала только статистику, а код создавался лишь набранной вручную
командой. Теперь путь кнопочный: тип → значение → код → лимит → срок → создать.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.bot.handlers import admin
from app.bot.states import OwnerStates
from app.db import repo
from app.db.database import session_scope
from app.timeutil import utcnow
from tests.helpers import (
    TEST_USER_ID,
    FakeCallback,
    FakeMessage,
    RecordingBot,
    button_labels,
)

pytestmark = pytest.mark.usefixtures("database")


def _state(user_id: int = TEST_USER_ID):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    return FSMContext(
        MemoryStorage(), StorageKey(bot_id=1, chat_id=1, user_id=user_id)
    )


@pytest.fixture
def owner(monkeypatch):
    monkeypatch.setattr(admin, "is_admin", lambda user_id: True)


def _press(data: str):
    return FakeCallback(data, RecordingBot())


def _typed(text: str):
    message = FakeMessage(text)
    message.from_user = SimpleNamespace(id=TEST_USER_ID)
    return message


async def _last_text(callback: FakeCallback) -> str:
    return callback.message.edits[-1][0]


async def test_stats_offer_creation(owner):
    """Статистика промокодов — с кнопкой создания, а не тупиком."""
    callback = _press("admin:promo")
    await admin.admin_promo(callback)

    assert "➕ Новый промокод" in button_labels(callback.message.edits[-1][1])


async def test_percent_wizard_creates_code(owner):
    """Полный путь скидки: 20% → код → лимит 100 → срок 7 дней → в базе."""
    state = _state()

    box = _press("admin:promo:new")
    await admin.admin_promo_step(box, state)
    assert "Что даёт код" in await _last_text(box)

    box = _press("admin:promo:type:percent")
    await admin.admin_promo_step(box, state)
    assert "Размер скидки" in await _last_text(box)

    box = _press("admin:promo:value:20")
    await admin.admin_promo_step(box, state)
    assert "−20% к оплате" in await _last_text(box)
    assert await state.get_state() == OwnerStates.promo_code.state

    typed = _typed("sale20")
    await admin.admin_promo_code(typed, state)
    assert "Сколько человек" in typed.edits[-1][0]
    assert "∞ Без лимита" in button_labels(typed.edits[-1][1])

    box = _press("admin:promo:limit:100")
    await admin.admin_promo_step(box, state)
    assert "Сколько живёт" in await _last_text(box)

    box = _press("admin:promo:ttl:7")
    await admin.admin_promo_step(box, state)
    draft = await _last_text(box)
    assert "SALE20" in draft and "−20% к оплате" in draft
    assert "100" in draft and "7 дн." in draft

    box = _press("admin:promo:make")
    await admin.admin_promo_step(box, state)
    assert "создан" in await _last_text(box)
    assert await state.get_state() is None

    async with session_scope() as session:
        promo = await repo.get_promo_code(session, "SALE20")
        assert promo is not None
        assert promo.percent == 20
        assert promo.days == 0
        assert promo.max_uses == 100
        assert promo.expires_at is not None
        left = promo.expires_at - utcnow()
        assert timedelta(days=6, hours=23) < left < timedelta(days=7, hours=1)


async def test_days_wizard_unlimited_forever(owner):
    """Дни без лимита и срока: 7 дней, max_uses 0, expires None."""
    state = _state()

    for data in ("admin:promo:new", "admin:promo:type:days", "admin:promo:value:7"):
        await admin.admin_promo_step(_press(data), state)
    await admin.admin_promo_code(_typed("gift7"), state)
    await admin.admin_promo_step(_press("admin:promo:limit:0"), state)

    box = _press("admin:promo:ttl:0")
    await admin.admin_promo_step(box, state)
    draft = await _last_text(box)
    assert "7 дн. доступа" in draft
    assert "без лимита" in draft and "бессрочно" in draft

    await admin.admin_promo_step(_press("admin:promo:make"), state)

    async with session_scope() as session:
        promo = await repo.get_promo_code(session, "GIFT7")
        assert promo is not None
        assert promo.percent == 0
        assert promo.days == 7
        assert promo.max_uses == 0
        assert promo.expires_at is None


async def test_custom_value_typed(owner):
    """Своё значение: мусор отклоняется, число принимается."""
    state = _state()
    for data in ("admin:promo:new", "admin:promo:type:percent", "admin:promo:value:custom"):
        await admin.admin_promo_step(_press(data), state)
    assert await state.get_state() == OwnerStates.promo_custom.state

    bad = _typed("200")
    await admin.admin_promo_custom(bad, state)
    assert "от 1 до 90" in bad.edits[-1][0]
    assert await state.get_state() == OwnerStates.promo_custom.state

    good = _typed("25")
    await admin.admin_promo_custom(good, state)
    assert "−25% к оплате" in good.edits[-1][0]
    assert await state.get_state() == OwnerStates.promo_code.state


async def test_duplicate_code_stays_in_step(owner):
    """Занятый код отклоняется — мастер ждёт другой на том же шаге."""
    state = _state()
    async with session_scope() as session:
        await repo.create_promo_code(session, "TAKEN", 7)
        await session.commit()
    for data in ("admin:promo:new", "admin:promo:type:days", "admin:promo:value:7"):
        await admin.admin_promo_step(_press(data), state)

    typed = _typed("taken")
    await admin.admin_promo_code(typed, state)
    assert "уже существует" in typed.edits[-1][0]
    assert await state.get_state() == OwnerStates.promo_code.state


async def test_empty_code_rejected(owner):
    """Пустой код — не код: шаг повторяется."""
    state = _state()
    await state.set_state(OwnerStates.promo_code)

    typed = _typed("   ")
    await admin.admin_promo_code(typed, state)
    assert "не может быть пустым" in typed.edits[-1][0]
    assert await state.get_state() == OwnerStates.promo_code.state


async def test_stranger_is_denied(monkeypatch):
    """Чужой жмёт кнопки мастера — получает отказ и ничего больше."""
    monkeypatch.setattr(admin, "is_admin", lambda user_id: False)
    callback = _press("admin:promo:new")

    await admin.admin_promo_step(callback, _state())

    assert "Нет доступа" in callback.alerts
    assert callback.message.edits == []
