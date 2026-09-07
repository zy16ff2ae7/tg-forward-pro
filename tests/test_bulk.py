"""Массовые действия: всё на паузу, всё запустить, всё в архив — одной кнопкой.

Область действия решает сервер: пауза бьёт по активным, запуск — по стоящим,
архив — по стоящим неархивным. Активные в архив пачкой не уезжают. Проверяем:

* pause_all гасит активные и не трогает паузу, архив и чужие задачи;
* resume_all запускает стоящие и обнуляет исчерпанные круги рассылки;
* archive_all убирает стоящие, активные на месте;
* пустой список — ноль без ошибки, мусор вместо действия — 400.
"""
from __future__ import annotations

from app.db import repo
from app.db.database import session_scope
from app.db.models import Rule
from tests.helpers import TEST_USER_ID

OTHER_ID = 768_000_401


async def _rule(user_id: int, account_id: int, *, enabled=True, archived=False,
                kind="forward", count=0):
    async with session_scope() as session:
        rule = Rule(
            user_id=user_id, account_id=account_id, source_id=-100, target_id=-200,
            enabled=enabled, archived=archived, kind=kind, forwarded_count=count,
        )
        session.add(rule)
        await session.flush()
        rule_id = rule.id
        await session.commit()
    return rule_id


async def _states(user_id: int):
    async with session_scope() as session:
        rules = await repo.list_rules(session, user_id, include_archived=True)
        return [(rule.enabled, rule.archived) for rule in rules]


async def test_pause_all_hits_only_active(client, auth_headers, create_user,
                                          create_account):
    """Пауза всем: активные встали, остальные и чужие — как были."""
    await create_user(id=TEST_USER_ID)
    await create_user(id=OTHER_ID)
    mine = await create_account(TEST_USER_ID)
    alien = await create_account(OTHER_ID)
    await _rule(TEST_USER_ID, mine, enabled=True)
    await _rule(TEST_USER_ID, mine, enabled=True)
    await _rule(TEST_USER_ID, mine, enabled=False)
    await _rule(TEST_USER_ID, mine, enabled=False, archived=True)
    await _rule(OTHER_ID, alien, enabled=True)

    response = await client.post(
        "/api/tasks/bulk", json={"action": "pause_all"}, headers=auth_headers
    )
    assert response.status == 200
    assert (await response.json())["affected"] == 2
    assert await _states(TEST_USER_ID) == [
        (False, False), (False, False), (False, False), (False, True),
    ]
    assert await _states(OTHER_ID) == [(True, False)]


async def test_resume_all_restarts_finished_mailing(client, auth_headers, create_user,
                                                   create_account):
    """Запуск всех: стоящие пошли, исчерпанная рассылка начала круги заново."""
    await create_user(id=TEST_USER_ID)
    account_id = await create_account(TEST_USER_ID)
    await _rule(TEST_USER_ID, account_id, enabled=False)
    # Рассылка в 1 чат на 1 круг: счётчик 1 — круги исчерпаны.
    async with session_scope() as session:
        rule = Rule(
            user_id=TEST_USER_ID, account_id=account_id, source_id=-100,
            target_id=-200, enabled=False, kind="mailing",
            filters={"targets": [-200], "repeats": 1}, forwarded_count=1,
        )
        session.add(rule)
        await session.commit()

    response = await client.post(
        "/api/tasks/bulk", json={"action": "resume_all"}, headers=auth_headers
    )
    assert (await response.json())["affected"] == 2
    async with session_scope() as session:
        rules = await repo.list_rules(session, TEST_USER_ID, include_archived=True)
        assert [rule.enabled for rule in rules] == [True, True]
        assert [rule.forwarded_count for rule in rules] == [0, 0]


async def test_archive_all_keeps_active(client, auth_headers, create_user,
                                        create_account):
    """Архив всех: стоящие убраны, активные работают дальше."""
    await create_user(id=TEST_USER_ID)
    account_id = await create_account(TEST_USER_ID)
    await _rule(TEST_USER_ID, account_id, enabled=True)
    await _rule(TEST_USER_ID, account_id, enabled=False)
    await _rule(TEST_USER_ID, account_id, enabled=False)

    response = await client.post(
        "/api/tasks/bulk", json={"action": "archive_all"}, headers=auth_headers
    )
    assert (await response.json())["affected"] == 2
    assert await _states(TEST_USER_ID) == [
        (True, False), (False, True), (False, True),
    ]


async def test_empty_list_is_zero_and_garbage_is_400(client, auth_headers,
                                                    create_user):
    """Пусто — ноль без ошибки, мусор — 400."""
    await create_user(id=TEST_USER_ID)
    response = await client.post(
        "/api/tasks/bulk", json={"action": "pause_all"}, headers=auth_headers
    )
    assert response.status == 200
    assert (await response.json())["affected"] == 0
    bad = await client.post(
        "/api/tasks/bulk", json={"action": "explode"}, headers=auth_headers
    )
    assert bad.status == 400
