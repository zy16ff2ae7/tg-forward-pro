"""Настройки публикации через API: окно, закреп, ветка, часы жизни, упоминания.

Сервис умеет всё это давно, а кабинет и API — нет: проверяем, что создание и
правка несут новые поля в задачу, карточка их показывает, а ветка в режиме
«форвард» отклоняется сразу — а не молча теряет посты.
"""
from __future__ import annotations

from app.db import repo
from app.db.database import session_scope
from tests.helpers import TEST_USER_ID
from tests.test_buttons import chats_resolved, login_open  # noqa: F401 — фикстуры соседнего файла


async def _filters(rule_id: int, user_id: int) -> dict:
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
        assert rule is not None
        return dict(rule.filters or {})


async def _make(client, auth_headers, create_account, payload: dict):
    # /api/me — первым: он заводит пользователя, без него аккаунт — сирота.
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    response = await client.post(
        "/api/tasks", json={"account_id": account_id, **payload}, headers=auth_headers
    )
    assert response.status == 201, await response.text()
    return (await response.json())["task"]


async def test_forward_publish_settings_roundtrip(
    client, auth_headers, create_account, login_open, chats_resolved
):
    """Пересылка: окно, закреп, ветка, часы жизни, джиттер и лимит — в задачу."""
    task = await _make(
        client,
        auth_headers,
        create_account,
        {
            "command": "copy_channel",
            "source": "@src",
            "target": "@dst",
            "mode": "copy",
            "start": "09:00",
            "end": "21:00",
            "pin_on_send": True,
            "topic": 7,
            "autodelete_hours": 2.5,
            "delay_jitter": 30,
            "daily_cap": 200,
        },
    )
    assert task["window_start"] == "09:00"
    assert task["window_end"] == "21:00"
    assert task["pin_on_send"] is True
    assert task["topic_id"] == 7
    assert task["autodelete_hours"] == 2.5
    assert task["daily_cap"] == 200
    edit = task["edit"]
    assert edit["start"] == "09:00"
    assert edit["end"] == "21:00"
    assert edit["pin_on_send"] is True
    assert edit["topic"] == 7
    assert edit["autodelete_hours"] == 2.5
    assert edit["delay_jitter"] == 30
    assert edit["daily_cap"] == 200
    saved = await _filters(task["id"], TEST_USER_ID)
    assert saved["window_start"] == "09:00"
    assert saved["topic_id"] == 7
    assert saved["autodelete_hours"] == 2.5
    assert saved["delay_jitter"] == 30


async def test_forward_topic_is_rejected_in_forward_mode(
    client, auth_headers, create_account, login_open, chats_resolved
):
    """Ветка + режим «форвард» = 400 с причиной, а не задача без постов."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    response = await client.post(
        "/api/tasks",
        json={
            "command": "copy_channel",
            "account_id": account_id,
            "source": "@src",
            "target": "@dst",
            "mode": "forward",
            "topic": 5,
        },
        headers=auth_headers,
    )
    assert response.status == 400
    assert "Ветка" in (await response.json())["error"]


async def test_forward_topic_is_rejected_on_update(
    client, auth_headers, create_account, login_open, chats_resolved
):
    """Правка тоже не пускает ветку мимо режима «копия» — с обеих сторон."""
    task = await _make(
        client,
        auth_headers,
        create_account,
        {
            "command": "copy_channel",
            "source": "@src",
            "target": "@dst",
            "mode": "copy",
            "topic": 5,
        },
    )
    # Переключение готовой задачи с веткой в форвард — нельзя.
    response = await client.patch(
        f"/api/tasks/{task['id']}", json={"mode": "forward"}, headers=auth_headers
    )
    assert response.status == 400
    assert "Ветка" in (await response.json())["error"]
    # И наоборот: ветка в задачу, которая уже в форварде, — тоже нельзя.
    plain = await _make(
        client,
        auth_headers,
        create_account,
        {
            "command": "copy_channel",
            "source": "@src",
            "target": "@dst",
            "mode": "forward",
        },
    )
    response = await client.patch(
        f"/api/tasks/{plain['id']}", json={"topic": 5}, headers=auth_headers
    )
    assert response.status == 400
    assert "Ветка" in (await response.json())["error"]


async def test_mailing_publish_settings_roundtrip(
    client, auth_headers, create_account, login_open, chats_resolved
):
    """Рассылка: закреп, ветка, часы жизни, упоминания, джиттеры, лимит."""
    task = await _make(
        client,
        auth_headers,
        create_account,
        {
            "command": "sender",
            "send_mode": "queue",
            "message": "пост",
            "targets": ["@ch-1", "@ch-2"],
            "pin_on_send": True,
            "topic": 3,
            "autodelete_hours": 12,
            "mention_all": True,
            "gap_jitter": 10,
            "cycle_jitter": 20,
            "daily_cap": 500,
        },
    )
    assert task["kind"] == "mailing"
    assert task["mention_all"] is True
    edit = task["edit"]
    assert edit["pin_on_send"] is True
    assert edit["topic"] == 3
    assert edit["autodelete_hours"] == 12
    assert edit["mention_all"] is True
    assert edit["gap_jitter"] == 10
    assert edit["cycle_jitter"] == 20
    assert edit["daily_cap"] == 500
    saved = await _filters(task["id"], TEST_USER_ID)
    assert saved["mention_all"] is True
    assert saved["cycle_jitter"] == 20


async def test_poster_gap_jitter_roundtrip(
    client, auth_headers, create_account, login_open, chats_resolved
):
    """Постинг: разброс пауз и упоминания доходят до задачи."""
    task = await _make(
        client,
        auth_headers,
        create_account,
        {
            "command": "sender",
            "send_mode": "schedule",
            "message": "пост",
            "targets": ["@ch-1"],
            "interval": 5,
            "mention_all": True,
            "gap_jitter": 15,
            "daily_cap": 100,
        },
    )
    assert task["kind"] == "poster"
    assert task["mention_all"] is True
    assert task["edit"]["gap_jitter"] == 15
    assert task["edit"]["mention_all"] is True
    assert task["edit"]["daily_cap"] == 100


async def test_broadcast_window_and_mention_roundtrip(
    client, auth_headers, create_account, login_open, chats_resolved
):
    """Веер: окно, закреп, ветка, часы жизни, упоминания, лимит."""
    task = await _make(
        client,
        auth_headers,
        create_account,
        {
            "command": "broadcast",
            "source": "@src",
            "targets": ["@ch-1", "@ch-2"],
            "start": "10:00",
            "end": "20:00",
            "pin_on_send": True,
            "topic": 9,
            "autodelete_hours": 1,
            "mention_all": True,
            "daily_cap": 300,
        },
    )
    assert task["window_start"] == "10:00"
    assert task["mention_all"] is True
    edit = task["edit"]
    assert edit["start"] == "10:00"
    assert edit["end"] == "20:00"
    assert edit["topic"] == 9
    assert edit["mention_all"] is True
    assert edit["daily_cap"] == 300


async def test_partial_update_preserves_publish_settings(
    client, auth_headers, create_account, login_open, chats_resolved
):
    """Правка одного поля не сносит остальные настройки публикации."""
    task = await _make(
        client,
        auth_headers,
        create_account,
        {
            "command": "copy_channel",
            "source": "@src",
            "target": "@dst",
            "pin_on_send": True,
            "topic": 7,
            "delay_jitter": 30,
            "daily_cap": 200,
        },
    )
    response = await client.patch(
        f"/api/tasks/{task['id']}", json={"alerts": False}, headers=auth_headers
    )
    assert response.status == 200, await response.text()
    edit = (await response.json())["task"]["edit"]
    assert edit["pin_on_send"] is True
    assert edit["topic"] == 7
    assert edit["delay_jitter"] == 30
    assert edit["daily_cap"] == 200
