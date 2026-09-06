"""Разовая задача обязана оставить след: что было при запуске.

Парсер и автоподписку запускают кнопкой, и весь их итог жил ровно до закрытия
всплывающей подсказки: «Telegram просит подождать 40 сек», «аккаунт не в сети» и
даже удачный сбор не сохранялись нигде. На карточке оставались метка «по кнопке»
и ноль собранных — по ней нельзя было понять, запускали задачу минуту назад или
ни разу, и почему ничего не вышло. Отдельно терялись вступления автоподписки:
их никто не считал, и полоса выполнения стояла на нуле даже после удачного
захода в пять чатов.

Проверяем три уровня обещания:

* **заход в чаты** — во что вступили, где уже были и куда не пустили: «вступили
  в 0 из 5» без этого читается как поломка;
* **журнал задачи** (``record_oneshot`` → ``repo.task_health``) — итог запуска
  там же, где итоги расписанных задач, и в том же порядке: сбой раньше успеха;
* **весь путь** — кнопка «Запустить» в кабинете и карточка сразу после неё.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    InviteRequestSentError,
    UserAlreadyParticipantError,
)

from app.db import repo
from app.db.database import session_scope
from app.db.models import Rule
from app.telegram_client import jobs
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID

# Фикстуры соседних файлов: чистка синглтона менеджера и включённый вход
# аккаунтов нужны здесь ровно те же, своя копия разошлась бы с оригиналом.
from tests.test_mailing import clean_manager  # noqa: F401
from tests.test_many_chats import login_open  # noqa: F401
from tests.test_task_health import health_of, logged  # noqa: F401


@pytest.fixture(autouse=True)
def instant_joins(monkeypatch):
    """Пауза между вступлениями: в бою две секунды на чат, в тесте не нужна."""
    monkeypatch.setattr(jobs, "JOIN_PAUSE_SECONDS", 0)


def _join_key(request) -> str:
    """По какой цели пришёл запрос: имя канала или хеш приглашения."""
    return str(getattr(request, "channel", None) or getattr(request, "hash", "") or "")


class OneShotClient:
    """Telethon-клиент без Telegram: участники, посты источника и ответы на заходы.

    ``joins`` — что ответить на вступление: исключение поднимаем, всё остальное
    считается удачей. Ключ — цель так, как её видит задача: «@name» для канала и
    хеш без плюса для ссылки-приглашения (её задача разбирает сама).
    """

    def __init__(
        self,
        *,
        participants: list = (),
        messages: list = (),
        joins: dict | None = None,
        source_error: BaseException | None = None,
        participants_error: BaseException | None = None,
        connected: bool = True,
    ) -> None:
        self.participants = list(participants)
        self.messages = list(messages)
        self.joins = dict(joins or {})
        self.source_error = source_error
        self.participants_error = participants_error
        self.connected = connected
        self.tried: list[str] = []

    def is_connected(self) -> bool:
        return self.connected

    async def __call__(self, request):
        target = _join_key(request)
        self.tried.append(target)
        answer = self.joins.get(target)
        if isinstance(answer, BaseException):
            raise answer
        return SimpleNamespace(updates=[])

    def iter_participants(self, chat_id, limit: int = 0):
        async def walk():
            if self.participants_error is not None:
                raise self.participants_error
            for user in self.participants[: limit or None]:
                yield user

        return walk()

    def iter_messages(self, chat_id, limit: int = 0):
        async def walk():
            if self.source_error is not None:
                raise self.source_error
            for message in self.messages[: limit or None]:
                yield message

        return walk()


def person(user_id: int, name: str = "Кто-то") -> SimpleNamespace:
    """Участник чата — ровно те поля, которые читает парсер."""
    return SimpleNamespace(
        id=user_id, first_name=name, last_name="", username=None, phone=None,
        deleted=False, bot=False,
    )


async def make_oneshot(
    create_user,
    create_account,
    *,
    kind: str,
    user_id: int | None = None,
    source_id: int = 0,
    **settings,
) -> tuple[int, int, int]:
    """Разовая задача прямо в базу. Возвращает (rule_id, user_id, account_id).

    Когда ``user_id`` задан, фабрика пользователей не нужна и на её место можно
    передать ``None``: так пишутся тесты кабинета, где пользователя уже создал
    подписанный initData.
    """
    user_id = user_id or await create_user()
    # Запуск gated абонементом: тесты проверяют сам запуск, а не оплату,
    # поэтому пробник выдаём здесь же (повторная выдача — no-op).
    async with session_scope() as session:
        await repo.grant_trial(session, user_id)
        await session.commit()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        rule = Rule(
            user_id=user_id,
            account_id=account_id,
            source_id=source_id,
            target_id=source_id or -1001,
            kind=kind,
            enabled=True,
        )
        session.add(rule)
        await session.flush()
        rule.filters = {"kind": kind, **settings}
        return rule.id, user_id, account_id


async def run_now(rule_id: int, user_id: int) -> dict:
    """Запуск задачи так, как его делает кабинет: через менеджер."""
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
        return await manager.run_task_now(rule)


async def forwarded(rule_id: int, user_id: int) -> int:
    """Счётчик задачи: из него кабинет рисует полосу выполнения автоподписки."""
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
        return int(rule.forwarded_count or 0)


# ───────────────────────────────── заход в чаты ───────────────────────────────


async def test_join_counts_what_it_joined():
    """Обычный заход: сколько чатов дали, во столько и вступили.

    Ссылка-приглашение идёт другим запросом, чем канал по имени, — поэтому в
    списке и то и другое: одна дорога однажды уже отваливалась молча.
    """
    client = OneShotClient()

    outcome = await jobs._join_all(client, ["@first", "+secret", "@third"])

    assert (outcome.joined, outcome.already, outcome.problems) == (3, 0, [])
    assert client.tried == ["@first", "secret", "@third"]


async def test_already_a_member_is_not_a_problem():
    """«Уже участник» и «заявка отправлена» — не сбой: исправлять нечего."""
    client = OneShotClient(
        joins={
            "@inside": UserAlreadyParticipantError(None),
            "@waiting": InviteRequestSentError(None),
        }
    )

    outcome = await jobs._join_all(client, ["@inside", "@waiting"])

    assert (outcome.joined, outcome.already) == (0, 2)
    assert outcome.problems == [], "краснеть карточке тут незачем"


async def test_a_refusal_names_the_chat_and_the_reason():
    """Не пустили — человек должен видеть, куда именно и почему."""
    client = OneShotClient(joins={"@closed": ChatAdminRequiredError(None)})

    outcome = await jobs._join_all(client, ["@open", "@closed"])

    assert outcome.joined == 1
    assert outcome.problems == ["не пустили в @closed (ChatAdminRequiredError)"]


async def test_flood_wait_keeps_what_was_already_joined():
    """«Подождите» на середине не отменяет сделанного.

    Отказ пробрасываем наверх (у задачи по сообщениям свой повтор после паузы), а
    вступления остаются в переданном итоге — иначе три чата превращались в ноль.
    """
    client = OneShotClient(joins={"@second": FloodWaitError(None, capture=40)})
    outcome = jobs.JoinOutcome()

    with pytest.raises(FloodWaitError):
        await jobs._join_all(client, ["@first", "@second", "@third"], outcome)

    assert outcome.joined == 1
    assert client.tried == ["@first", "@second"], "после отказа задача остановилась"


# ─────────────────────────── журнал: парсер аудитории ─────────────────────────


async def test_a_successful_parser_run_is_dated_in_the_journal(create_user, create_account):
    """Собрал участников — на карточке появилось «сработала только что».

    Раньше удачный запуск не оставлял ничего: полоса показывала собранных, но
    когда это было — час назад или в прошлом месяце — не знал никто.
    """
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="parser", source_id=-4242, limit=50
    )
    manager._clients[account_id] = OneShotClient(participants=[person(11), person(12)])

    result = await run_now(rule_id, user_id)

    assert result["ok"] is True and result["collected"] == 2
    assert await logged(rule_id) == [("ok", "")]
    health = await health_of(rule_id)
    assert health["ok_at"] is not None and health["failing"] is False


async def test_a_run_that_collected_nothing_new_still_counts_as_a_run(
    create_user, create_account
):
    """Второй запуск подряд не приносит новых людей — но запуск-то был.

    «Ничего нового» и «не запускалось» человеку выглядят одинаково, поэтому
    удачный проход отмечаем и тогда, когда собрано ноль.
    """
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="parser", source_id=-4242
    )
    manager._clients[account_id] = OneShotClient(participants=[person(21)])

    first = await run_now(rule_id, user_id)
    second = await run_now(rule_id, user_id)

    assert (first["collected"], second["collected"]) == (1, 0)
    assert await logged(rule_id) == [("ok", ""), ("ok", "")]
    assert (await health_of(rule_id))["failing"] is False


async def test_flood_wait_on_a_parser_run_lands_on_the_card(create_user, create_account):
    """«Подождите 40 секунд» — это ответ человеку, а не строчка в логе сервера.

    У разовой задачи нет следующего прохода, который всё исправит сам: за кнопкой
    должен вернуться человек, значит причина обязана дожить до карточки.
    """
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="parser", source_id=-4242
    )
    manager._clients[account_id] = OneShotClient(
        participants_error=FloodWaitError(None, capture=40)
    )

    result = await run_now(rule_id, user_id)

    assert result["ok"] is False and "40" in result["error"]
    health = await health_of(rule_id)
    assert health["failing"] is True
    assert "подождать" in health["error"]
    assert health["ok_at"] is None, "запуска, который что-то сделал, не было"


async def test_an_offline_account_explains_itself_on_the_card(create_user, create_account):
    """Аккаунт не в сети — самый частый отказ, и он тоже жил один тост."""
    rule_id, user_id, _ = await make_oneshot(
        create_user, create_account, kind="parser", source_id=-4242
    )

    result = await run_now(rule_id, user_id)

    assert result["ok"] is False
    assert "не в сети" in (await health_of(rule_id))["error"]


async def test_a_dropped_connection_is_not_an_empty_result(create_user, create_account):
    """Аккаунт в пуле, но связь оборвалась — это отказ, а не «собрано 0».

    Случай, который на сервере встречается чаще пустого пула: клиент создан,
    сессия жива, а сокет отвалился. Если такой запуск не остановить, задача
    отчитается бодрым «Собрано участников: 0» — и человек будет искать ошибку в
    настройках вместо того, чтобы перезапустить аккаунт.
    """
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="parser", source_id=-4242
    )
    manager._clients[account_id] = OneShotClient(
        participants=[person(51)], connected=False
    )

    result = await run_now(rule_id, user_id)

    assert result["ok"] is False and "collected" not in result
    assert "не в сети" in (await health_of(rule_id))["error"]


async def test_a_crash_inside_the_run_reaches_the_card(create_user, create_account):
    """Неожиданная поломка — с типом ошибки: без него разбирать нечего."""
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="parser", source_id=-4242
    )
    manager._clients[account_id] = OneShotClient(
        participants_error=RuntimeError("клиент рассыпался")
    )

    result = await run_now(rule_id, user_id)

    assert result["ok"] is False
    error = (await health_of(rule_id))["error"]
    assert "RuntimeError" in error and "клиент рассыпался" in error


# ───────────────────────────── журнал: автоподписка ───────────────────────────


async def test_autosubscribe_counts_the_chats_it_joined(create_user, create_account):
    """Вступили в три чата — полоса выполнения это показывает.

    Счётчик задачи и есть её прогресс (``_task_view``), а ручной запуск его не
    трогал: после удачного захода в пять чатов на карточке стоял ноль.
    """
    rule_id, user_id, account_id = await make_oneshot(
        create_user,
        create_account,
        kind="autosubscribe",
        subscribe_to=["@a", "@b", "@c"],
    )
    manager._clients[account_id] = OneShotClient()

    result = await run_now(rule_id, user_id)

    assert (result["ok"], result["joined"], result["total"]) == (True, 3, 3)
    assert await forwarded(rule_id, user_id) == 3
    assert await logged(rule_id) == [("ok", "")]


async def test_a_refused_chat_is_named_but_the_run_is_not_broken(create_user, create_account):
    """Два чата из трёх приняли — проход рабочий, но про третий надо сказать.

    Порядок записи тот же, что у расписанных задач: сбой раньше успеха, поэтому
    карточка не краснеет, а причина на ней остаётся.
    """
    rule_id, user_id, account_id = await make_oneshot(
        create_user,
        create_account,
        kind="autosubscribe",
        subscribe_to=["@a", "@closed", "@c"],
    )
    manager._clients[account_id] = OneShotClient(
        joins={"@closed": ChatAdminRequiredError(None)}
    )

    result = await run_now(rule_id, user_id)

    assert result["joined"] == 2 and result["problems"]
    assert [status for status, _ in await logged(rule_id)] == ["error", "ok"]
    health = await health_of(rule_id)
    assert health["failing"] is False, "два чата из трёх вступили — проход рабочий"
    assert "не пустили в @closed" in health["error"]


async def test_a_run_where_nothing_was_joined_is_broken(create_user, create_account):
    """Не пустили никуда — вот это сбой, и масштаб виден в причине."""
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="autosubscribe", subscribe_to=["@x", "@y"]
    )
    manager._clients[account_id] = OneShotClient(
        joins={
            "@x": ChatAdminRequiredError(None),
            "@y": ChannelPrivateError(None),
        }
    )

    result = await run_now(rule_id, user_id)

    assert result["joined"] == 0
    health = await health_of(rule_id)
    assert health["failing"] is True and health["ok_at"] is None
    assert "и ещё 1" in health["error"], "в причине видно, что отказ не один"


async def test_already_being_in_every_chat_is_not_a_failure(create_user, create_account):
    """Аккаунт уже во всех чатах: делать нечего — и краснеть нечему."""
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="autosubscribe", subscribe_to=["@a", "@b"]
    )
    manager._clients[account_id] = OneShotClient(
        joins={
            "@a": UserAlreadyParticipantError(None),
            "@b": UserAlreadyParticipantError(None),
        }
    )

    result = await run_now(rule_id, user_id)

    assert (result["joined"], result["already"]) == (0, 2)
    assert await logged(rule_id) == [("ok", "")]
    assert (await health_of(rule_id))["error"] is None


async def test_flood_wait_keeps_the_partial_joins_in_the_count(create_user, create_account):
    """Отказ на середине: один чат уже наш, и счётчик обязан это знать."""
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="autosubscribe", subscribe_to=["@a", "@b", "@c"]
    )
    manager._clients[account_id] = OneShotClient(
        joins={"@b": FloodWaitError(None, capture=40)}
    )

    result = await run_now(rule_id, user_id)

    assert (result["ok"], result["joined"]) == (False, 1)
    assert await forwarded(rule_id, user_id) == 1
    # Успехом неудачный запуск не дополняем: человеку надо вернуться к кнопке.
    assert [status for status, _ in await logged(rule_id)] == ["error"]
    assert (await health_of(rule_id))["failing"] is True


async def test_an_unreadable_source_is_written_down(create_user, create_account):
    """Источник ссылок не прочитан — задача сделала половину работы.

    Ссылок из его постов она не увидит, и знать об этом должен человек, а не
    только лог службы на сервере: чаты из настроек при этом обходятся как обычно.
    """
    rule_id, user_id, account_id = await make_oneshot(
        create_user,
        create_account,
        kind="autosubscribe",
        source_id=-777,
        subscribe_to=["@a"],
    )
    manager._clients[account_id] = OneShotClient(source_error=ChannelPrivateError(None))

    result = await run_now(rule_id, user_id)

    assert result["ok"] is True and result["joined"] == 1
    health = await health_of(rule_id)
    assert "источник не прочитан" in health["error"]
    assert "ChannelPrivateError" in health["error"]
    assert health["failing"] is False, "в чат из настроек вступили — проход рабочий"


async def test_no_channels_to_join_says_so(create_user, create_account):
    """Пустой список чатов — самая обидная тишина: делать нечего и не сказано."""
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="autosubscribe"
    )
    manager._clients[account_id] = OneShotClient()

    result = await run_now(rule_id, user_id)

    assert result["ok"] is False
    assert "Не указано ни одного канала" in (await health_of(rule_id))["error"]


async def test_one_run_is_one_pair_of_records(create_user, create_account):
    """Двенадцать чатов — не двенадцать строк журнала, а одна пара «сбой + успех».

    Иначе автоподписка по сотне ссылок превращала бы журнал в поток, в котором
    ничего не найти, — и вымывала бы из него остальные задачи (журнал живёт месяц).
    """
    chats = [f"@ch-{n}" for n in range(12)]
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="autosubscribe", subscribe_to=chats
    )
    manager._clients[account_id] = OneShotClient(
        joins={name: ChatAdminRequiredError(None) for name in chats[:5]}
    )

    result = await run_now(rule_id, user_id)

    assert result["joined"] == 7
    assert [status for status, _ in await logged(rule_id)] == ["error", "ok"]
    assert "и ещё 4" in (await health_of(rule_id))["error"]


async def test_a_broken_journal_does_not_break_the_run(create_user, create_account, monkeypatch):
    """Запись в журнал не должна отнимать у человека результат запуска.

    Сводку он уже ждёт в ответе: если журнал почему-то недоступен, запуск обязан
    вернуть то, что сделал, а не превратиться в «Task failed».
    """
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="parser", source_id=-4242
    )
    manager._clients[account_id] = OneShotClient(participants=[person(31)])

    async def boom(*args, **kwargs):
        raise RuntimeError("журнал недоступен")

    monkeypatch.setattr(jobs, "record_oneshot", boom)

    result = await run_now(rule_id, user_id)

    assert result["ok"] is True and result["collected"] == 1


# ─────────────────────── весь путь: кнопка и карточка ─────────────────────────


async def card(client, auth_headers, task_id: int) -> dict:
    response = await client.get("/api/tasks", headers=auth_headers)
    assert response.status == 200, await response.text()
    tasks = (await response.json())["tasks"]
    return next(task for task in tasks if task["id"] == task_id)


async def test_the_card_shows_why_the_button_did_nothing(
    client, auth_headers, create_account, login_open
):
    """Нажали «Запустить», запуск не удался — причина осталась на карточке.

    Тот самый случай, из-за которого всё это писалось: подсказка гасла, и задача
    выглядела как ни разу не запущенная. Теперь метка «сбой» и причина приходят в
    том же ответе, которым кабинет обновляет список задач после кнопки.
    """
    await client.get("/api/me", headers=auth_headers)
    rule_id, _, account_id = await make_oneshot(
        None, create_account, kind="parser", user_id=TEST_USER_ID, source_id=-4242
    )
    manager._clients[account_id] = OneShotClient(
        participants_error=FloodWaitError(None, capture=40)
    )

    response = await client.post(f"/api/tasks/{rule_id}/run", headers=auth_headers)

    assert response.status == 200, await response.text()
    assert (await response.json())["run"]["ok"] is False
    health = (await card(client, auth_headers, rule_id))["health"]
    assert health["failing"] is True
    assert "подождать" in health["error"]


async def test_the_card_dates_the_successful_run(
    client, auth_headers, create_account, login_open
):
    """Удачный запуск: карточка знает, когда это было, и сколько собрано."""
    await client.get("/api/me", headers=auth_headers)
    rule_id, _, account_id = await make_oneshot(
        None, create_account, kind="parser", user_id=TEST_USER_ID, source_id=-4242, limit=100
    )
    manager._clients[account_id] = OneShotClient(participants=[person(41), person(42)])

    response = await client.post(f"/api/tasks/{rule_id}/run", headers=auth_headers)

    assert response.status == 200, await response.text()
    task = await card(client, auth_headers, rule_id)
    # Метка UTC обязательна: иначе «только что» съезжает на часовой пояс.
    assert task["health"]["ok_at"].endswith("+00:00")
    assert task["health"]["failing"] is False
    assert task["progress"] == {"done": 2, "total": 100}
