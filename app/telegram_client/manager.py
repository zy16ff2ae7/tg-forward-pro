"""Пул Telethon-клиентов: вход по номеру телефона, запуск, маршрутизация сообщений."""
from __future__ import annotations

import asyncio
from typing import Any, Iterable
from urllib.parse import urlparse

from loguru import logger
from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from app.config import settings
from app.db.database import SessionLocal, session_scope
from app.db.models import TelegramAccount
from app.db import repo
from app.telegram_client.filters import FilterConfig
from app.telegram_client.forwarder import deliver, log_delivery_error
from app.telegram_client.jobs import FLOATING_KINDS
from app.telegram_client.queue import DeliveryQueue
from app.telegram_client.types import RuleSnapshot


class TelegramApiCredentialsMissing(RuntimeError):
    """Шлюз личных аккаунтов ещё не настроен на стороне сервиса."""


PUBLIC_LOGIN_UNAVAILABLE = (
    "Подключение Telegram-аккаунтов временно недоступно. "
    "Бот и кабинет работают, но шлюз входа по номеру ещё не настроен на стороне сервиса."
)


def _proxy_dict(proxy_url: str | None) -> dict | None:
    """Превращает строку вида socks5://user:pass@host:port в словарь для Telethon."""
    if not proxy_url:
        return None
    parsed = urlparse(proxy_url)
    if not parsed.hostname:
        logger.warning("PROXY указан, но не распознан: {}", proxy_url)
        return None
    scheme = parsed.scheme.lower()
    if scheme.startswith("socks5"):
        proxy_type = "socks5"
    elif scheme.startswith("socks4"):
        proxy_type = "socks4"
    else:
        proxy_type = "http"
    return {
        "proxy_type": proxy_type,
        "addr": parsed.hostname,
        "port": parsed.port or (1080 if proxy_type != "http" else 8080),
        "username": parsed.username,
        "password": parsed.password,
        "rdns": True,
    }


def _snapshot(rule) -> RuleSnapshot:
    """Правило из БД → снимок для обработчика сообщений."""
    return RuleSnapshot(
        id=rule.id,
        user_id=rule.user_id,
        target_id=rule.target_id,
        mode=rule.mode,
        delay_seconds=rule.delay_seconds,
        filters=FilterConfig.from_dict(rule.filters or {}),
        kind=rule.kind or "forward",
        source_id=rule.source_id,
        source_title=rule.source_title or "",
        target_title=rule.target_title or "",
    )


class ClientManager:
    """Держит живые Telethon-сессии и раздаёт им входящие сообщения."""

    def __init__(self) -> None:
        self._clients: dict[int, TelegramClient] = {}
        # (account_id, source_chat_id) -> список правил
        self._rules: dict[tuple[int, int], list[RuleSnapshot]] = {}
        # account_id -> правила, слушающие все чаты аккаунта (например, ЛС)
        self._floating_rules: dict[int, list[RuleSnapshot]] = {}
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task | None = None

    # ───────────────────────────── Вход по номеру ─────────────────────────────

    def _ensure_mtproto_ready(self) -> None:
        if not settings.mtproto_ready:
            raise TelegramApiCredentialsMissing(PUBLIC_LOGIN_UNAVAILABLE)

    def _new_client(self, session_string: str = "") -> TelegramClient:
        self._ensure_mtproto_ready()
        return TelegramClient(
            StringSession(session_string),
            settings.api_id,
            settings.api_hash,
            proxy=_proxy_dict(settings.proxy),
            device_model="MacBook Pro",
            system_version="macOS",
            app_version="1.0",
            connection_retries=5,
            request_retries=5,
        )

    async def send_code(self, phone: str) -> tuple[str, str]:
        """Отправляет код подтверждения. Возвращает (сессия, phone_code_hash)."""
        client = self._new_client()
        await client.connect()
        try:
            result = await client.send_code_request(phone)
            return client.session.save(), result.phone_code_hash
        finally:
            await client.disconnect()

    async def sign_in_code(
        self, phone: str, code: str, session_string: str, phone_code_hash: str
    ) -> str:
        """Вводит код из Telegram. Возвращает обновлённую сессию."""
        client = self._new_client(session_string)
        await client.connect()
        try:
            await client.sign_in(
                phone=phone, code=code, phone_code_hash=phone_code_hash
            )
            return client.session.save()
        except SessionPasswordNeededError:
            # облачный пароль — просим пользователя во второй ступени
            return client.session.save()
        finally:
            await client.disconnect()

    async def sign_in_password(self, password: str, session_string: str) -> str:
        """Вводит облачный пароль (2FA). Возвращает итоговую сессию."""
        client = self._new_client(session_string)
        await client.connect()
        try:
            await client.sign_in(password=password)
            return client.session.save()
        finally:
            await client.disconnect()

    async def check_session(self, session_string: str) -> tuple[bool, str | None, str | None]:
        """Проверяет, что сессия жива. Возвращает (ok, имя_пользователя, ошибка)."""
        client = self._new_client(session_string)
        try:
            await client.connect()
            me = await client.get_me()
            if me is None:
                return False, None, "Не удалось получить данные аккаунта"
            name = " ".join(
                part for part in (me.first_name, me.last_name) if part
            ) or (me.username or str(me.id))
            return True, name, None
        except Exception as exc:  # noqa: BLE001 — нам важно вернуть текст ошибки
            return False, None, f"{type(exc).__name__}: {exc}"
        finally:
            await client.disconnect()

    # ─────────────────────────── Запуск и остановка ───────────────────────────

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        """Обработчик всех новых сообщений подключённых аккаунтов."""
        account_id: int | None = getattr(event.client, "_account_id", None)
        if account_id is None:
            return
        chat_id = event.chat_id
        rules = list(self._rules.get((account_id, chat_id)) or [])
        # «плавающие» задачи (уведомления из ЛС) слушают любой приватный чат
        if getattr(event, "is_private", False):
            rules.extend(self._floating_rules.get(account_id) or [])
        if not rules:
            return

        for rule in rules:
            # Не asyncio.create_task: при всплеске (сотня постов разом) задачи
            # скопом лезли в Telegram и ловили FloodWait. Очередь держит темп,
            # а при переполнении честно отбрасывает с записью в журнал.
            delivery_queue.submit(
                client=event.client, message=event.message, rule=rule
            )

    async def start_account(self, account: TelegramAccount, session_string: str) -> bool:
        """Подключает один аккаунт и вешает на него обработчик."""
        client = self._new_client(session_string)
        setattr(client, "_account_id", account.id)
        client.add_event_handler(
            self._on_new_message,
            events.NewMessage(incoming=True),
        )
        try:
            await client.start()  # для StringSession без авторизации start() = connect
        except Exception as exc:  # noqa: BLE001
            logger.error("Аккаунт #{} ({}) не запустился: {}", account.id, account.phone, exc)
            async with session_scope() as session:
                db_account = await session.get(TelegramAccount, account.id)
                if db_account is not None:
                    await repo.set_account_error(session, db_account, f"{type(exc).__name__}: {exc}")
            return False

        me = await client.get_me()
        if me is None:
            await client.disconnect()
            return False

        async with self._lock:
            old = self._clients.get(account.id)
            self._clients[account.id] = client
        if old is not None:
            try:
                await old.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.debug("Старая сессия аккаунта #{} уже закрыта или не отвечает: {}", account.id, exc)

        logger.info(
            "Аккаунт #{} ({}) на связи: {}",
            account.id,
            account.phone,
            getattr(me, "username", None) or me.id,
        )
        return True

    async def stop_account(self, account_id: int) -> None:
        async with self._lock:
            client = self._clients.pop(account_id, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("Не удалось корректно закрыть клиент #{}", account_id)

    async def start_all(self) -> None:
        """Поднимает все активные аккаунты из БД."""
        from app.security import decrypt_session

        await delivery_queue.start()

        if not settings.mtproto_ready:
            logger.warning(
                "API_ID/API_HASH не заданы: бот и мини-апп стартуют, вход аккаунтов отключён"
            )
            await self.refresh_rules()
            return

        async with SessionLocal() as session:
            accounts = list(await repo.all_active_accounts(session))

        await self.refresh_rules()

        for account in accounts:
            try:
                session_string = decrypt_session(account.session_encrypted)
            except Exception as exc:  # noqa: BLE001
                logger.error("Сессия аккаунта #{} не читается: {}", account.id, exc)
                async with session_scope() as db:
                    db_account = await db.get(TelegramAccount, account.id)
                    if db_account is not None:
                        await repo.set_account_error(db, db_account, str(exc))
                continue
            ok = await self.start_account(account, session_string)
            async with session_scope() as db:
                db_account = await db.get(TelegramAccount, account.id)
                if db_account is not None:
                    if ok:
                        db_account.last_seen_at = repo.utcnow()
                        await repo.set_account_error(db, db_account, None)
                    else:
                        await repo.set_account_error(db, db_account, "Не удалось запустить сессию")

    async def stop_all(self) -> None:
        # Сначала дожимаем очередь: если отключить клиенты раньше, то, что уже
        # стоит в очереди, упадёт с ошибкой соединения.
        await delivery_queue.stop()
        for account_id in list(self._clients):
            await self.stop_account(account_id)
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None

    def is_online(self, account_id: int) -> bool:
        client = self._clients.get(account_id)
        return bool(client is not None and client.is_connected())

    def online_ids(self) -> Iterable[int]:
        return [acc_id for acc_id in self._clients if self.is_online(acc_id)]

    def delivery_stats(self) -> dict[str, int]:
        """Счётчики очереди доставки — для админки и /api/health."""
        return delivery_queue.stats()

    async def list_dialogs(self, account_id: int, limit: int = 30) -> list[dict[str, Any]]:
        """Список чатов аккаунта: для выбора источника и приёмника."""
        if not settings.mtproto_ready:
            return []
        client = self._clients.get(account_id)
        if client is None:
            return []
        result: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs(limit=limit):
            entity = dialog.entity
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or "Без имени"
            result.append(
                {
                    "id": dialog.id,
                    "title": title,
                    "is_channel": bool(getattr(entity, "broadcast", False)),
                    "is_group": bool(getattr(entity, "megagroup", False)),
                }
            )
        return result

    async def resolve_chat(self, account_id: int, query: str) -> tuple[int, str] | None:
        """Находит чат по @username, ссылке t.me, числовому id или названию.

        Возвращает (id, название). Числовой id (в т.ч. отрицательный id канала)
        резолвится по списку диалогов аккаунта — именно это нужно для выбора
        чатов мышью в мини-аппе, где chatToRef отдаёт «голый» id без username.
        """
        if not settings.mtproto_ready:
            return None
        client = self._clients.get(account_id)
        if client is None:
            return None
        query = (query or "").strip()
        if not query:
            return None

        # нормализуем: ссылки t.me/c/... и t.me/..., а также ведущий @
        raw = query
        low = raw.lower()
        if "t.me/" in low:
            raw = raw.split("t.me/", 1)[1].split("/")[0].split("?")[0]
        raw = raw.lstrip("@")
        raw = raw.strip()
        if not raw:
            return None

        # числовой id (голые id чатов/каналов/пользователей, в т.ч. отрицательные)
        numeric_id: int | None = None
        if raw.lstrip("-").isdigit():
            numeric_id = int(raw)

        # 1) username / ссылка — резолвим напрямую через API
        if numeric_id is None:
            try:
                entity = await client.get_entity(raw)
                title = (
                    getattr(entity, "title", None)
                    or getattr(entity, "first_name", None)
                    or str(getattr(entity, "id", "?"))
                )
                return int(getattr(entity, "id")), title
            except Exception:  # noqa: BLE001
                pass  # дальше ищем по диалогам

        # 2) ищем среди диалогов: сначала точное совпадение по id, затем по названию
        async for dialog in client.iter_dialogs(limit=200):
            entity = dialog.entity
            title = (
                getattr(entity, "title", None)
                or getattr(entity, "first_name", None)
                or "Без имени"
            )
            if numeric_id is not None and int(dialog.id) == numeric_id:
                return int(dialog.id), title
            if raw and raw.lower() in title.lower():
                return int(dialog.id), title
        return None

    # ─────────────────────────────── Кэш правил ───────────────────────────────

    async def refresh_rules(self) -> None:
        """Перечитывает правила из БД в память (вызывается при изменениях и по таймеру)."""
        from sqlalchemy import select

        from app.db.models import Rule

        async with SessionLocal() as session:
            result = await session.execute(
                select(Rule).where(Rule.enabled.is_(True))
            )
            rules = result.scalars().all()

        fresh: dict[tuple[int, int], list[RuleSnapshot]] = {}
        floating: dict[int, list[RuleSnapshot]] = {}
        for rule in rules:
            snapshot = _snapshot(rule)
            if snapshot.kind in FLOATING_KINDS:
                floating.setdefault(rule.account_id, []).append(snapshot)
            else:
                fresh.setdefault((rule.account_id, rule.source_id), []).append(snapshot)
        async with self._lock:
            self._rules = fresh
            self._floating_rules = floating

    def rules_for(self, account_id: int, chat_id: int) -> list[RuleSnapshot]:
        return self._rules.get((account_id, chat_id), [])

    # ───────────────────────────── Разовые задачи ─────────────────────────────

    async def run_task_now(self, rule) -> dict:
        """Запускает разовую задачу (парсер, автоподписка) на живом клиенте.

        Возвращает сводку запуска: {"ok": bool, ...}.
        """
        from app.telegram_client.jobs import run_oneshot

        snapshot = _snapshot(rule)
        client = self._clients.get(rule.account_id)
        if client is None or not client.is_connected():
            return {
                "ok": False,
                "error": "Аккаунт не в сети. Перезапустите его в боте и повторите запуск.",
            }

        try:
            return await run_oneshot(client, snapshot)
        except Exception as exc:  # noqa: BLE001 — результат нужен в API, а не в лог
            logger.exception("Задача #{} не выполнилась", rule.id)
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def start_periodic_refresh(self, interval: int = 60) -> None:
        """Фоновое обновление кэша правил, чтобы правки из бота подхватывались сами."""

        async def loop() -> None:
            while True:
                try:
                    await asyncio.sleep(interval)
                    await self.refresh_rules()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Не удалось обновить кэш правил: {}", exc)

        self._refresh_task = asyncio.create_task(loop())


#: Очередь доставки одна на процесс: она и держит общий темп отправки.
delivery_queue = DeliveryQueue.from_settings(deliver, on_error=log_delivery_error)

manager = ClientManager()

# Удобные ссылки на типы ошибок для хендлеров входа
LOGIN_ERRORS = (
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    FloodWaitError,
    SessionPasswordNeededError,
)
