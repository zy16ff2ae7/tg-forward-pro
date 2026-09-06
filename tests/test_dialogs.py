"""Уведомления из диалогов: боты, архив и мут не будят.

Личка шумит: боты сыплют служебным, архив и мут человек глушил сам — и ждёт,
что уведомления это уважают. Флаги чата задача читает из кэша диалогов, а не
ходит в Telegram ради каждого письма.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.telegram_client import jobs
from app.telegram_client.filters import FilterConfig
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID
from tests.test_delivery_fixes import _db_rule, _snapshot
from tests.test_task_edit import make_task

from tests.test_many_chats import (  # noqa: F401
    login_open,
    many_chats_resolved,
    one_shot_stubbed,
)


def dm(chat_id: int, sender: SimpleNamespace, text: str = "привет") -> SimpleNamespace:
    return SimpleNamespace(
        id=chat_id * 10, is_private=True, out=False, message=text, media=None,
        chat_id=chat_id, sender_id=sender.id, sender=sender,
    )


def human(user_id: int) -> SimpleNamespace:
    return SimpleNamespace(id=user_id, bot=False, first_name="Человек")


def flags_dialog(chat_id: int, **flags) -> dict:
    row = {"id": chat_id, "title": "чат", "archived": False, "muted": False}
    row.update(flags)
    return row


async def test_dialogs_skip_bot_senders_by_default(create_user, create_account, monkeypatch):
    rule = await _db_rule(create_user, create_account, kind="dialogs", with_trial=True)
    calls = []
    monkeypatch.setattr(jobs, "send_copy", _recorder(calls))
    message = dm(501, SimpleNamespace(id=501, bot=True, first_name="Бот"))
    await jobs._dialogs(object(), message, _snapshot(rule))
    assert calls == []


def _recorder(calls: list):
    async def fake_send_copy(*args, **kwargs):
        calls.append(args)

    return fake_send_copy


async def test_dialogs_skip_archived_and_muted(create_user, create_account, monkeypatch):
    rule = await _db_rule(create_user, create_account, kind="dialogs", with_trial=True)
    calls = []
    monkeypatch.setattr(jobs, "send_copy", _recorder(calls))

    async def fake_list_dialogs(account_id: int, limit: int = 0):
        assert account_id == rule.account_id
        return [flags_dialog(501, archived=True), flags_dialog(502, muted=True)]

    monkeypatch.setattr(manager, "list_dialogs", fake_list_dialogs)
    await jobs._dialogs(object(), dm(501, human(501)), _snapshot(rule))
    await jobs._dialogs(object(), dm(502, human(502)), _snapshot(rule))
    assert calls == []


async def test_dialogs_flags_can_be_relaxed(create_user, create_account, monkeypatch):
    rule = await _db_rule(create_user, create_account, kind="dialogs", with_trial=True)
    calls = []
    monkeypatch.setattr(jobs, "send_copy", _recorder(calls))

    async def fake_list_dialogs(account_id: int, limit: int = 0):
        return [flags_dialog(501, archived=True, muted=True)]

    monkeypatch.setattr(manager, "list_dialogs", fake_list_dialogs)
    conf = FilterConfig(ignore_bots=False, ignore_archived=False, ignore_muted=False)
    await jobs._dialogs(object(), dm(501, human(501)), _snapshot(rule, filters=conf))
    assert len(calls) == 1


async def test_dialogs_plain_dm_notifies(create_user, create_account, monkeypatch):
    rule = await _db_rule(create_user, create_account, kind="dialogs", with_trial=True)
    calls = []
    monkeypatch.setattr(jobs, "send_copy", _recorder(calls))

    async def fake_list_dialogs(account_id: int, limit: int = 0):
        return [flags_dialog(501)]

    monkeypatch.setattr(manager, "list_dialogs", fake_list_dialogs)
    await jobs._dialogs(object(), dm(501, human(501)), _snapshot(rule))
    assert len(calls) == 1


async def test_dialogs_api_stores_flags(
    client, auth_headers, create_account, login_open, many_chats_resolved, one_shot_stubbed
):
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    task = await make_task(
        client, auth_headers, account_id,
        command="dialogs", target="@ch-1", keywords=["привет"], ignore_bots=False,
    )
    assert task["edit"]["ignore_bots"] is False
    assert task["edit"]["ignore_archived"] is True
    assert task["edit"]["ignore_muted"] is True


async def test_dialogs_catalog_lists_flags(client, auth_headers):
    response = await client.get("/api/commands", headers=auth_headers)
    commands = {item["id"]: item for item in (await response.json())["commands"]}
    optional = commands["dialogs"]["optional"]
    assert {"keywords", "ignore_bots", "ignore_archived", "ignore_muted"} <= set(optional)
