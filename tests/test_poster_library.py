"""Свои тексты постинга живут в библиотеке — там же, где у рассылки.

Раньше постинг держал копии текстов в своих настройках (`filters["messages"]`), и
из-за этой второй кладовки:

* одну и ту же опечатку правили дважды — в библиотеке и в задаче;
* правка записи в библиотеке до постинга не доходила вовсе;
* библиотека не знала, что запись кто-то постит, и удаление выглядело уборкой;
* постинг умел только простой текст: сохранённый пост с медиа ему был недоступен.

Теперь у постинга и рассылки одна кладовка и один путь отправки. Проверяем:

* набранный текст ложится записями библиотеки, а в задаче остаются ссылки;
* правка записи меняет то, что уходит в чаты, на следующем же круге;
* сохранённый пост постинг тоже умеет — перечитывает его и копирует;
* запись, занятую постингом, библиотека называет по имени задачи;
* задачи из прошлой версии (тексты в настройках) продолжают постить, а первая же
  правка переносит их в библиотеку и копию из настроек убирает;
* когда постить нечего или пост удалили, причина попадает на карточку.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog, Rule
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID
from tests.test_mailing import FakeClient
from tests.test_many_chats import (  # noqa: F401 — фикстуры соседнего файла
    instant_poster,
    login_open,
    make_poster,
    many_chats_resolved,
)


class PostClient(FakeClient):
    """Клиент, который умеет ещё и перечитать сохранённый пост."""

    def __init__(self, post: object | None = None) -> None:
        super().__init__()
        self.post = post
        self.reads: list[tuple[int, int]] = []

    async def get_messages(self, chat_id: int, ids: int):
        self.reads.append((chat_id, ids))
        return self.post


def saved_post(text: str) -> SimpleNamespace:
    """Пост в канале: постинг копирует его, а не пересылает с меткой."""
    return SimpleNamespace(id=77, message=text, media=None, entities=None)


async def library(client, auth_headers) -> list[dict]:
    return (await (await client.get("/api/library", headers=auth_headers)).json())["items"]


async def stored(task_id: int) -> dict:
    """Настройки задачи в базе — то, с чем работает планировщик."""
    async with session_scope() as session:
        rule = await session.get(Rule, task_id)
        return dict(rule.filters or {})


async def journal(rule_id: int) -> list[str]:
    async with session_scope() as session:
        rows = await session.execute(
            select(ForwardLog.error).where(ForwardLog.rule_id == rule_id)
        )
    return [str(row[0] or "") for row in rows.all()]


@pytest.fixture
async def poster(client, auth_headers, create_account, login_open, many_chats_resolved):
    """Постинг, созданный из кабинета набранным текстом."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    response = await client.post(
        "/api/tasks",
        json={
            "command": "poster",
            "account_id": account_id,
            "targets": ["@ch-1"],
            "message": "первое\n\nвторое",
        },
        headers=auth_headers,
    )
    assert response.status == 201, await response.text()
    return (await response.json())["task"]


# ───────────────────────────── одна кладовка на двоих ─────────────────────────


async def test_the_typed_text_goes_to_the_library(client, auth_headers, poster):
    """Набранное в форме — записи библиотеки, в задаче только ссылки на них."""
    filters = await stored(poster["id"])

    assert "messages" not in filters, "второй кладовки больше нет"
    assert len(filters["library_ids"]) == 2
    assert sorted(item["text"] for item in await library(client, auth_headers)) == [
        "второе",
        "первое",
    ]
    # И карточка считает то же самое, что уйдёт.
    assert (poster["messages_count"], poster["whole_library"]) == (2, False)
    assert poster["edit"]["message"] == "первое\n\nвторое"


async def test_the_library_edit_reaches_the_chats(
    client, auth_headers, poster, instant_poster
):
    """Исправили запись — постинг шлёт исправленное, задачу не трогали."""
    before = (await stored(poster["id"]))["library_ids"]
    # Круг начинается с первого по очереди — его запись и правим.
    first = next(row for row in await library(client, auth_headers) if row["text"] == "первое")
    response = await client.patch(
        f"/api/library/{first['id']}",
        json={"text": "первое (исправлено)"},
        headers=auth_headers,
    )
    assert response.status == 200, await response.text()

    poster_client = FakeClient()
    manager._clients[poster["account_id"]] = poster_client
    await manager.refresh_rules()
    await manager._poster_tick()

    assert [text for _, text in poster_client.sent] == ["первое (исправлено)"]
    assert (await stored(poster["id"]))["library_ids"] == before, "ссылки те же"


async def test_a_saved_post_is_posted_too(
    client, auth_headers, create_account, login_open, many_chats_resolved, instant_poster
):
    """Сохранённый пост постинг умеет: перечитывает его и копирует как свой.

    С копиями текстов в настройках это было невозможно — постинг знал только
    строки, и пост с картинкой в него не помещался.
    """
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    saved = await client.post(
        "/api/library", json={"chat_id": -1001, "message_id": 77}, headers=auth_headers
    )
    item_id = (await saved.json())["item"]["id"]

    created = await client.post(
        "/api/tasks",
        json={
            "command": "poster",
            "account_id": account_id,
            "targets": ["@ch-1"],
            "library_ids": [item_id],
        },
        headers=auth_headers,
    )
    assert created.status == 201, await created.text()
    task = (await created.json())["task"]
    assert task["messages_count"] == 1

    poster_client = PostClient(saved_post("афиша из канала"))
    manager._clients[account_id] = poster_client
    await manager.refresh_rules()
    await manager._poster_tick()

    assert poster_client.reads == [(-1001, 77)], "пост перечитали в канале"
    assert [text for _, text in poster_client.sent] == ["афиша из канала"]


async def test_the_library_names_the_poster_that_holds_a_record(
    client, auth_headers, poster
):
    """Запись занята постингом — список библиотеки говорит это словами."""
    items = await library(client, auth_headers)

    assert [item["used_by"] for item in items] == [[poster["title"]], [poster["title"]]]


async def test_the_poster_and_the_mailing_share_one_record(
    client, auth_headers, create_account, login_open, many_chats_resolved, poster
):
    """Один текст на две задачи: библиотека называет обе, копии не появляются."""
    account_id = poster["account_id"]
    item = (await library(client, auth_headers))[0]

    created = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@ch-2"],
            "library_ids": [item["id"]],
        },
        headers=auth_headers,
    )
    assert created.status == 201, await created.text()
    mailing_title = (await created.json())["task"]["title"]

    items = {row["id"]: row for row in await library(client, auth_headers)}
    assert len(items) == 2, "запись переиспользовали, а не удвоили"
    assert sorted(items[item["id"]]["used_by"]) == sorted([poster["title"], mailing_title])


# ─────────────────────────── задачи из прошлой версии ─────────────────────────


async def test_an_old_poster_keeps_posting_from_its_settings(
    create_user, create_account, instant_poster
):
    """Тексты в настройках у людей уже лежат: слать они обязаны по-прежнему."""
    rule_id, _, account_id = await make_poster(
        create_user, create_account, chats=[-8001], messages=["старый текст"], legacy=True
    )
    poster_client = FakeClient()
    manager._clients[account_id] = poster_client

    await manager._poster_tick()

    assert poster_client.sent == [(-8001, "старый текст")]


async def test_an_old_poster_shows_its_text_in_the_form(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Форма правки показывает старый текст — иначе первое «Сохранить» его сотрёт.

    А как только текст сохранён, он переезжает в библиотеку и копия из настроек
    уходит: двух источников правды у задачи не остаётся.
    """
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    created = await client.post(
        "/api/tasks",
        json={
            "command": "poster",
            "account_id": account_id,
            "targets": ["@ch-1"],
            "message": "старый текст",
        },
        headers=auth_headers,
    )
    task_id = (await created.json())["task"]["id"]
    # Возвращаем задачу к прошлому виду: тексты в настройках, ссылок нет.
    async with session_scope() as session:
        rule = await session.get(Rule, task_id)
        rule.filters = {**rule.filters, "messages": ["старый текст"], "library_ids": []}
    for item in await library(client, auth_headers):
        await client.delete(f"/api/library/{item['id']}", headers=auth_headers)

    listed = await (await client.get("/api/tasks", headers=auth_headers)).json()
    card = next(row for row in listed["tasks"] if row["id"] == task_id)
    assert card["edit"]["message"] == "старый текст"
    assert (card["messages_count"], card["whole_library"]) == (1, False)

    response = await client.patch(
        f"/api/tasks/{task_id}", json={"message": "новый текст"}, headers=auth_headers
    )

    assert response.status == 200, await response.text()
    saved = (await response.json())["task"]
    assert saved["edit"]["message"] == "новый текст"
    filters = await stored(task_id)
    assert "messages" not in filters, "копия из настроек убрана"
    assert len(filters["library_ids"]) == 1
    assert [row["text"] for row in await library(client, auth_headers)] == ["новый текст"]


async def test_an_old_poster_does_not_hold_the_whole_library(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Пустой список ссылок у старой задачи — не «вся библиотека», а «свой текст».

    Иначе всякая чужая запись показывалась бы занятой этой задачей, и удаление
    пугало бы зря.
    """
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    created = await client.post(
        "/api/tasks",
        json={
            "command": "poster",
            "account_id": account_id,
            "targets": ["@ch-1"],
            "message": "старый текст",
        },
        headers=auth_headers,
    )
    task_id = (await created.json())["task"]["id"]
    async with session_scope() as session:
        rule = await session.get(Rule, task_id)
        rule.filters = {**rule.filters, "messages": ["старый текст"], "library_ids": []}

    spare = await client.post("/api/library", json={"text": "запас"}, headers=auth_headers)
    spare_id = (await spare.json())["item"]["id"]

    items = {row["id"]: row for row in await library(client, auth_headers)}
    assert items[spare_id]["used_by"] == []


# ──────────────────────────── честность на карточке ───────────────────────────


async def test_the_emptied_library_is_said_on_the_card(
    client, auth_headers, poster, instant_poster
):
    """Записи убрали — карточка говорит «постить нечего», и только один раз."""
    for item in await library(client, auth_headers):
        await client.delete(f"/api/library/{item['id']}", headers=auth_headers)

    manager._clients[poster["account_id"]] = FakeClient()
    await manager.refresh_rules()
    await manager._poster_tick()
    await manager._poster_tick()

    lines = await journal(poster["id"])
    assert lines == ["постить нечего: в библиотеке не осталось сообщений"]
    listed = await (await client.get("/api/tasks", headers=auth_headers)).json()
    card = next(row for row in listed["tasks"] if row["id"] == poster["id"])
    assert (card["messages_count"], card["messages_gone"]) == (0, 2)


async def test_a_vanished_post_stops_the_round_once(
    client, auth_headers, create_account, login_open, many_chats_resolved, instant_poster
):
    """Пост удалили из канала — одна строка в журнале, а не отказ на каждый чат."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    saved = await client.post(
        "/api/library", json={"chat_id": -1001, "message_id": 77}, headers=auth_headers
    )
    created = await client.post(
        "/api/tasks",
        json={
            "command": "poster",
            "account_id": account_id,
            "targets": ["@ch-1", "@ch-2", "@ch-3"],
            "library_ids": [(await saved.json())["item"]["id"]],
        },
        headers=auth_headers,
    )
    task_id = (await created.json())["task"]["id"]

    poster_client = PostClient(None)  # пост не найден
    manager._clients[account_id] = poster_client
    await manager.refresh_rules()
    await manager._poster_tick()

    assert poster_client.sent == []
    assert manager._poster_state[task_id]["queue"] == [], "круг закрыт, а не брошен"
    lines = await journal(task_id)
    assert len(lines) == 1 and lines[0].startswith("постить нечего:")


@pytest.fixture(autouse=True)
def clean_manager():
    """Менеджер — синглтон на весь процесс: состояние между тестами не тащим."""
    yield
    manager._poster_rules = []
    manager._poster_state.clear()
    manager._mailing_rules = []
    manager._mailing_state.clear()
    manager._clients.clear()
