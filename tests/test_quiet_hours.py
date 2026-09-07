"""Тихие часы: окно отправки для пересылки, веера и рассылки.

Постер окном уже жил, а пересылка слала ночью и рассылка — тоже. Теперь окно
одно на всех: пересылка откладывает сообщение до открытия (а не выбрасывает),
рассылка пропускает тики и продолжает круг с того же чата.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

from app.telegram_client import jobs
from app.telegram_client.filters import FilterConfig
from app.telegram_client.manager import manager
from app.telegram_client.queue import DeliveryQueue
from app.telegram_client.types import RuleSnapshot

HOUR = 3600
DAY = 24 * HOUR
# Полночь UTC: от неё строим «сейчас» с известным временем на часах.
MIDNIGHT = 1_800_000_000 - 1_800_000_000 % DAY


def _filters(start: str = "09:00", end: str = "18:00", tz: int | None = 0) -> FilterConfig:
    return FilterConfig(window_start=start, window_end=end, window_tz=tz)


# ────────────────────────── арифметика ожидания ───────────────────────────────


def test_inside_window_sends_now():
    """Внутри окна ждать нечего — ни секундой дольше."""
    assert jobs.quiet_wait_seconds(_filters(), now=MIDNIGHT + 12 * HOUR) == 0


def test_before_window_waits_until_start():
    """До открытия — ждём ровно до старта, а не «примерно до утра»."""
    assert jobs.quiet_wait_seconds(_filters(), now=MIDNIGHT + 7 * HOUR) == 2 * HOUR


def test_after_window_waits_until_tomorrow():
    """После закрытия — ждём до завтрашнего старта."""
    assert jobs.quiet_wait_seconds(_filters(), now=MIDNIGHT + 20 * HOUR) == 13 * HOUR


def test_overnight_window_inside_is_free():
    """Ночное окно 22:00–06:00: в 23:00 отправляем сразу."""
    filters = _filters("22:00", "06:00")
    assert jobs.quiet_wait_seconds(filters, now=MIDNIGHT + 23 * HOUR) == 0
    assert jobs.quiet_wait_seconds(filters, now=MIDNIGHT + 3 * HOUR) == 0


def test_overnight_window_waits_until_evening():
    """Ночное окно: днём ждём до вечернего старта."""
    filters = _filters("22:00", "06:00")
    assert jobs.quiet_wait_seconds(filters, now=MIDNIGHT + 12 * HOUR) == 10 * HOUR


def test_window_uses_owner_clock():
    """Окно 12:00–13:00 по Москве открыто в 09:30 UTC."""
    filters = _filters("12:00", "13:00", tz=180)
    assert jobs.quiet_wait_seconds(filters, now=MIDNIGHT + 9 * HOUR + 30 * 60) == 0
    # 13:30 по Москве — закрыто, ждём до завтрашних 12:00 (22.5 часа).
    assert jobs.quiet_wait_seconds(filters, now=MIDNIGHT + 10 * HOUR + 30 * 60) == 81000


def test_default_window_is_always_open():
    """Окно по умолчанию (весь день) никого не держит."""
    filters = FilterConfig()
    for hour in (0, 3, 12, 23):
        assert jobs.quiet_wait_seconds(filters, now=MIDNIGHT + hour * HOUR) == 0
        assert jobs.window_allows(filters, now=MIDNIGHT + hour * HOUR)


def test_window_allows_mirrors_wait():
    """«Можно слать» и «ждать» не спорят друг с другом."""
    filters = _filters()
    assert jobs.window_allows(filters, now=MIDNIGHT + 12 * HOUR)
    assert not jobs.window_allows(filters, now=MIDNIGHT + 2 * HOUR)


# ──────────────────── очередь: ожидание — часть задержки ──────────────────────


def _snapshot(filters: FilterConfig, kind: str = "forward") -> RuleSnapshot:
    return RuleSnapshot(
        id=1, user_id=100, target_id=200, mode="copy", delay_seconds=10,
        account_id=1, filters=filters, kind=kind,
    )


async def test_queue_adds_quiet_to_rule_delay(monkeypatch):
    """Задержка правила и тихие часы складываются, а не перекрывают друг друга."""
    monkeypatch.setattr(jobs, "quiet_wait_seconds", lambda config, **_: 3600)
    calls: list[int] = []

    async def handler(client, message, rule) -> bool:
        return True

    queue = DeliveryQueue(handler, workers=1, maxsize=10)
    monkeypatch.setattr(queue, "_spawn_delayed", lambda job, delay: calls.append(delay))

    assert queue.submit(None, None, _snapshot(_filters())) is True
    assert calls == [10 + 3600]


async def test_queue_skips_quiet_for_moderation_kinds(monkeypatch):
    """Мут и подписки ночью работают: тишина — только для отправки постов."""

    async def handler(client, message, rule) -> bool:
        return True

    def _boom(config, **_):
        raise AssertionError("у модерации окна спрашивать нечего")

    monkeypatch.setattr(jobs, "quiet_wait_seconds", _boom)
    queue = DeliveryQueue(handler, workers=1, maxsize=10)

    assert queue.submit(None, None, _snapshot(_filters(), kind="mute")) is True


async def test_restore_delay_ignores_quiet(monkeypatch):
    """Восстановление после перезапуска окно не пересчитывает: оно уже в due_at."""

    async def handler(client, message, rule) -> bool:
        return True

    def _boom(config, **_):
        raise AssertionError("восстановление идёт мимо окна")

    monkeypatch.setattr(jobs, "quiet_wait_seconds", _boom)
    queue = DeliveryQueue(handler, workers=1, maxsize=10)

    assert DeliveryQueue._delay_for(_snapshot(_filters()), 120) == 120


def _hhmm(sec: int) -> str:
    sec %= DAY
    return f"{sec // HOUR:02d}:{(sec % HOUR) // 60:02d}"


# ─────────────────────── рассылка ждёт открытия окна ──────────────────────────


import pytest  # noqa: E402

from tests.test_mailing import FakeClient, make_mailing  # noqa: E402


@pytest.fixture
def clean_mailing():
    yield
    manager._mailing_rules = []
    manager._mailing_state.clear()
    manager._clients.clear()


async def test_mailing_keeps_silent_outside_window(create_user, create_account, clean_mailing):
    """Вне окна рассылка молчит и позицию в круге не теряет."""
    now_utc = int(time.time()) % DAY
    # Окно откроется через час: сейчас тихо.
    _rule_id, _user_id, account_id = await make_mailing(
        create_user, create_account,
        targets=[-1001, -1002], texts=["всем привет"],
        window_start=_hhmm(now_utc + HOUR), window_end=_hhmm(now_utc + 2 * HOUR),
        window_tz=0,
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._mailing_tick()
    await manager._mailing_tick()

    assert client.sent == []
    # Позицию не сдвинули: первый чат круга дождётся открытия окна.
    assert manager._mailing_state == {}


async def test_mailing_continues_inside_window(create_user, create_account, clean_mailing):
    """В окне рассылка идёт как обычно — окно её не душит."""
    now_utc = int(time.time()) % DAY
    _rule_id, _user_id, account_id = await make_mailing(
        create_user, create_account,
        targets=[-1001, -1002], texts=["всем привет"],
        window_start=_hhmm(now_utc - HOUR), window_end=_hhmm(now_utc + HOUR),
        window_tz=0,
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._mailing_tick()

    assert client.recipients == [-1001]


async def test_queue_adds_quiet_for_broadcast(monkeypatch):
    """Веер — тоже пересылка: ночью пост ждёт утра, а не уходит сразу."""
    monkeypatch.setattr(jobs, "quiet_wait_seconds", lambda config, **_: 3600)
    calls: list[int] = []

    async def handler(client, message, rule) -> bool:
        return True

    queue = DeliveryQueue(handler, workers=1, maxsize=10)
    monkeypatch.setattr(queue, "_spawn_delayed", lambda job, delay: calls.append(delay))

    assert queue.submit(None, None, _snapshot(_filters(), kind="broadcast")) is True
    assert calls == [10 + 3600]
