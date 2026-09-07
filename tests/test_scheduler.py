"""Планировщик постинга по датам: слоты вместо кругов.

Постер с галочкой «только по датам» не ходит кругами по интервалу, а ждёт свои
даты и рассылает слоты по чатам. Проверяем:

* слоты из формы нормализуются (часовой пояс — в UTC, мусор отклоняется);
* наступившая дата уходит во все чаты, будущая — ждёт;
* круги по интервалу при включённых датах не ходят;
* прогресс переживает тики: перезапуск посреди рассылки не шлёт заново;
* когда все даты ушли, задача сама встаёт на паузу;
* правка не воскрешает ушедшее: состояние слотов сливается по id;
* создание и правка через API несут даты, карточка их показывает.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

import pytest

from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog, Rule
from app.errors import ValidationError
from app.telegram_client import jobs
from app.telegram_client.jobs import (
    due_scheduled_slot,
    merge_scheduled_state,
    normalize_scheduled_posts,
    task_title,
)
from app.telegram_client.manager import manager
from app.timeutil import utcnow
from tests.helpers import TEST_USER_ID
from tests.test_mailing import FakeClient
from tests.test_many_chats import (  # noqa: F401 — фикстуры соседнего файла
    instant_poster,
    login_open,
    make_poster,
    many_chats_resolved,
)


@pytest.fixture(autouse=True)
def clean_poster_state():
    """Тики расписания пишут в память менеджера — между тестами её чистим."""
    yield
    manager._poster_state.clear()
    manager._clients.clear()


def _past_slot(text: str = "вечерний пост") -> dict:
    return {"at": (utcnow() - timedelta(hours=1)).isoformat(), "text": text}


def _future_slot(text: str = "утренний пост") -> dict:
    return {"at": (utcnow() + timedelta(days=1)).isoformat(), "text": text}


async def _filters(rule_id: int, user_id: int) -> dict:
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
        assert rule is not None
        return dict(rule.filters or {})


async def _journal(rule_id: int) -> list[str]:
    async with session_scope() as session:
        rows = await session.execute(
            select(ForwardLog.status).where(ForwardLog.rule_id == rule_id)
        )
        return [str(row[0]) for row in rows.all()]


# ───────────────────────────── нормализация ─────────────────────────────


async def test_slots_normalize_to_sorted_utc():
    """Часовой пояс — в UTC, даты — по порядку, у каждой свой id."""
    slots = normalize_scheduled_posts(
        [
            {"at": "2026-09-10T19:00+03:00", "text": "второй"},
            {"at": "2026-09-10T10:00Z", "text": "первый"},
        ]
    )
    assert [slot["text"] for slot in slots] == ["первый", "второй"]
    assert slots[0]["at"] == "2026-09-10T10:00"
    assert slots[1]["at"] == "2026-09-10T16:00"
    assert slots[0]["id"] != slots[1]["id"]
    assert slots[0]["sent"] is False and slots[0]["sent_to"] == []


async def test_garbage_slots_are_rejected_with_reasons():
    """Мусор отклоняется понятной ошибкой, а не чинится молча."""
    with pytest.raises(ValidationError, match="Дата №1"):
        normalize_scheduled_posts([{"at": "когда-нибудь", "text": "x"}])
    with pytest.raises(ValidationError, match="Дата №2"):
        normalize_scheduled_posts(
            [_future_slot(), {"at": (utcnow() + timedelta(days=2)).isoformat()}]
        )
    with pytest.raises(ValidationError, match="Не больше 50"):
        normalize_scheduled_posts(
            [
                {"at": (utcnow() + timedelta(days=n)).isoformat(), "text": "x"}
                for n in range(1, 52)
            ]
        )
    assert normalize_scheduled_posts(None) == []


async def test_due_means_unsent_and_past():
    """Наступил — не отправлен и дата прошла; ушедший не наступает снова."""
    slots = normalize_scheduled_posts([_past_slot(), _future_slot()])
    assert due_scheduled_slot(slots, utcnow()) is not None
    assert due_scheduled_slot(slots, utcnow())["text"] == "вечерний пост"
    slots[0]["sent"] = True
    assert due_scheduled_slot(slots, utcnow()) is None


async def test_edit_keeps_the_sent_state_by_id():
    """Правка не воскрешает ушедшее: sent едет за слотом по его id."""
    old = normalize_scheduled_posts([_past_slot(), _future_slot()])
    old[0]["sent"] = True
    old[0]["sent_to"] = [-1001, -1002]
    fresh = normalize_scheduled_posts(
        [
            {"id": old[0]["id"], "at": old[0]["at"], "text": "поправленный"},
            {"id": old[1]["id"], "at": old[1]["at"], "text": old[1]["text"]},
        ]
    )
    merged = merge_scheduled_state(old, fresh)
    assert merged[0]["sent"] is True
    assert merged[0]["sent_to"] == [-1001, -1002]
    assert merged[1]["sent"] is False


# ───────────────────────────── движок ─────────────────────────────


async def test_due_slot_goes_to_all_chats_and_finishes(
    create_user, create_account, instant_poster
):
    """Наступившая дата уходит во все чаты, слот закрывается, задача встаёт."""
    chats = [-3001, -3002, -3003]
    rule_id, user_id, account_id = await make_poster(
        create_user,
        create_account,
        chats=chats,
        messages=[],
        schedule_only=True,
        scheduled_posts=normalize_scheduled_posts([_past_slot()]),
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()

    assert sorted(client.recipients) == sorted(chats)
    assert {text for _, text in client.sent} == {"вечерний пост"}
    filters = await _filters(rule_id, user_id)
    assert filters["scheduled_posts"][0]["sent"] is True
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
        assert rule is not None and rule.enabled is False
    assert "ok" in await _journal(rule_id)


async def test_future_slot_waits(create_user, create_account, instant_poster):
    """Будущая дата молчит, задача остаётся включённой."""
    rule_id, user_id, account_id = await make_poster(
        create_user,
        create_account,
        chats=[-3001],
        messages=[],
        schedule_only=True,
        scheduled_posts=normalize_scheduled_posts([_future_slot()]),
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()

    assert client.sent == []
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
        assert rule is not None and rule.enabled is True


async def test_schedule_disables_the_rounds(create_user, create_account, instant_poster):
    """Включённые даты отменяют круги: интервал не шлёт ничего."""
    rule_id, user_id, account_id = await make_poster(
        create_user,
        create_account,
        chats=[-3001],
        messages=["круговой текст"],
        schedule_only=True,
        scheduled_posts=normalize_scheduled_posts([_future_slot()]),
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()

    # Без schedule_only круг ушёл бы на первом же тике (интервал прошёл давно).
    assert client.sent == []


async def test_progress_survives_across_ticks(
    create_user, create_account, instant_poster, monkeypatch
):
    """Рассылка идёт порциями и не повторяет чаты: прогресс — в слоте."""
    monkeypatch.setattr(jobs, "POSTER_BATCH", 1)
    chats = [-3001, -3002, -3003]
    rule_id, user_id, account_id = await make_poster(
        create_user,
        create_account,
        chats=chats,
        messages=[],
        schedule_only=True,
        scheduled_posts=normalize_scheduled_posts([_past_slot()]),
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()
    assert client.recipients == chats[:1]
    await manager.refresh_rules()  # снимок свежий — прогресс читается из базы
    await manager._poster_tick()
    assert client.recipients == chats[:2]
    await manager._poster_tick()
    assert sorted(client.recipients) == sorted(chats)
    filters = await _filters(rule_id, user_id)
    assert filters["scheduled_posts"][0]["sent"] is True


async def test_dead_chat_does_not_hold_the_slot(
    create_user, create_account, instant_poster
):
    """Мёртвый чат — тоже обработанный: слот закрывается, причина — в журнале."""
    chats = [-3001, -3002]
    rule_id, user_id, account_id = await make_poster(
        create_user,
        create_account,
        chats=chats,
        messages=[],
        schedule_only=True,
        scheduled_posts=normalize_scheduled_posts([_past_slot()]),
    )
    client = FakeClient()
    real_send = client.send_message

    async def flaky(chat_id: int, text: str, **kwargs):
        if chat_id == -3002:
            raise RuntimeError("нет прав писать")
        return await real_send(chat_id, text, **kwargs)

    client.send_message = flaky  # type: ignore[method-assign]
    manager._clients[account_id] = client

    await manager._poster_tick()

    assert client.recipients == [-3001]
    filters = await _filters(rule_id, user_id)
    assert filters["scheduled_posts"][0]["sent"] is True
    assert "error" in await _journal(rule_id)


async def test_deleted_library_entry_skips_the_slot(
    create_user, create_account, instant_poster
):
    """Запись библиотеки удалили — слот закрывается с причиной, а не висит."""
    rule_id, user_id, account_id = await make_poster(
        create_user,
        create_account,
        chats=[-3001],
        messages=[],
        schedule_only=True,
        scheduled_posts=normalize_scheduled_posts(
            [
                {
                    "at": (utcnow() - timedelta(hours=1)).isoformat(),
                    "text": "",
                    "library_id": 987654,
                }
            ]
        ),
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()

    assert client.sent == []
    filters = await _filters(rule_id, user_id)
    slot = filters["scheduled_posts"][0]
    assert slot["sent"] is True and "библиотеки" in (slot.get("skipped") or "")
    assert "error" in await _journal(rule_id)


# ───────────────────────────── API и карточка ─────────────────────────────


async def test_task_with_dates_is_created_without_message(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Постер по датам создаётся без текста: каждый слот несёт свой."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    response = await client.post(
        "/api/tasks",
        json={
            "command": "sender",
            "send_mode": "schedule",
            "schedule_only": True,
            "scheduled_posts": [_future_slot("завтрашний")],
            "account_id": account_id,
            "targets": ["@ch-1", "@ch-2"],
        },
        headers=auth_headers,
    )
    assert response.status == 201, await response.text()
    task = (await response.json())["task"]
    assert task["schedule_only"] is True
    assert task["scheduled_total"] == 1
    assert task["scheduled_pending"] == 1
    assert "Постинг по датам" in task["title"]
    edit = task["edit"]
    assert edit["schedule_only"] is True
    assert edit["scheduled_posts"][0]["text"] == "завтрашний"


async def test_garbage_dates_are_rejected_by_api(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Мусор в датах — 400 с причиной, а не задача без расписания."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    response = await client.post(
        "/api/tasks",
        json={
            "command": "sender",
            "send_mode": "schedule",
            "schedule_only": True,
            "scheduled_posts": [{"at": "когда-нибудь", "text": "x"}],
            "account_id": account_id,
            "targets": ["@ch-1"],
        },
        headers=auth_headers,
    )
    assert response.status == 400
    assert "Дата №1" in (await response.json())["error"]


async def test_dated_poster_has_its_own_title(create_user, create_account):
    """Заголовок отличает даты от кругов — и в боте, и в кабинете."""
    rule_id, user_id, _ = await make_poster(
        create_user,
        create_account,
        chats=[-3001, -3002],
        messages=[],
        schedule_only=True,
        scheduled_posts=normalize_scheduled_posts([_future_slot()]),
    )
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
        assert rule is not None
        assert task_title(rule).startswith("Постинг по датам")
