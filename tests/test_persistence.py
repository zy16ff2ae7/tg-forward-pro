"""Журнал и счётчики обязаны реально попадать в базу.

Исторически запись счётчика пересылок и строки журнала делалась внутри
``async with SessionLocal()`` без commit — сессия закрывалась, транзакция
откатывалась, и все успешные пересылки пропадали без следа. Эти тесты
закрывают дыру двумя способами: проверяют, что данные видны из другой
сессии, и следят, чтобы такой код больше не появился.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.db.models import ForwardLog, Rule

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Функции репозитория, которые что-то меняют в БД
WRITE_CALLS = {
    "add_account",
    "add_collected_items",
    "add_rule",
    "activate_subscription",
    "bank_days",
    "bump_forwarded",
    "create_payment",
    "delete_pending_login",
    "delete_rule",
    "get_or_create_user",
    "grant_trial",
    "log_forward",
    "mark_payment_paid",
    "mark_reminded",
    "save_pending_login",
    "set_account_error",
    "set_rule_archived",
    "unbank_days",
}


@pytest.fixture
async def rule_id(create_user, create_account):
    user_id = await create_user()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        created = await repo.add_rule(
            session,
            user_id=user_id,
            account_id=account_id,
            source_id=-100,
            source_title="Источник",
            target_id=-200,
            target_title="Приёмник",
        )
        return created.id


async def test_forward_journal_survives_session_close(rule_id):
    """Ровно тот порядок вызовов, что в forwarder.deliver() после успешной отправки."""
    async with session_scope() as session:
        await repo.bump_forwarded(session, rule_id, 5)
        await repo.log_forward(
            session,
            rule_id=rule_id,
            user_id=1,
            source_msg_id=1001,
            target_msg_id=2002,
            status="ok",
        )

    async with SessionLocal() as session:
        rule = await session.get(Rule, rule_id)
        assert rule.forwarded_count == 5

        total = await session.execute(
            select(func.count()).select_from(ForwardLog).where(ForwardLog.rule_id == rule_id)
        )
        assert total.scalar() == 1


async def test_error_journal_survives_session_close(rule_id):
    """То же для ветки с ошибкой: forwarder._log_error()."""
    async with session_scope() as session:
        await repo.log_forward(
            session,
            rule_id=rule_id,
            user_id=1,
            source_msg_id=1002,
            target_msg_id=None,
            status="error",
            error="FloodWaitError: 30",
        )

    async with SessionLocal() as session:
        entry = (
            await session.execute(select(ForwardLog).where(ForwardLog.rule_id == rule_id))
        ).scalar_one()
        assert entry.status == "error"
        assert entry.error == "FloodWaitError: 30"


async def test_session_scope_rolls_back_on_error(rule_id):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with session_scope() as session:
            await repo.bump_forwarded(session, rule_id, 7)
            raise Boom()

    async with SessionLocal() as session:
        rule = await session.get(Rule, rule_id)
        assert rule.forwarded_count == 0


def _uncommitted_writes(path: Path) -> list[int]:
    """Номера строк ``async with SessionLocal()``, где пишут, но не коммитят."""
    found: list[int] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.AsyncWith) or not node.items:
            continue
        # Контекст — это вызов SessionLocal(), а не имя, поэтому смотрим внутрь
        target = node.items[0].context_expr
        opened = getattr(target, "id", None) or getattr(getattr(target, "func", None), "id", None)
        if opened != "SessionLocal":
            continue

        calls = {
            call.func.attr
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        }
        body = ast.unparse(ast.Module(body=node.body, type_ignores=[]))
        if calls & WRITE_CALLS and "commit()" not in body:
            found.append(node.lineno)
    return found


def test_no_sessionlocal_block_writes_without_commit():
    offenders = {
        str(path.relative_to(APP_DIR.parent)): lines
        for path in sorted(APP_DIR.rglob("*.py"))
        if (lines := _uncommitted_writes(path))
    }
    assert offenders == {}, (
        "Запись без commit откатывается при выходе из блока. "
        "Используйте session_scope()."
    )
