"""Задачи «в любое число чатов»: поиск пачкой, список чатов, круг постинга.

Постинг и рассылка ходят в неограниченное число чатов, и упереться в предел
можно тремя разными способами — каждый проверяем отдельно:

* поиск чатов при сохранении задачи. Двести ссылок — это по-прежнему один обход
  диалогов, а не двести: обход на каждую ссылку означал бы минуты ожидания в
  кабинете и FloodWait в конце;
* список чатов в кабинете. Чат, которого нет в списке, нельзя выбрать мышкой,
  поэтому список не обрезаем;
* сам проход планировщика. Круг идёт порциями, но проходит все чаты — иначе
  задача тихо обслуживала бы только начало списка.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from telethon.errors import FloodWaitError

from app.db import repo
from app.db.database import session_scope
from app.db.models import Rule, Subscription
from app.telegram_client import jobs
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID

MANY = 250
# Ключи-заглушки: настоящие не нужны, но плейсхолдеры из .env.example менеджер
# сам считает «шлюз не настроен» и честно отвечает пустотой.
FAKE_API_ID = 1_234_567
FAKE_API_HASH = "1234abcd" * 4


# ──────────────────────────── поиск чатов пачкой ──────────────────────────────


class FakeDialog:
    """Диалог Telethon: менеджеру от него нужны id и entity."""

    def __init__(self, chat_id: int, title: str, username: str = "") -> None:
        self.id = chat_id
        self.entity = type(
            "Entity", (), {"title": title, "username": username, "broadcast": False, "megagroup": True}
        )()


class DialogsClient:
    """Клиент, который умеет только перечислять диалоги и считать обходы."""

    def __init__(self, dialogs: list[FakeDialog]) -> None:
        self.dialogs = dialogs
        self.sweeps = 0
        self.asked: list[object] = []

    def is_connected(self) -> bool:
        return True

    def iter_dialogs(self, limit=None):
        self.sweeps += 1
        items = self.dialogs if limit is None else self.dialogs[:limit]

        async def gen():
            for dialog in items:
                yield dialog

        return gen()

    async def get_entity(self, ref):
        self.asked.append(ref)
        raise ValueError("нет такого чата")


@pytest.fixture
def mtproto_on(monkeypatch):
    """Шлюз «настроен»: без этого менеджер честно отвечает пустотой."""
    from app.config import settings

    monkeypatch.setattr(settings, "api_id", FAKE_API_ID)
    monkeypatch.setattr(settings, "api_hash", FAKE_API_HASH)


@pytest.fixture(autouse=True)
def clean_manager():
    """Менеджер — синглтон на весь процесс: после теста забываем его состояние."""
    yield
    manager._clients.clear()
    manager._poster_rules = []
    manager._poster_state.clear()
    manager._mailing_rules = []
    manager._mailing_state.clear()
    manager.forget_dialogs()


async def test_many_chats_are_found_in_a_single_sweep(mtproto_on):
    """Двести ссылок — один обход диалогов, а не двести."""
    dialogs = [FakeDialog(-1000 - n, f"чат {n}", f"chat{n}") for n in range(MANY)]
    client = DialogsClient(dialogs)
    manager._clients[1] = client

    found = await manager.resolve_many(1, [f"@chat{n}" for n in range(MANY)])

    assert len(found) == MANY
    assert found["@chat7"] == (-1007, "чат 7")
    assert client.sweeps == 1
    # Ни одного запроса к Telegram по одному чату: всё нашлось в списке диалогов.
    assert client.asked == []


async def test_lookups_come_in_every_shape_the_cabinet_sends(mtproto_on):
    """Ник, ссылка t.me, числовой id и название — один и тот же чат."""
    manager._clients[1] = DialogsClient([FakeDialog(-1001, "Афиша Москвы", "afisha")])

    found = await manager.resolve_many(
        1, ["@afisha", "https://t.me/afisha", "-1001", "Афиша Москвы", "афиша"]
    )

    assert set(found) == {"@afisha", "https://t.me/afisha", "-1001", "Афиша Москвы", "афиша"}
    assert {pair[0] for pair in found.values()} == {-1001}


async def test_dialogs_cache_saves_the_second_sweep(mtproto_on):
    """Кабинет спрашивает чаты часто: второй обход подряд ни к чему."""
    client = DialogsClient([FakeDialog(-1001, "чат", "chat")])
    manager._clients[1] = client

    await manager.list_dialogs(1)
    await manager.list_dialogs(1)
    await manager.resolve_many(1, ["@chat"])

    assert client.sweeps == 1
    # Вступили в новый чат — он должен появиться сразу, а не через минуту.
    manager.forget_dialogs(1)
    await manager.list_dialogs(1)
    assert client.sweeps == 2


async def test_cut_list_never_lands_in_the_cache(mtproto_on):
    """Короткая витрина не должна становиться ответом на «все чаты»."""
    client = DialogsClient([FakeDialog(-1000 - n, f"чат {n}") for n in range(30)])
    manager._clients[1] = client

    short = await manager.list_dialogs(1, limit=5)
    full = await manager.list_dialogs(1)

    assert len(short) == 5
    assert len(full) == 30


# ─────────────────────────────── круг постинга ────────────────────────────────


async def make_poster(
    create_user,
    create_account,
    *,
    chats: list[int],
    messages: list[str],
    legacy: bool = False,
    **settings,
) -> tuple[int, int, int]:
    """Кладёт в базу авто-постинг по списку чатов. → (rule_id, user_id, account_id).

    Свои тексты постинга живут в библиотеке — там же, где у рассылки, поэтому по
    умолчанию раскладываем их записями и оставляем в задаче только ссылки. С
    `legacy=True` получается задача из прошлой версии, с копиями текстов в
    настройках: такие в базе у людей уже лежат, и слать они обязаны по-прежнему.
    """
    user_id = await create_user()
    account_id = await create_account(user_id)

    async with session_scope() as session:
        session.add(Subscription(user_id=user_id, active_until=repo.utcnow() + timedelta(days=1)))
        library_ids: list[int] = []
        if not legacy:
            for text in messages:
                item = await repo.add_saved_message(session, user_id=user_id, text=text)
                library_ids.append(item.id)
        rule = Rule(
            user_id=user_id,
            account_id=account_id,
            source_id=0,
            target_id=chats[0],
            kind="poster",
            enabled=True,
        )
        session.add(rule)
        await session.flush()
        rule.filters = {
            **({"messages": list(messages)} if legacy else {"library_ids": library_ids}),
            "targets": list(chats[1:]),
            "interval_seconds": 60,
            "window_start": "00:00",
            "window_end": "23:59",
            **settings,
        }

    await manager.refresh_rules()
    return rule.id, user_id, account_id


@pytest.fixture
def instant_poster(monkeypatch):
    """Пауз между чатами нет: иначе тест ждал бы настоящие секунды."""
    monkeypatch.setattr(jobs, "POSTER_CHAT_GAP", 0.0)


async def test_poster_walks_all_chats_over_consecutive_ticks(
    create_user, create_account, instant_poster
):
    """Круг идёт порциями, но обходит все чаты: остаток ждёт следующего тика."""
    from tests.test_mailing import FakeClient

    chats = [-2000 - n for n in range(MANY)]
    _, _, account_id = await make_poster(
        create_user, create_account, chats=chats, messages=["раз"]
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()
    after_first = len(client.sent)
    assert after_first == jobs.POSTER_BATCH, "за один тик уходит не больше порции"

    # Крутим тики, пока очередь не опустеет: столько же их сделает планировщик.
    for _ in range(MANY // jobs.POSTER_BATCH + 2):
        await manager._poster_tick()

    assert client.recipients == chats, "все чаты обошли по одному разу и по порядку"


async def test_poster_keeps_one_message_for_the_whole_round(
    create_user, create_account, instant_poster
):
    """Одно сообщение на круг: иначе половина чатов получила бы другой текст."""
    chats = [-3000 - n for n in range(12)]
    from tests.test_mailing import FakeClient

    rule_id, user_id, account_id = await make_poster(
        create_user, create_account, chats=chats, messages=["первое", "второе"]
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()
    await manager._poster_tick()

    assert [text for _, text in client.sent] == ["первое"] * 12
    # Круг закрыт — счётчик отправок знает про все чаты.
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
    assert rule.forwarded_count == 12


async def test_poster_next_round_takes_the_next_message(
    create_user, create_account, instant_poster, monkeypatch
):
    """Следующий круг — следующее сообщение по очереди."""
    from tests.test_mailing import FakeClient

    rule_id, user_id, account_id = await make_poster(
        create_user, create_account, chats=[-4001, -4002], messages=["первое", "второе"]
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()
    # Интервал вышел: круг можно начинать заново.
    manager._poster_state[rule_id]["last"] = 0.0
    await manager._poster_tick()

    assert [text for _, text in client.sent] == ["первое", "первое", "второе", "второе"]


async def test_unreachable_chat_does_not_block_the_round(
    create_user, create_account, instant_poster
):
    """Недоступный чат выкидываем из круга, остальные получают своё."""
    from tests.test_mailing import FakeClient

    class PickyClient(FakeClient):
        async def send_message(self, chat_id: int, text: str, **kwargs):
            if chat_id == -5002:
                raise RuntimeError("нет доступа")
            return await FakeClient.send_message(self, chat_id, text, **kwargs)

    rule_id, _, account_id = await make_poster(
        create_user, create_account, chats=[-5001, -5002, -5003], messages=["раз"]
    )
    client = PickyClient()
    manager._clients[account_id] = client

    await manager._poster_tick()

    assert client.recipients == [-5001, -5003]
    assert manager._poster_state[rule_id]["queue"] == []


async def test_floodwait_pauses_the_round_and_keeps_the_chat(
    create_user, create_account, instant_poster
):
    """FloodWait — это «подожди»: чат остаётся в очереди, круг продолжится."""
    from tests.test_mailing import FakeClient

    rule_id, _, account_id = await make_poster(
        create_user, create_account, chats=[-6001, -6002], messages=["раз"]
    )
    client = FakeClient(error=FloodWaitError(None, capture=30))
    manager._clients[account_id] = client

    await manager._poster_tick()

    assert client.sent == []
    state = manager._poster_state[rule_id]
    assert state["queue"] == [-6001, -6002], "ни одного чата не потеряли"
    assert state["not_before"] > 0, "пауза назначена"


async def test_single_chat_poster_behaves_exactly_as_before(
    create_user, create_account, instant_poster
):
    """Один чат — привычное поведение: по сообщению за интервал, по очереди."""
    from tests.test_mailing import FakeClient

    rule_id, _, account_id = await make_poster(
        create_user, create_account, chats=[-7001], messages=["первое", "второе"]
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()
    await manager._poster_tick()  # интервал не вышел — второй раз не шлём
    assert client.sent == [(-7001, "первое")]

    manager._poster_state[rule_id]["last"] = 0.0
    await manager._poster_tick()
    assert [text for _, text in client.sent] == ["первое", "второе"]


# ──────────────────────────────── кабинет ─────────────────────────────────────


@pytest.fixture
def many_chats_resolved(monkeypatch):
    """Все ссылки «находятся», и видно, сколько раз кабинет искал чаты."""
    calls: list[int] = []

    async def fake_resolve_many(account_id: int, queries):
        refs = [str(raw or "").strip() for raw in queries]
        calls.append(len([ref for ref in refs if ref]))
        return {ref: (-9000 - int(ref.split("-")[-1]), ref) for ref in refs if ref.startswith("@ch")}

    monkeypatch.setattr(manager, "resolve_many", fake_resolve_many)
    return calls


@pytest.fixture
def login_open(monkeypatch):
    """Вход аккаунтов в тесте включён: без него создание задачи — 503."""
    from app import accounts_login

    monkeypatch.setattr(accounts_login, "require_enabled", lambda: None)


async def test_task_with_many_chats_is_created(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Двести пятьдесят чатов в задаче — 201, и все они в ней."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    refs = [f"@ch-{n}" for n in range(MANY)]

    response = await client.post(
        "/api/tasks",
        json={
            "command": "poster",
            "account_id": account_id,
            "targets": refs,
            "message": "постим всем",
            "interval": 5,
        },
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    task = (await response.json())["task"]
    assert task["targets_count"] == MANY
    assert f"{MANY} чат" in task["title"]
    # Один поиск на всю задачу: обход диалогов на каждую ссылку — это FloodWait.
    assert many_chats_resolved == [MANY]


async def test_mailing_with_many_chats_counts_the_work(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Работа рассылки измерима и на большом списке: получатели × круги."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": [f"@ch-{n}" for n in range(MANY)],
            "message": "текст",
            "repeats": 2,
        },
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    task = (await response.json())["task"]
    assert task["mailing"]["recipients"] == MANY
    assert task["progress"] == {"done": 0, "total": MANY * 2}


async def test_poster_still_takes_a_single_target_field(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Одиночное поле «приёмник» постинг принимает: с ним приходят задачи из бота."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "poster",
            "account_id": account_id,
            "target": "@ch-1",
            "message": "постим в один",
        },
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    task = (await response.json())["task"]
    assert task["targets_count"] == 1
    assert task["title"] == "Постинг по расписанию → @ch-1"


async def test_poster_keeps_a_multiline_message_whole(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Прайс в четыре строки — одно сообщение постинга, а не четыре.

    Отдельные сообщения задаются пустой строкой: круг берёт по одному, поэтому
    разбитый по переносам прайс уходил бы четырьмя кругами по кусочку.
    """
    from app.db.database import SessionLocal
    from app.telegram_client.jobs import load_mailing_library

    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    price = "Приму 1 код,момент\n4000 - 552\nПриму 1 код,момент\n4500 - 585"

    response = await client.post(
        "/api/tasks",
        json={
            "command": "poster",
            "account_id": account_id,
            "targets": ["@ch-1", "@ch-2"],
            "message": f"{price}\n\nвторое сообщение",
        },
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    rule_id = (await response.json())["task"]["id"]
    async with SessionLocal() as session:
        rule = await session.get(Rule, rule_id)
        assert rule is not None
        ids = rule.filters["library_ids"]
    # Свой текст постинга лежит в библиотеке — там же, где у рассылки, и деление
    # на сообщения происходит до неё: в записях уже готовые куски.
    sending = await load_mailing_library(TEST_USER_ID, ids)
    assert [row.text for row in sending] == [price, "второе сообщение"]


async def test_broadcast_takes_the_whole_chat_list(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Пересылка в чаты просит один список чатов — и ходит во все.

    Раньше у неё было отдельное обязательное поле «приёмник», а счёт чатов шёл по
    настройкам и терял чат из этого поля. Теперь список один и считается целиком.
    """
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "broadcast",
            "account_id": account_id,
            "source": "@ch-900",
            "targets": [f"@ch-{n}" for n in range(MANY)],
        },
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    task = (await response.json())["task"]
    assert task["targets_count"] == MANY
    assert f"{MANY} чат" in task["title"]
    # Источник ищем тем же обходом диалогов, что и чаты: поиск всё равно один.
    assert many_chats_resolved == [MANY + 1]


async def test_broadcast_never_posts_back_into_its_source(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Источник в списке чатов — это пересылка самому себе: выкидываем его."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "broadcast",
            "account_id": account_id,
            "source": "@ch-7",
            "targets": ["@ch-7", "@ch-8"],
        },
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    task = (await response.json())["task"]
    assert task["targets_count"] == 1
    assert task["title"] == "Пересылка: @ch-7 → @ch-8"


async def test_broadcast_still_takes_a_single_target_field(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Одиночный «приёмник» пересылка принимает: с ним приходят задачи из бота."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "broadcast",
            "account_id": account_id,
            "source": "@ch-900",
            "target": "@ch-1",
        },
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    task = (await response.json())["task"]
    assert task["targets_count"] == 1
    assert task["title"] == "Пересылка: @ch-900 → @ch-1"


async def test_broadcast_without_chats_is_rejected(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Пересылка «в никуда» молча ничего не делала бы — отказываем сразу."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={"command": "broadcast", "account_id": account_id, "source": "@ch-900", "targets": []},
        headers=auth_headers,
    )

    assert response.status == 400
    assert "чаты" in (await response.json())["error"]


async def test_bot_card_counts_the_chats(create_user, create_account):
    """Карточка в боте называет число чатов, а не только первый из них."""
    from app.bot.texts import rule_card

    rule_id, user_id, _ = await make_poster(
        create_user, create_account, chats=[-1001, -1002, -1003], messages=["раз"]
    )

    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
        card = rule_card(rule)

    assert "Чатов: <b>3</b>" in card


async def test_missing_chats_are_reported_in_one_short_line(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    """Ненайденных чатов может быть сотня: в ответе — счёт и первые имена."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "poster",
            "account_id": account_id,
            "targets": ["@ch-1", "нет-1", "нет-2", "нет-3", "нет-4", "нет-5"],
            "message": "текст",
        },
        headers=auth_headers,
    )

    assert response.status == 404
    error = (await response.json())["error"]
    assert "Не нашёл чаты (5)" in error
    assert "и ещё 2" in error
    # Длинное перечисление в кабинете не читается — держим строку короткой.
    assert len(error) < 120


@pytest.fixture
def one_shot_stubbed(monkeypatch):
    """Задачи «по запросу» в тестах не ходят в Telegram: запуск подменён."""

    async def fake_run_task_now(rule):
        return {"ok": True, "kind": getattr(rule, "kind", "")}

    monkeypatch.setattr(manager, "run_task_now", fake_run_task_now)


async def test_autosubscribe_takes_channels_the_account_has_not_joined_yet(
    client, auth_headers, create_account, login_open, many_chats_resolved, one_shot_stubbed
):
    """Канал, которого нет в диалогах, — это норма для автоподписки, а не 404.

    Задача существует ровно для того, чтобы в такой канал вступить: до вступления
    его нет в диалогах аккаунта, а ссылку-приглашение t.me/+… не разрешает вообще
    никто. Раньше кабинет отвечал «Не нашёл чаты» на то, что и просили сделать.
    """
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    refs = ["@ch-1", "@nowhere", "t.me/+invite"]

    response = await client.post(
        "/api/tasks",
        json={"command": "autosubscribe", "account_id": account_id, "targets": refs},
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    rule_id = (await response.json())["task"]["id"]
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, TEST_USER_ID)
        assert rule is not None
        # Ссылки уходят задаче как есть: разбирает их уже run_autosubscribe.
        assert rule.filters["subscribe_to"] == refs


async def test_autosubscribe_still_needs_access_to_its_source(
    client, auth_headers, create_account, login_open, many_chats_resolved, one_shot_stubbed
):
    """Источник ссылок — другое дело: из него читают посты, без доступа никак."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "autosubscribe",
            "account_id": account_id,
            "source": "@closed-chat",
            "targets": ["@ch-1"],
        },
        headers=auth_headers,
    )

    assert response.status == 404
    assert "источник" in (await response.json())["error"]


async def test_chats_list_is_not_cut(client, auth_headers, create_account, monkeypatch):
    """Чат, которого нет в списке, нельзя выбрать мышкой — значит, не обрезаем."""
    from app.config import settings

    monkeypatch.setattr(settings, "api_id", FAKE_API_ID)
    monkeypatch.setattr(settings, "api_hash", FAKE_API_HASH)
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    dialogs = [
        {"id": -8000 - n, "title": f"чат {n}", "username": "", "is_channel": False, "is_group": True}
        for n in range(300)
    ]

    async def fake_list_dialogs(_account_id: int, limit: int = 0):
        return dialogs[:limit] if limit > 0 else list(dialogs)

    monkeypatch.setattr(manager, "list_dialogs", fake_list_dialogs)

    response = await client.get(f"/api/chats?account_id={account_id}", headers=auth_headers)
    body = await response.json()
    assert body["total"] == 300

    short = await client.get(
        f"/api/chats?account_id={account_id}&limit=10", headers=auth_headers
    )
    assert (await short.json())["total"] == 10

    tail = await client.get(
        f"/api/chats?account_id={account_id}&q=чат 299", headers=auth_headers
    )
    assert (await tail.json())["total"] == 1
