"""Рассылка по чатам: считалки планировщика, сам проход и библиотека сообщений.

Рассылка — единственная задача, которая сама решает, когда отправлять: она не
ждёт входящих сообщений, а ходит по списку получателей по кругу. Проверять её
важно в трёх местах:

* считалки (паузы, позиция в круге, выбор сообщения) — чистые функции, их
  видно без Telethon и без базы;
* проход планировщика — что за один тик уходит ровно одно сообщение одному
  получателю, что круг закрывается, а заданное число кругов останавливает
  задачу;
* библиотека — что созданная из кабинета задача кладёт тексты туда, откуда их
  потом читает планировщик.
"""
from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from telethon.errors import FloodWaitError, RPCError

from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog, Rule, Subscription
from app.telegram_client import jobs
from app.telegram_client.filters import FilterConfig
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID

# ──────────────────────────────── считалки ────────────────────────────────────


def config(**overrides) -> FilterConfig:
    """Настройки рассылки: defaults как в FilterConfig, поверх — правки теста."""
    return FilterConfig.from_dict({"kind": "mailing", **overrides})


def test_gap_is_never_shorter_than_a_second():
    """Быстрее секунды между чатами Telegram не даст — и смысла в этом нет."""
    assert jobs.mailing_gap(config(gap_seconds=0)) == jobs.MAILING_MIN_GAP


def test_jitter_only_adds_to_the_pause():
    """Джиттер прибавляется к паузе, а не разбрасывается вокруг неё: человек
    задал минимум, и уходить ниже него — прямой путь под ограничения."""
    base = jobs.MAILING_MIN_GAP
    spread = 4
    gaps = [jobs.mailing_gap(config(gap_seconds=base, gap_jitter=spread)) for _ in range(50)]

    assert all(base <= gap <= base + spread for gap in gaps)
    # Случайная добавка действительно случайна: иначе это обычная пауза.
    assert len(set(gaps)) > 1


def test_cycle_pause_has_its_own_setting():
    pause = config(gap_seconds=90, cycle_seconds=420)
    assert jobs.mailing_gap(pause) == 90
    assert jobs.mailing_gap(pause, cycle=True) == 420


def test_tiny_pauses_are_lifted_to_the_antispam_floor():
    """Паузы ниже пола поднимаются: старые задачи с gap=5 лечатся сами."""
    tiny = config(gap_seconds=5, cycle_seconds=10)
    assert jobs.mailing_gap(tiny) == jobs.MAILING_MIN_GAP
    assert jobs.mailing_gap(tiny, cycle=True) == jobs.MAILING_MIN_CYCLE


def test_absurd_pause_is_capped():
    """Пауза в месяц — это опечатка, а не настройка: держим разумный предел."""
    assert jobs.mailing_gap(config(gap_seconds=10**9)) == jobs.MAILING_MAX_GAP


def test_next_due_counts_from_the_plan_not_from_now():
    """Отсчёт от планового времени: иначе задержки RPC копятся и темп уползает."""
    assert jobs.mailing_next_due(100.0, 90.0, 5) == 105.0
    # Но и не раньше «сейчас»: после простоя долг не выливается очередью подряд.
    assert jobs.mailing_next_due(100.0, 120.0, 5) == 125.0


def test_position_is_derived_from_the_counter():
    """Счётчик отправок и есть прогресс: отдельной таблицы хода не нужно."""
    assert jobs.mailing_position(0, 3) == (0, 0)
    assert jobs.mailing_position(4, 3) == (1, 1)
    assert jobs.mailing_position(6, 3) == (0, 2)
    # Ноль получателей — не деление на ноль, а «рассылать некуда».
    assert jobs.mailing_position(5, 0) == (0, 0)


def test_pick_goes_round_and_random_pick_does_not():
    items = ["первое", "второе", "third"]

    assert [jobs.mailing_pick(items, step).removeprefix("") for step in range(4)] == [
        "первое",
        "второе",
        "third",
        "первое",
    ]
    single = ["одно"]
    assert jobs.mailing_pick(single, 5, random_pick=True) == "одно"
    assert jobs.mailing_pick([], 0) is None


# ──────────────────────────────── фейковый клиент ─────────────────────────────


class TypingAction:
    """Контекст «печатает»: в Telethon это ``client.action(chat, 'typing')``."""

    def __init__(self, client: "FakeClient", chat_id: int) -> None:
        self.client = client
        self.chat_id = chat_id

    async def __aenter__(self) -> "TypingAction":
        self.client.typing.append(self.chat_id)
        return self

    async def __aexit__(self, *exc_info) -> bool:
        return False


class FakeClient:
    """Telethon-клиент без Telegram: пишет, «печатает» и умеет ломаться."""

    def __init__(self, *, error: BaseException | None = None, connected: bool = True) -> None:
        self.sent: list[tuple[int, str]] = []
        self.typing: list[int] = []
        self.error = error
        self.connected = connected

    def is_connected(self) -> bool:
        return self.connected

    def action(self, chat_id: int, mode: str = ""):
        return TypingAction(self, chat_id)

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent))

    @property
    def recipients(self) -> list[int]:
        return [chat_id for chat_id, _ in self.sent]


@pytest.fixture(autouse=True)
def clean_manager():
    """Менеджер — синглтон на весь процесс: после теста забываем его состояние."""
    yield
    manager._mailing_rules = []
    manager._mailing_state.clear()
    manager._clients.clear()
    manager._poster_rules = []
    manager._poster_state.clear()
    manager._send_pause_until.clear()


@pytest.fixture
def no_pauses(monkeypatch):
    """Пауз между отправками нет: иначе тест ждал бы настоящие секунды."""
    monkeypatch.setattr(jobs, "mailing_gap", lambda *args, **kwargs: 0.0)


async def make_mailing(
    create_user,
    create_account,
    *,
    targets: list[int],
    texts: list[str] = (),
    forwarded: int = 0,
    subscription: bool = True,
    **settings,
) -> tuple[int, int, int]:
    """Кладёт в базу рассылку с получателями и сообщениями. Возвращает
    (rule_id, user_id, account_id)."""
    user_id = await create_user()
    account_id = await create_account(user_id)

    async with session_scope() as session:
        if subscription:
            session.add(
                Subscription(user_id=user_id, active_until=repo.utcnow() + timedelta(days=1))
            )
        rule = Rule(
            user_id=user_id,
            account_id=account_id,
            source_id=0,
            target_id=targets[0],
            kind="mailing",
            enabled=True,
            forwarded_count=forwarded,
        )
        session.add(rule)
        await session.flush()
        rule.filters = {"targets": list(targets[1:]), **settings}
        for text in texts:
            await repo.add_saved_message(session, user_id=user_id, text=text)

    await manager.refresh_rules()
    return rule.id, user_id, account_id


# ─────────────────────────────── проход планировщика ──────────────────────────


async def test_mailing_goes_round_over_the_recipients(create_user, create_account, no_pauses):
    """Один тик — одно сообщение одному получателю, дальше по кругу."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001, -1002], texts=["всем привет"]
    )
    client = FakeClient()
    manager._clients[account_id] = client

    for _ in range(3):
        await manager._mailing_tick()

    assert client.recipients == [-1001, -1002, -1001]


async def test_each_step_takes_the_next_message(create_user, create_account, no_pauses):
    """Сообщения идут по очереди: за круг получатели видят разные тексты."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001, -1002], texts=["первое", "второе"]
    )
    client = FakeClient()
    manager._clients[account_id] = client

    for _ in range(3):
        await manager._mailing_tick()

    assert client.sent == [(-1001, "первое"), (-1002, "второе"), (-1001, "первое")]


async def test_given_number_of_cycles_stops_the_task(create_user, create_account, no_pauses):
    """Сделали два круга — задача сама встаёт на паузу, а не крутится впустую."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001, -1002], texts=["раз"], repeats=2
    )
    client = FakeClient()
    manager._clients[account_id] = client

    for _ in range(5):
        await manager._mailing_tick()

    assert client.recipients == [-1001, -1002, -1001, -1002]
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
    assert rule.enabled is False


async def test_counter_survives_a_restart(create_user, create_account, no_pauses):
    """После перезапуска процесса рассылка продолжает с того же получателя."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001, -1002], texts=["раз"], forwarded=3
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._mailing_tick()

    # Отправлено уже три, четвёртая — второму получателю второго круга.
    assert client.recipients == [-1002]


async def test_empty_library_sends_nothing_and_does_not_spam(create_user, create_account, no_pauses):
    """Сообщений нет — ждём, а не сыплем предупреждениями каждый тик."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001]
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._mailing_tick()
    await manager._mailing_tick()

    assert client.sent == []
    assert manager._mailing_state[rule_id]["not_before"] > time.time()


async def test_nothing_to_send_is_said_on_the_card(create_user, create_account, no_pauses):
    """Рассылать нечего — причина попадает на карточку, и только один раз.

    Так бывает после уборки в библиотеке: задача осталась, а сообщений больше
    нет. Раньше про это знал только лог службы, до которого человеку не
    добраться: карточка показывала «работает», а в чаты ничего не уходило.
    Повторять строку каждую минуту тоже нельзя — журнал утонул бы в одинаковых
    сбоях, поэтому пишем её один раз на простой.
    """
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001]
    )
    manager._clients[account_id] = FakeClient()

    await manager._mailing_tick()
    # Пауза после простоя не даёт дойти до отправки — снимаем её руками, иначе
    # второй тик вернулся бы раньше проверки и «один раз» вышло бы само собой.
    manager._mailing_state[rule_id]["not_before"] = 0.0
    await manager._mailing_tick()

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(ForwardLog.status, ForwardLog.error).where(
                        ForwardLog.rule_id == rule_id
                    )
                )
            )
            .all()
        )
        health = await repo.task_health(session, [rule_id])

    assert [row[0] for row in rows] == ["error"]
    assert "рассылать нечего" in str(rows[0][1])
    assert "рассылать нечего" in str((health.get(rule_id) or {}).get("error"))


async def test_a_fresh_message_ends_the_pause(create_user, create_account, no_pauses):
    """Сообщение появилось — рассылка идёт дальше и о простое больше не пишет."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001]
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._mailing_tick()
    async with session_scope() as session:
        await repo.add_saved_message(session, user_id=user_id, text="наконец-то")
    manager._mailing_state[rule_id]["not_before"] = 0.0
    await manager._mailing_tick()

    assert client.sent == [(-1001, "наконец-то")]
    assert manager._mailing_state[rule_id]["empty"] is False


async def test_flood_wait_pauses_the_whole_mailing(create_user, create_account, no_pauses):
    """Telegram сказал подождать — слушаемся, иначе на следующем тике тот же отказ."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001], texts=["раз"]
    )
    client = FakeClient(error=FloodWaitError(None, capture=30))
    manager._clients[account_id] = client

    await manager._mailing_tick()

    assert client.sent == []
    state = manager._mailing_state[rule_id]
    assert state["not_before"] > time.time() + 29


async def test_rpc_error_is_logged_and_does_not_kill_the_task(
    create_user, create_account, no_pauses
):
    """Ошибка в одном чате не должна ни ронять планировщик, ни останавливать рассылку."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001], texts=["раз"]
    )
    client = FakeClient(error=RPCError(None, "чат недоступен", 400))
    manager._clients[account_id] = client

    await manager._mailing_tick()
    await manager._mailing_tick()

    assert client.sent == []
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
    assert rule.enabled is True


async def test_typing_is_shown_before_sending(create_user, create_account, no_pauses):
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001], texts=["раз"], typing=True
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._mailing_tick()

    assert client.typing == [-1001]
    assert client.recipients == [-1001]


async def test_without_subscription_nothing_is_sent(create_user, create_account, no_pauses):
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001], texts=["раз"], subscription=False
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._mailing_tick()

    assert client.sent == []


async def test_offline_account_is_skipped(create_user, create_account, no_pauses):
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001], texts=["раз"]
    )
    manager._clients[account_id] = FakeClient(connected=False)

    await manager._mailing_tick()

    assert manager._mailing_state == {}


async def test_empty_records_never_reach_the_library(create_user):
    """Пустая запись — это «отправить ничего»: такая в рассылке только мешает."""
    user_id = await create_user()
    async with session_scope() as session:
        await repo.add_saved_message(session, user_id=user_id, text="")
        await repo.add_saved_message(session, user_id=user_id, text="нормальное")

    items = await jobs.load_mailing_library(user_id)

    assert [item.text for item in items] == ["нормальное"]


# ─────────────────────────────────── API ──────────────────────────────────────


@pytest.fixture
def login_open(monkeypatch):
    """Вход аккаунтов в тесте включён: без него создание задачи — 503."""
    from app import accounts_login

    monkeypatch.setattr(accounts_login, "require_enabled", lambda: None)


@pytest.fixture
def resolved_chats(monkeypatch):
    """Чаты «находятся» без Telegram: имя запроса и есть чат.

    Подменяем поиск пачкой: кабинет ищет все чаты задачи одним вызовом, и в нём
    же живёт разбор ссылок. Одиночный ``resolve_chat`` ходит через ту же пачку,
    поэтому подмена одного метода закрывает оба пути.
    """
    chats = {"@a": 111, "@b": 222, "@c": 333}

    async def fake_resolve_many(account_id: int, queries):
        found = {}
        for raw in queries:
            key = (raw or "").strip()
            if key in chats:
                found[key] = (chats[key], key)
        return found

    monkeypatch.setattr(manager, "resolve_many", fake_resolve_many)
    return chats


async def test_mailing_from_the_cabinet_fills_the_library(
    client, auth_headers, create_account, login_open, resolved_chats
):
    """Тексты из формы ложатся в библиотеку: оттуда их читает планировщик."""
    await client.get("/api/me", headers=auth_headers)  # создаёт пользователя и триал
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@a", "@b"],
            # Пустая строка — граница сообщений. Одиночный перенос её не делает,
            # см. test_line_breaks_inside_a_message_stay_in_one_message.
            "message": "первое\n\nвторое",
            "gap": 70,
            "repeats": 2,
        },
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    task = (await response.json())["task"]
    assert task["kind"] == "mailing"
    assert task["mailing"]["recipients"] == 2
    assert task["mailing"]["messages_count"] == 2
    assert task["mailing"]["gap_seconds"] == 70
    # Два получателя × два круга — вот и вся работа задачи, она измерима.
    assert task["progress"] == {"done": 0, "total": 4}

    library = await (await client.get("/api/library", headers=auth_headers)).json()
    assert sorted(item["text"] for item in library["items"]) == ["второе", "первое"]


async def test_line_breaks_inside_a_message_stay_in_one_message(
    client, auth_headers, create_account, login_open, resolved_chats
):
    """Многострочный текст — одно сообщение, а не строчка на отправку.

    Так его и пишут люди: прайс, объявление в два-три ряда. Раньше резали по
    каждому переносу, и прайс из четырёх строк уходил четырьмя сообщениями —
    ровно то, чего человек не хотел. Граница сообщений теперь пустая строка.
    """
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    price = "Приму 1 код,момент\n4000 - 552\nПриму 1 код,момент\n4500 - 585"

    response = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@a"],
            "message": price,
        },
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    assert (await response.json())["task"]["mailing"]["messages_count"] == 1

    library = await (await client.get("/api/library", headers=auth_headers)).json()
    assert [item["text"] for item in library["items"]] == [price]
    # Заголовок записи — первая строка, а не «Приму 1 код,момент\n4000…» целиком:
    # в списке библиотеки нужна одна строка, по которой текст узнают.
    assert library["items"][0]["title"] == "Приму 1 код,момент"


async def test_mailing_takes_the_chosen_library_without_retyping_the_text(
    client, auth_headers, create_account, login_open, resolved_chats
):
    """Сообщения выбраны в библиотеке — второй раз набирать их в форме незачем."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    chosen = []
    for text in ("афиша", "напоминание"):
        created = await client.post("/api/library", json={"text": text}, headers=auth_headers)
        chosen.append((await created.json())["item"]["id"])

    response = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@a"],
            "library_ids": chosen,
        },
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    task = (await response.json())["task"]
    assert task["mailing"]["messages_count"] == len(chosen)
    # Библиотека не растёт от самого факта запуска задачи: копий текста не появилось.
    library = await (await client.get("/api/library", headers=auth_headers)).json()
    assert len(library["items"]) == len(chosen)


async def test_mailing_without_text_and_without_library_is_400(
    client, auth_headers, create_account, login_open, resolved_chats
):
    """Нечего рассылать — просим текст, а не создаём молчаливую задачу."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={"command": "mailing", "account_id": account_id, "targets": ["@a"]},
        headers=auth_headers,
    )

    assert response.status == 400
    assert "сообщение" in (await response.json())["error"]


async def test_mailing_without_recipients_is_400(
    client, auth_headers, create_account, login_open, resolved_chats
):
    """Рассылка некуда — это ошибка ввода, а не задача, которая молча молчит."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@unknown"],
            "message": "текст",
        },
        headers=auth_headers,
    )

    assert response.status == 400
    assert "получател" in (await response.json())["error"]


async def test_library_item_without_text_is_rejected(client, auth_headers):
    """Пустая запись — это «отправить ничего»: такие не принимаем."""
    await client.get("/api/me", headers=auth_headers)

    response = await client.post("/api/library", json={"text": "  "}, headers=auth_headers)

    assert response.status == 400


async def test_library_item_is_deleted(client, auth_headers):
    await client.get("/api/me", headers=auth_headers)
    created = await client.post(
        "/api/library", json={"text": "привет"}, headers=auth_headers
    )
    item_id = (await created.json())["item"]["id"]

    assert (await client.delete(f"/api/library/{item_id}", headers=auth_headers)).status == 200
    assert (await client.delete(f"/api/library/{item_id}", headers=auth_headers)).status == 404


async def test_library_delete_rejects_foreign_item(client, auth_headers, create_user):
    """Чужая запись не удаляется и не выдаёт своего владельца лишний раз."""
    await client.get("/api/me", headers=auth_headers)
    other = await create_user()
    async with session_scope() as session:
        item = await repo.add_saved_message(session, user_id=other, text="чужое")

    assert (await client.delete(f"/api/library/{item.id}", headers=auth_headers)).status == 404

async def test_mailing_repeat_forever_is_an_explicit_user_setting(
    client, auth_headers, create_account, login_open, resolved_chats
):
    """Бесконечные круги включаются отдельной настройкой, а не магическим нулём."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "mailing",
            "account_id": account_id,
            "targets": ["@a", "@b"],
            "message": "одно сообщение",
            "repeats": 1,
            "repeat_forever": True,
        },
        headers=auth_headers,
    )

    assert response.status == 201, await response.text()
    task = (await response.json())["task"]
    assert task["mailing"]["repeat_forever"] is True
    assert task["mailing"]["repeats"] == 0
    assert task["progress"]["total"] is None
