"""Библиотека сообщений: правка записи на месте и «кто её рассылает».

Записи библиотеки — это то, что реально уходит из рассылки. Раньше их можно
было только добавить и удалить, поэтому опечатку исправляли через «удалить и
добавить заново»: у новой записи новый id, а задача помнила старый и молча
оставалась без сообщения. И в списке не было видно, что текст вообще кем-то
рассылается, — удаление выглядело безобидной уборкой.

Проверяем обещания:

* правка меняет текст на месте: id тот же, задача про правку даже не знает и
  сразу рассылает исправленное;
* готовый пост текстом не подменить, а пустой текст — это удаление, и об этом
  говорят словами, а не молча стирают сообщение;
* список показывает, какие задачи держат запись, включая рассылку «вся
  библиотека» — она держит и ту запись, которую добавят завтра.
"""
from __future__ import annotations

import pytest

from app.db import repo
from app.db.database import session_scope
from app.telegram_client.jobs import load_mailing_library
from tests.helpers import TEST_USER_ID
from tests.test_many_chats import (  # noqa: F401
    login_open,
    many_chats_resolved,
    one_shot_stubbed,
)


async def add_item(client, auth_headers, **payload) -> dict:
    response = await client.post("/api/library", json=payload, headers=auth_headers)
    assert response.status == 201, await response.text()
    return (await response.json())["item"]


async def library(client, auth_headers) -> list[dict]:
    response = await client.get("/api/library", headers=auth_headers)
    assert response.status == 200, await response.text()
    return (await response.json())["items"]


async def edit_item(client, auth_headers, item_id: int, **payload):
    return await client.patch(
        f"/api/library/{item_id}", json=payload, headers=auth_headers
    )


@pytest.fixture
async def known_user(client, auth_headers):
    """Пользователь кабинета уже есть: библиотека ссылается на него ключом."""
    await client.get("/api/me", headers=auth_headers)


@pytest.fixture
async def mailing(client, auth_headers, create_account, login_open, many_chats_resolved):
    """Готовая рассылка одного текста — задача, вокруг которой всё вертится."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    response = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@ch-1"],
            "message": "Скидка 20% на всё",
        },
        headers=auth_headers,
    )
    assert response.status == 201, await response.text()
    return (await response.json())["task"]


# ───────────────────────────── правка на месте ────────────────────────────────


async def test_text_is_fixed_without_losing_the_task(client, auth_headers, mailing):
    """Исправили опечатку — рассылка сразу шлёт новое, настройки не тронуты."""
    items = await library(client, auth_headers)
    assert len(items) == 1
    item_id = items[0]["id"]

    response = await edit_item(client, auth_headers, item_id, text="Скидка 25% на всё")

    assert response.status == 200, await response.text()
    saved = (await response.json())["item"]
    assert saved["id"] == item_id, "запись та же — иначе задача потеряет ссылку"
    assert saved["text"] == "Скидка 25% на всё"

    async with session_scope() as session:
        rule = await repo.get_rule(session, mailing["id"], TEST_USER_ID)
    assert rule.filters["library_ids"] == [item_id], "задача про правку и не знала"
    # То, что уйдёт в чаты, планировщик берёт из библиотеки на каждом проходе.
    sending = await load_mailing_library(TEST_USER_ID, rule.filters["library_ids"])
    assert [row.text for row in sending] == ["Скидка 25% на всё"]

    # И форма правки задачи показывает то же самое.
    tasks = await (await client.get("/api/tasks", headers=auth_headers)).json()
    card = next(task for task in tasks["tasks"] if task["id"] == mailing["id"])
    assert card["edit"]["message"] == "Скидка 25% на всё"
    assert card["mailing"]["messages_count"] == 1


async def test_name_follows_the_fixed_text(client, auth_headers, mailing):
    """Имя записи собрано из её текста — значит, идёт за текстом."""
    item_id = (await library(client, auth_headers))[0]["id"]

    await edit_item(client, auth_headers, item_id, text="Новая афиша\nвторая строка")

    items = await library(client, auth_headers)
    assert items[0]["title"] == "Новая афиша", "в списке новая первая строка"


async def test_own_name_survives_the_text_edit(client, auth_headers, known_user):
    """Имя, заданное руками, — не обрезок текста: правка текста его не трёт."""
    item = await add_item(client, auth_headers, text="прайс", title="Прайс на осень")

    await edit_item(client, auth_headers, item["id"], text="прайс подорожал")

    items = await library(client, auth_headers)
    assert items[0]["title"] == "Прайс на осень"
    assert items[0]["text"] == "прайс подорожал"


async def test_name_is_edited_alone(client, auth_headers, known_user):
    """Переименовать запись можно, не трогая текст."""
    item = await add_item(client, auth_headers, text="объявление")

    response = await edit_item(client, auth_headers, item["id"], title="Для чатов")

    assert response.status == 200, await response.text()
    saved = (await response.json())["item"]
    assert (saved["title"], saved["text"]) == ("Для чатов", "объявление")


async def test_empty_text_is_refused(client, auth_headers, mailing):
    """Пустой текст — это удаление записи: молча стирать нечего рассылке."""
    item_id = (await library(client, auth_headers))[0]["id"]

    response = await edit_item(client, auth_headers, item_id, text="   ")

    assert response.status == 400
    assert "удалите" in (await response.json())["error"]
    assert (await library(client, auth_headers))[0]["text"] == "Скидка 20% на всё"


async def test_saved_post_is_not_replaced_by_text(client, auth_headers, known_user):
    """Готовый пост правят в канале: текстом его подменять нельзя.

    У записи-поста своего текста нет, уходит сам пост. Разреши мы текст — запись
    тихо стала бы другой по смыслу, и рассылка отправила бы не то.
    """
    item = await add_item(client, auth_headers, chat_id=-1001, message_id=77)

    response = await edit_item(client, auth_headers, item["id"], text="подмена")

    assert response.status == 400
    assert "пост" in (await response.json())["error"]

    async with session_scope() as session:
        saved = await repo.get_saved_message(session, item["id"], TEST_USER_ID)
    assert (saved.text, saved.message_id) == ("", 77)


async def test_post_can_still_be_renamed(client, auth_headers, known_user):
    """А имя у поста своё: по нему его и узнают в списке."""
    item = await add_item(client, auth_headers, chat_id=-1001, message_id=77)

    response = await edit_item(client, auth_headers, item["id"], title="Пост про скидки")

    assert response.status == 200, await response.text()
    assert (await response.json())["item"]["title"] == "Пост про скидки"


async def test_foreign_record_is_not_edited(client, auth_headers, create_user):
    """Чужая запись — 404, как и при удалении."""
    other = await create_user()
    async with session_scope() as session:
        item = await repo.add_saved_message(session, user_id=other, text="чужое")
        item_id = item.id

    response = await edit_item(client, auth_headers, item_id, text="моё")

    assert response.status == 404


# ─────────────────────────── кто рассылает запись ─────────────────────────────


async def test_list_names_the_tasks_that_send_the_record(
    client, auth_headers, mailing
):
    """Список говорит, какая задача держит запись, а какая — ничья."""
    spare = await add_item(client, auth_headers, text="запас")
    async with session_scope() as session:
        rule = await repo.get_rule(session, mailing["id"], TEST_USER_ID)
    used_id = rule.filters["library_ids"][0]

    items = {item["id"]: item for item in await library(client, auth_headers)}

    assert items[used_id]["used_by"] == [mailing["title"]], "текст рассылки — за задачей"
    assert items[spare["id"]]["used_by"] == [], "запас ничей"


async def test_whole_library_mailing_holds_every_record(
    client, auth_headers, mailing
):
    """Рассылка «вся библиотека» держит и ту запись, которую добавят завтра."""
    response = await client.patch(
        f"/api/tasks/{mailing['id']}", json={"library_ids": []}, headers=auth_headers
    )
    assert response.status == 200, await response.text()

    fresh = await add_item(client, auth_headers, text="добавили после")

    items = {item["id"]: item for item in await library(client, auth_headers)}
    assert items[fresh["id"]]["used_by"] == [mailing["title"]]
    assert fresh["used_by"] == [mailing["title"]], "и сразу при добавлении"


async def test_archived_task_does_not_hold_records(client, auth_headers, mailing):
    """Архивная задача не работает — и пугать ею при удалении незачем."""
    await client.post(f"/api/tasks/{mailing['id']}/archive", headers=auth_headers)

    assert [item["used_by"] for item in await library(client, auth_headers)] == [[]]
