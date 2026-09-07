"""Апсейл в упор: потолок бесплатного режима продаёт абонемент.

Бесплатно — 3 задачи, дальше стена. Раньше стена была сухой («оформите
абонемент» + редирект в бота), теперь называет цену безлимита, а кабинет
продаёт оплату в один тап из пейволла. Проверяем:

* текст стены называет и потолок, и цену;
* четвёртая задача без абонемента — 402 с флагом;
* с абонементом четвёртая создаётся как обычно.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.bot.handlers.rules import free_limit_text
from app.config import settings
from app.db.database import session_scope
from app.db.models import Subscription
from app.telegram_client.manager import manager
from app.timeutil import utcnow
from tests.helpers import TEST_USER_ID, add_rule


@pytest.fixture
def login_open(monkeypatch):
    from app import accounts_login

    monkeypatch.setattr(accounts_login, "require_enabled", lambda: None)


@pytest.fixture
def chats_resolved(monkeypatch):
    async def fake_resolve_many(account_id: int, queries):
        refs = [str(raw or "").strip() for raw in queries]
        return {ref: (-1000 - pos, ref) for pos, ref in enumerate(refs) if ref}

    monkeypatch.setattr(manager, "resolve_many", fake_resolve_many)


def test_wall_names_ceiling_and_price():
    """Стена: сколько бесплатно и почём безлимит."""
    text = free_limit_text()
    assert str(settings.max_rules_free) in text
    assert str(settings.price_stars) in text


async def test_fourth_task_without_sub_is_paywall(
    client, auth_headers, create_account, create_user
):
    """Без абонемента четвёртая задача — 402 с флагом для пейволла."""
    await create_user(id=TEST_USER_ID)
    account_id = await create_account(TEST_USER_ID)
    for _ in range(settings.max_rules_free):
        await add_rule(TEST_USER_ID, account_id)
    resp = await client.post(
        "/api/tasks",
        json={"command": "copy_channel", "account_id": account_id,
              "source": "@src", "target": "@dst"},
        headers=auth_headers,
    )
    assert resp.status == 402
    body = await resp.json()
    assert body.get("need_subscription") is True


async def test_subscriber_passes_the_wall(
    client, auth_headers, create_account, create_user, login_open, chats_resolved
):
    """С абонементом четвёртая задача создаётся как обычно."""
    await create_user(id=TEST_USER_ID)
    account_id = await create_account(TEST_USER_ID)
    for _ in range(settings.max_rules_free):
        await add_rule(TEST_USER_ID, account_id)
    async with session_scope() as session:
        session.add(Subscription(
            user_id=TEST_USER_ID, active_until=utcnow() + timedelta(days=30)
        ))
        await session.commit()
    resp = await client.post(
        "/api/tasks",
        json={"command": "copy_channel", "account_id": account_id,
              "source": "@src", "target": "@dst"},
        headers=auth_headers,
    )
    assert resp.status == 201, await resp.text()
