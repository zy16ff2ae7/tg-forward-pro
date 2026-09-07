"""Лучшее время для постинга: пик активности ленты за 14 дней.

Сигналы — живые события: доставки журнала и пойманное ловцом чеков.
Парсер не в счёт (его метки — время сбора). Часы — в tz задачи.
Проверяем:

* пик — лучшее двухчасовое окно, парсер и чужие события мимо;
* сдвиг tz двигает пик в часы задачи;
* меньше десяти событий — пика нет, а не выдумка;
* эндпоинт отдаёт гистограмму и режет мусор в параметрах.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.db import repo
from app.db.database import session_scope
from app.db.models import CollectedItem, ForwardLog
from app.timeutil import utcnow
from tests.helpers import TEST_USER_ID

OTHER_ID = 768_000_501


async def _log(user_id: int, at: datetime, status: str = "ok"):
    async with session_scope() as session:
        session.add(ForwardLog(
            rule_id=1, user_id=user_id, source_msg_id=1,
            status=status, created_at=at,
        ))
        await session.commit()


async def _check(user_id: int, at: datetime):
    async with session_scope() as session:
        session.add(CollectedItem(
            rule_id=1, user_id=user_id, kind="checks",
            payload={"text": "чек"}, created_at=at,
        ))
        await session.commit()


async def _parse_hit(user_id: int, at: datetime):
    async with session_scope() as session:
        session.add(CollectedItem(
            rule_id=1, user_id=user_id, kind="parser",
            payload={"name": "мёртвая душа"}, created_at=at,
        ))
        await session.commit()


def _day_at(hour: int, days_ago: int = 1) -> datetime:
    base = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return base - timedelta(days=days_ago) + timedelta(hours=hour)


async def test_peak_is_best_two_hour_window(create_user):
    """Пик — два соседних часа с максимумом; парсер и чужие мимо."""
    await create_user(id=TEST_USER_ID)
    await create_user(id=OTHER_ID)
    for _ in range(8):
        await _log(TEST_USER_ID, _day_at(9))
    for _ in range(4):
        await _log(TEST_USER_ID, _day_at(10))
    for _ in range(2):
        await _log(TEST_USER_ID, _day_at(22))
    await _check(TEST_USER_ID, _day_at(9))
    for _ in range(30):
        await _parse_hit(TEST_USER_ID, _day_at(3))
    for _ in range(30):
        await _log(OTHER_ID, _day_at(15))

    async with session_scope() as session:
        result = await repo.activity_hours(session, TEST_USER_ID)
    assert result["total"] == 15
    assert result["peak"] == {"start": 9, "end": 11}
    assert result["hours"][9] == 9 and result["hours"][3] == 0


async def test_tz_moves_peak_to_task_hours(create_user):
    """Сдвиг +3 часа: пик 09 UTC — это 12 в часах задачи."""
    await create_user(id=TEST_USER_ID)
    for _ in range(10):
        await _log(TEST_USER_ID, _day_at(9))
    async with session_scope() as session:
        result = await repo.activity_hours(session, TEST_USER_ID, tz_offset=180)
    assert result["peak"] == {"start": 12, "end": 14}


async def test_few_events_mean_no_peak(create_user):
    """Девять событий — пика нет: мало данных, а не окно."""
    await create_user(id=TEST_USER_ID)
    for _ in range(9):
        await _log(TEST_USER_ID, _day_at(9))
    async with session_scope() as session:
        result = await repo.activity_hours(session, TEST_USER_ID)
    assert result["total"] == 9 and result["peak"] is None


async def test_endpoint_reports_histogram(client, auth_headers, create_user):
    """Эндпоинт: гистограмма из 24, пик, мусор в параметрах режется."""
    await create_user(id=TEST_USER_ID)
    for _ in range(10):
        await _log(TEST_USER_ID, _day_at(20))
    response = await client.get(
        "/api/activity/hours?days=14&tz=60", headers=auth_headers
    )
    assert response.status == 200
    body = await response.json()
    assert len(body["hours"]) == 24 and body["total"] == 10
    assert body["peak"] == {"start": 21, "end": 23}

    garbage = await client.get(
        "/api/activity/hours?days=abc&tz=zzz", headers=auth_headers
    )
    assert garbage.status == 200
    assert (await garbage.json())["total"] == 10
