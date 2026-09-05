"""Удалённая задача не должна оставлять следов.

Три таблицы ссылаются на задачу номером и без внешнего ключа: журнал
пересылок, находки («Результаты») и очередь недосланных сообщений. Удаление
задачи их не трогало, и это был не просто мусор: SQLite выдаёт номера по
правилу «наибольший плюс один», без ``AUTOINCREMENT``. Номер удалённой задачи
достаётся следующей созданной — вместе с её историей. Новая задача рождалась с
красным «сбой» на карточке, с чужими находками в «Результатах» и, если это был
постинг, с чужой очередью чатов в памяти планировщика.

Проверяем оба конца: удаление уносит данные с собой, а фоновая уборка лечит
базы, где задачи удаляли раньше.
"""
from __future__ import annotations

import time

import pytest
from sqlalchemy import func, select

from app.db import repo
from app.db.database import session_scope
from app.db.models import CollectedItem, ForwardLog, PendingDelivery, Rule
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID


async def add_rule(user_id: int, account_id: int, kind: str = "poster") -> int:
    async with session_scope() as session:
        rule = Rule(
            user_id=user_id,
            account_id=account_id,
            source_id=0,
            target_id=-1001,
            kind=kind,
            enabled=True,
        )
        session.add(rule)
        await session.flush()
        return rule.id


async def fill_history(rule_id: int, user_id: int, account_id: int) -> None:
    """Задача поработала: сбой в журнале, находка и недосланное сообщение."""
    async with session_scope() as session:
        await repo.log_forward(
            session,
            rule_id=rule_id,
            user_id=user_id,
            source_msg_id=1,
            target_msg_id=None,
            status="error",
            error="чат закрыт",
        )
        session.add(
            CollectedItem(rule_id=rule_id, user_id=user_id, kind="parser", payload={"id": 7})
        )
        await repo.remember_pending_delivery(
            session,
            rule_id=rule_id,
            user_id=user_id,
            account_id=account_id,
            source_chat_id=-1001,
            message_id=500,
        )


async def traces(rule_id: int) -> dict[str, int]:
    """Сколько строк осталось за задачей в каждой из трёх таблиц."""
    async with session_scope() as session:
        counts = {}
        for model in (ForwardLog, CollectedItem, PendingDelivery):
            counts[model.__tablename__] = int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(model)
                        .where(model.rule_id == rule_id)
                    )
                ).scalar_one()
            )
        return counts


async def drop(rule_id: int, user_id: int) -> None:
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, user_id)
        await repo.delete_rule(session, rule)


# ─────────────────────────────── Удаление задачи ──────────────────────────────


async def test_delete_takes_journal_findings_and_queue(create_user, create_account):
    """Удалили задачу — её строки ушли из всех трёх таблиц."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule_id = await add_rule(user_id, account_id)
    await fill_history(rule_id, user_id, account_id)
    assert await traces(rule_id) == {
        "forward_logs": 1,
        "collected_items": 1,
        "pending_deliveries": 1,
    }

    await drop(rule_id, user_id)

    assert await traces(rule_id) == {
        "forward_logs": 0,
        "collected_items": 0,
        "pending_deliveries": 0,
    }


async def test_records_of_a_neighbour_task_survive(create_user, create_account):
    """Чистка касается только удаляемой задачи: у соседней всё на месте."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    doomed = await add_rule(user_id, account_id)
    neighbour = await add_rule(user_id, account_id)
    await fill_history(doomed, user_id, account_id)
    await fill_history(neighbour, user_id, account_id)

    await drop(doomed, user_id)

    assert await traces(neighbour) == {
        "forward_logs": 1,
        "collected_items": 1,
        "pending_deliveries": 1,
    }


async def test_sqlite_gives_the_number_of_a_deleted_task_to_the_next_one(
    create_user, create_account
):
    """Почему чистка обязательна: номер задачи переиспользуется.

    Тест документирует поведение базы, а не наш код. Если однажды номера
    перестанут повторяться — этот тест первым об этом скажет.
    """
    user_id = await create_user()
    account_id = await create_account(user_id)
    doomed = await add_rule(user_id, account_id)

    await drop(doomed, user_id)
    reborn = await add_rule(user_id, account_id, kind="mailing")

    assert reborn == doomed


async def test_task_with_a_recycled_number_starts_with_a_clean_card(
    create_user, create_account
):
    """Новая задача с номером удалённой — без чужого сбоя и чужих находок."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    doomed = await add_rule(user_id, account_id)
    await fill_history(doomed, user_id, account_id)

    await drop(doomed, user_id)
    reborn = await add_rule(user_id, account_id, kind="mailing")

    async with session_scope() as session:
        assert await repo.task_health(session, [reborn]) == {}
        assert await repo.count_collected_items(session, reborn) == 0
    assert await traces(reborn) == {
        "forward_logs": 0,
        "collected_items": 0,
        "pending_deliveries": 0,
    }


async def test_cabinet_delete_leaves_nothing_behind(
    client, auth_headers, create_account
):
    """То же самое через кабинет: у обеих кнопок «Удалить» одна чистка."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    rule_id = await add_rule(TEST_USER_ID, account_id)
    await fill_history(rule_id, TEST_USER_ID, account_id)

    response = await client.delete(f"/api/tasks/{rule_id}", headers=auth_headers)

    assert response.status == 200, await response.text()
    assert await traces(rule_id) == {
        "forward_logs": 0,
        "collected_items": 0,
        "pending_deliveries": 0,
    }


# ────────────────────────────── Уборка сирот ──────────────────────────────────


async def test_sweep_removes_records_of_vanished_tasks(create_user, create_account):
    """База после старого удаления: строки есть, задачи нет — их убирает проход."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    alive = await add_rule(user_id, account_id)
    doomed = await add_rule(user_id, account_id)
    await fill_history(alive, user_id, account_id)
    await fill_history(doomed, user_id, account_id)
    # Удаляем задачу так, как это делала прежняя версия: строки остаются.
    async with session_scope() as session:
        rule = await repo.get_rule(session, doomed, user_id)
        await session.delete(rule)

    async with session_scope() as session:
        dropped = await repo.drop_orphan_records(session)

    assert dropped == {"forward_logs": 1, "collected_items": 1, "pending_deliveries": 1}
    assert await traces(doomed) == {
        "forward_logs": 0,
        "collected_items": 0,
        "pending_deliveries": 0,
    }
    assert await traces(alive) == {
        "forward_logs": 1,
        "collected_items": 1,
        "pending_deliveries": 1,
    }


async def test_sweep_on_a_clean_base_says_nothing(create_user, create_account):
    """Проход зовётся каждые пять минут: на чистой базе он молчит."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule_id = await add_rule(user_id, account_id)
    await fill_history(rule_id, user_id, account_id)

    async with session_scope() as session:
        assert await repo.drop_orphan_records(session) == {}


async def test_sweep_reports_only_tables_it_touched(create_user, create_account):
    """В ответе — только то, что нашлось: строка в логе службы должна быть о деле."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    doomed = await add_rule(user_id, account_id)
    async with session_scope() as session:
        await repo.log_forward(
            session,
            rule_id=doomed,
            user_id=user_id,
            source_msg_id=1,
            target_msg_id=None,
            status="ok",
        )
        rule = await repo.get_rule(session, doomed, user_id)
        await session.delete(rule)

    async with session_scope() as session:
        assert await repo.drop_orphan_records(session) == {"forward_logs": 1}


# ──────────────────────── Планировщик: состояние в памяти ─────────────────────


@pytest.fixture(autouse=True)
def clean_scheduler_state():
    """Планировщик — синглтон: состояние из соседних тестов здесь только мешает."""
    manager._poster_state.clear()
    manager._mailing_state.clear()
    yield
    manager._poster_state.clear()
    manager._mailing_state.clear()


async def test_scheduler_forgets_a_deleted_task(create_user, create_account):
    """Очередь чатов и пауза после FloodWait не должны достаться новой задаче.

    Круг постинга держится в памяти: пока очередь не пуста, задача досылает её
    как есть. Задача с номером удалённой унаследовала бы чужие чаты и чужой
    текст в первом же проходе, а из ``not_before`` — паузу, которую Telegram
    назначил кому-то другому.
    """
    user_id = await create_user()
    account_id = await create_account(user_id)
    poster = await add_rule(user_id, account_id, kind="poster")
    mailing = await add_rule(user_id, account_id, kind="mailing")
    manager._poster_state[poster] = {
        "last": time.time(),
        "idx": 1,
        "runs": 3,
        "not_before": time.time() + 600,
        "queue": [-1001, -1002],
        "msg": "текст удалённой задачи",
    }
    manager._mailing_state[mailing] = {"pos": 5, "not_before": 0.0}

    await manager.refresh_rules()
    assert poster in manager._poster_state, "живая задача своё место в круге не теряет"

    await drop(poster, user_id)
    await drop(mailing, user_id)
    await manager.refresh_rules()

    assert manager._poster_state == {}
    assert manager._mailing_state == {}
