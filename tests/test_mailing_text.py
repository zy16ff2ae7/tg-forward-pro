"""Текст рассылки: его видно в форме правки, и правка его меняет.

Рассылка отправляет записи библиотеки, поэтому набранный текст сначала
становится её записями. Из-за этого форма правки показывала вместо текста
только ссылки на записи: поле «Сообщение» стояло пустым, а выбор из библиотеки
был важнее набранного. Человек вписывал новый текст, видел «Сохранено» — и
рассылка продолжала слать старый, а набранное оставалось в библиотеке никому не
нужной записью. Поменять текст можно было только пересозданием задачи, вместе с
ней терялись счётчики, номер и место в круге.

Здесь проверяем новый договор:

* поле «Сообщение» показывает то, что уйдёт, — как у постинга;
* набранный текст важнее прежних ссылок: правка поля меняет рассылку;
* сохранённые посты (записи без текста) руками не набрать — они остаются при
  задаче даже когда текст поменяли;
* тот же текст копий в библиотеке не плодит;
* оставить рассылку вообще без сообщений нельзя.
"""
from __future__ import annotations

import pytest

from app.db import repo
from app.db.database import session_scope
from tests.helpers import TEST_USER_ID

# Фикстуры соседних файлов: «все ссылки находятся» и «вход открыт». Своя копия
# разошлась бы с оригиналом на первой же правке.
from tests.test_many_chats import (  # noqa: F401
    login_open,
    many_chats_resolved,
)


@pytest.fixture
async def mailing(client, auth_headers, create_account, login_open, many_chats_resolved):
    """Готовая рассылка с набранным текстом — задача, которую дальше правим."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    response = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@ch-1"],
            "message": "привет\n\nещё раз",
        },
        headers=auth_headers,
    )
    assert response.status == 201, await response.text()
    return (await response.json())["task"]


async def library(client, auth_headers) -> list[dict]:
    response = await client.get("/api/library", headers=auth_headers)
    return (await response.json())["items"]


async def stored_ids(task_id: int) -> list[int]:
    """На что задача ссылается в базе — то, что возьмёт планировщик."""
    async with session_scope() as session:
        rule = await session.get(repo.Rule, task_id)
        return [int(value) for value in (rule.filters or {}).get("library_ids") or []]


async def patch(client, auth_headers, task_id: int, **fields):
    return await client.patch(f"/api/tasks/{task_id}", json=fields, headers=auth_headers)


async def test_the_form_shows_what_the_mailing_sends(mailing):
    """Главное: в форме правки стоит сам текст, а не пустое поле."""
    assert mailing["edit"]["message"] == "привет\n\nещё раз"
    # Чипсы — только для записей без текста: набранное видно в поле, и второй
    # раз его показывать нечего.
    assert mailing["edit"]["library_ids"] == []
    assert mailing["mailing"]["messages_count"] == 2


async def test_the_new_text_wins_over_the_old_links(client, auth_headers, mailing):
    """Кабинет присылает форму целиком — вместе с прежними ссылками.

    Именно на этом текст и пропадал: ссылки были важнее, и новый текст уходил в
    библиотеку записью, которой никто не пользуется.
    """
    before = await stored_ids(mailing["id"])

    response = await patch(
        client, auth_headers, mailing["id"], message="новый текст", library_ids=before
    )

    assert response.status == 200, await response.text()
    task = (await response.json())["task"]
    assert task["edit"]["message"] == "новый текст"
    assert task["mailing"]["messages_count"] == 1
    assert await stored_ids(mailing["id"]) != before, "задача ссылается на новый текст"
    texts = [item["text"] for item in await library(client, auth_headers)]
    assert "новый текст" in texts


async def test_a_saved_post_stays_when_the_text_changes(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Сохранённый пост руками не набрать: правка текста не должна его терять."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    saved = await client.post(
        "/api/library", json={"chat_id": -1001, "message_id": 77}, headers=auth_headers
    )
    post_id = (await saved.json())["item"]["id"]

    created = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@ch-1"],
            "message": "текст",
            "library_ids": [post_id],
        },
        headers=auth_headers,
    )
    assert created.status == 201, await created.text()
    task = (await created.json())["task"]
    assert task["edit"]["library_ids"] == [post_id], "пост стоит чипсом рядом с полем"
    assert task["edit"]["message"] == "текст"

    response = await patch(
        client, auth_headers, task["id"], message="другой", library_ids=[post_id]
    )

    assert response.status == 200, await response.text()
    saved_task = (await response.json())["task"]
    assert saved_task["edit"]["message"] == "другой"
    assert saved_task["edit"]["library_ids"] == [post_id]
    assert (await stored_ids(task["id"]))[-1] == post_id, "пост остался в задаче"
    assert saved_task["mailing"]["messages_count"] == 2


async def test_the_text_from_the_library_does_not_become_a_second_copy(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """«📚 из библиотеки» ставит текст в поле — сохранять его заново незачем.

    Кабинет подставляет в поле сам текст записи, поэтому сервер получает его как
    набранный руками. Если бы он на это заводил новую запись, выбор из
    библиотеки удваивал бы её при каждом «Запустить».
    """
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    saved = await client.post("/api/library", json={"text": "афиша"}, headers=auth_headers)
    item_id = (await saved.json())["item"]["id"]

    created = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@ch-1"],
            "message": "афиша",
        },
        headers=auth_headers,
    )

    assert created.status == 201, await created.text()
    assert len(await library(client, auth_headers)) == 1, "копия записи не появилась"
    task = (await created.json())["task"]
    assert await stored_ids(task["id"]) == [item_id]


async def test_the_mailing_cannot_be_left_with_nothing_to_send(client, auth_headers, mailing):
    """Пустое поле без выбранных записей — молчаливая задача, а не правка."""
    response = await patch(client, auth_headers, mailing["id"], message="", library_ids=[])

    assert response.status == 400
    assert "сообщение" in (await response.json())["error"]
    assert await stored_ids(mailing["id"]), "текст остался прежним"


async def test_settings_edit_does_not_touch_the_text(client, auth_headers, mailing):
    """Поправили паузу — тексты те же: правка меняет только присланное."""
    before = await stored_ids(mailing["id"])

    response = await patch(client, auth_headers, mailing["id"], gap=30)

    assert response.status == 200, await response.text()
    assert (await response.json())["task"]["edit"]["message"] == "привет\n\nещё раз"
    assert await stored_ids(mailing["id"]) == before
    assert len(await library(client, auth_headers)) == 2


async def test_the_card_says_out_loud_that_the_whole_library_goes(
    client, auth_headers, mailing
):
    """Пустой список записей — это «вся библиотека», а не «нечего слать».

    Так его читает планировщик, поэтому карточка обязана сказать это словами:
    без пометки она показывала ноль сообщений и молчала о том, что уйдёт.
    """
    async with session_scope() as session:
        rule = await session.get(repo.Rule, mailing["id"])
        rule.filters = {**(rule.filters or {}), "library_ids": []}

    response = await client.get("/api/tasks", headers=auth_headers)
    task = next(
        item for item in (await response.json())["tasks"] if item["id"] == mailing["id"]
    )

    assert task["mailing"]["whole_library"] is True
    assert task["mailing"]["messages_count"] == 0
    assert task["edit"]["message"] == ""


async def test_the_card_admits_the_messages_were_deleted(client, auth_headers, mailing):
    """Сообщения убрали из библиотеки — карточка говорит, что рассылать нечего.

    Ссылки в задаче при этом остаются: молча выбросить их нельзя, пустой список
    планировщик читает как «вся библиотека» — рассылка начала бы слать всё
    подряд. Поэтому счёт на карточке считаем по живым записям, а про повисшие
    ссылки говорим отдельно: раньше карточка бодро показывала два сообщения,
    которых уже нет.
    """
    for item in await library(client, auth_headers):
        assert (
            await client.delete(f"/api/library/{item['id']}", headers=auth_headers)
        ).status == 200

    response = await client.get("/api/tasks", headers=auth_headers)
    task = next(
        item for item in (await response.json())["tasks"] if item["id"] == mailing["id"]
    )

    assert task["mailing"]["messages_count"] == 0
    assert task["mailing"]["messages_gone"] == 2
    # Это не «вся библиотека»: выбор был, его записи удалили.
    assert task["mailing"]["whole_library"] is False
    assert task["edit"]["message"] == ""
    assert await stored_ids(mailing["id"]), "ссылки на месте — иначе уйдёт вся библиотека"


async def test_a_live_message_is_still_counted(client, auth_headers, mailing):
    """Удалили одно из двух — карточка честно показывает одно, а не два."""
    items = await library(client, auth_headers)
    await client.delete(f"/api/library/{items[0]['id']}", headers=auth_headers)

    response = await client.get("/api/tasks", headers=auth_headers)
    task = next(
        item for item in (await response.json())["tasks"] if item["id"] == mailing["id"]
    )

    assert (task["mailing"]["messages_count"], task["mailing"]["messages_gone"]) == (1, 1)
    assert task["edit"]["message"] == (items[1]["text"] or "")


async def test_a_new_text_clears_the_dead_links(client, auth_headers, mailing):
    """Правка после уборки в библиотеке оставляет только новую запись.

    Кабинет присылает форму целиком, вместе с повисшими ссылками. Держать их
    дальше незачем: записей нет, и в очереди они были бы дырой.
    """
    for item in await library(client, auth_headers):
        await client.delete(f"/api/library/{item['id']}", headers=auth_headers)

    response = await patch(client, auth_headers, mailing["id"], message="снова с нуля")

    assert response.status == 200, await response.text()
    task = (await response.json())["task"]
    assert task["edit"]["message"] == "снова с нуля"
    assert (task["mailing"]["messages_count"], task["mailing"]["messages_gone"]) == (1, 0)
    assert len(await stored_ids(mailing["id"])) == 1
