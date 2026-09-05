"""Очередь доставки не должна терять сообщения при перезапуске процесса.

Раньше очередь жила только в памяти: деплой, падение или ``systemctl restart``
уносили всё, что стояло в ней и в отложенных задержках. Пользователь видел
«сообщение не переслалось» и никаких следов в журнале. Теперь задача пишется в
``pending_deliveries`` до постановки в очередь и удаляется, только когда доведена
до конца — успешно или окончательной ошибкой.

Семантика намеренно «хотя бы один раз»: если процесс умрёт между отправкой и
удалением строки, сообщение уйдёт дважды. Дубликат виден и его можно удалить,
потерянное сообщение — нет.

Второй урок — из перезагрузки боевого сервера. Проход восстановления был один,
на старте: аккаунт, который подключился минутой позже, оставался без своих
отправок — их строки к тому времени уже удалили. И ни одна из потерь не
попадала в журнал задачи, поэтому карточка в кабинете продолжала показывать
«работает», пока человек ждал пост, который уже не придёт. Отсюда два правила
этого файла: строка ждёт возвращения аккаунта, а каждая настоящая потеря
называет причину в том самом журнале, который читает карточка.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.db.models import PendingDelivery
from app.telegram_client import manager as manager_module
from app.telegram_client.manager import delivery_queue, manager
from app.telegram_client.queue import RESTORE_ATTEMPTS_LIMIT, DeliveryQueue
from app.timeutil import utcnow

from tests.test_delivery_queue import make_queue, make_rule, wait_until

SOURCE_CHAT = -1001234567890


class FakeMessage:
    def __init__(self, message_id: int = 4242) -> None:
        self.id = message_id


class FakeClient:
    """Клиент, который умеет ровно одно: перечитать сообщение из источника.

    ``error`` — источник, который сегодня не читается: закрытый канал, забранные
    права, оборванная сеть. Для восстановления это отдельный исход: досылать
    нечего, но и молчать нельзя.
    """

    def __init__(
        self, message: object | None, error: BaseException | None = None
    ) -> None:
        self._message = message
        self._error = error
        self.requests: list[tuple[int, int]] = []

    async def get_messages(self, chat_id: int, ids: int):
        self.requests.append((chat_id, ids))
        if self._error is not None:
            raise self._error
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


async def remember(
    rule_id: int, user_id: int, account_id: int, message_id: int, **kwargs
) -> int:
    """Строка «отправка записана, но не доведена до конца» — как её пишет форвардер."""
    async with session_scope() as session:
        return await repo.remember_pending_delivery(
            session,
            rule_id=rule_id,
            user_id=user_id,
            account_id=account_id,
            source_chat_id=SOURCE_CHAT,
            message_id=message_id,
            **kwargs,
        )


async def task_error(rule_id: int) -> str | None:
    """Что скажет карточка задачи в кабинете: она читает этот же журнал.

    ``None`` — карточка бодра: либо в журнале ничего нет, либо после сбоя была
    удачная отправка. Именно этот ответ на потерянной отправке и был бедой:
    сообщение не доехало, а задача выглядела работающей.
    """
    async with SessionLocal() as session:
        health = await repo.task_health(session, [rule_id])
    entry = health.get(rule_id)
    if entry is None or not entry["failing"]:
        return None
    return entry["error"]


async def add_stale_row(
    rule_id: int, user_id: int, account_id: int, message_id: int
) -> None:
    """Отправка, записанная больше суток назад: досылать её уже поздно."""
    long_ago = utcnow() - timedelta(days=3)
    async with session_scope() as session:
        session.add(
            PendingDelivery(
                rule_id=rule_id,
                user_id=user_id,
                account_id=account_id,
                source_chat_id=SOURCE_CHAT,
                message_id=message_id,
                due_at=long_ago,
                created_at=long_ago,
            )
        )


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

    first = await remember(rule_id, user_id, account_id, 777)
    second = await remember(rule_id, user_id, account_id, 777)

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
    """Отброшенное при переполнении восстанавливать незачем — но сказать об этом надо.

    Решение осознанное: очередь забита, и досылать это после перезапуска поздно.
    А вот молчать нельзя — сообщение не доехало, и в журнале задачи должна быть
    строка, иначе карточка так и покажет «работает».
    """
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
    assert "переполнена" in (await task_error(rule_id) or ""), "потеря без объяснения"


# ────────────────────────── Восстановление после старта ───────────────────────


async def test_restore_pending_resends_after_restart(create_user, create_account):
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    await remember(rule_id, user_id, account_id, 555)

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
    assert await task_error(rule_id) is None, "успех не должен выглядеть сбоем"


async def test_a_task_out_of_work_explains_the_loss(create_user, create_account):
    """Задача на паузе или в архиве: досылать нечего, но в журнале должна быть строка.

    Правило само по себе живо, человек его просто выключил — и тем самым отменил
    отправку, о которой уже не помнит. Карточка обязана сказать, что пост не
    ушёл, иначе выключение выглядит бесследным.
    """
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)

    await remember(rule_id, user_id, account_id, 556)

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
    assert "задача не в работе" in (await task_error(rule_id) or "")


async def test_a_deleted_task_leaves_no_journal_line(create_user, create_account):
    """Задачи больше нет — и строки в её журнале появиться не должно.

    SQLite отдаёт номер удалённой задачи следующей созданной. Строка «отправка
    отменена», написанная в журнал несуществующей задачи, досталась бы новой
    вместе с номером: красный «сбой» на карточке, которая ещё ничего не делала.
    """
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    orphan_rule_id = rule_id + 10_000

    await remember(orphan_rule_id, user_id, account_id, 559)

    async def handler(client, message, rule_) -> bool:
        return True

    queue = make_queue(handler, maxsize=10)
    restored = await queue.restore_pending(
        lambda _acc: FakeClient(FakeMessage(559)), lambda _rid: None
    )

    assert restored == 0
    assert await count_rows() == 0
    assert await task_error(orphan_rule_id) is None, "журнал удалённой задачи"


async def test_restore_drops_row_when_message_deleted(create_user, create_account):
    """Пост удалён в источнике — досылать нечего, запись должна уйти."""
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    await remember(rule_id, user_id, account_id, 557)

    async def handler(client, message, rule_) -> bool:
        return True

    queue = make_queue(handler, maxsize=10)
    restored = await queue.restore_pending(
        lambda _acc: FakeClient(None), lambda _rid: rule
    )

    assert restored == 0
    assert await count_rows() == 0
    assert "удалено в источнике" in (await task_error(rule_id) or "")


async def test_an_unreadable_source_explains_the_loss(create_user, create_account):
    """Источник не читается — в журнале причина, а не пустота.

    Так выглядит забранный доступ к каналу: пост, скорее всего, на месте, но
    перечитать его нечем. Тип ошибки в тексте — единственная зацепка для
    разбора, поэтому он в строке остаётся.
    """
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    await remember(rule_id, user_id, account_id, 560)

    async def handler(client, message, rule_) -> bool:
        return True

    queue = make_queue(handler, maxsize=10)
    restored = await queue.restore_pending(
        lambda _acc: FakeClient(None, error=PermissionError("нет доступа к чату")),
        lambda _rid: rule,
    )

    assert restored == 0
    assert await count_rows() == 0
    reason = await task_error(rule_id) or ""
    assert "не перечитали сообщение" in reason
    assert "PermissionError" in reason, f"по такой причине не разобраться: {reason}"


async def test_stale_rows_are_dropped(create_user, create_account):
    """Сутки спустя пересылать поздно: «вчерашнее» хуже, чем ничего."""
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    await add_stale_row(rule_id, user_id, account_id, 558)

    async with SessionLocal() as session:
        assert list(await repo.due_pending_deliveries(session)) == []

    async with session_scope() as session:
        dropped = list(await repo.drop_stale_pending_deliveries(session))
    assert await count_rows() == 0
    # Отдаём сами строки, а не их число: у каждой есть хозяин и задача, и о
    # потерянной отправке надо сказать в журнале задачи. По счётчику этого не
    # сделать — раньше здесь и стоял счётчик, а сообщения уходили в тишину.
    assert [(row.rule_id, row.user_id, row.message_id) for row in dropped] == [
        (rule_id, user_id, 558)
    ]


# ─────────────────────── Аккаунт, вернувшийся позже ───────────────────────────


async def test_a_late_account_keeps_the_row_waiting(create_user, create_account):
    """Аккаунт ещё не на связи — отправка ждёт его, а не выбрасывается.

    Боевой случай: сервер перезагрузился, и часть аккаунтов вышла на связь
    минутой-двумя позже единственного прохода восстановления. Проход не находил
    клиента, удалял строку — и сообщение не доезжало уже никогда.
    """
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    await remember(rule_id, user_id, account_id, 601)

    async def handler(client, message, rule_) -> bool:
        raise AssertionError("отправлять пока нечем")

    queue = make_queue(handler, maxsize=10)
    restored = await queue.restore_pending(lambda _acc: None, lambda _rid: rule)

    assert restored == 0
    assert await count_rows() == 1, "строку выбросили, не дождавшись аккаунта"
    assert queue.stats()["deferred"] == 1
    assert await task_error(rule_id) is None, "пока ждём — это ещё не потеря"


async def test_the_delivery_goes_out_when_the_account_returns(
    create_user, create_account
):
    """Аккаунт вернулся — следующий проход досылает то, что его ждало."""
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    await remember(rule_id, user_id, account_id, 602)

    delivered: list[object] = []

    async def handler(client, message, rule_) -> bool:
        delivered.append(message)
        return True

    queue = make_queue(handler, maxsize=10)
    await queue.start()

    assert await queue.restore_pending(lambda _acc: None, lambda _rid: rule) == 0
    client = FakeClient(FakeMessage(602))
    assert await queue.restore_pending(lambda _acc: client, lambda _rid: rule) == 1

    assert await wait_until(lambda: len(delivered) == 1)
    await queue.stop()

    assert client.requests == [(SOURCE_CHAT, 602)]
    assert await count_rows() == 0
    assert await task_error(rule_id) is None, "дошло — значит не сбой"


async def test_two_passes_do_not_send_the_same_post_twice(create_user, create_account):
    """Проход идёт по кругу, а взятое в работу обязан пропускать.

    Строка живёт до конца отправки, поэтому следующий проход видит её снова.
    Без признака «уже в работе» получатель получил бы один и тот же пост дважды
    — и это была бы плата за терпение к опоздавшим аккаунтам.
    """
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    await remember(rule_id, user_id, account_id, 603)

    release = asyncio.Event()
    calls: list[object] = []

    async def handler(client, message, rule_) -> bool:
        calls.append(message)
        await release.wait()
        return True

    queue = make_queue(handler, maxsize=10)
    await queue.start()
    client = FakeClient(FakeMessage(603))

    assert await queue.restore_pending(lambda _acc: client, lambda _rid: rule) == 1
    assert await wait_until(lambda: len(calls) == 1)
    # Отправка ещё идёт, строка на месте — второй проход обязан её узнать.
    assert await count_rows() == 1
    assert await queue.restore_pending(lambda _acc: client, lambda _rid: rule) == 0

    release.set()
    await queue.stop()

    assert len(calls) == 1, "пост ушёл дважды"
    assert client.requests == [(SOURCE_CHAT, 603)]
    assert await count_rows() == 0


async def test_an_account_that_never_returns_explains_itself(
    create_user, create_account
):
    """Ждём не бесконечно: после пяти проходов отправка отменяется с причиной.

    Пять проходов круга подъёма — это около четверти часа терпения. Дольше
    держать строку незачем: суточный предел всё равно её уберёт, а человеку
    важнее вовремя узнать, что пост не ушёл, чем ждать его до утра.
    """
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    rule = make_rule(rule_id, user_id=user_id, account_id=account_id)

    await remember(rule_id, user_id, account_id, 605)

    async def handler(client, message, rule_) -> bool:
        raise AssertionError("отправлять нечем")

    queue = make_queue(handler, maxsize=10)
    for _ in range(RESTORE_ATTEMPTS_LIMIT - 1):
        assert await queue.restore_pending(lambda _acc: None, lambda _rid: rule) == 0
    assert await count_rows() == 1, "сдались раньше времени"
    assert await task_error(rule_id) is None

    assert await queue.restore_pending(lambda _acc: None, lambda _rid: rule) == 0

    assert await count_rows() == 0
    assert "не вышел на связь" in (await task_error(rule_id) or "")


# ────────────────────────── Кто зовёт восстановление ──────────────────────────


async def test_the_service_explains_a_delivery_it_gave_up_on(
    create_user, create_account, monkeypatch
):
    """Сутки без связи — отправка отменена, и в журнале задачи это сказано.

    Строку убирает БД, а объяснить потерю может только тот, кто её убрал.
    Поэтому уборка отдаёт сами строки: по счётчику написать причину некуда, и
    раньше суточный простой уносил сообщения молча.
    """
    rule_id, user_id, account_id = await make_rule_row(create_user, create_account)
    await add_stale_row(rule_id, user_id, account_id, 604)

    calls: list[int] = []

    async def fake_restore(client_for, rule_for) -> int:
        calls.append(1)
        return 0

    monkeypatch.setattr(delivery_queue, "restore_pending", fake_restore)

    await manager._restore_deliveries()

    assert await count_rows() == 0
    assert "просрочена" in (await task_error(rule_id) or "")
    assert calls == [1], "восстановление так и не запустили"


async def test_a_returning_account_starts_a_new_restore(mtproto_on, monkeypatch):
    """Аккаунт вернулся в работу — круг подъёма тут же зовёт восстановление.

    Это и есть недостававшее звено. Проход был один, на старте: отправки
    аккаунта, поднявшегося позже, ждали в базе до суточного предела, и никто
    больше не пытался их досылать.
    """
    monkeypatch.setattr(manager_module, "REVIVE_INTERVAL", 0.02)
    restores: list[int] = []

    async def revived() -> list[int]:
        return [7]

    async def fake_restore() -> None:
        restores.append(1)

    monkeypatch.setattr(manager, "_start_pending_accounts", revived)
    monkeypatch.setattr(manager, "_restore_deliveries", fake_restore)

    task = asyncio.create_task(manager._revive_loop())
    try:
        assert await wait_until(lambda: restores != []), "отправки остались ждать"
    finally:
        task.cancel()
