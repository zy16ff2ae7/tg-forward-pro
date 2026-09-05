"""Правка готовой задачи: PATCH /api/tasks/{id}.

Кабинет умел только создать задачу и удалить её. Чтобы поменять интервал
постинга, текст или список чатов, задачу приходилось пересоздавать — вместе с
ней терялись счётчики, место в круге рассылки и сам номер, по которому человек
её узнавал. Проверяем обещания правки:

* меняется только то, что прислали: остальные настройки и счётчики на месте;
* пока чаты те же, правка не ходит в Telegram — интервал можно поправить и при
  закрытом входе, а вот новый чат без него не добавить: его негде искать;
* правка не плодит записей в библиотеке сообщений и не пускает пустое
  обязательное поле;
* что кабинет подставил в форму, то сервер принимает обратно без изменений.
"""
from __future__ import annotations

import pytest

from app.db import repo
from app.db.database import session_scope
from tests.helpers import TEST_USER_ID

# Фикстуры соседнего файла: «все ссылки находятся», «вход открыт» и «разовые
# задачи не ходят в Telegram» нужны здесь ровно те же. Своя копия разошлась бы
# с оригиналом на первой же правке.
from tests.test_many_chats import (  # noqa: F401
    login_open,
    many_chats_resolved,
    one_shot_stubbed,
)


async def make_task(client, auth_headers, account_id: int, **fields) -> dict:
    """Создаёт задачу через кабинет — как это делает человек в шторке."""
    response = await client.post(
        "/api/tasks", json={"account_id": account_id, **fields}, headers=auth_headers
    )
    assert response.status == 201, await response.text()
    return (await response.json())["task"]


async def patch_task(client, auth_headers, task_id: int, **fields):
    return await client.patch(f"/api/tasks/{task_id}", json=fields, headers=auth_headers)


@pytest.fixture
async def poster(client, auth_headers, create_account, login_open, many_chats_resolved):
    """Готовый постинг в два чата — задача, которую дальше правим."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    task = await make_task(
        client,
        auth_headers,
        account_id,
        command="poster",
        targets=["@ch-1", "@ch-2"],
        message="объявление",
        interval=5,
        start="09:00",
        end="21:00",
    )
    return task


async def test_interval_changes_without_recreating_the_task(client, auth_headers, poster):
    """Поменяли интервал — задача та же, остальные настройки на месте."""
    response = await patch_task(client, auth_headers, poster["id"], interval=15)

    assert response.status == 200, await response.text()
    saved = (await response.json())["task"]
    assert saved["id"] == poster["id"], "это по-прежнему та же задача"
    assert saved["interval_min"] == 15
    # Правка про интервал — это только интервал: окно, текст и чаты не тронуты.
    assert (saved["window_start"], saved["window_end"]) == ("09:00", "21:00")
    assert saved["messages_count"] == 1
    assert saved["targets_count"] == 2

    async with session_scope() as session:
        rule = await repo.get_rule(session, poster["id"], TEST_USER_ID)
    assert rule.delay_seconds == 15 * 60, "интервал планировщик читает из delay_seconds"


async def test_counters_survive_the_edit(client, auth_headers, poster):
    """Счётчик отправок — не настройка: правка его не сбрасывает.

    Пересоздание задачи обнуляло и счётчик, и место в круге: круг начинался
    заново, и первые чаты получали сообщение второй раз.
    """
    async with session_scope() as session:
        rule = await repo.get_rule(session, poster["id"], TEST_USER_ID)
        rule.forwarded_count = 17

    response = await patch_task(client, auth_headers, poster["id"], message="другое")

    assert response.status == 200, await response.text()
    assert (await response.json())["task"]["forwarded"] == 17


async def test_same_chats_are_not_looked_up_again(
    client, auth_headers, poster, many_chats_resolved
):
    """Прежний список чатов второго обхода диалогов не стоит.

    Кабинет присылает форму целиком, поэтому чаты приходят при каждой правке. У
    задачи они уже есть вместе с названиями — искать их снова значит гонять
    обход диалогов на каждое «Сохранить».
    """
    lookups_before = list(many_chats_resolved)

    response = await patch_task(
        client, auth_headers, poster["id"], targets=poster["edit"]["targets"], interval=7
    )

    assert response.status == 200, await response.text()
    assert many_chats_resolved == lookups_before, "в Telegram не ходили"
    assert (await response.json())["task"]["targets_count"] == 2


async def test_new_chat_is_added_by_one_lookup(client, auth_headers, poster, many_chats_resolved):
    """Новый чат ищем — но только его одного, а не весь список заново."""
    lookups_before = len(many_chats_resolved)

    response = await patch_task(
        client, auth_headers, poster["id"], targets=[*poster["edit"]["targets"], "@ch-9"]
    )

    assert response.status == 200, await response.text()
    saved = (await response.json())["task"]
    assert saved["targets_count"] == 3
    assert many_chats_resolved[lookups_before:] == [1], "искали одну новую ссылку"


async def test_unknown_chat_leaves_the_task_alone(client, auth_headers, poster):
    """Опечатка в ссылке — отказ, а старый список чатов остаётся рабочим."""
    response = await patch_task(
        client, auth_headers, poster["id"], targets=["@ch-1", "нет-такого"]
    )

    assert response.status == 404
    assert "Не нашёл" in (await response.json())["error"]

    async with session_scope() as session:
        rule = await repo.get_rule(session, poster["id"], TEST_USER_ID)
    assert rule.filters["targets"] == [-9002], "второй чат на месте"


async def test_settings_are_edited_with_the_gateway_closed(
    client, auth_headers, poster, monkeypatch
):
    """Текст и интервал правятся без живого входа: чаты у задачи уже есть.

    Раньше «переделать задачу» означало создать её заново, а создание требует
    входа в аккаунт: при неподключённом шлюзе поправить свой же текст было
    нельзя вообще никак.
    """
    from app import accounts_login
    from app.errors import FeatureUnavailable

    def closed() -> None:
        raise FeatureUnavailable("Вход в аккаунт недоступен", feature="account_login")

    monkeypatch.setattr(accounts_login, "require_enabled", closed)

    response = await patch_task(client, auth_headers, poster["id"], message="новый текст")
    assert response.status == 200, await response.text()
    assert (await response.json())["task"]["messages_count"] == 1

    # А новый чат без входа не добавить — его физически негде искать.
    denied = await patch_task(client, auth_headers, poster["id"], targets=["@ch-77"])
    assert denied.status == 503


async def test_empty_required_field_is_refused(client, auth_headers, poster):
    """Пустое поле — это не «оставить как было», а попытка стереть нужное."""
    response = await patch_task(client, auth_headers, poster["id"], message="")

    assert response.status == 400
    assert "сообщение" in (await response.json())["error"]


async def test_archived_task_is_not_edited(client, auth_headers, poster):
    """Архивная задача не работает: молча менять её настройки — обман."""
    await client.post(f"/api/tasks/{poster['id']}/archive", headers=auth_headers)

    response = await patch_task(client, auth_headers, poster["id"], interval=3)

    assert response.status == 409
    assert "архив" in (await response.json())["error"]


async def test_missing_task_is_a_clean_404(client, auth_headers, create_account):
    """Чужая или удалённая задача — 404, а не пятисотка."""
    await client.get("/api/me", headers=auth_headers)
    await create_account(TEST_USER_ID)

    response = await patch_task(client, auth_headers, 999_999, interval=3)

    assert response.status == 404


# ─────────────────────────── тексты и имена чатов ─────────────────────────────


async def test_same_text_does_not_pile_up_in_the_library(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Рассылка хранит текст в библиотеке — правка не должна плодить копии.

    Кабинет присылает форму целиком, поэтому текст приходит при каждом
    «Сохранить». Если бы он каждый раз становился новой записью, библиотека
    после пяти правок интервала выглядела бы как пять одинаковых сообщений.
    """
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    task = await make_task(
        client,
        auth_headers,
        account_id,
        command="mailing",
        targets=["@ch-1"],
        message="первое\n\nвторое",
    )

    async def library_size() -> int:
        response = await client.get("/api/library", headers=auth_headers)
        return len((await response.json())["items"])

    assert await library_size() == 2, "пустая строка разделила текст на два сообщения"
    before = task["edit"]["library_ids"]

    same = await patch_task(
        client, auth_headers, task["id"], message="первое\n\nвторое", gap=9
    )
    assert same.status == 200, await same.text()
    assert await library_size() == 2, "тот же текст — те же записи"
    assert (await same.json())["task"]["edit"]["library_ids"] == before
    assert (await same.json())["task"]["mailing"]["gap_seconds"] == 9

    other = await patch_task(client, auth_headers, task["id"], message="третье")
    assert other.status == 200, await other.text()
    assert await library_size() == 3, "новый текст — новая запись"
    assert (await other.json())["task"]["edit"]["library_ids"] != before


async def test_task_remembers_the_names_of_all_its_chats(client, auth_headers, poster):
    """Все чаты задачи — с именами, а не «-1001234567890».

    В колонках правила есть название только первого чата, остальные — числа.
    Из-за этого задача на двадцать чатов не могла показать ни одного имени, а
    форма правки предлагала выбирать по идентификаторам.
    """
    assert [chat["title"] for chat in poster["chats"]] == ["@ch-1", "@ch-2"]
    assert poster["edit"]["targets"] == ["-9001", "-9002"]
    assert poster["edit"]["names"]["-9002"] == "@ch-2"


# ────────────────────────── форма правки для всех задач ───────────────────────

# Что кабинет подставляет в форму правки, то сервер обязан принять обратно без
# изменений: форма правки — это форма создания, и второго набора полей у неё нет.
ROUND_TRIP: list[tuple[str, dict]] = [
    ("copy_channel", {"source": "@ch-1", "target": "@ch-2", "mode": "forward"}),
    ("broadcast", {"source": "@ch-1", "targets": ["@ch-2", "@ch-3"]}),
    ("parser", {"source": "@ch-1", "limit": 50}),
    ("autosubscribe", {"targets": ["@ch-1", "@ch-2"]}),
    ("checks", {"source": "@ch-1", "target": "@ch-2", "keywords": "чек, подарок"}),
    ("dialogs", {"target": "@ch-2", "keywords": "оплата"}),
    ("baiting", {"source": "@ch-1", "target_user": "@ch-4", "reaction": "🔥"}),
    ("mute", {"source": "@ch-1", "target_user": "@ch-4", "keywords": "реклама"}),
    (
        "poster",
        {"targets": ["@ch-1"], "message": "прайс\n4000\n\nвторое", "interval": 9,
         "start": "08:00", "end": "22:30"},
    ),
    (
        "mailing",
        {"targets": ["@ch-1", "@ch-2"], "message": "текст", "gap": 7, "cycle": 30,
         "repeats": 3, "typing": True, "random_pick": True},
    ),
]


@pytest.mark.parametrize("command,fields", ROUND_TRIP, ids=[item[0] for item in ROUND_TRIP])
async def test_form_values_return_unchanged(
    client, auth_headers, create_account, login_open, many_chats_resolved, one_shot_stubbed,
    command, fields,
):
    """Задача любого типа: сохранили её форму как есть — ничего не поехало."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    task = await make_task(client, auth_headers, account_id, command=command, **fields)

    response = await patch_task(client, auth_headers, task["id"], **task["edit"])

    assert response.status == 200, await response.text()
    saved = (await response.json())["task"]
    assert saved["edit"] == task["edit"]
    assert saved["title"] == task["title"]




