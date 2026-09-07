"""Контракт мастера первой задачи: то, на что визард полагается.

Мастер строит обычную пересылку copy_channel двумя полями — источник
и приёмник. Окна у пересылки нет: зеркало идёт в реальном времени.
Проверяем:

* payload мастера создаёт задачу;
* приёмник, совпавший с источником, отклоняется с понятной причиной.
"""
from __future__ import annotations

import pytest

from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID


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


async def test_wizard_payload_builds_forward(
    client, auth_headers, create_user, create_account, login_open, chats_resolved
):
    """Источник + приёмник — задача создана, пересылка обычная."""
    await create_user(id=TEST_USER_ID)
    account_id = await create_account(TEST_USER_ID)
    resp = await client.post(
        "/api/tasks",
        json={
            "command": "copy_channel", "account_id": account_id,
            "source": "@src", "target": "@dst",
        },
        headers=auth_headers,
    )
    assert resp.status == 201, await resp.text()
    task = (await resp.json())["task"]
    assert task["kind"] == "forward"


async def test_wizard_refuses_same_chat(
    client, auth_headers, create_user, create_account, login_open, chats_resolved
):
    """Приёмник = источник — 400 с причиной, а не молчаливая задача."""
    await create_user(id=TEST_USER_ID)
    account_id = await create_account(TEST_USER_ID)
    resp = await client.post(
        "/api/tasks",
        json={
            "command": "copy_channel", "account_id": account_id,
            "source": "@same", "target": "@same",
        },
        headers=auth_headers,
    )
    assert resp.status == 400
    assert "совпадают" in (await resp.json())["error"]
