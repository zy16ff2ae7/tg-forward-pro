"""Фоновый цикл: проверки платежей, напоминания и честная остановка.

Цикл живёт всё время работы сервиса и трогает деньги и рассылку, поэтому важны
три свойства:

* первый проход — сразу, без ожидания: пока сервис перезапускался, перевод мог
  уже прийти, и держать человека без доступа пять минут не за что;
* сбой одной проверки не уносит с собой остальные и сам цикл — иначе одна
  недоступность провайдера тихо выключает и напоминания, и вторую оплату;
* цикл останавливается по команде: задача, продолжающая работать после
  shutdown, пишет в уже закрытую БД.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app import main
from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.db.models import Subscription
from tests.helpers import RecordingBot


@pytest.fixture
def fast_loop(monkeypatch):
    """Цикл без пауз: иначе тест ждал бы пять минут до второго прохода."""
    monkeypatch.setattr(main, "BACKGROUND_INTERVAL_SECONDS", 0)


async def run_until(task: asyncio.Task, condition, timeout: float = 2.0) -> None:
    """Ждёт выполнения условия, затем гасит задачу.

    Ждём именно условие, а не «немного времени»: sleep(0.1) в тесте цикла — это
    либо лишняя секунда на прогоне, либо мигающий тест на загруженной машине.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while not condition():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("фоновый цикл не дошёл до ожидаемого состояния")
            if task.done():
                await task  # если цикл упал — покажем настоящую причину
                raise AssertionError("фоновый цикл завершился раньше времени")
            await asyncio.sleep(0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.fixture
def recorded_checks(monkeypatch):
    """Подменяет три фоновые проверки на запись вызовов.

    Настоящие ходят в TronGrid и ЮKassa — в тесте цикла проверяется порядок и
    живучесть, а не они сами (для них есть tests/test_paylink.py).
    """
    calls: list[str] = []

    def stub(name: str, error: Exception | None = None):
        async def check(bot):
            calls.append(name)
            if error is not None:
                raise error
            return 0

        return check

    def install(*, usdt_error: Exception | None = None) -> list[str]:
        monkeypatch.setattr(main.crypto, "check_pending", stub("usdt", usdt_error))
        monkeypatch.setattr(main.yookassa, "check_pending", stub("yookassa"))
        monkeypatch.setattr(main, "notify_expiring", stub("reminders"))
        return calls

    return install


# ──────────────────────────────── ход цикла ───────────────────────────────────


async def test_first_pass_runs_without_waiting(recorded_checks):
    """Пауза — после проверок, а не до них: иначе перезапуск стоит пять минут."""
    calls = recorded_checks()

    task = asyncio.create_task(main.background_loop(RecordingBot()))
    await run_until(task, lambda: len(calls) >= 3)

    assert calls[:3] == ["usdt", "yookassa", "reminders"]


async def test_failing_check_does_not_take_down_the_others(recorded_checks):
    """Недоступный TronGrid не должен отменять зачисление картой и напоминания."""
    calls = recorded_checks(usdt_error=RuntimeError("TronGrid недоступен"))

    task = asyncio.create_task(main.background_loop(RecordingBot()))
    await run_until(task, lambda: len(calls) >= 3)

    assert calls[:3] == ["usdt", "yookassa", "reminders"]


async def test_loop_survives_a_failing_check(recorded_checks, fast_loop):
    """Сбой провайдера не должен глушить цикл до перезапуска сервиса."""
    calls = recorded_checks(usdt_error=RuntimeError("TronGrid недоступен"))

    task = asyncio.create_task(main.background_loop(RecordingBot()))
    await run_until(task, lambda: calls.count("usdt") >= 2)

    assert calls.count("usdt") >= 2


# ─────────────────────────────── остановка ────────────────────────────────────


async def test_stop_cancels_the_running_loop(recorded_checks, monkeypatch):
    """На shutdown цикл обязан остановиться: иначе он пишет в закрытую БД."""
    calls = recorded_checks()
    task = asyncio.create_task(main.background_loop(RecordingBot()))
    monkeypatch.setattr(main, "_background_task", task)

    while not calls:
        await asyncio.sleep(0)
    await main.stop_background_loop()

    assert task.cancelled()
    # Ссылку обязательно забываем: иначе повторный стоп ждёт мёртвую задачу.
    assert main._background_task is None


async def test_stop_without_running_loop_is_noop(monkeypatch):
    """Падение на старте не должно превращаться в падение на выходе."""
    monkeypatch.setattr(main, "_background_task", None)
    await main.stop_background_loop()

    assert main._background_task is None


async def test_second_stop_is_harmless(recorded_checks, monkeypatch):
    calls = recorded_checks()
    task = asyncio.create_task(main.background_loop(RecordingBot()))
    monkeypatch.setattr(main, "_background_task", task)

    while not calls:
        await asyncio.sleep(0)
    await main.stop_background_loop()
    await main.stop_background_loop()

    assert task.cancelled()


# ──────────────────── напоминания о конце абонемента ──────────────────────────


async def make_subscription(user_id: int, *, days_left: float, reminded: bool = False) -> None:
    """Абонемент с заданным остатком: ждать реального конца срока нечем."""
    async with session_scope() as session:
        session.add(
            Subscription(
                user_id=user_id,
                active_until=repo.utcnow() + timedelta(days=days_left),
                reminded_at=repo.utcnow() if reminded else None,
            )
        )


async def reminded_at(user_id: int):
    async with SessionLocal() as session:
        sub = await session.get(Subscription, user_id)
        return sub.reminded_at if sub is not None else None


async def test_reminder_goes_out_before_the_end(create_user):
    user_id = await create_user()
    await make_subscription(user_id, days_left=1)
    bot = RecordingBot()

    await main.notify_expiring(bot)

    assert bot.recipients == [user_id]
    assert "заканчивается" in bot.messages[0][1]
    assert await reminded_at(user_id) is not None


async def test_reminder_is_sent_only_once(create_user):
    """Иначе каждые пять минут человеку приходит одно и то же напоминание."""
    user_id = await create_user()
    await make_subscription(user_id, days_left=1)
    bot = RecordingBot()

    await main.notify_expiring(bot)
    await main.notify_expiring(bot)

    assert bot.recipients == [user_id]


async def test_long_subscription_is_left_alone(create_user):
    user_id = await create_user()
    await make_subscription(user_id, days_left=30)
    bot = RecordingBot()

    await main.notify_expiring(bot)

    assert bot.messages == []
    assert await reminded_at(user_id) is None


async def test_expired_subscription_is_not_reminded(create_user):
    """Срок уже вышел — это не «скоро закончится», а другой разговор."""
    user_id = await create_user()
    await make_subscription(user_id, days_left=-1)
    bot = RecordingBot()

    await main.notify_expiring(bot)

    assert bot.messages == []


async def test_blocked_user_does_not_stop_the_rest(create_user):
    """Один заблокировавший бота не должен лишать напоминания остальных."""
    blocked = await create_user()
    normal = await create_user()
    await make_subscription(blocked, days_left=1)
    await make_subscription(normal, days_left=2)
    bot = RecordingBot(fail_for={blocked})

    await main.notify_expiring(bot)

    assert bot.recipients == [normal]
    # Метку ставим всем: бесконечно долбиться в заблокированного бессмысленно.
    assert await reminded_at(blocked) is not None
