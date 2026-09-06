"""Счётчики бота: меню с цифрами, спарклайн /stats, глобальные 24 ч в админке."""
from __future__ import annotations

from app.bot import keyboards as kb
from app.bot.handlers.menu import _menu_counts, _sparkline
from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog, Rule


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_main_menu_counters_in_labels():
    labels = _labels(
        kb.main_menu(False, rules_count=3, accounts_online=1, accounts_total=2, sub_active=True)
    )
    assert "📡 Мои правила (3)" in labels
    assert "👤 Аккаунты (1/2)" in labels
    assert "💳 Подписка ✅" in labels


def test_main_menu_without_counters_unchanged():
    labels = _labels(kb.main_menu(False))
    assert "📡 Мои правила" in labels
    assert "👤 Аккаунты" in labels
    assert "💳 Подписка" in labels


def test_sparkline_shapes():
    assert _sparkline([]) == "—"
    assert _sparkline([0, 0]) == "—"
    graph = _sparkline([0, 5, 10])
    assert len(graph) == 3
    assert graph[0] == "▁" and graph[-1] == "█"


async def test_menu_counts_offline(create_user, create_account):
    user_id = 768_200_001
    await create_user(id=user_id)
    account_id = await create_account(user_id, phone="+79160000001")
    async with session_scope() as session:
        session.add(
            Rule(
                user_id=user_id,
                account_id=account_id,
                source_id=-100100,
                source_title="Источник",
                target_id=-100200,
                target_title="Приёмник",
            )
        )
    counts = await _menu_counts(user_id)
    assert counts == {
        "rules_count": 1,
        "accounts_online": 0,
        "accounts_total": 1,
        "sub_active": False,
    }


async def test_forward_stats_global_24h(create_user, create_account):
    """Админка зовёт forward_stats с user_id=None: счёт идёт по всем юзерам."""
    for uid in (768_200_002, 768_200_003):
        await create_user(id=uid)
        account_id = await create_account(uid, phone=f"+7916{uid}")
        async with session_scope() as session:
            rule = Rule(
                user_id=uid,
                account_id=account_id,
                source_id=-100100,
                source_title="И",
                target_id=-100200,
                target_title="П",
            )
            session.add(rule)
            await session.flush()
            session.add(
                ForwardLog(rule_id=rule.id, user_id=uid, source_msg_id=1, status="ok")
            )
            session.add(
                ForwardLog(rule_id=rule.id, user_id=uid, source_msg_id=2, status="error")
            )
    async with session_scope() as session:
        day = await repo.forward_stats(session, None, 1)
        personal = await repo.forward_stats(session, 768_200_002, 1)
    assert day["total"] == 2
    assert personal["total"] == 1
