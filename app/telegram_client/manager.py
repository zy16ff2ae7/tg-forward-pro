"""Пул Telethon-клиентов: вход по номеру телефона, запуск, маршрутизация сообщений."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Iterable, Sequence
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
from telethon.tl import functions, types

from app.config import settings
from app.db.database import SessionLocal, session_scope
from app.db.models import TelegramAccount
from app.db import repo
from app.telegram_client.filters import FilterConfig
from app.telegram_client.forwarder import (
    deliver,
    log_delivery_error,
    subscription_active,
)
from app.telegram_client.jobs import (
    FLOATING_KINDS,
    MANUAL_ONLY_KINDS,
    SCHEDULED_KINDS,
    record_batch,
)
from app.telegram_client.queue import DeliveryQueue
from app.telegram_client.types import RuleSnapshot


class TelegramApiCredentialsMissing(RuntimeError):
    """Шлюз личных аккаунтов ещё не настроен на стороне сервиса."""


PUBLIC_LOGIN_UNAVAILABLE = (
    "Подключение Telegram-аккаунтов временно недоступно. "
    "Бот и кабинет работают, но шлюз входа по номеру ещё не настроен на стороне сервиса."
)

# Что показать, когда ключ сессии мёртв: аккаунт вышел из Telegram сам («Устройства»
# → выйти), сменил пароль или его сессию убил вход тем же ключом с другой машины.
# Эта строка попадает человеку в кабинет как есть, поэтому в ней сказано, что
# делать, а не что за исключение поймал Telethon.
SESSION_REVOKED = "Аккаунт вышел из Telegram — подключите номер заново"

# Причина, после которой пробовать снова бессмысленно: сессии больше нет, и
# оживить её нечем — нужен вход по номеру. Всё остальное (сеть, таймаут, Telegram
# не ответил) проходит само, поэтому такие аккаунты сервис поднимает снова.
SESSION_UNREADABLE = "Сохранённая сессия не читается — подключите номер заново"
HOPELESS_ERRORS = (SESSION_REVOKED, SESSION_UNREADABLE)
# Что написать, когда Telegram соединение принял, но себя не назвал. Бывает при
# обрыве на полуслове; проходит само, поэтому аккаунт остаётся в работе.
ACCOUNT_SILENT = "Telegram не отдал данные аккаунта — пробуем снова"
# Как часто поднимать аккаунты, которые сейчас не на связи. Три минуты — чтобы
# короткий обрыв сети чинился сам и незаметно, но и не стучать в Telegram зря.
REVIVE_INTERVAL = 180.0

# Рассылка по чатам: шаг у неё в секундах (пауза между получателями), поэтому
# тик планировщика — секунда, а не 20 секунд, как у авто-постера.
MAILING_TICK_SECONDS = 1.0
# Как часто переспрашивать базу про подписку: тик каждую секунду, и на каждый
# проход таскать запрос незачем.
SUBSCRIPTION_CHECK_TTL = 60.0
# Паузы после сбоя: пустая библиотека и ошибка отправки лечатся по-разному,
# но оба случая не должны засыпать журнал сообщением каждую секунду.
MAILING_EMPTY_PAUSE = 60.0
MAILING_ERROR_PAUSE = 30.0

# Список диалогов аккаунта живёт в памяти минуту. Один обход диалогов — это
# запрос за запросом к Telegram, а кабинет спрашивает чаты часто: список во
# вкладке «Чаты», шторка выбора, поиск в ней, и потом ещё раз при сохранении
# задачи, где надо найти каждый отмеченный чат. Без кэша выбор двухсот чатов
# мышкой означал бы двести обходов подряд — это верный FloodWait.
DIALOGS_CACHE_TTL = 60.0


# Куда Telegram положил код из ответа SendCode/ResendCode — короткими именами
# для журнала и подсказок человеку. Полный список типов см. в
# telethon.tl.types.auth: неизвестное будущее сводим к "other", а не роняем вход.
_DELIVERY_VIA = {
    "SentCodeTypeApp": "app",
    "SentCodeTypeSms": "sms",
    "SentCodeTypeCall": "call",
    "SentCodeTypeFlashCall": "flashcall",
    "SentCodeTypeFirebaseSms": "firebase",
    "SentCodeTypeMissedCall": "missed",
}


def _delivery_info(result) -> dict:
    """Тип доставки кода из ответа Telegram — журналу и человеку.

    ``via`` — куда ушёл этот код, ``next`` — каким способом придёт повтор,
    ``timeout`` — через сколько секунд повтор станет доступен (None — неизвестно).
    """
    via = _DELIVERY_VIA.get(type(getattr(result, "type", None)).__name__, "other")
    nxt = getattr(result, "next_type", None)
    next_via = _DELIVERY_VIA.get(type(nxt).__name__) if nxt is not None else None
    timeout = getattr(result, "timeout", None)
    return {
        "via": via,
        "next": next_via,
        "timeout": int(timeout) if timeout is not None else None,
    }


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
        account_id=rule.account_id,
        mode=rule.mode,
        delay_seconds=rule.delay_seconds,
        filters=FilterConfig.from_dict(rule.filters or {}),
        kind=rule.kind or "forward",
        source_id=rule.source_id,
        source_title=rule.source_title or "",
        target_title=rule.target_title or "",
        enabled=bool(rule.enabled),
        archived=bool(rule.archived),
        forwarded_count=int(rule.forwarded_count or 0),
    )


def _normalize_ref(query: str) -> str:
    """Ссылка на чат → то, по чему его можно искать.

    Принимает «@name», «t.me/name», «https://t.me/c/123/45?single» и числовой id,
    отдаёт «name» либо «123». Разбор один на все пути поиска: раньше он жил
    внутри resolve_chat, и поиск пачкой повторил бы его второй копией.
    """
    raw = (query or "").strip()
    if "t.me/" in raw.lower():
        raw = raw.split("t.me/", 1)[1].split("/")[0].split("?")[0]
    return raw.lstrip("@").strip()


def _hhmm_to_sec(value: str) -> int:
    """«ЧЧ:ММ» → секунды от начала суток. Невалидное значение → 0."""
    try:
        hours, minutes = str(value).split(":")
        return max(0, min(23, int(hours))) * 3600 + max(0, min(59, int(minutes))) * 60
    except Exception:  # noqa: BLE001
        return 0


def _in_window(now_sec: int, start: int, end: int) -> bool:
    """Попадает ли момент в окно. Окно через полночь (23:00→01:00) тоже ок."""
    if start <= end:
        return start <= now_sec <= end
    return now_sec >= start or now_sec <= end


class ClientManager:
    """Держит живые Telethon-сессии и раздаёт им входящие сообщения."""

    def __init__(self) -> None:
        self._clients: dict[int, TelegramClient] = {}
        # (account_id, source_chat_id) -> список правил
        self._rules: dict[tuple[int, int], list[RuleSnapshot]] = {}
        # account_id -> правила, слушающие все чаты аккаунта (например, ЛС)
        self._floating_rules: dict[int, list[RuleSnapshot]] = {}
        # rule_id -> снимок: нужен восстановлению отправок после перезапуска,
        # где на руках только id правила из БД.
        self._rules_by_id: dict[int, RuleSnapshot] = {}
        # Авто-постеры — отдельный список: стреляют по расписанию, а не по
        # входящим сообщениям, поэтому в _rules их класть не надо.
        self._poster_rules: list[RuleSnapshot] = []
        # Состояние планировщика на правило: last — время последней отправки,
        # idx — индекс следующего сообщения, runs — сколько раз отправили.
        self._poster_state: dict[int, dict] = {}
        self._poster_task: asyncio.Task | None = None
        # Рассылки по чатам — свой список и свой цикл: у них шаг в секундах, а
        # постер тикает раз в 20 сек и такой темп просто не выдержал бы.
        self._mailing_rules: list[RuleSnapshot] = []
        # rule_id -> {"pos", "cycle", "due", "not_before", "typed"}
        self._mailing_state: dict[int, dict] = {}
        self._mailing_task: asyncio.Task | None = None
        # Кто сейчас не на связи, того поднимают снова — см. _revive_loop.
        self._revive_task: asyncio.Task | None = None
        # account_id -> (когда собрали, все диалоги аккаунта). Кэш на минуту:
        # см. DIALOGS_CACHE_TTL — без него выбор чатов пачкой означал бы обход
        # диалогов на каждый отмеченный чат.
        self._dialogs_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
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

    async def send_code(self, phone: str) -> tuple[str, str, dict]:
        """Отправляет код подтверждения.

        Возвращает (сессия, phone_code_hash, доставка), где доставка — словарь
        ``{"via", "next", "timeout"}``: куда Telegram положил код сейчас
        (``app`` — в приложение, ``sms`` — по SMS, ``call`` — звонком),
        каким способом придёт следующий повтор и через сколько секунд он
        станет доступен. Тип доставки пишется и в журнал: иначе «код не
        пришёл» гадается вслепую.
        """
        client = self._new_client()
        await client.connect()
        try:
            result = await client.send_code_request(phone)
            delivery = _delivery_info(result)
            logger.info(
                "Код на {}: отправлен {} (следующий: {}, через {} сек)",
                phone,
                delivery["via"],
                delivery["next"],
                delivery["timeout"],
            )
            return client.session.save(), result.phone_code_hash, delivery
        finally:
            await client.disconnect()

    async def resend_code(
        self, phone: str, session_string: str, phone_code_hash: str
    ) -> tuple[str, str, dict]:
        """Просит Telegram прислать код ещё раз — следующим способом доставки.

        В отличие от нового ``send_code`` это ``auth.ResendCode``: сервер
        продолжает ту же попытку входа и обычно переключается с приложения
        на SMS/звонок, а не начинает всё заново. Новый ``phone_code_hash``
        заменяет старый — код из прошлого сообщения после повтора мёртв.
        ``PhoneCodeExpiredError`` наружу не глотаем: попытка целиком протухла
        и вызывающий должен начать вход заново.
        """
        client = self._new_client(session_string)
        await client.connect()
        try:
            result = await client(
                functions.auth.ResendCodeRequest(phone, phone_code_hash)
            )
            delivery = _delivery_info(result)
            logger.info(
                "Код на {}: повтор отправлен {} (следующий: {}, через {} сек)",
                phone,
                delivery["via"],
                delivery["next"],
                delivery["timeout"],
            )
            return client.session.save(), result.phone_code_hash, delivery
        finally:
            await client.disconnect()

    async def sign_in_code(
        self, phone: str, code: str, session_string: str, phone_code_hash: str
    ) -> str:
        """Вводит код из Telegram. Возвращает обновлённую сессию.

        ``SessionPasswordNeededError`` наружу не глотаем: по нему вызывающий
        переводит вход на ступень облачного пароля. Раньше здесь на эту ошибку
        возвращалась сессия — как при успешном входе, и для вызывающего код
        выглядел принятым целиком. Сессия при этом оставалась неавторизованной,
        поэтому вход у всех, у кого включён 2FA, падал не на шаге пароля, а на
        итоговой проверке: ``get_me()`` отдаёт ``None`` → «Не удалось получить
        данные аккаунта». Отдельную сессию для шага пароля возвращать не нужно —
        подойдёт та же, что пришла: ключ авторизации и DC при вводе кода не
        меняются, а SRP-обмен идёт по тому же ключу.
        """
        client = self._new_client(session_string)
        await client.connect()
        try:
            await client.sign_in(
                phone=phone, code=code, phone_code_hash=phone_code_hash
            )
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
            if rule.kind == "forward":
                # Пересылку записываем в БД: перезапуск процесса (деплой,
                # падение) не должен съедать сообщение — очередь досылает его
                # на старте. Остальные задачи (сборщики, уведомления) не
                # восстанавливаем: повторный проход насобирал бы дубликаты, а
                # пропущенное уведомление уже неактуально.
                await delivery_queue.submit_persistent(
                    event.client, event.message, rule, source_chat_id=chat_id
                )
            else:
                delivery_queue.submit(event.client, event.message, rule)

    async def start_account(self, account: TelegramAccount, session_string: str) -> bool:
        """Подключает один аккаунт и вешает на него обработчик."""
        client = self._new_client(session_string)
        setattr(client, "_account_id", account.id)
        client.add_event_handler(
            self._on_new_message,
            events.NewMessage(incoming=True),
        )
        try:
            # start() у Telethon для неавторизованной сессии спрашивает номер и
            # код через input(). Под systemd stdin закрыт, поэтому вместо
            # понятной причины и в журнал, и человеку в кабинет попадало «EOF
            # when reading a line» — про такое не догадаешься, что аккаунт надо
            # подключить заново. Подключаемся сами и сами смотрим авторизацию.
            await client.connect()
            authorized = await client.is_user_authorized()
        except Exception as exc:  # noqa: BLE001
            logger.error("Аккаунт #{} ({}) не запустился: {}", account.id, account.phone, exc)
            async with session_scope() as session:
                db_account = await session.get(TelegramAccount, account.id)
                if db_account is not None:
                    # Сеть, таймаут, Telegram не в духе — это пройдёт, и аккаунт
                    # остаётся в работе: поднимем его следующим заходом сами.
                    await repo.note_account_trouble(
                        session, db_account, f"{type(exc).__name__}: {exc}"
                    )
            return False

        if not authorized:
            # Сессию отозвали из Telegram («Устройства» → выйти), сменили пароль
            # или её убил вход тем же ключом с другой машины. Ключ уже мёртв:
            # чинить нечего, человеку нужно подключить номер заново.
            logger.error(
                "Сессия аккаунта #{} ({}) больше не действует — нужен повторный вход",
                account.id,
                account.phone,
            )
            await client.disconnect()
            async with session_scope() as session:
                db_account = await session.get(TelegramAccount, account.id)
                if db_account is not None:
                    await repo.set_account_error(session, db_account, SESSION_REVOKED)
            return False

        me = await client.get_me()
        if me is None:
            # Соединение есть, а данных нет: обрыв на полуслове. Причину пишем —
            # иначе в кабинете «офлайн» без объяснения, — но аккаунт не гасим.
            logger.warning(
                "Аккаунт #{} ({}) подключился, но не назвал себя", account.id, account.phone
            )
            await client.disconnect()
            async with session_scope() as session:
                db_account = await session.get(TelegramAccount, account.id)
                if db_account is not None:
                    await repo.note_account_trouble(session, db_account, ACCOUNT_SILENT)
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
            # Чаты остановленного аккаунта — уже не его чаты: следующий вход
            # должен увидеть свежий список, а не тот, что лежал в кэше.
            self._dialogs_cache.pop(account_id, None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                logger.debug("Не удалось корректно закрыть клиент #{}", account_id)

    async def start_all(self) -> None:
        """Поднимает все активные аккаунты из БД."""
        await delivery_queue.start()

        if not settings.mtproto_ready:
            logger.warning(
                "API_ID/API_HASH не заданы: бот и мини-апп стартуют, вход аккаунтов отключён"
            )
            await self.refresh_rules()
            return

        await self.refresh_rules()
        await self._start_pending_accounts()

        # Планировщик авто-постера поднимаем, только когда аккаунты реально
        # могут постить (MTProto готов и хотя бы один поднялся).
        if self._poster_task is None or self._poster_task.done():
            self._poster_task = asyncio.create_task(self._poster_loop())
        if self._mailing_task is None or self._mailing_task.done():
            self._mailing_task = asyncio.create_task(self._mailing_loop())
        # Аккаунт, у которого не задалось соединение, раньше оставался
        # выключенным до вмешательства человека. Теперь его поднимают снова сами.
        if self._revive_task is None or self._revive_task.done():
            self._revive_task = asyncio.create_task(self._revive_loop())

        await self._restore_deliveries()

    async def _start_pending_accounts(self) -> list[int]:
        """Поднимает тех, кто должен работать, но сейчас не на связи.

        Зовётся и на старте сервиса, и потом по кругу — см. ``_revive_loop``.
        Уже подключённых пропускаем: второй клиент на ту же сессию Telegram не
        нужен никому.
        """
        async with SessionLocal() as session:
            accounts = list(await repo.accounts_to_start(session, HOPELESS_ERRORS))

        started: list[int] = []
        for account in accounts:
            if self.is_online(account.id):
                continue
            if await self._start_and_record(account):
                started.append(account.id)
        return started

    async def retry_account(self, account_id: int) -> tuple[bool, str | None]:
        """Ещё одна попытка по просьбе человека: кнопка «Попробовать снова».

        Просьбу выполняем, даже если аккаунт числится выключенным: человек видит
        причину в кабинете и сам решает, стоит ли пробовать. Возвращаем, вышел
        ли аккаунт на связь, и причину, если нет.
        """
        async with SessionLocal() as session:
            account = await session.get(TelegramAccount, account_id)
            if account is None:
                return False, "Аккаунт не найден"
            session.expunge(account)

        if self.is_online(account_id):
            return True, None

        ok = await self._start_and_record(account)
        if ok:
            return True, None
        async with SessionLocal() as session:
            fresh = await session.get(TelegramAccount, account_id)
            return False, (fresh.last_error if fresh is not None else None)

    async def _start_and_record(self, account: TelegramAccount) -> bool:
        """Поднимает аккаунт и записывает итог: время связи или причину отказа."""
        from app.security import decrypt_session

        try:
            session_string = decrypt_session(account.session_encrypted)
        except Exception as exc:  # noqa: BLE001
            logger.error("Сессия аккаунта #{} не читается: {}", account.id, exc)
            async with session_scope() as db:
                db_account = await db.get(TelegramAccount, account.id)
                if db_account is not None:
                    # Здесь повтор не поможет: сменился ключ шифрования или
                    # строка испорчена — сама она не выправится. Человеку важно
                    # не имя исключения, а что делать, поэтому причина общая.
                    await repo.set_account_error(db, db_account, SESSION_UNREADABLE)
            return False

        ok = await self.start_account(account, session_string)
        async with session_scope() as db:
            db_account = await db.get(TelegramAccount, account.id)
            if db_account is not None:
                if ok:
                    db_account.last_seen_at = repo.utcnow()
                    await repo.set_account_error(db, db_account, None)
                elif not db_account.last_error:
                    # Причину, если она известна, записал start_account. Общая
                    # подпись затирала её — и в кабинете вместо «сессия больше
                    # не действует» оставалось безадресное «не удалось
                    # запустить сессию».
                    await repo.note_account_trouble(
                        db, db_account, "Не удалось запустить сессию"
                    )
        return ok

    async def _revive_loop(self) -> None:
        """Возвращает в работу аккаунты, которые сейчас не на связи.

        Обрыв сети, перезапуск сервиса раньше, чем поднялась сеть, Telegram не
        ответил — всё это проходит само. Но раньше сервис к аккаунту больше не
        возвращался: одна осечка на старте, и пересылка стояла молча, а вернуть
        её мог только полный вход по номеру заново.
        """
        while True:
            try:
                await asyncio.sleep(REVIVE_INTERVAL)
                if not settings.mtproto_ready:
                    continue
                revived = await self._start_pending_accounts()
                if revived:
                    logger.info("Аккаунты вернулись в работу: {}", revived)
                    # Их незавершённые отправки ждали именно этого: пока
                    # аккаунта не было, проход восстановления оставлял строки
                    # в базе. Теперь есть чем перечитать сообщение — досылаем.
                    await self._restore_deliveries()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("Не получилось поднять аккаунты заново: {}", exc)

    async def _restore_deliveries(self) -> None:
        """Досылает то, что не успел прошлый запуск.

        Вызывается после подключения аккаунтов: раньше клиентов ещё нет, и
        перечитать сообщение из источника нечем. И повторно — когда аккаунт
        вернулся в работу позже (``_revive_loop``): его отправки к первому
        проходу были отложены, а не выброшены.
        """
        try:
            async with session_scope() as session:
                stale = list(await repo.drop_stale_pending_deliveries(session))
            if stale:
                logger.info("Отправок просрочено и убрано: {}", len(stale))
            for row in stale:
                # Сутки — это уже не «задержалось», а «не будет». Молча такое
                # терять нельзя: на карточке задача выглядела работающей.
                await delivery_queue.journal_loss(
                    rule_id=row.rule_id,
                    user_id=row.user_id,
                    message_id=row.message_id,
                    reason="отправка просрочена: больше суток без связи — отменена",
                )
            await delivery_queue.restore_pending(
                self._connected_client, self._rules_by_id.get
            )
        except Exception as exc:  # noqa: BLE001 — старт важнее восстановления
            logger.warning("Не восстановили отправки после перезапуска: {}", exc)

    def _connected_client(self, account_id: int) -> TelegramClient | None:
        client = self._clients.get(account_id)
        if client is None or not client.is_connected():
            return None
        return client

    async def stop_all(self) -> None:
        # Сначала дожимаем очередь: если отключить клиенты раньше, то, что уже
        # стоит в очереди, упадёт с ошибкой соединения.
        if self._poster_task is not None:
            self._poster_task.cancel()
            self._poster_task = None
        if self._mailing_task is not None:
            self._mailing_task.cancel()
            self._mailing_task = None
        if self._revive_task is not None:
            self._revive_task.cancel()
            self._revive_task = None
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

    def delivery_stats(self) -> dict[str, Any]:
        """Счётчики очереди доставки — для админки и /api/health.

        Кроме сумм здесь лежит разбивка пропусков (``skips``): «отфильтровано»
        и «нет подписки» выглядят одинаково в общем счётчике, а лечатся
        по-разному.
        """
        return delivery_queue.stats()

    async def list_dialogs(self, account_id: int, limit: int = 0) -> list[dict[str, Any]]:
        """Чаты аккаунта: для выбора источника, приёмника и получателей.

        ``limit <= 0`` — отдать все чаты, сколько их у аккаунта есть. Именно это
        нужно кабинету: постить и рассылать можно в любое число чатов, а список,
        обрезанный на двухсотом, молча прятал бы остальные — человек не находил
        чат поиском и считал, что задача его «не видит».

        Полный обход кладём в кэш на ``DIALOGS_CACHE_TTL``: один обход — это
        череда запросов к Telegram, а спрашивают список часто.
        """
        if not settings.mtproto_ready:
            return []
        client = self._clients.get(account_id)
        if client is None:
            return []

        cached = self._dialogs_cache.get(account_id)
        if cached is not None and time.time() - cached[0] < DIALOGS_CACHE_TTL:
            return cached[1][:limit] if limit > 0 else list(cached[1])

        result: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs(limit=limit if limit > 0 else None):
            entity = dialog.entity
            title = getattr(entity, "title", None) or getattr(entity, "first_name", None) or "Без имени"
            result.append(
                {
                    "id": dialog.id,
                    "title": title,
                    # Ник отдаём кабинету: по нему чат подписан в списке, и выбор
                    # мышью уезжает в задачу как @username, а не «голым» id —
                    # такую ссылку resolve_chat находит и без списка диалогов.
                    "username": getattr(entity, "username", None) or "",
                    "is_channel": bool(getattr(entity, "broadcast", False)),
                    "is_group": bool(getattr(entity, "megagroup", False)),
                }
            )
        # В кэш идёт только полный обход: обрезанным списком потом ответили бы на
        # запрос «все чаты», и часть чатов пропала бы на целую минуту.
        if limit <= 0:
            self._dialogs_cache[account_id] = (time.time(), list(result))
        return result

    def forget_dialogs(self, account_id: int | None = None) -> None:
        """Забыть кэш чатов: вступили в новый чат — он должен появиться сразу."""
        if account_id is None:
            self._dialogs_cache.clear()
        else:
            self._dialogs_cache.pop(account_id, None)

    async def resolve_chat(self, account_id: int, query: str) -> tuple[int, str] | None:
        """Находит чат по @username, ссылке t.me, числовому id или названию.

        Возвращает (id, название). Один запрос — частный случай пачки, поэтому
        поиск живёт в ``resolve_many``: иначе две копии разбора ссылок разошлись
        бы при первой же правке.
        """
        found = await self.resolve_many(account_id, [query])
        return found.get((query or "").strip())

    async def resolve_many(
        self, account_id: int, queries: Sequence[str]
    ) -> dict[str, tuple[int, str]]:
        """Находит сразу все чаты из списка: {запрос → (id, название)}.

        Ключ ответа — исходный запрос со снятыми пробелами, чтобы вызывающий
        сразу видел, что именно не нашлось. Ненайденных в ответе просто нет.

        Смысл пачки — один обход диалогов на весь список вместо обхода на каждую
        ссылку. Задача «постить в 200 чатов» иначе стоила бы 200 обходов подряд
        при сохранении: минуты ожидания в кабинете и FloodWait в конце. Порядок
        поиска: сначала указатель по диалогам (там уже лежат все чаты аккаунта,
        и его строим один раз), и только для незнакомых ссылок — запрос к
        Telegram по одной.
        """
        if not settings.mtproto_ready:
            return {}
        client = self._clients.get(account_id)
        if client is None:
            return {}

        wanted: list[tuple[str, str, int | None]] = []
        for raw_query in queries:
            key = (raw_query or "").strip()
            ref = _normalize_ref(key)
            if not key or not ref:
                continue
            wanted.append((key, ref, int(ref) if ref.lstrip("-").isdigit() else None))
        if not wanted:
            return {}

        found: dict[str, tuple[int, str]] = {}
        dialogs = await self.list_dialogs(account_id)
        by_id = {int(chat["id"]): chat for chat in dialogs}
        by_username = {
            str(chat["username"]).lower(): chat for chat in dialogs if chat.get("username")
        }
        by_title = {str(chat["title"]).strip().lower(): chat for chat in dialogs if chat.get("title")}

        rest: list[tuple[str, str, int | None]] = []
        for key, ref, numeric in wanted:
            chat = by_id.get(numeric) if numeric is not None else by_username.get(ref.lower())
            if chat is None and numeric is None:
                chat = by_title.get(ref.lower())
            if chat is not None:
                found[key] = (int(chat["id"]), str(chat["title"]))
            else:
                rest.append((key, ref, numeric))

        for key, ref, numeric in rest:
            pair = await self._resolve_by_api(client, ref, numeric)
            if pair is None and numeric is None:
                # Последняя попытка — часть названия: так чат ищут словами
                # («афиша»), когда ни ника, ни id под рукой нет.
                match = next(
                    (chat for chat in dialogs if ref.lower() in str(chat["title"]).lower()), None
                )
                if match is not None:
                    pair = (int(match["id"]), str(match["title"]))
            if pair is not None:
                found[key] = pair
        return found

    async def _resolve_by_api(
        self, client: TelegramClient, ref: str, numeric: int | None
    ) -> tuple[int, str] | None:
        """Спрашивает Telegram про один чат: по нику или по числовому id.

        Нужно для чатов, которых нет в списке диалогов: аккаунт может иметь
        доступ к каналу (быть участником или админом), но чат не попадает в
        недавние — тогда только прямой запрос и находит его.
        """
        candidates: list[Any] = [ref] if numeric is None else [numeric]
        if numeric is not None and numeric < 0:
            # id канала ходит как -100<channel_id>; PeerChannel ждёт channel_id
            candidates.append(numeric + 1000000000000)
        for candidate in candidates:
            try:
                entity = await client.get_entity(candidate)
            except Exception:  # noqa: BLE001 — не нашлось: пробуем следующий вид ссылки
                continue
            title = (
                getattr(entity, "title", None)
                or getattr(entity, "first_name", None)
                or str(getattr(entity, "id", "?"))
            )
            return int(getattr(entity, "id")), title
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
        by_id: dict[int, RuleSnapshot] = {}
        for rule in rules:
            snapshot = _snapshot(rule)
            by_id[snapshot.id] = snapshot
            # Ручные задачи (парсер) запускаются только по кнопке, а постеры —
            # по расписанию. Слушать входящие сообщения тем и другим незачем:
            # иначе каждое сообщение в источнике звало бы run_job и писало
            # «Неизвестный тип задачи».
            if snapshot.kind in MANUAL_ONLY_KINDS or snapshot.kind in SCHEDULED_KINDS:
                continue
            if snapshot.kind in FLOATING_KINDS:
                floating.setdefault(rule.account_id, []).append(snapshot)
            else:
                fresh.setdefault((rule.account_id, rule.source_id), []).append(snapshot)
        # Авто-постеры и рассылки собираем отдельно — они живут по расписанию, а
        # не по входящим сообщениям, поэтому в _rules не попадают (иначе входящее
        # сообщение в приёмнике случайно бы «подхватило» постер).
        fresh_posters: list[RuleSnapshot] = []
        fresh_mailings: list[RuleSnapshot] = []
        for rule in rules:
            if rule.kind not in SCHEDULED_KINDS or not rule.enabled or rule.archived:
                continue
            snapshot = _snapshot(rule)
            if rule.kind == "mailing":
                fresh_mailings.append(snapshot)
            else:
                fresh_posters.append(snapshot)

        async with self._lock:
            self._rules = fresh
            self._floating_rules = floating
            self._rules_by_id = by_id
            self._poster_rules = fresh_posters
            self._mailing_rules = fresh_mailings
            # Правило выключили или удалили — состояние планировщика ему больше
            # не нужно. Иначе словари растут весь uptime процесса, а номер
            # удалённой задачи SQLite отдаёт следующей созданной: та получала
            # чужую очередь чатов и чужой текст в первом же круге, а заодно
            # чужую паузу после FloodWait. Выключенная задача круг начинает
            # заново — это дешевле, чем помнить её место неделю.
            for state, live in (
                (self._poster_state, {snapshot.id for snapshot in fresh_posters}),
                (self._mailing_state, {snapshot.id for snapshot in fresh_mailings}),
            ):
                for rule_id in [key for key in state if key not in live]:
                    state.pop(rule_id, None)

    # ─────────────────────────── Авто-постер (планировщик) ───────────────────────────

    async def _poster_loop(self) -> None:
        """Фоновый цикл авто-постера: раз в 20 сек проверяет расписание."""
        while True:
            try:
                await asyncio.sleep(20)
                await self._poster_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — планировщик не должен падать
                logger.exception("Постер-планировщик упал: {}", exc)

    async def _poster_tick(self) -> None:
        """Один проход: каждому постеру, которому пора, — очередное сообщение.

        Что отправлять, постинг берёт из библиотеки — там же, где рассылка: в
        задаче лежат только ссылки на записи, а перечитываются они на каждом
        проходе, поэтому правка текста в библиотеке доходит до чатов сразу.

        Чатов у постера может быть сколько угодно, поэтому круг идёт не залпом:
        за проход правило отправляет не больше ``POSTER_BATCH`` сообщений, между
        чатами держит паузу, а весь проход ограничен ``POSTER_TICK_BUDGET`` —
        иначе задача с сотней чатов заняла бы цикл целиком, и остальные постеры
        ждали бы её. Недоотправленные чаты остаются в очереди правила и уйдут на
        следующих тиках; интервал отсчитывается от конца круга, а не от первой
        отправки, — иначе круги наезжали бы друг на друга.
        """
        from app.telegram_client.jobs import (
            POSTER_BATCH,
            POSTER_CHAT_GAP,
            POSTER_TICK_BUDGET,
            MailingMessageGone,
            chat_recipients,
            load_mailing_library,
            mailing_pick,
            mailing_send,
            own_text_item,
            window_now_sec,
            window_tz_minutes,
        )

        deadline = time.time() + POSTER_TICK_BUDGET

        async with self._lock:
            rules = list(self._poster_rules)

        for rule in rules:
            if not rule.enabled or rule.archived:
                continue
            client = self._clients.get(rule.account_id)
            if client is None or not client.is_connected():
                continue

            f = rule.filters
            chats = chat_recipients(rule)
            if not chats:
                continue
            start = _hhmm_to_sec(f.window_start if hasattr(f, "window_start") else "00:00")
            end = _hhmm_to_sec(f.window_end if hasattr(f, "window_end") else "23:59")
            # Окно сверяем с часами хозяина задачи, а не сервера: сервер стоит в
            # UTC, и московское «окно 10:00–20:00» работало на нём 13:00–23:00 по
            # Москве — последний круг уходил людям в полночь. Смещение задача
            # хранит рядом с окном; у задач до этой настройки его нет, и окно
            # остаётся по часам сервера (кабинет так и пишет).
            now_sec = window_now_sec(window_tz_minutes(getattr(f, "window_tz", None)))
            if not _in_window(now_sec, start, end):
                continue

            interval = max(30, int(getattr(f, "interval_seconds", 120)))
            st = self._poster_state.setdefault(
                rule.id,
                {"last": 0.0, "idx": 0, "step": 0, "runs": 0, "not_before": 0.0, "queue": []},
            )
            # Пауза, которую назначил сам Telegram после FloodWait: раньше этого
            # времени не пробуем, иначе получаем отказ по кругу.
            if time.time() < st.get("not_before", 0.0):
                continue
            # Пора? Начатый круг — всегда, закрытый — когда прошёл интервал.
            # Спрашиваем до чтения библиотеки: тик идёт раз в 20 секунд, и лишний
            # запрос на каждую задачу в каждом проходе ничем не оправдан.
            if not st.get("queue") and time.time() - st["last"] < interval:
                continue

            # Что постить: записи библиотеки — те же, что у рассылки. Старая
            # задача держит копии текстов в своих настройках (messages): для неё
            # библиотеку не читаем, иначе задача, созданная до переезда,
            # замолчала бы. В библиотеку её текст переедет при первой правке.
            legacy = list(getattr(f, "messages", None) or [])
            if legacy and not f.library_ids:
                items: list[Any] = [own_text_item(text) for text in legacy]
            else:
                items = await load_mailing_library(rule.user_id, f.library_ids)
            if not items:
                # Записи удалили из библиотеки, а задача на них ссылается —
                # отправлять нечего. Причину пишем в журнал один раз на простой:
                # строка на каждом тике утопила бы карточку в одинаковых сбоях, а
                # без неё задача бодро «работает» и в чаты ничего не уходит.
                if not st.get("empty"):
                    st["empty"] = True
                    await self._poster_nothing_to_send(rule)
                continue
            st["empty"] = False
            if not st.get("queue"):
                # Новый круг: сообщение фиксируем на весь обход, иначе половина
                # чатов получила бы один текст, половина — следующий. Держим не
                # текст, а номер по очереди: сам текст лежит в библиотеке и
                # перечитывается на каждом тике — значит правка записи доходит и
                # до тех чатов круга, которые ещё не получили пост.
                st["queue"] = list(chats)
                st["step"] = st["idx"]
                st["idx"] = (st["idx"] + 1) % len(items)

            item = mailing_pick(items, int(st.get("step", 0)))
            sent = 0
            # Чаты, которые круг потерял: о них человек узнаёт из карточки задачи,
            # поэтому итог прохода уходит в журнал (см. jobs.record_batch).
            failed: list[str] = []
            while st["queue"] and sent < POSTER_BATCH and time.time() < deadline:
                chat_id = st["queue"][0]
                try:
                    # Отправка общая с рассылкой: свои тексты и сохранённые посты
                    # уходят одним путём, поэтому пост с медиа постинг тоже умеет.
                    await mailing_send(client, rule, item, chat_id)
                except FloodWaitError as exc:
                    # Telegram явно говорит, сколько ждать. Слушаемся: иначе на
                    # следующем тике тот же отказ и поток предупреждений в журнале.
                    # Чат остаётся в очереди — круг продолжится после паузы.
                    wait = int(getattr(exc, "seconds", 30)) + 1
                    st["not_before"] = time.time() + wait
                    logger.warning(
                        "Постер #{}: Telegram просит подождать {} сек — ставлю паузу",
                        rule.id,
                        wait,
                    )
                    break
                except MailingMessageGone as exc:
                    # Запись круга ссылается на пост, которого больше нет: в
                    # остальные чаты он тоже не уйдёт. Круг закрываем и пишем
                    # причину один раз — иначе каждый чат отметился бы отдельным
                    # сбоем, а на карточке стояло бы «не ушло в сто чатов».
                    logger.warning("Постер #{}: {}", rule.id, exc)
                    st["queue"].clear()
                    await self._nothing_to_send(rule, f"постить нечего: {exc}")
                    break
                except Exception as exc:  # noqa: BLE001 — не спамим при ошибке
                    # Недоступный чат выкидываем из круга: иначе он держал бы
                    # очередь и остальные чаты не получили бы ничего.
                    logger.warning("Постер #{} не отправил в {}: {}", rule.id, chat_id, exc)
                    failed.append(f"{chat_id}: {type(exc).__name__}")
                    st["queue"].pop(0)
                    continue
                st["queue"].pop(0)
                st["runs"] += 1
                sent += 1
                if st["queue"] and time.time() < deadline:
                    await asyncio.sleep(POSTER_CHAT_GAP)

            # Счётчик отправок и итог прохода — одним заходом в базу: по нему в
            # кабинете видно и работу задачи, и потерянные чаты.
            await record_batch(rule, sent=sent, failed=failed)
            if not st["queue"]:
                # Круг закрыт — интервал считаем от него, а не от начала обхода.
                st["last"] = time.time()

    # ───────────────── Рассылка по чатам (планировщик, свой цикл) ─────────────────

    async def _mailing_loop(self) -> None:
        """Фоновый цикл рассылок: тикает раз в секунду, чтобы держать паузы."""
        while True:
            try:
                await asyncio.sleep(MAILING_TICK_SECONDS)
                await self._mailing_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — планировщик не должен падать
                logger.exception("Планировщик рассылок упал: {}", exc)

    async def _mailing_tick(self) -> None:
        """Один проход: каждой рассылке, которой пора, — по одному сообщению.

        За проход уходит не больше одного сообщения на правило: темп задают
        паузы (``gap_seconds`` между получателями, ``cycle_seconds`` между
        кругами), а не скорость цикла. Позиция в списке получателей живёт в
        состоянии планировщика, но выводится из ``forwarded_count`` — значит
        после перезапуска процесса рассылка продолжает с того же чата, а не
        начинает круг заново.
        """
        from app.telegram_client.jobs import (
            load_mailing_library,
            mailing_gap,
            mailing_next_due,
            mailing_pick,
            mailing_position,
            mailing_recipients,
            mailing_send,
        )

        now = time.time()
        async with self._lock:
            rules = list(self._mailing_rules)

        for rule in rules:
            if not rule.enabled or rule.archived:
                continue
            client = self._clients.get(rule.account_id)
            if client is None or not client.is_connected():
                continue

            recipients = mailing_recipients(rule)
            if not recipients:
                continue

            st = self._mailing_state.setdefault(
                rule.id,
                {
                    "pos": None,
                    "cycle": 0,
                    "due": 0.0,
                    "not_before": 0.0,
                    "checked": 0.0,
                    "allowed": False,
                },
            )
            # not_before — пауза после FloodWait или сбоя; due — плановое время
            # следующей отправки (0 означает «ещё не отправляли»).
            if now < st["not_before"] or (st["due"] and now < st["due"]):
                continue

            if now - st["checked"] >= SUBSCRIPTION_CHECK_TTL:
                st["checked"] = now
                st["allowed"] = await subscription_active(rule.user_id)
            if not st["allowed"]:
                continue

            if st["pos"] is None:
                st["pos"], st["cycle"] = mailing_position(
                    rule.forwarded_count, len(recipients)
                )

            repeats = max(0, int(rule.filters.repeats or 0))
            if repeats and st["cycle"] >= repeats:
                await self._finish_mailing(rule, st["cycle"])
                continue

            target_id = recipients[st["pos"] % len(recipients)]
            try:
                items = await load_mailing_library(rule.user_id, rule.filters.library_ids)
                item = mailing_pick(
                    items,
                    st["cycle"] * len(recipients) + st["pos"],
                    random_pick=bool(rule.filters.random_pick),
                )
                if item is None:
                    st["not_before"] = now + MAILING_EMPTY_PAUSE
                    logger.warning(
                        "Рассылка #{}: в библиотеке нет сообщений — пауза {} сек",
                        rule.id,
                        int(MAILING_EMPTY_PAUSE),
                    )
                    # Причина видна только в логе службы, до которого человеку не
                    # добраться, — а на карточке задача бодро «работает». Пишем её
                    # в журнал один раз на простой: строка каждую минуту утопила
                    # бы карточку в одинаковых сбоях.
                    if not st.get("empty"):
                        st["empty"] = True
                        await self._mailing_nothing_to_send(rule)
                    continue
                st["empty"] = False
                await mailing_send(client, rule, item, target_id)
            except FloodWaitError as exc:
                # Telegram явно сказал, сколько ждать, — слушаемся, иначе на
                # следующем тике тот же отказ и поток предупреждений в журнале.
                wait = int(getattr(exc, "seconds", 30)) + 1
                st["not_before"] = time.time() + wait
                logger.warning("Рассылка #{}: Telegram просит подождать {} сек", rule.id, wait)
                continue
            except Exception as exc:  # noqa: BLE001 — одна рассылка не роняет цикл
                # Отказ по одному получателю (нет прав, чат удалён, сеть) — в
                # журнал: иначе задача бодро «работает», сообщения не приходят, а
                # причина видна только в логе службы на сервере.
                await self._mailing_failed(rule, st, target_id, exc)
                continue

            st["pos"] += 1
            cycle_closed = st["pos"] >= len(recipients)
            if cycle_closed:
                st["pos"] = 0
                st["cycle"] += 1
            st["due"] = mailing_next_due(
                st["due"],
                time.time(),
                mailing_gap(rule.filters, cycle=cycle_closed),
            )

            # Итог отправки — тем же способом, каким его пишет постер: счётчик,
            # запись в журнале и, если не ушло, причина для карточки.
            await record_batch(rule, sent=1, target_id=target_id)
            logger.debug("Рассылка #{}: отправлено в {}", rule.id, target_id)

    async def _mailing_failed(
        self, rule: RuleSnapshot, state: dict, target_id: int, exc: BaseException
    ) -> None:
        """Один получатель не принял сообщение: пишем сбой и держим паузу.

        Пауза нужна, чтобы не долбиться в тот же чат каждую секунду, а запись в
        журнале — чтобы сбой было видно на карточке задачи, а не только в логе
        службы, до которого человеку не добраться.
        """
        logger.warning(
            "Рассылка #{}: не ушло в {}: {}: {}",
            rule.id,
            target_id,
            type(exc).__name__,
            exc,
        )
        await record_batch(rule, failed=[f"{target_id}: {type(exc).__name__}"])
        state["not_before"] = time.time() + MAILING_ERROR_PAUSE

    async def _mailing_nothing_to_send(self, rule: RuleSnapshot) -> None:
        """Рассылке нечего отправлять — говорим об этом на карточке задачи.

        Так бывает после уборки в библиотеке: сообщения удалили, а рассылка
        осталась и ссылается на то, чего уже нет. Раньше про это знал только лог
        службы, до которого человеку не добраться: карточка показывала
        «работает», журнал был пуст, и человек ждал сообщений, которых не будет.
        """
        await self._nothing_to_send(
            rule, "рассылать нечего: в библиотеке не осталось сообщений"
        )

    async def _poster_nothing_to_send(self, rule: RuleSnapshot) -> None:
        """То же для постинга: его тексты лежат в той же библиотеке."""
        await self._nothing_to_send(
            rule, "постить нечего: в библиотеке не осталось сообщений"
        )

    async def _nothing_to_send(self, rule: RuleSnapshot, error: str) -> None:
        """Запись в журнал задачи о простое: отправлять нечего, и вот почему."""
        async with session_scope() as session:
            await repo.log_forward(
                session,
                rule_id=rule.id,
                user_id=rule.user_id,
                # У расписанных задач нет входящего сообщения: они его создают.
                source_msg_id=0,
                target_msg_id=None,
                status="error",
                error=error,
            )

    async def _finish_mailing(self, rule: RuleSnapshot, cycles: int) -> None:
        """Останавливает рассылку, сделавшую заданное число кругов.

        Задача остаётся в списке, но снятой с паузы не считается: «работает» на
        карточке было бы враньём. Снять паузу можно вручную — тогда прогресс
        обнуляется (см. ``/api/tasks/{id}/toggle``).
        """
        logger.info("Рассылка #{}: сделала {} круг(ов) — останавливаю", rule.id, cycles)
        async with session_scope() as session:
            db_rule = await repo.get_rule(session, rule.id, rule.user_id)
            if db_rule is not None:
                db_rule.enabled = False
        await self.refresh_rules()

    def rules_for(self, account_id: int, chat_id: int) -> list[RuleSnapshot]:
        return self._rules.get((account_id, chat_id), [])

    # ───────────────────────────── Разовые задачи ─────────────────────────────

    async def run_task_now(self, rule) -> dict:
        """Запускает разовую задачу (парсер, автоподписка) на живом клиенте.

        Возвращает сводку запуска: {"ok": bool, ...} — и её же кладёт в журнал
        задачи. Здесь единственное место, куда сходятся оба запуска (кнопка в
        кабинете и первый запуск при создании), поэтому и запись одна на оба.
        """
        from app.telegram_client.jobs import record_oneshot, run_oneshot

        snapshot = _snapshot(rule)
        client = self._clients.get(rule.account_id)
        if client is None or not client.is_connected():
            result = {
                "ok": False,
                "error": "Аккаунт не в сети. Перезапустите его в боте и повторите запуск.",
            }
        else:
            try:
                result = await run_oneshot(client, snapshot)
            except Exception as exc:  # noqa: BLE001 — результат нужен в API, а не в лог
                logger.exception("Задача #{} не выполнилась", rule.id)
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        # Журнал не должен уронить запуск: сводку человек уже ждёт в ответе.
        try:
            await record_oneshot(snapshot, result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Запуск задачи #{} не попал в журнал: {}", rule.id, exc)
        return result

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
