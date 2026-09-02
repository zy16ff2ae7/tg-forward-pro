"""Очередь доставки: темп, параллелизм, повторы, отбрасывание при переполнении."""
from __future__ import annotations

import asyncio
import time

from telethon.errors import FloodWaitError, RPCError

from app.telegram_client.queue import DeliveryQueue
from app.telegram_client.types import RuleSnapshot


def make_rule(rule_id: int = 1, delay_seconds: int = 0) -> RuleSnapshot:
    return RuleSnapshot(
        id=rule_id,
        user_id=100,
        target_id=200,
        mode="copy",
        delay_seconds=delay_seconds,
    )


def make_queue(handler, **kwargs) -> DeliveryQueue:
    """Очередь с «быстрыми» настройками: без пауз, чтобы тесты не спали."""
    params = {
        "workers": 1,
        "maxsize": 10,
        "concurrency": 1,
        "min_interval": 0,
        "retry_attempts": 0,
        "retry_base": 0.01,
        "flood_wait_max": 60,
    }
    params.update(kwargs)
    return DeliveryQueue(handler, **params)


async def wait_until(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


class Recorder:
    """Считает вызовы обработчика и собирает ошибки, отданные в on_error."""

    def __init__(self, failures: int = 0, error: BaseException | None = None) -> None:
        self.calls = 0
        self.errors: list[BaseException] = []
        self._failures_left = failures
        self._error = error

    async def handler(self, client, message, rule) -> bool:
        self.calls += 1
        if self._failures_left > 0:
            self._failures_left -= 1
            raise self._error
        return True

    async def on_error(self, client, message, rule, error) -> None:
        self.errors.append(error)


# ─────────────────────────────── Успех и пропуск ──────────────────────────────


async def test_sent_message_is_counted():
    rec = Recorder()
    queue = make_queue(rec.handler)
    await queue.start()

    assert queue.submit(None, None, make_rule()) is True
    assert await wait_until(lambda: queue.stats()["sent"] == 1)
    await queue.stop()

    assert rec.calls == 1
    assert queue.stats()["failed"] == 0
    assert rec.errors == []


async def test_skipped_is_not_an_error():
    """False от обработчика — «пропустили», а не «сломались»."""

    async def handler(client, message, rule) -> bool:
        return False

    errors: list[BaseException] = []

    async def on_error(client, message, rule, error) -> None:
        errors.append(error)

    queue = make_queue(handler, on_error=on_error)
    await queue.start()
    queue.submit(None, None, make_rule())
    await queue.stop()

    assert queue.stats()["sent"] == 0
    assert errors == []


async def test_stop_waits_for_queued_messages():
    rec = Recorder()
    queue = make_queue(rec.handler, maxsize=50)
    await queue.start()

    for index in range(5):
        queue.submit(None, None, make_rule(index))
    await queue.stop()

    assert rec.calls == 5
    assert queue.stats()["sent"] == 5


# ──────────────────────────────────── Повторы ─────────────────────────────────


async def test_rpc_error_is_retried_up_to_limit():
    rec = Recorder(failures=99, error=RPCError(None, "boom", 400))
    queue = make_queue(rec.handler, on_error=rec.on_error, retry_attempts=2)
    await queue.start()

    queue.submit(None, None, make_rule())
    assert await wait_until(lambda: len(rec.errors) == 1)
    await queue.stop()

    # 1 попытка + 2 повтора
    assert rec.calls == 3
    assert queue.stats()["failed"] == 1
    assert isinstance(rec.errors[0], RPCError)


async def test_retry_succeeds_on_second_attempt():
    rec = Recorder(failures=1, error=RPCError(None, "boom", 400))
    queue = make_queue(rec.handler, on_error=rec.on_error, retry_attempts=2)
    await queue.start()

    queue.submit(None, None, make_rule())
    assert await wait_until(lambda: queue.stats()["sent"] == 1)
    await queue.stop()

    assert rec.calls == 2
    assert rec.errors == []
    assert queue.stats()["failed"] == 0


async def test_non_telegram_error_is_not_retried():
    """Сбой базы или опечатку в коде повторять бессмысленно."""
    rec = Recorder(failures=99, error=RuntimeError("диск отвалился"))
    queue = make_queue(rec.handler, on_error=rec.on_error, retry_attempts=3)
    await queue.start()

    queue.submit(None, None, make_rule())
    assert await wait_until(lambda: len(rec.errors) == 1)
    await queue.stop()

    assert rec.calls == 1
    assert queue.stats()["failed"] == 1


async def test_flood_wait_is_waited_out():
    """Telegram просит подождать — ждём столько, сколько сказали, и повторяем."""
    rec = Recorder(failures=1, error=FloodWaitError(None, capture=1))
    queue = make_queue(rec.handler, on_error=rec.on_error, retry_attempts=1)
    await queue.start()

    started = time.monotonic()
    queue.submit(None, None, make_rule())
    assert await wait_until(lambda: queue.stats()["sent"] == 1)
    elapsed = time.monotonic() - started

    await queue.stop()

    assert rec.calls == 2
    # seconds=1, плюс секунда запаса — значит ждали не меньше секунды
    assert elapsed >= 1.0


async def test_flood_wait_over_limit_is_not_waited():
    """Ждать 100 секунд не будем: сдаёмся и пишем в журнал."""
    rec = Recorder(failures=99, error=FloodWaitError(None, capture=100))
    queue = make_queue(rec.handler, on_error=rec.on_error, retry_attempts=2, flood_wait_max=5)
    await queue.start()

    queue.submit(None, None, make_rule())
    assert await wait_until(lambda: len(rec.errors) == 1)
    await queue.stop()

    assert rec.calls == 1
    assert queue.stats()["failed"] == 1


# ──────────────────────────── Темп и параллелизм ──────────────────────────────


async def test_concurrency_is_capped():
    state = {"current": 0, "max": 0}

    async def handler(client, message, rule) -> bool:
        state["current"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.03)
        state["current"] -= 1
        return True

    queue = make_queue(handler, workers=4, concurrency=2, maxsize=50)
    await queue.start()
    for index in range(6):
        queue.submit(None, None, make_rule(index))
    await queue.stop()

    assert queue.stats()["sent"] == 6
    assert state["max"] == 2


async def test_min_interval_spaces_out_sends():
    async def handler(client, message, rule) -> bool:
        return True

    queue = make_queue(handler, workers=1, concurrency=1, min_interval=0.05, maxsize=50)
    await queue.start()

    started = time.monotonic()
    for index in range(3):
        queue.submit(None, None, make_rule(index))
    assert await wait_until(lambda: queue.stats()["sent"] == 3)
    elapsed = time.monotonic() - started
    await queue.stop()

    # Три отправки — два интервала между ними
    assert elapsed >= 0.1


# ─────────────────────────────── Связь с manager ──────────────────────────────


async def test_manager_starts_and_stops_queue():
    """Очередь живёт в жизненном цикле менеджера, а не сама по себе.

    Порядок важен на остановке: сначала дожимаем очередь, потом рвём
    соединения Telethon — иначе недоотправленное падает с ошибкой сети.
    """
    from app.telegram_client.manager import delivery_queue, manager

    await manager.start_all()
    assert delivery_queue.stats()["workers"] > 0

    async def handler(client, message, rule) -> bool:
        return True

    # Подменяем обработчик: настоящий требует живой Telethon-клиент
    delivery_queue._handler = handler
    delivery_queue.submit(None, None, make_rule())
    assert await wait_until(lambda: delivery_queue.stats()["sent"] >= 1)

    await manager.stop_all()
    assert delivery_queue.stats()["workers"] == 0


# ──────────────────────── Переполнение и отложенные задачи ────────────────────


async def test_overflow_drops_instead_of_growing():
    rec = Recorder()
    queue = make_queue(rec.handler, maxsize=2)
    # Три submit идут подряд без await, поэтому воркер между ними не запустится
    # и очередь гарантированно заполнится.

    assert queue.submit(None, None, make_rule(1)) is True
    assert queue.submit(None, None, make_rule(2)) is True
    assert queue.submit(None, None, make_rule(3)) is False

    assert queue.stats()["dropped"] == 1
    assert queue.stats()["submitted"] == 2
    await queue.stop(drain=False, timeout=0.1)


async def test_delayed_rule_does_not_block_worker():
    """Задержка в час не должна занимать воркер: быстрые сообщения идут мимо."""
    sent: list[int] = []

    async def handler(client, message, rule) -> bool:
        sent.append(rule.id)
        return True

    queue = make_queue(handler, workers=1, maxsize=50)
    await queue.start()

    queue.submit(None, None, make_rule(rule_id=1, delay_seconds=60))
    assert queue.stats()["queued"] == 0
    assert queue.stats()["pending_delays"] == 1

    queue.submit(None, None, make_rule(rule_id=2))
    assert await wait_until(lambda: sent == [2])
    await queue.stop(drain=False, timeout=0.1)

    # Отложенная задача отменена вместе с остановкой, а не отправлена
    assert sent == [2]
    assert queue.stats()["pending_delays"] == 0


async def test_delayed_rule_is_delivered_after_delay():
    sent: list[int] = []

    async def handler(client, message, rule) -> bool:
        sent.append(rule.id)
        return True

    queue = make_queue(handler, workers=1, maxsize=50)
    await queue.start()
    queue.submit(None, None, make_rule(rule_id=7, delay_seconds=0))
    assert await wait_until(lambda: sent == [7])
    await queue.stop()

    assert queue.stats()["sent"] == 1
