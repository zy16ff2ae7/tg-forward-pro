"""Здоровье задачи: журнал пересылок наконец кто-то читает.

В журнал писали четыре места, а читать его не умел никто: карточка бодро
показывала «работает» задаче, которая сутки только падала, а причину было видно
только в логе службы на сервере. Проверяем три уровня обещания:

* **журнал** (``repo.task_health``) — когда задача сработала в последний раз, на
  чём сломалась и сломана ли она сейчас. Плюс чистка: без неё таблица растёт
  быстрее всех остальных;
* **кабинет** — здоровье приходит в каждой карточке, время помечено UTC (иначе
  «5 минут назад» съезжает на часовой пояс), id чата заменён названием;
* **планировщик** — постинг и рассылка отмечают сбой прохода. Без этого
  предупреждение на карточке навсегда осталось бы пустым ровно у задач «в любое
  число чатов»: их отказы раньше уходили только в лог.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from telethon.errors import FloodWaitError

from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog, Rule
from app.telegram_client import jobs
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID

# Фикстуры соседних файлов: клиент-заглушка, чистка синглтона менеджера и
# отсутствие пауз нужны здесь ровно те же. Своя копия разошлась бы с оригиналом.
from tests.test_mailing import (  # noqa: F401
    FakeClient,
    clean_manager,
    make_mailing,
    no_pauses,
)
from tests.test_many_chats import instant_poster, make_poster  # noqa: F401


async def add_rule(user_id: int, account_id: int, **fields) -> int:
    """Правило прямо в базу: журнал не зависит от того, как задачу создали."""
    async with session_scope() as session:
        rule = Rule(
            user_id=user_id,
            account_id=account_id,
            source_id=0,
            target_id=fields.pop("target_id", -1001),
            kind=fields.pop("kind", "poster"),
            enabled=True,
            **fields,
        )
        session.add(rule)
        await session.flush()
        return rule.id


async def add_log(rule_id: int, user_id: int, *, status: str = "ok", error: str = "", age=None):
    """Строка журнала с нужным возрастом: «давно» и «только что» — разные ответы.

    Пишем через ``repo.log_forward``, а не напрямую: обрезка длинного текста
    живёт там, и тест должен проверять её, а не свою копию.
    """
    async with session_scope() as session:
        await repo.log_forward(
            session,
            rule_id=rule_id,
            user_id=user_id,
            source_msg_id=0,
            target_msg_id=None,
            status=status,
            error=error or None,
        )
        log = (
            await session.execute(
                select(ForwardLog)
                .where(ForwardLog.rule_id == rule_id)
                .order_by(ForwardLog.id.desc())
                .limit(1)
            )
        ).scalar_one()
        if age is not None:
            log.created_at = repo.utcnow() - age
            await session.flush()
        return log.id


async def health_of(rule_id: int) -> dict:
    async with session_scope() as session:
        health = await repo.task_health(session, [rule_id])
    return health.get(rule_id) or {}


async def logged(rule_id: int) -> list[tuple[str, str]]:
    """Журнал задачи в порядке записи — парами (статус, причина)."""
    async with session_scope() as session:
        rows = await session.execute(
            select(ForwardLog.status, ForwardLog.error)
            .where(ForwardLog.rule_id == rule_id)
            .order_by(ForwardLog.id)
        )
        return [(status, error or "") for status, error in rows]


# ────────────────────────────── журнал: чтение ────────────────────────────────


async def test_no_rules_asked_no_queries_made():
    """Пустой список задач — пустой ответ: список задач бывает и пустым."""
    async with session_scope() as session:
        assert await repo.task_health(session, []) == {}
        assert await repo.task_health(session, [0, None]) == {}


async def test_task_without_records_is_not_called_broken(create_user, create_account):
    """Задача, которая ещё ни разу не сработала, — не сломанная.

    Её просто не было в журнале, и в ответе её тоже нет: карточка покажет
    «работает», а не предупреждение на пустом месте.
    """
    user_id = await create_user()
    rule_id = await add_rule(user_id, await create_account(user_id))

    async with session_scope() as session:
        assert await repo.task_health(session, [rule_id]) == {}


async def test_last_success_time_is_reported(create_user, create_account):
    """Успех — это время последней отправки, по нему карточка пишет «5 минут назад»."""
    user_id = await create_user()
    rule_id = await add_rule(user_id, await create_account(user_id))
    await add_log(rule_id, user_id, age=timedelta(hours=2))
    await add_log(rule_id, user_id)

    health = await health_of(rule_id)

    assert health["error"] is None
    assert health["failing"] is False
    # Взяли последнюю отправку, а не первую.
    assert repo.utcnow() - health["ok_at"] < timedelta(minutes=1)


async def test_error_after_success_marks_the_task_broken(create_user, create_account):
    """Последняя запись — сбой: значит задача сломана сейчас, а не когда-то."""
    user_id = await create_user()
    rule_id = await add_rule(user_id, await create_account(user_id))
    await add_log(rule_id, user_id, age=timedelta(minutes=30))
    await add_log(rule_id, user_id, status="error", error="ChatWriteForbiddenError")

    health = await health_of(rule_id)

    assert health["failing"] is True
    assert health["error"] == "ChatWriteForbiddenError"
    assert health["error_at"] is not None
    # Время прошлого успеха не теряется: «работала до 14:20» — это тоже ответ.
    assert health["ok_at"] is not None


async def test_success_after_error_clears_the_failing_flag(create_user, create_account):
    """Задача заработала — предупреждение больше не кричит, но причину видно.

    Сбой не стирается: «час назад в один чат не ушло» человеку нужно знать даже
    тогда, когда следующий проход прошёл.
    """
    user_id = await create_user()
    rule_id = await add_rule(user_id, await create_account(user_id))
    await add_log(rule_id, user_id, status="error", error="таймаут")
    await add_log(rule_id, user_id)

    health = await health_of(rule_id)

    assert health["failing"] is False
    assert health["error"] == "таймаут"


async def test_informational_event_is_not_a_task_error(create_user, create_account):
    """Начало или перенос шага видны в журнале, но не красят задачу красным."""
    user_id = await create_user()
    rule_id = await add_rule(user_id, await create_account(user_id))
    await add_log(rule_id, user_id, status="info", error="Шаг автопрогрева начат")

    assert await health_of(rule_id) == {}


async def test_error_without_text_still_has_a_reason(create_user, create_account):
    """Сбой без описания — тоже сбой: карточке нужно что-то показать."""
    user_id = await create_user()
    rule_id = await add_rule(user_id, await create_account(user_id))
    await add_log(rule_id, user_id, status="error")

    assert (await health_of(rule_id))["error"] == "неизвестная ошибка"


async def test_only_the_newest_error_is_shown(create_user, create_account):
    """Из десяти отказов интересен последний: остальные уже история."""
    user_id = await create_user()
    rule_id = await add_rule(user_id, await create_account(user_id))
    for n in range(10):
        await add_log(rule_id, user_id, status="error", error=f"отказ {n}")

    assert (await health_of(rule_id))["error"] == "отказ 9"


async def test_many_tasks_are_answered_in_one_call(create_user, create_account):
    """Двадцать задач — один ответ: иначе список задач стоил бы двадцать запросов."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule_ids = [await add_rule(user_id, account_id) for _ in range(20)]
    for number, rule_id in enumerate(rule_ids):
        if number % 2:
            await add_log(rule_id, user_id, status="error", error=f"сбой {number}")
        else:
            await add_log(rule_id, user_id)

    async with session_scope() as session:
        health = await repo.task_health(session, rule_ids)

    assert len(health) == 20
    assert [entry["failing"] for entry in (health[rule_id] for rule_id in rule_ids)] == [
        bool(number % 2) for number in range(20)
    ]


async def test_records_of_another_task_do_not_leak(create_user, create_account):
    """Чужой сбой на свою карточку не попадает."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    mine = await add_rule(user_id, account_id)
    other = await add_rule(user_id, account_id)
    await add_log(mine, user_id)
    await add_log(other, user_id, status="error", error="это не моё")

    assert (await health_of(mine))["error"] is None
    assert (await health_of(other))["error"] == "это не моё"


async def test_huge_error_text_is_cut_before_the_database(create_user, create_account):
    """Трассировка на сто килобайт в журнале не нужна — храним начало."""
    user_id = await create_user()
    rule_id = await add_rule(user_id, await create_account(user_id))
    await add_log(rule_id, user_id, status="error", error="я" * 5000)

    assert len((await health_of(rule_id))["error"]) == 1000


# ────────────────────────────── журнал: чистка ────────────────────────────────


async def test_old_records_are_dropped_and_fresh_ones_stay(create_user, create_account):
    """Месяц истории хватает, чтобы понять состояние задачи; дальше — балласт."""
    user_id = await create_user()
    rule_id = await add_rule(user_id, await create_account(user_id))
    await add_log(rule_id, user_id, age=timedelta(days=repo.FORWARD_LOG_TTL_DAYS + 1))
    await add_log(rule_id, user_id, age=timedelta(days=repo.FORWARD_LOG_TTL_DAYS + 40))
    await add_log(rule_id, user_id, age=timedelta(days=1))

    async with session_scope() as session:
        dropped = await repo.trim_forward_logs(session)

    assert dropped == 2
    assert len(await logged(rule_id)) == 1
    # Чистка не сделала задачу «никогда не работавшей».
    assert (await health_of(rule_id))["ok_at"] is not None


async def test_trim_on_empty_journal_is_harmless():
    """Чистка на пустой таблице — не ошибка: фоновый цикл зовёт её каждые пять минут."""
    async with session_scope() as session:
        assert await repo.trim_forward_logs(session) == 0


async def test_trim_window_never_collapses_to_zero(create_user, create_account):
    """Ноль дней в настройке не должен означать «снести журнал целиком»."""
    user_id = await create_user()
    rule_id = await add_rule(user_id, await create_account(user_id))
    await add_log(rule_id, user_id)

    async with session_scope() as session:
        assert await repo.trim_forward_logs(session, older_than_days=0) == 0


# ───────────────────────────── кабинет: карточка ──────────────────────────────


@pytest.fixture
async def own_poster(client, auth_headers, create_account):
    """Задача того самого пользователя, которым подписан initData."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    rule_id = await add_rule(TEST_USER_ID, account_id, target_id=-1001234567890)
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, TEST_USER_ID)
        rule.filters = {
            "messages": ["объявление"],
            "targets": [-1009876543210],
            "chat_titles": {"-1001234567890": "Афиша", "-1009876543210": "Зеркало афиши"},
        }
    return rule_id


async def task_from_list(client, auth_headers, task_id: int) -> dict:
    response = await client.get("/api/tasks", headers=auth_headers)
    assert response.status == 200, await response.text()
    tasks = (await response.json())["tasks"]
    return next(task for task in tasks if task["id"] == task_id)


async def test_card_gets_health_even_with_an_empty_journal(client, auth_headers, own_poster):
    """Ключ есть всегда: кабинету не приходится угадывать, сбоев нет или ответа нет."""
    task = await task_from_list(client, auth_headers, own_poster)

    assert task["health"] == {"ok_at": None, "error": None, "error_at": None, "failing": False}


async def test_card_time_is_marked_as_utc(client, auth_headers, own_poster):
    """Без пометки часового пояса браузер читает строку как местное время.

    «5 минут назад» превращалось бы в «3 часа назад» ровно на разницу поясов —
    и на карточке живой задачи стояло бы «давно не работала».
    """
    await add_log(own_poster, TEST_USER_ID)

    task = await task_from_list(client, auth_headers, own_poster)

    assert task["health"]["ok_at"].endswith("+00:00")


async def test_chat_id_in_the_reason_becomes_a_chat_name(client, auth_headers, own_poster):
    """«-1009876543210» человеку ничего не говорит, а название задача уже помнит."""
    await add_log(
        own_poster,
        TEST_USER_ID,
        status="error",
        error="не ушло в -1009876543210: ChatWriteForbiddenError",
    )

    task = await task_from_list(client, auth_headers, own_poster)

    assert task["health"]["error"] == "не ушло в Зеркало афиши: ChatWriteForbiddenError"
    assert task["health"]["failing"] is True


async def test_long_reason_is_cut_for_the_narrow_screen(client, auth_headers, own_poster):
    """Экран шириной 390 px: длинную причину обрезаем, полная остаётся в журнале."""
    await add_log(own_poster, TEST_USER_ID, status="error", error="очень длинная причина " * 40)

    error = (await task_from_list(client, auth_headers, own_poster))["health"]["error"]

    assert error.endswith("…")
    assert len(error) <= 161


async def test_short_error_number_is_not_mistaken_for_a_chat(client, auth_headers, own_poster):
    """Код ошибки — не id чата: замена названий его не трогает."""
    await add_log(own_poster, TEST_USER_ID, status="error", error="Telegram ответил 420")

    assert (await task_from_list(client, auth_headers, own_poster))["health"][
        "error"
    ] == "Telegram ответил 420"


async def test_toggle_answers_with_health_too(client, auth_headers, own_poster):
    """Ответ на одну задачу — того же состава, что и карточка в списке."""
    await add_log(own_poster, TEST_USER_ID, status="error", error="сломалось")

    response = await client.post(f"/api/tasks/{own_poster}/toggle", headers=auth_headers)

    assert response.status == 200, await response.text()
    task = (await response.json())["task"]
    assert task["health"]["error"] == "сломалось"
    assert task["health"]["failing"] is True


# ──────────────────────────── планировщик: постинг ────────────────────────────


class PartlyBrokenClient(FakeClient):
    """Клиент, у которого часть чатов закрыта: остальные принимают сообщения."""

    def __init__(self, *, broken: set[int], error: BaseException | None = None) -> None:
        super().__init__()
        self.broken = set(broken)
        self.failure = error or RuntimeError("ChatWriteForbiddenError")

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if chat_id in self.broken:
            raise self.failure
        return await super().send_message(chat_id, text, **kwargs)


async def test_poster_records_the_lost_chat_but_keeps_working(
    create_user, create_account, instant_poster
):
    """Один чат из трёх потерян: причина видна, а задача не объявлена сломанной.

    Проход, в котором что-то ушло, — рабочий проход. Иначе постинг по сотне
    чатов носил бы красное предупреждение из-за одного удалённого чата.
    """
    chats = [-4001, -4002, -4003]
    rule_id, _, account_id = await make_poster(
        create_user, create_account, chats=chats, messages=["всем привет"]
    )
    manager._clients[account_id] = PartlyBrokenClient(broken={-4002})

    await manager._poster_tick()

    health = await health_of(rule_id)
    assert health["failing"] is False, "два чата из трёх получили — проход рабочий"
    assert "-4002" in health["error"]
    assert health["ok_at"] is not None
    # Одна запись на проход, а не на чат: журнал не должен стать потоком.
    assert [status for status, _ in await logged(rule_id)] == ["error", "ok"]


async def test_poster_with_all_chats_dead_is_broken(create_user, create_account, instant_poster):
    """Не ушло никуда — вот это сбой, и карточка обязана о нём сказать."""
    chats = [-5001, -5002]
    rule_id, _, account_id = await make_poster(
        create_user, create_account, chats=chats, messages=["никому не дойдёт"]
    )
    manager._clients[account_id] = PartlyBrokenClient(broken=set(chats))

    await manager._poster_tick()

    health = await health_of(rule_id)
    assert health["failing"] is True
    assert health["ok_at"] is None
    assert "2 чат" in health["error"], "в причине видно масштаб, а не только первый чат"


async def test_flood_wait_is_a_pause_not_a_failure(create_user, create_account, instant_poster):
    """«Подождите 30 секунд» — не сбой: задача сама вернётся к этим чатам.

    Запись о сбое здесь означала бы красную карточку у совершенно здоровой
    задачи, которая просто разогналась.
    """
    rule_id, _, account_id = await make_poster(
        create_user, create_account, chats=[-6001, -6002], messages=["часто"]
    )
    manager._clients[account_id] = FakeClient(error=FloodWaitError(None, capture=30))

    await manager._poster_tick()

    assert await logged(rule_id) == [], "журнал молчит"
    assert await health_of(rule_id) == {}


async def test_successful_round_is_one_record_per_pass(
    create_user, create_account, instant_poster
):
    """Одна строка на проход, а не на чат.

    Десять чатов круг обходит за два прохода (за раз уходит не больше порции) —
    значит в журнале две записи, а не десять. Иначе рассылка по сотне чатов
    превращала бы журнал в поток, в котором сбой не найти.
    """
    chats = [-7000 - n for n in range(10)]
    rule_id, user_id, account_id = await make_poster(
        create_user, create_account, chats=chats, messages=["раз"]
    )
    manager._clients[account_id] = FakeClient()

    await manager._poster_tick()
    await manager._poster_tick()

    assert await logged(rule_id) == [("ok", ""), ("ok", "")]
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
    assert rule.forwarded_count == 10


async def test_idle_tick_writes_nothing(create_user, create_account, instant_poster):
    """Пустой проход — пустой журнал: планировщик тикает каждые несколько секунд."""
    rule_id, _, account_id = await make_poster(
        create_user, create_account, chats=[-8001], messages=["раз"], interval_seconds=600
    )
    manager._clients[account_id] = FakeClient()

    await manager._poster_tick()
    await manager._poster_tick()  # интервал не вышел — отправки нет

    assert await logged(rule_id) == [("ok", "")]


# ──────────────────────────── планировщик: рассылка ───────────────────────────


async def test_mailing_failure_is_recorded_and_then_forgiven(
    create_user, create_account, no_pauses
):
    """Рассылка спотыкается на получателе — и продолжает круг.

    Отказ пишем в журнал (иначе на карточке навсегда «работает»), а следующая
    удачная отправка снимает предупреждение сама. Место в круге при отказе не
    двигается: получатель, который не принял сообщение, ещё не обслужен.
    """
    rule_id, _, account_id = await make_mailing(
        create_user, create_account, targets=[-9001, -9002], texts=["текст"]
    )
    manager._clients[account_id] = PartlyBrokenClient(broken={-9001})

    await manager._mailing_tick()

    broken = await health_of(rule_id)
    assert broken["failing"] is True
    assert "-9001" in broken["error"]
    assert manager._mailing_state[rule_id]["pos"] == 0, "получатель ещё не обслужен"

    # Чат снова принимает, пауза после отказа вышла — в бою её выдерживает время.
    manager._clients[account_id].broken.clear()
    manager._mailing_state[rule_id]["not_before"] = 0.0
    await manager._mailing_tick()

    healed = await health_of(rule_id)
    assert healed["failing"] is False
    assert healed["ok_at"] is not None
    assert [status for status, _ in await logged(rule_id)] == ["error", "ok"]
