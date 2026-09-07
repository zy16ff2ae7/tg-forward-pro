"""Очередь доставки: воркеры, темп отправки, повторы при ошибках Telegram.

Раньше каждое входящее сообщение порождало отдельную задачу
(``asyncio.create_task``): если в канал залетала сотня постов разом, все сто
одновременно лезли в Telegram и скопом получали FloodWait. Теперь отправка
идёт через ограниченную очередь:

* не больше ``SEND_GLOBAL_CONCURRENCY`` одновременных отправок на весь сервис;
* не чаще одной отправки в ``SEND_MIN_INTERVAL_SECONDS`` **на каждый аккаунт**
  (лимиты Telegram считаются по аккаунту, а не по сервису: общий темп на всех
  замедлял бы одного пользователя из-за активности другого);
* при FloodWait — пауза ровно на сколько просит Telegram, затем повтор;
* при других ошибках RPC — повтор с экспоненциальной паузой;
* при всплеске сверх ``DELIVERY_QUEUE_MAXSIZE`` — отбрасывание с записью в лог,
  а не бесконечный рост памяти;
* задача может быть записана в БД (``pending_deliveries``) — тогда перезапуск
  процесса её не теряет: см. ``submit_persistent`` и ``restore_pending``.

Повторяем только ошибки Telegram (FloodWait и RPCError): сбой базы или опечатку
в коде повторять бессмысленно, такая задача сразу уходит в журнал ошибок.

Отправка, которую доводить до конца больше не будем, обязана оставить строку в
журнале задачи: карточка в кабинете читает тот же журнал, и молча выброшенная
отправка выглядела на ней как «работает».
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger
from telethon.errors import FloodWaitError, RPCError

from app.config import settings
from app.telegram_client.types import DeliveryResult, RuleSnapshot

# Сколько раз восстановление ждёт аккаунт, который ещё не на связи. Проходов по
# три минуты (``REVIVE_INTERVAL``) — это четверть часа терпения: после
# перезапуска сервера аккаунт обычно возвращается за секунды, а бросать чужое
# сообщение из-за того, что сеть поднялась позже службы, не за что. Записи
# старше суток убирает ``drop_stale_pending_deliveries`` — бессмертных строк тут
# нет и без этого предохранителя.
RESTORE_ATTEMPTS_LIMIT = 5

# Обработчик получает «живой» клиент Telethon, сообщение и снимок правила.
#   вернул DeliveryResult или True — отправлено;
#   вернул False — пропущено (фильтр, нет подписки, служебное сообщение);
#   бросил исключение — ошибка отправки, решает очередь: повторить или сдатьcя.
Handler = Callable[[Any, Any, RuleSnapshot], Awaitable[Any]]
ErrorHandler = Callable[[Any, Any, RuleSnapshot, BaseException], Awaitable[None]]


def _interpret(result: Any) -> tuple[bool, str]:
    """Приводит ответ обработчика к паре «отправлено, причина пропуска».

    Обработчик пересылки возвращает ``DeliveryResult`` с причиной, но простой
    ``bool`` тоже остаётся рабочим ответом — на нём держатся служебные задачи
    и тесты очереди.
    """
    if isinstance(result, DeliveryResult):
        return result.sent, result.reason
    if result:
        return True, ""
    return False, "unknown"


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
    # id строки в pending_deliveries: пока она есть, задача считается
    # незавершённой и восстановится после перезапуска. None — задача нигде не
    # записана (служебные вызовы, тесты).
    delivery_id: int | None = None


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
        self._min_interval = max(0.0, float(min_interval))
        # Темп считается на каждый аккаунт отдельно: лимиты Telegram — тоже.
        self._limiters: dict[int, _IntervalLimiter] = {}
        self._attempts = max(0, int(retry_attempts))
        self._retry_base = max(0.1, float(retry_base))
        self._flood_max = max(1, int(flood_wait_max))

        self._queue: asyncio.Queue[_Job] = asyncio.Queue(maxsize=self._maxsize)
        self._tasks: list[asyncio.Task] = []
        # Задачи, спящие в отложенной отправке (задержка из правила)
        self._pending: set[asyncio.Task] = set()
        # id строк pending_deliveries, которые уже у нас в руках: стоят в очереди
        # или ждут своей задержки. Восстановление ходит по базе не один раз (см.
        # ``_revive_loop``), и без этого набора второй проход поставил бы тот же
        # пост в очередь ещё раз — получателю пришёл бы дубль.
        self._inflight: set[int] = set()
        self._started = False
        self._counters = {
            "submitted": 0,
            "dropped": 0,
            "sent": 0,
            "failed": 0,
            "skipped": 0,
            "restored": 0,
            # Оставлено до следующего прохода: аккаунт ещё не на связи.
            "deferred": 0,
        }
        # Пропуски по причинам: «фильтр» и «нет подписки» — это разные истории,
        # и в диагностике их надо различать.
        self._skips: dict[str, int] = {}

    def _limiter_for(self, account_id: int) -> _IntervalLimiter:
        limiter = self._limiters.get(account_id)
        if limiter is None:
            limiter = _IntervalLimiter(self._min_interval)
            self._limiters[account_id] = limiter
        return limiter

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
            "одной отправки в {} сек на аккаунт",
            self._workers_count,
            self._maxsize,
            self._min_interval,
        )

    async def stop(self, *, drain: bool = True, timeout: float = 30.0) -> None:
        """Останавливает воркеры. По умолчанию дожимает то, что уже в очереди.

        Отложенные задачи (задержка из правила) отменяются, но их строки в
        ``pending_deliveries`` остаются — после запуска ``restore_pending``
        поднимет их снова с остатком задержки.
        """
        self._started = False

        for task in list(self._pending):
            task.cancel()
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

        if drain:
            # join() ждёт и то, что уже взято воркером: пока не вызван
            # task_done, задача считается незавершённой. Проверка «очередь
            # пуста» этого не видела, и отправку, шедшую в этот момент, стоп
            # обрывал на полпути.
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
        # Строки в pending_deliveries остаются, а вот «у нас в руках» их больше
        # нет: следующий запуск обязан увидеть их как незавершённые.
        self._inflight.clear()
        logger.info("Очередь доставки остановлена: {}", self.stats())

    # ───────────────────────────────── Отправка ───────────────────────────────

    @staticmethod
    def _delay_for(rule: RuleSnapshot, delay_override: int | None = None) -> int:
        """Сколько ждать перед отправкой: задержка правила плюс тихие часы.

        Восстановление после перезапуска идёт мимо: там задержка уже лежит в
        строке БД (``due_at``), и окно в неё посчитали при постановке.
        """
        if delay_override is not None:
            return max(0, int(delay_override))
        delay = max(0, int(getattr(rule, "delay_seconds", 0) or 0))
        # Тихо ждут только пересылки: модерации и подпискам ночь не помеха —
        # спам в три часа ночи мутить надо, а не откладывать до утра.
        if getattr(rule, "kind", "forward") in ("forward", "clone"):
            from app.telegram_client.jobs import quiet_wait_seconds

            delay += quiet_wait_seconds(getattr(rule, "filters", None))
        return delay

    def submit(
        self,
        client: Any,
        message: Any,
        rule: RuleSnapshot,
        *,
        delivery_id: int | None = None,
        delay_override: int | None = None,
    ) -> bool:
        """Ставит задачу в очередь. False — очередь полна, задача отброшена.

        Задержка из правила (``delay_seconds``) отрабатывается до постановки
        в очередь: иначе правило с задержкой в час намертво занимало бы воркер.
        ``delay_override`` нужен восстановлению после перезапуска — там ждать
        осталось меньше, чем сказано в правиле.
        """
        job = _Job(client=client, message=message, rule=rule, delivery_id=delivery_id)
        if delivery_id is not None:
            self._inflight.add(int(delivery_id))
        delay = self._delay_for(rule, delay_override)
        if delay:
            self._spawn_delayed(job, delay)
            return True
        return self._put(job)

    async def submit_persistent(
        self,
        client: Any,
        message: Any,
        rule: RuleSnapshot,
        *,
        source_chat_id: int,
    ) -> bool:
        """Ставит задачу в очередь, предварительно записав её в БД.

        Порядок именно такой: сначала запись, потом очередь. Если процесс умрёт
        между двумя шагами, задача восстановится на старте — потеря сообщения
        хуже, чем повторная отправка (её видно и можно удалить).
        """
        from app.db import repo
        from app.db.database import session_scope

        message_id = int(getattr(message, "id", 0) or 0)
        delay = self._delay_for(rule)
        delivery_id: int | None = None
        if message_id:
            try:
                async with session_scope() as session:
                    delivery_id = await repo.remember_pending_delivery(
                        session,
                        rule_id=rule.id,
                        user_id=rule.user_id,
                        account_id=rule.account_id,
                        source_chat_id=int(source_chat_id),
                        message_id=message_id,
                        delay_seconds=delay,
                    )
            except Exception as exc:  # noqa: BLE001 — БД не должна съесть сообщение
                logger.warning(
                    "Правило #{}: не записали отправку в журнал ожидания: {}", rule.id, exc
                )

        ok = self.submit(client, message, rule, delivery_id=delivery_id)
        if not ok and delivery_id is not None:
            # Переполнение — решение осознанное, восстанавливать нечего. Но это
            # ровно то самое «сообщение не доехало, а карточка бодра»: причину
            # кладём в журнал задачи, там её видно человеку.
            await self.journal_loss(
                rule_id=rule.id,
                user_id=rule.user_id,
                message_id=int(getattr(message, "id", 0) or 0),
                reason="очередь доставки переполнена — сообщение отброшено",
            )
            await self._forget(delivery_id)
        return ok

    async def restore_pending(
        self,
        client_for: Callable[[int], Any],
        rule_for: Callable[[int], RuleSnapshot | None],
    ) -> int:
        """Поднимает отправки, не доведённые до конца прошлым запуском.

        ``client_for`` отдаёт живой Telethon-клиент по account_id, ``rule_for``
        — снимок правила по rule_id (None, если правило успели удалить).
        Сообщение перечитывается из источника: держать его копию в БД не нужно
        и не хочется — там могут быть личные переписки пользователей.

        Зовётся не только на старте: аккаунт, не успевший подключиться к первому
        проходу, поднимается позже сам (``_revive_loop``), и его отправки ждут
        этого прохода, а не выбрасываются. Уже взятые в работу строки проход
        пропускает — иначе получатель получил бы один и тот же пост дважды.
        """
        from app.db import repo
        from app.db.database import session_scope
        from app.timeutil import utcnow

        async with session_scope() as session:
            rows = list(await repo.due_pending_deliveries(session))

        if not rows:
            return 0

        restored = 0
        deferred = 0
        for row in rows:
            if int(row.id) in self._inflight:
                continue

            rule = rule_for(row.rule_id)
            if rule is None:
                # Пауза, архив или удаление задачи. Досылать нечего, но если
                # задача ещё жива — её журнал обязан объяснить пропажу поста.
                await self._notice_if_task_alive(
                    row, "задача не в работе — отложенная отправка отменена"
                )
                await self._forget(row.id)
                continue

            client = client_for(row.account_id)
            if client is None:
                attempts = await self._defer(row)
                if attempts < RESTORE_ATTEMPTS_LIMIT:
                    logger.info(
                        "Отправка #{}: аккаунт #{} ещё не на связи — попытка {}, ждём",
                        row.id,
                        row.account_id,
                        attempts,
                    )
                    deferred += 1
                    continue
                logger.warning(
                    "Отправка #{}: аккаунт #{} не вышел на связь за {} проходов — "
                    "запись убираем",
                    row.id,
                    row.account_id,
                    RESTORE_ATTEMPTS_LIMIT,
                )
                await self.journal_loss(
                    rule_id=row.rule_id,
                    user_id=row.user_id,
                    message_id=row.message_id,
                    reason="аккаунт так и не вышел на связь — отложенная отправка отменена",
                )
                await self._forget(row.id)
                continue

            try:
                message = await client.get_messages(row.source_chat_id, ids=row.message_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Отправка #{}: не перечитали сообщение: {}", row.id, exc)
                await self.journal_loss(
                    rule_id=row.rule_id,
                    user_id=row.user_id,
                    message_id=row.message_id,
                    reason=(
                        "не перечитали сообщение в источнике "
                        f"({type(exc).__name__}) — отложенная отправка отменена"
                    ),
                )
                await self._forget(row.id)
                continue
            if message is None:
                logger.info("Отправка #{}: сообщение удалено в источнике", row.id)
                await self.journal_loss(
                    rule_id=row.rule_id,
                    user_id=row.user_id,
                    message_id=row.message_id,
                    reason="сообщение удалено в источнике — досылать нечего",
                )
                await self._forget(row.id)
                continue

            remaining = int((row.due_at - utcnow()).total_seconds())
            self.submit(
                client,
                message,
                rule,
                delivery_id=row.id,
                delay_override=max(0, remaining),
            )
            restored += 1

        self._counters["restored"] += restored
        self._counters["deferred"] += deferred
        logger.info(
            "После перезапуска восстановлено отправок: {} (ждут аккаунт: {})",
            restored,
            deferred,
        )
        return restored

    async def _forget(self, delivery_id: int | None) -> None:
        """Убирает запись об отправке: задача доведена до конца (или отменена)."""
        if delivery_id is None:
            return
        from app.db import repo
        from app.db.database import session_scope

        self._inflight.discard(int(delivery_id))
        try:
            async with session_scope() as session:
                await repo.delete_pending_delivery(session, delivery_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалили запись об отправке #{}: {}", delivery_id, exc)

    async def _defer(self, row: Any) -> int:
        """Считает пустой проход по строке и оставляет её ждать следующего."""
        from app.db import repo
        from app.db.database import session_scope

        try:
            async with session_scope() as session:
                return await repo.defer_pending_delivery(session, int(row.id))
        except Exception as exc:  # noqa: BLE001 — не смогли посчитать, значит ждём дальше
            logger.warning("Не отметили попытку по отправке #{}: {}", row.id, exc)
            return 0

    async def journal_loss(
        self, *, rule_id: int, user_id: int, message_id: int, reason: str
    ) -> None:
        """Пишет в журнал задачи, что отправки не будет, и почему.

        Тот же журнал читает карточка в кабинете: без этой строки задача с
        потерянным сообщением выглядит работающей, а человек ждёт поста, который
        уже не придёт. Ошибку записи глотаем — восстановление важнее журнала.
        """
        from app.db import repo
        from app.db.database import session_scope

        try:
            async with session_scope() as session:
                await repo.log_forward(
                    session,
                    rule_id=int(rule_id),
                    user_id=int(user_id),
                    source_msg_id=int(message_id or 0),
                    target_msg_id=None,
                    status="error",
                    error=reason,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не записали в журнал задачи #{}: {}", rule_id, exc)

    async def _notice_if_task_alive(self, row: Any, reason: str) -> None:
        """То же, но только для задачи, которая ещё существует.

        Правило могли и удалить — тогда журнал писать некуда: карточки нет, а
        строки удалённых задач всё равно подчищает уборка (``trim_logs``).
        """
        from app.db import repo
        from app.db.database import SessionLocal

        try:
            async with SessionLocal() as session:
                rule = await repo.get_rule(session, int(row.rule_id), int(row.user_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не проверили задачу #{}: {}", row.rule_id, exc)
            return
        if rule is None:
            logger.info("Отправка #{}: задача #{} удалена", row.id, row.rule_id)
            return
        await self.journal_loss(
            rule_id=row.rule_id,
            user_id=row.user_id,
            message_id=row.message_id,
            reason=reason,
        )

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
        # Отложенные отправки тоже ограничиваем: иначе флуд в источник с правилом
        # delay=3600 создаст тысячи спящих задач и съест память. Граница та же,
        # что у очереди, — всплеск сверх неё отбрасывается с записью в журнал.
        if len(self._pending) >= self._maxsize:
            self._counters["dropped"] += 1
            logger.error(
                "Отложенных отправок уже {}: сообщение по правилу #{} отброшено",
                self._maxsize,
                job.rule.id,
            )
            return

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
        limiter = self._limiter_for(int(getattr(job.rule, "account_id", 0) or 0))
        for attempt in range(self._attempts + 1):
            if delay > 0:
                # Паузу держим вне семафора: ждать FloodWait, занимая слот
                # отправки, — значит блокировать остальные правила.
                await asyncio.sleep(delay)
            delay = 0.0
            try:
                async with self._semaphore:
                    await limiter.acquire()
                    result = await self._handler(job.client, job.message, job.rule)
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
                sent, reason = _interpret(result)
                if sent:
                    self._counters["sent"] += 1
                else:
                    self._counters["skipped"] += 1
                    self._skips[reason] = self._skips.get(reason, 0) + 1
                # Задача доведена до конца — восстанавливать её больше не нужно.
                await self._forget(job.delivery_id)
                return

        # Сюда не попадаем: в последней попытке либо return, либо _fail
        await self._fail(job, RuntimeError("повторы исчерпаны"))

    async def _fail(self, job: _Job, error: BaseException) -> None:
        self._counters["failed"] += 1
        # Повторы исчерпаны: держать запись дальше значит повторять отправку
        # после каждого перезапуска.
        await self._forget(job.delivery_id)
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

    def stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = dict(self._counters)
        stats["queued"] = self._queue.qsize()
        stats["workers"] = len(self._tasks)
        stats["pending_delays"] = len(self._pending)
        # Причины пропусков: «фильтр» и «нет подписки» лечатся по-разному,
        # поэтому в /api/health они идут раздельно.
        stats["skips"] = dict(sorted(self._skips.items()))
        return stats
