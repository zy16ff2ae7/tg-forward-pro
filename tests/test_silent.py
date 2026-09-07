"""«Задача молчит»: сутки без доставок — письмо с тремя причинами.

Человек собрал задачу, а она молчит — и решает, что сервис не работает.
Пишем один раз: источник, приёмник и фильтры — типовые причины, тихий
источник — тоже ответ. Проверяем:

* молчащая сутки задача с живым сроком и сетью — письмо;
* доставлявшая, свежая, выключенная — тишина;
* без абонемента и без сети — молча помечаем (там свои письма);
* повторный проход молчит.
"""
from __future__ import annotations

from datetime import timedelta

from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog, Rule, Subscription
from app.main import notify_silent_rules
from app.telegram_client.manager import manager
from app.timeutil import utcnow
from tests.helpers import add_rule


class FakeBot:
    def __init__(self) -> None:
        self.dms: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.dms.append((chat_id, text))


async def _sub(user_id: int, days: float = 30) -> None:
    async with session_scope() as session:
        session.add(Subscription(
            user_id=user_id, active_until=utcnow() + timedelta(days=days)
        ))
        await session.commit()


async def _logged(rule_id: int, user_id: int, status: str = "ok") -> None:
    async with session_scope() as session:
        session.add(ForwardLog(
            rule_id=rule_id, user_id=user_id, source_msg_id=1, status=status
        ))
        await session.commit()


async def _rule_id(user_id: int) -> int:
    async with session_scope() as session:
        return (await repo.list_rules(session, user_id))[0].id


async def _marked(user_id: int) -> bool:
    async with session_scope() as session:
        rule = (await repo.list_rules(session, user_id))[0]
        return rule.silent_notified_at is not None


async def test_silent_day_gets_letter(create_user, create_account, monkeypatch):
    """Сутки тишины при живом сроке и сети — письмо с причинами."""
    monkeypatch.setattr(manager, "is_online", lambda account_id: True)
    user_id = await create_user()
    await _sub(user_id)
    await add_rule(
        user_id, await create_account(user_id),
        created_at=utcnow() - timedelta(hours=25),
        source_title="Источник", target_title="Приёмник",
    )
    bot = FakeBot()
    await notify_silent_rules(bot)  # type: ignore[arg-type]
    assert len(bot.dms) == 1
    assert "молчит сутки" in bot.dms[0][1]
    assert "В источнике тихо" in bot.dms[0][1]
    assert await _marked(user_id)

    quiet = FakeBot()
    await notify_silent_rules(quiet)  # type: ignore[arg-type]
    assert quiet.dms == []


async def test_working_fresh_and_stopped_stay_quiet(
    create_user, create_account, monkeypatch
):
    """Доставлявшая, свежая, выключенная, архивная — тишина."""
    monkeypatch.setattr(manager, "is_online", lambda account_id: True)
    working = await create_user()
    await _sub(working)
    await add_rule(
        working, await create_account(working),
        created_at=utcnow() - timedelta(hours=25),
    )
    await _logged(await _rule_id(working), working)

    fresh = await create_user()
    await _sub(fresh)
    await add_rule(fresh, await create_account(fresh))

    stopped = await create_user()
    await _sub(stopped)
    await add_rule(
        stopped, await create_account(stopped),
        created_at=utcnow() - timedelta(hours=25), enabled=False,
    )

    bot = FakeBot()
    await notify_silent_rules(bot)  # type: ignore[arg-type]
    assert bot.dms == []
    # Доставлявшая в выборку немых не входит — метки ей не положено.
    assert not await _marked(working)


async def test_dead_sub_and_offline_mark_silently(
    create_user, create_account, monkeypatch
):
    """Без абонемента и без сети — помечаем молча: там свои письма."""
    monkeypatch.setattr(manager, "is_online", lambda account_id: False)
    user_id = await create_user()
    await add_rule(
        user_id, await create_account(user_id),
        created_at=utcnow() - timedelta(hours=25),
    )
    bot = FakeBot()
    await notify_silent_rules(bot)  # type: ignore[arg-type]
    assert bot.dms == []
    assert await _marked(user_id)
