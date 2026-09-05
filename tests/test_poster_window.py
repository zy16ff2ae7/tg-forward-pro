"""Окно постинга идёт по часам хозяина задачи, а не по часам сервера.

Окно «с 10:00 до 20:00» человек задаёт по своим часам: он ими и живёт, и по ним
решает, когда людям писать можно, а когда уже поздно. Сервер же стоит в UTC —
и московское окно 10:00–20:00 работало на нём с 13:00 до 23:00 по Москве:
первые три часа окна задача молчала, а последний круг уходил в полночь. Ровно
то, от чего окно и защищает.

Теперь рядом с окном лежит смещение хозяина от UTC (`window_tz`, минуты), и
планировщик сверяется с его часами. Проверяем:

* окно открыто/закрыто по часам хозяина, даже когда у сервера они другие;
* смещение приходит из кабинета, живёт в настройках задачи и видно в карточке
  и в форме правки;
* ерунда вместо часового пояса не выдумывает хозяину чужие часы;
* задачи из прошлой версии (смещения нет) считают окно по часам сервера
  по-прежнему, и карточка говорит об этом словами.
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from app.db.database import session_scope
from app.db.models import Rule
from app.telegram_client.jobs import (
    SECONDS_IN_DAY,
    window_now_sec,
    window_tz_minutes,
)
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID
from tests.test_mailing import FakeClient
from tests.test_many_chats import (  # noqa: F401 — фикстуры соседнего файла
    instant_poster,
    login_open,
    make_poster,
    many_chats_resolved,
)

HOUR = 3600


def server_tz_minutes() -> int:
    """Смещение часов сервера от UTC — то, по чему окно считалось раньше."""
    offset = datetime.now().astimezone().utcoffset()
    return int((offset.total_seconds() if offset else 0) // 60)


def hhmm(seconds: int) -> str:
    """Секунды от полуночи → «ЧЧ:ММ» (сутки замыкаются)."""
    seconds %= SECONDS_IN_DAY
    return f"{seconds // HOUR:02d}:{seconds % HOUR // 60:02d}"


def window_around(now_sec: int, half: int = 1800) -> tuple[str, str]:
    """Окно вокруг момента: полчаса до и полчаса после."""
    return hhmm(now_sec - half), hhmm(now_sec + half)


# ─────────────────────── часы хозяина считаются верно ─────────────────────────


def test_the_owner_clock_is_read_off_the_offset():
    """Смещение известно — время считаем от UTC, а не от часов сервера."""
    midday_utc = 1_800_000_000 - 1_800_000_000 % SECONDS_IN_DAY + 9 * HOUR  # 09:00 UTC

    assert window_now_sec(0, midday_utc) == 9 * HOUR
    assert window_now_sec(180, midday_utc) == 12 * HOUR, "Москва: UTC+3"
    assert window_now_sec(-300, midday_utc) == 4 * HOUR, "Нью-Йорк: UTC−5"
    assert window_now_sec(330, midday_utc) == 14 * HOUR + 30 * 60, "Индия: UTC+5:30"


def test_the_owner_clock_wraps_around_midnight():
    """Сутки замыкаются: 23:00 UTC у московского хозяина — уже 02:00."""
    late_utc = 1_800_000_000 - 1_800_000_000 % SECONDS_IN_DAY + 23 * HOUR

    assert window_now_sec(180, late_utc) == 2 * HOUR
    assert window_now_sec(-300, late_utc) == 18 * HOUR


def test_no_offset_means_the_server_clock():
    """Смещения нет — остаются часы сервера: так работали задачи до настройки."""
    now = time.time()
    local = time.localtime(now)

    assert window_now_sec(None, now) == (
        local.tm_hour * HOUR + local.tm_min * 60 + local.tm_sec
    )


@pytest.mark.parametrize("raw", ["", None, "полдень", 5000, -5000, [], True])
def test_nonsense_instead_of_a_timezone_invents_nothing(raw):
    """Непонятное смещение — это «часы сервера», а не выдуманный пояс.

    Подобрать человеку пояс наугад значит молча сдвинуть ему время рассылки.
    """
    assert window_tz_minutes(raw) is None


@pytest.mark.parametrize("raw,minutes", [(180, 180), ("-300", -300), (0, 0), (840, 840)])
def test_a_sane_offset_is_kept_as_is(raw, minutes):
    assert window_tz_minutes(raw) == minutes


# ───────────────────── планировщик смотрит на часы хозяина ────────────────────


async def poster_with_window(create_user, create_account, *, tz, window):
    """Постинг с окном и смещением. → (rule_id, account_id)."""
    start, end = window
    rule_id, _, account_id = await make_poster(
        create_user,
        create_account,
        chats=[-9001],
        messages=["афиша"],
        window_start=start,
        window_end=end,
        **({} if tz is None else {"window_tz": tz}),
    )
    return rule_id, account_id


async def test_the_window_opens_by_the_owner_clock(
    create_user, create_account, instant_poster
):
    """У хозяина полдень — задача постит, хотя у сервера в это время ночь."""
    owner_tz = server_tz_minutes() + 6 * 60  # хозяин живёт на шесть часов восточнее
    window = window_around(window_now_sec(owner_tz))
    _, account_id = await poster_with_window(
        create_user, create_account, tz=owner_tz, window=window
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()

    assert client.sent == [(-9001, "афиша")], f"окно {window} у хозяина открыто"


async def test_the_window_closes_by_the_owner_clock(
    create_user, create_account, instant_poster
):
    """У хозяина ночь — задача молчит, хотя по часам сервера окно открыто."""
    owner_tz = server_tz_minutes() + 6 * 60
    # Окно вокруг часов СЕРВЕРА: для хозяина оно закрыто — он на шесть часов
    # восточнее. Раньше именно так и считалось, и задача бы отправила.
    window = window_around(window_now_sec(None))
    _, account_id = await poster_with_window(
        create_user, create_account, tz=owner_tz, window=window
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()

    assert client.sent == [], f"окно {window} для хозяина уже закрыто"


async def test_an_old_poster_keeps_the_server_clock(
    create_user, create_account, instant_poster
):
    """Смещения у задачи нет — окно по часам сервера, как и раньше.

    Иначе задача, чьё окно человек однажды подобрал под серверные часы, молча
    съехала бы на несколько часов.
    """
    window = window_around(window_now_sec(None))
    _, account_id = await poster_with_window(
        create_user, create_account, tz=None, window=window
    )
    client = FakeClient()
    manager._clients[account_id] = client

    await manager._poster_tick()

    assert client.sent == [(-9001, "афиша")]


# ──────────────────────── смещение приходит из кабинета ───────────────────────


@pytest.fixture
async def poster_account(client, auth_headers, create_account, login_open, many_chats_resolved):
    await client.get("/api/me", headers=auth_headers)
    return await create_account(TEST_USER_ID)


async def create_poster(client, auth_headers, account_id, **extra):
    response = await client.post(
        "/api/tasks",
        json={
            "command": "poster",
            "account_id": account_id,
            "targets": ["@ch-1"],
            "message": "афиша",
            "start": "10:00",
            "end": "20:00",
            **extra,
        },
        headers=auth_headers,
    )
    assert response.status == 201, await response.text()
    return (await response.json())["task"]


async def test_the_cabinet_sends_its_clock_with_the_window(
    client, auth_headers, poster_account
):
    """Кабинет прислал смещение — оно легло в задачу, карточка и форма его знают."""
    task = await create_poster(client, auth_headers, poster_account, tz=180)

    assert (task["window_start"], task["window_end"]) == ("10:00", "20:00")
    assert task["window_tz"] == 180, "карточка знает, чьи это часы"
    assert task["edit"]["tz"] == 180, "форма правки — тоже"
    async with session_scope() as session:
        rule = await session.get(Rule, task["id"])
        assert (rule.filters or {})["window_tz"] == 180


async def test_a_poster_without_a_clock_says_so(client, auth_headers, poster_account):
    """Смещения не прислали — окно по часам сервера, и это видно в ответе.

    Так приходят задачи от старых клиентов: выдумывать им пояс нельзя, но и
    молчать нельзя — кабинет пишет на карточке «по часам сервера».
    """
    task = await create_poster(client, auth_headers, poster_account)

    assert task["window_tz"] is None
    assert task["edit"]["tz"] is None


async def test_the_clock_moves_with_the_task(client, auth_headers, poster_account):
    """Задачу правят из другого пояса — окно начинает считаться по нему."""
    task = await create_poster(client, auth_headers, poster_account, tz=180)

    response = await client.patch(
        f"/api/tasks/{task['id']}", json={"start": "09:00", "tz": -300}, headers=auth_headers
    )

    assert response.status == 200, await response.text()
    saved = (await response.json())["task"]
    assert (saved["window_start"], saved["window_end"]) == ("09:00", "20:00")
    assert saved["window_tz"] == -300


async def test_editing_the_interval_keeps_the_clock(client, auth_headers, poster_account):
    """Правка не про окно — смещение остаётся прежним, а не сбрасывается."""
    task = await create_poster(client, auth_headers, poster_account, tz=180)

    response = await client.patch(
        f"/api/tasks/{task['id']}", json={"interval": 15}, headers=auth_headers
    )

    assert response.status == 200, await response.text()
    saved = (await response.json())["task"]
    assert (saved["interval_min"], saved["window_tz"]) == (15, 180)


async def test_a_nonsense_clock_does_not_move_the_window(
    client, auth_headers, poster_account
):
    """Ерунда вместо пояса — «часы сервера», а не окно, съехавшее на сутки."""
    task = await create_poster(client, auth_headers, poster_account, tz="полдень")

    assert task["window_tz"] is None
    assert (task["window_start"], task["window_end"]) == ("10:00", "20:00")


@pytest.fixture(autouse=True)
def clean_manager():
    """Менеджер — синглтон на весь процесс: состояние между тестами не тащим."""
    yield
    manager._poster_rules = []
    manager._poster_state.clear()
    manager._mailing_rules = []
    manager._mailing_state.clear()
    manager._clients.clear()
