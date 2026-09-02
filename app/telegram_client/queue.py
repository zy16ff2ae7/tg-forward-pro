"""Очередь доставки: воркеры, темп отправки, повторы при ошибках Telegram.

Раньше каждое входящее сообщение порождало отдельную задачу
(``asyncio.create_task``): если в канал залетала сотня постов разом, все сто
одновременно лезли в Telegram, скопом получали FloodWait, а при перезапуске
процесса вся неотправленное просто исчезало. Теперь отправка идёт через
ограниченную очередь:

* не больше ``SEND_GLOBAL_CONCURRENCY`` одновременных отправок;
* не чаще одной отправки в ``SEND_MIN_INTERVAL_SECONDS`` (экономим лимиты);
* при FloodWait — пауза ровно на сколько просит Telegram, затем повтор;
* при других ошибках RPC — повтор с экспоненциальной паузой;
* при всплеске сверх ``DELIVERY_QUEUE_MAXSIZE`` — отбрасывание с записью в лог,
  а не бесконечный рост памяти.

Повторяем только ошибки Telegram (FloodWait и RPCError): сбой базы или опечатку
в коде повторять бессмысленно, такая задача сразу уходит в журнал ошибок.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger
from telethon.errors import FloodWaitError, RPCError

from app.config import settings
from app.telegram_client.types import RuleSnapshot

# Обработчик получает «живой» клиент Telethon, сообщение и снимок правила.
#   вернул True  — отправлено;
#   вернул False — пропущено (фильтр, нет подписки, служебное сообщение);
#   бросил исключение — ошибка отправки, решает очередь: повторить или сдатьcя.
Handler = Callable[[Any, Any, RuleSnapshot], Awaitable[Any]]
ErrorHandler = Callable[[Any, Any, RuleSnapshot, BaseException], Awaitable[None]]


class _IntervalLimiter:
    """Держит минимальный интервал между отправками.

    Замок нужен, чтобы интервал соблюдался именно между *соседними* вызовами:
    каждый, дождавшись своей очереди, передвигает «следующий разрешённый момент»
    на ``min_interval`` вперёд. Ожидание идёт внутри замка, поэтому пауза
    гарантирована, а порядок — тот, в котором пришли задачи.
    """

    def __init__(self, min_interval: float) -> None:
        self._min = max(0.0, float(min_interval))
        self._next_at = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        if self._min <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._min


@dataclass(slots=True)
class _Job:
    client: Any
    message: Any
    rule: RuleSnapshot


class DeliveryQueue:
    """Пул воркеров, которые отправляют сообщения в заданном темпе."""

    def __init__(
        self,
        handler: Handler,
        *,
        on_error: ErrorHandler | None = None,
        workers: int = 4,
        maxsize: int = 2000,
        concurrency: int = 8,
        min_interval: float = 1.2,
        retry_attempts: int = 2,
        retry_base: float = 2.0,
        flood_wait_max: int = 900,
    ) -> None:
        self._handler = handler
        self._on_error = on_error
        self._workers_count = max(1, int(workers))
        self._maxsize = max(1, int(maxsize))
        self._semaphore = asyncio.Semaphore(max(1, int(concurrency)))
        self._limiter = _IntervalLimiter(min_interval)
        self._attempts = max(0, int(retry_attempts))
        self._retry_base = max(0.1, float(retry_base))
        self._flood_max = max(1, int(flood_wait_max))

        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=self._maxsize)
        self._tasks: list[asyncio.Task] = []
        # Задачи, спящие в отложенной отправке (задержка из правила)
        self._pending: set[asyncio.Task] = set()
        self._started = False
        self._counters = {"submitted": 0, "dropped": 0, "sent": 0, "failed": 0}

    @classmethod
    def from_settings(
        cls, handler: Handler, *, on_error: ErrorHandler | None = None
    ) -> "DeliveryQueue":
        """Собирает очередь из настроек приложения (.env)."""
        return cls(
            handler,
            on_error=on_error,
            workers=settings.delivery_workers,
            maxsize=settings.delivery_queue_maxsize,
            concurrency=settings.send_global_concurrency,
            min_interval=settings.send_min_interval_seconds,
            retry_attempts=settings.send_retry_attempts,
            retry_base=settings.send_retry_base_seconds,
            flood_wait_max=settings.flood_wait_max_seconds,
        )

    # ─────────────────────────────── Жизненный цикл ───────────────────────────

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"delivery-{index}")
            for index in range(self._workers_count)
        ]
        logger.info(
            "Очередь доставки поднята: воркеров {}, очередь до {}, темп не чаще "
            "одной отправки в {} сек",
            self._workers_count,
            self._maxsize,
            self._limiter._min,
        )

    async def stop(self, *, drain: bool = True, timeout: float = 30.0) -> None:
        """Останавливает воркеры. По умолчанию дожимает то, что уже в очереди."""
        self._started = False

        for task in list(self._pending):
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

        if drain and not self._queue.empty():
            try:
                await asyncio.wait_for(self._queue.join(), timeout=timeout)
            except TimeoutError:
                logger.warning(
                    "Очередь доставки не успела опустеть за {} сек — осталось {}",
                    timeout,
                    self._queue.qsize(),
                )

        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("Очередь доставки остановлена: {}", self.stats())

    # ───────────────────────────────── Отправка ───────────────────────────────

    def submit(self, client: Any, message: Any, rule: RuleSnapshot) -> bool:
        """Ставит задачу в очередь. False — очередь полна, задача отброшена.

        Задержка из правила (``delay_seconds``) отрабатывается до постановки
        в очередь: иначе правило с задержкой в час намертво занимало бы воркер.
        """
        job = _Job(client=client, message=message, rule=rule)
        delay = max(0, int(getattr(rule, "delay_seconds", 0) or 0))
        if delay:
            self._spawn_delayed(job, delay)
            return True
        return self._put(job)

    def _put(self, job: _Job) -> bool:
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            self._counters["dropped"] += 1
            logger.error(
                "Очередь доставки переполнена ({}): сообщение по правилу #{} отброшено",
                self._maxsize,
                job.rule.id,
            )
            return False
        self._counters["submitted"] += 1
        return True

    def _spawn_delayed(self, job: _Job, delay: int) -> None:
        async def waiter() -> None:
            await asyncio.sleep(delay)
            self._put(job)

        task = asyncio.create_task(
            waiter(), name=f"delivery-delay-{job.rule.id}"
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _worker(self, index: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                await self._run(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — воркер не должен умирать
                logger.exception(
                    "Воркер доставки #{}: необработанная ошибка: {}", index, exc
                )
            finally:
                self._queue.task_done()

    async def _run(self, job: _Job) -> None:
        """Одна задача с повторами. Темп и параллелизм — здесь."""
        delay = 0.0
        for attempt in range(self._attempts + 1):
            if delay > 0:
                # Паузу держим вне семафора: ждать FloodWait, занимая слот
                # отправки, — значит блокировать остальные правила.
                await asyncio.sleep(delay)
            delay = 0.0
            try:
                async with self._semaphore:
                    await self._limiter.acquire()
                    sent = await self._handler(job.client, job.message, job.rule)
            except FloodWaitError as exc:
                wait = int(getattr(exc, "seconds", 5)) + 1
                if wait > self._flood_max or attempt >= self._attempts:
                    if wait > self._flood_max:
                        logger.error(
                            "Правило #{}: Telegram просит подождать {} сек — это "
                            "больше лимита {} сек, сдаёмся",
                            job.rule.id,
                            wait,
                            self._flood_max,
                        )
                    await self._fail(job, exc)
                    return
                logger.warning(
                    "Правило #{}: FloodWait {} сек, повтор {}/{}",
                    job.rule.id,
                    wait,
                    attempt + 1,
                    self._attempts,
                )
                delay = float(wait)
            except RPCError as exc:
                if attempt >= self._attempts:
                    await self._fail(job, exc)
                    return
                delay = self._retry_base * (2**attempt)
                logger.warning(
                    "Правило #{}: {} — повтор {}/{} через {} сек",
                    job.rule.id,
                    type(exc).__name__,
                    attempt + 1,
                    self._attempts,
                    round(delay, 1),
                )
            except Exception as exc:  # noqa: BLE001 — не Telegram, повторять нельзя
                await self._fail(job, exc)
                return
            else:
                if sent:
                    self._counters["sent"] += 1
                return

        # Сюда не попадаем: в последней попытке либо return, либо _fail
        await self._fail(job, RuntimeError("повторы исчерпаны"))

    async def _fail(self, job: _Job, error: BaseException) -> None:
        self._counters["failed"] += 1
        if self._on_error is None:
            logger.error(
                "Правило #{}: не удалось доставить — {}", job.rule.id, error
            )
            return
        try:
            await self._on_error(job.client, job.message, job.rule, error)
        except Exception:  # noqa: BLE001 — журнал не должен ронять воркер
            logger.exception("Обработчик ошибки доставки сам упал")

    # ──────────────────────────────── Диагностика ─────────────────────────────

    def stats(self) -> dict[str, int]:
        stats = dict(self._counters)
        stats["queued"] = self._queue.qsize()
        stats["workers"] = len(self._tasks)
        stats["pending_delays"] = len(self._pending)
        return stats
