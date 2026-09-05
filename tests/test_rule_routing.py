"""Маршрутизация правил: какие задачи слушают сообщения, а какие — нет.

Регрессия, которую ловит этот файл: задача «парсер аудитории» запускается
только вручную, но попадала в кэш слушающих правил. На каждое входящее
сообщение вызывался run_job, не находил обработчик и писал в журнал
«Неизвестный тип задачи: parser».
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog, Rule, Subscription
from app.telegram_client import jobs
from app.telegram_client.manager import manager
from app.telegram_client.types import RuleSnapshot

pytestmark = pytest.mark.asyncio


async def _add_rule(user_id: int, account_id: int, kind: str, **kwargs) -> int:
    async with session_scope() as session:
        rule = Rule(
            user_id=user_id,
            account_id=account_id,
            source_id=kwargs.get("source_id", -1001),
            target_id=kwargs.get("target_id", -1002),
            kind=kind,
            enabled=kwargs.get("enabled", True),
            archived=kwargs.get("archived", False),
        )
        session.add(rule)
        await session.flush()
        return rule.id


def _listening_rule_ids() -> set[int]:
    """Все правила, которые попадают в кэш «слушающих» входящие сообщения."""
    ids = {rule.id for rules in manager._rules.values() for rule in rules}
    ids |= {rule.id for rules in manager._floating_rules.values() for rule in rules}
    return ids


async def test_forward_rule_listens_to_messages(create_user, create_account):
    """Обычная пересылка должна реагировать на сообщения источника."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule_id = await _add_rule(user_id, account_id, "forward")

    await manager.refresh_rules()

    assert rule_id in _listening_rule_ids()


async def test_parser_rule_does_not_listen(create_user, create_account):
    """Парсер запускается только кнопкой — слушать сообщения он не должен."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule_id = await _add_rule(user_id, account_id, "parser")

    await manager.refresh_rules()

    assert rule_id not in _listening_rule_ids()


async def test_poster_rule_goes_to_scheduler_only(create_user, create_account):
    """Авто-постинг живёт по расписанию: в планировщике есть, в слушателях нет."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule_id = await _add_rule(user_id, account_id, "poster")

    await manager.refresh_rules()

    assert rule_id not in _listening_rule_ids()
    assert rule_id in {rule.id for rule in manager._poster_rules}


async def test_dialogs_rule_is_floating(create_user, create_account):
    """Уведомления из диалогов слушают все личные чаты, а не один источник."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule_id = await _add_rule(user_id, account_id, "dialogs")

    await manager.refresh_rules()

    assert rule_id in {r.id for rules in manager._floating_rules.values() for r in rules}


async def test_disabled_rule_does_not_listen(create_user, create_account):
    """Выключенная задача не должна ничего обрабатывать."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    await _add_rule(user_id, account_id, "forward", enabled=False)

    await manager.refresh_rules()

    assert not _listening_rule_ids()


async def test_manual_task_writes_nothing_to_the_journal(create_user, create_account):
    """Даже если сообщение до парсера долетело, в журнал оно попасть не должно.

    Карточка задачи читает журнал и по последней записи решает, сломана задача
    или работает. Пока внутренняя «Неизвестный тип задачи» писалась туда как
    обычный сбой, у живого парсера навсегда оставался красный «сбой» с текстом,
    который человеку ничего не говорит и починить который он не может.
    """
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule_id = await _add_rule(user_id, account_id, "parser")
    async with session_scope() as session:
        session.add(
            Subscription(user_id=user_id, active_until=repo.utcnow() + timedelta(days=1))
        )

    await jobs.run_job(
        client=None,
        message=SimpleNamespace(id=1, text="привет", action=None, media=None),
        rule=RuleSnapshot(
            id=rule_id,
            user_id=user_id,
            target_id=-1002,
            mode="copy",
            delay_seconds=0,
            account_id=account_id,
            kind="parser",
        ),
    )

    async with session_scope() as session:
        rows = (
            await session.execute(select(ForwardLog).where(ForwardLog.rule_id == rule_id))
        ).scalars().all()
    assert rows == []
    async with session_scope() as session:
        assert await repo.task_health(session, [rule_id]) == {}
