"""Очередь доставки не должна терять сообщения при перезапуске процесса.

Раньше очередь жила только в памяти: деплой, падение или ``systemctl restart``
уносили всё, что стояло в ней и в отложенных задержках. Пользователь видел
«сообщение не переслалось» и никаких следов в журнале. Теперь задача пишется в
``pending_deliveries`` до постановки в очередь и удаляется, только когда доведена
до конца — успешно или окончательной ошибкой.

Семантика намеренно «хотя бы один раз»: если процесс умрёт между отправкой и
удалением строки, сообщение уйдёт дважды. Дубликат виден и его можно удалить,
потерянное сообщение — нет.
"""
from __future__ import annotations

import asyncio

from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.telegram_client.queue import DeliveryQueue

from tests.test_delivery_queue import make_queue, make_rule, wait_until

SOURCE_CHAT = -1001234567890


class FakeMessage:
    def __init__(self, message_id: int = 4242) -> None:
        self.id = message_id


class FakeClient:
    """Клиент, который умеет ровно одно: перечитать сообщение из источника."""

    def __init__(self, message: object | None) -> None:
        self._message = message
        self.requests: list[tuple[int, int]] = []

    async def get_messages(self, chat_id: int, ids: int):
        self.requests.append((chat_id, ids))
        return self._message


async def count_rows() -> int:
    async with SessionLocal() as session:
        return await repo.count_pending_deliveries(session)


async def make_rule_row(create_user, create_account):
    """Правило в БД: восстановление ищет его по id, а FK требует пользователя."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        rule = await repo.add_rule(
            session,
            user_id=user_id,
            account_id=account_id,
            source_id=SOURCE_CHAT,
            source_title="Источник",
            target_id=-200,
            target_title="Приёмник",
        )
        return rule.id, user_id, account_id


# ────────────────────────────── Запись и очистка ──────────────────────────────


async def test_persistent_submit_records_and_clears(create_user, create_account):
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    release = asyncio.Event()
    seen_rows: list[int] = []

    async def handler(client, message, rule_) -> bool:
        # Пока обработчик работает, задача считается незавершённой — строка
        # обязана быть в базе, иначе перезапуск в этот момент её потеряет.
        seen_rows.append(await count_rows())
        await release.wait()
        return True

    queue = make_queue(handler, maxsize=10)
    await queue.start()

    assert await queue.submit_persistent(
        None, FakeMessage(), rule, source_chat_id=SOURCE_CHAT
    )
    assert await wait_until(lambda: seen_rows == [1])
    release.set()
    await queue.stop()

    assert queue.stats()["sent"] == 1
    assert await count_rows() == 0, "доведённую задачу восстанавливать нечего"


async def test_same_message_is_recorded_once(create_user, create_account):
    """Повторная постановка того же поста не должна плодить строки."""
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)

    async with session_scope() as session:
        first = await repo.remember_pending_delivery(
            session,
            rule_id=rule_id,
            user_id=user_id,
            account_id=account_id,
            source_chat_id=SOURCE_CHAT,
            message_id=777,
        )
    async with session_scope() as session:
        second = await repo.remember_pending_delivery(
            session,
            rule_id=rule_id,
            user_id=user_id,
            account_id=account_id,
            source_chat_id=SOURCE_CHAT,
            message_id=777,
        )

    assert first == second
    assert await count_rows() == 1


async def test_failed_delivery_forgets_the_row(create_user, create_account):
    """Окончательная ошибка — не повод досылать её после каждого перезапуска."""
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    errors: list[BaseException] = []

    async def handler(client, message, rule_) -> bool:
        raise RuntimeError("приёмник удалён")

    async def on_error(client, message, rule_, error) -> None:
        errors.append(error)

    queue = make_queue(handler, on_error=on_error, maxsize=10)
    await queue.start()
    await queue.submit_persistent(None, FakeMessage(), rule, source_chat_id=SOURCE_CHAT)
    assert await wait_until(lambda: len(errors) == 1)
    await queue.stop()

    assert await count_rows() == 0


async def test_overflow_removes_the_row(create_user, create_account):
    """Отброшенное при переполнении восстанавливать незачем: это решение сервиса."""
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    async def handler(client, message, rule_) -> bool:
        return True

    queue = DeliveryQueue(handler, workers=1, maxsize=1, min_interval=0)
    # Воркеры не запущены — очередь гарантированно переполнится на втором.
    assert await queue.submit_persistent(
        None, FakeMessage(1), rule, source_chat_id=SOURCE_CHAT
    )
    assert not await queue.submit_persistent(
        None, FakeMessage(2), rule, source_chat_id=SOURCE_CHAT
    )

    assert queue.stats()["dropped"] == 1
    assert await count_rows() == 1


# ────────────────────────── Восстановление после старта ───────────────────────


async def test_restore_pending_resends_after_restart(create_user, create_account):
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    async with session_scope() as session:
        await repo.remember_pending_delivery(
            session,
            rule_id=rule_id,
            user_id=user_id,
            account_id=account_id,
            source_chat_id=SOURCE_CHAT,
            message_id=555,
        )

    delivered: list[object] = []

    async def handler(client, message, rule_) -> bool:
        delivered.append(message)
        return True

    message = FakeMessage(555)
    client = FakeClient(message)
    queue = make_queue(handler, maxsize=10)
    await queue.start()

    restored = await queue.restore_pending(lambda _acc: client, lambda _rid: rule)
    assert restored == 1
    assert await wait_until(lambda: delivered == [message])
    await queue.stop()

    # Сообщение перечитано из источника, а не хранилось у нас
    assert client.requests == [(SOURCE_CHAT, 555)]
    assert await count_rows() == 0


async def test_restore_drops_row_when_rule_disappeared(create_user, create_account):
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)

    async with session_scope() as session:
        await repo.remember_pending_delivery(
            session,
            rule_id=rule_id,
            user_id=user_id,
            account_id=account_id,
            source_chat_id=SOURCE_CHAT,
            message_id=556,
        )

    called: list[int] = []

    async def handler(client, message, rule_) -> bool:
        called.append(1)
        return True

    queue = make_queue(handler, maxsize=10)
    restored = await queue.restore_pending(
        lambda _acc: FakeClient(FakeMessage(556)), lambda _rid: None
    )

    assert restored == 0
    assert called == []
    assert await count_rows() == 0


async def test_restore_drops_row_when_message_deleted(create_user, create_account):
    """Пост удалён в источнике — досылать нечего, запись должна уйти."""
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    async with session_scope() as session:
        await repo.remember_pending_delivery(
            session,
            rule_id=rule_id,
            user_id=user_id,
            account_id=account_id,
            source_chat_id=SOURCE_CHAT,
            message_id=557,
        )

    async def handler(client, message, rule_) -> bool:
        return True

    queue = make_queue(handler, maxsize=10)
    restored = await queue.restore_pending(
        lambda _acc: FakeClient(None), lambda _rid: rule
    )

    assert restored == 0
    assert await count_rows() == 0


async def test_stale_rows_are_dropped(create_user, create_account):
    """Сутки спустя пересылать поздно: «вчерашнее» хуже, чем ничего."""
    from datetime import timedelta

    from app.db.models import PendingDelivery
    from app.timeutil import utcnow

    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)

    async with session_scope() as session:
        old = PendingDelivery(
            rule_id=rule_id,
            user_id=user_id,
            account_id=account_id,
            source_chat_id=SOURCE_CHAT,
            message_id=558,
            due_at=utcnow() - timedelta(days=3),
            created_at=utcnow() - timedelta(days=3),
        )
        session.add(old)

    async with SessionLocal() as session:
        assert list(await repo.due_pending_deliveries(session)) == []

    async with session_scope() as session:
        assert await repo.drop_stale_pending_deliveries(session) == 1
    assert await count_rows() == 0
