"""Сторож сервиса: авария — письмом владельцу, а не тишиной.

Всплеск ошибок за пять минут (с топом текстов) или пачка аккаунтов вне
сети — вести владельцу. Проверяем:

* всплеск — письмо с топом ошибок; повтор в кулдауне молчит;
* тише порога и нулевые пороги — тишина;
* аккаунты посыпались — отдельное письмо;
* без владельцев в настройках писать некому — тишина.
"""
from __future__ import annotations

import pytest

from app import main
from app.config import settings
from app.db.database import session_scope
from app.db.models import ForwardLog
from app.main import notify_watchdog
from app.telegram_client.manager import manager


@pytest.fixture(autouse=True)
def _cool_watcher():
    main._watch_last.clear()
    yield
    main._watch_last.clear()


class FakeBot:
    def __init__(self) -> None:
        self.dms: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.dms.append((chat_id, text))


async def _errors(count: int, text: str = "FloodWait 300") -> None:
    async with session_scope() as session:
        for pos in range(count):
            session.add(ForwardLog(
                rule_id=1, user_id=1, source_msg_id=pos,
                status="error", error=text,
            ))
        await session.commit()


async def _admin(monkeypatch, **fields) -> None:
    monkeypatch.setattr(settings, "admin_ids", [1])
    for key, value in fields.items():
        monkeypatch.setattr(settings, key, value)


async def test_spike_alerts_with_top_and_cools_down(monkeypatch):
    """Всплеск — письмо с топом; повтор в кулдауне молчит."""
    await _admin(monkeypatch, watch_errors=3)
    await _errors(2, "FloodWait 300")
    await _errors(2, "чат снесён")
    bot = FakeBot()
    await notify_watchdog(bot)  # type: ignore[arg-type]
    assert len(bot.dms) == 1
    assert "Всплеск ошибок: 4 за 5 мин" in bot.dms[0][1]
    assert "FloodWait 300 — 2" in bot.dms[0][1]

    quiet = FakeBot()
    await notify_watchdog(quiet)  # type: ignore[arg-type]
    assert quiet.dms == []


async def test_quiet_and_disabled_stay_silent(monkeypatch):
    """Тише порога и нулевой порог — тишина."""
    await _admin(monkeypatch, watch_errors=10)
    await _errors(2)
    bot = FakeBot()
    await notify_watchdog(bot)  # type: ignore[arg-type]
    assert bot.dms == []

    await _admin(monkeypatch, watch_errors=0)
    await _errors(50)
    await notify_watchdog(bot)  # type: ignore[arg-type]
    assert bot.dms == []


async def test_fallen_accounts_alert(monkeypatch, create_user, create_account):
    """Три аккаунта вне сети — отдельное письмо владельцу."""
    await _admin(monkeypatch, watch_errors=1000, watch_offline=3)
    user_id = await create_user()
    for _ in range(3):
        await create_account(user_id)
    monkeypatch.setattr(manager, "online_ids", lambda: iter([]))
    bot = FakeBot()
    await notify_watchdog(bot)  # type: ignore[arg-type]
    assert len(bot.dms) == 1
    assert "не в сети: 3" in bot.dms[0][1]


async def test_nobody_to_write_to(monkeypatch):
    """Без владельцев в настройках писать некому — тишина."""
    monkeypatch.setattr(settings, "admin_ids", [])
    await _errors(50)
    bot = FakeBot()
    await notify_watchdog(bot)  # type: ignore[arg-type]
    assert bot.dms == []


async def test_death_wave_alerts(monkeypatch, create_user, create_account):
    """Три смерти за сутки — письмо про волну, а не тишина."""
    from app.db import repo
    from app.db.database import session_scope
    from app.db.models import TelegramAccount
    from app.telegram_client.manager import SESSION_REVOKED

    await _admin(monkeypatch, watch_errors=1000, watch_offline=1000, watch_deaths=3)
    user_id = await create_user()
    for _ in range(3):
        account_id = await create_account(user_id)
        async with session_scope() as session:
            account = await session.get(TelegramAccount, account_id)
            await repo.set_account_error(session, account, SESSION_REVOKED)
    monkeypatch.setattr(manager, "online_ids", lambda: iter([]))
    bot = FakeBot()
    await notify_watchdog(bot)  # type: ignore[arg-type]
    assert len(bot.dms) == 1
    assert "умерло аккаунтов: 3" in bot.dms[0][1]
