"""Задачи, отличные от обычной пересылки.

Тип задачи лежит в ``Rule.kind``. Всё, что не ``forward``, приходит сюда из
``forwarder.deliver`` — на том же контракте: подключённый Telethon-клиент,
сообщение и снимок правила. Классическая пересылка от этого не меняется.

Задачи делятся на три класса:

* **потоковые** — реагируют на каждое сообщение (broadcast, baiting, mute,
  dialogs, checks, autosubscribe);
* **разовые** — запускаются по команде пользователя и сразу отдают результат
  (parser, autosubscribe);
* **расписанные** — живут в планировщике ``manager`` и сами решают, когда
  отправлять (poster, mailing); входящие сообщения им не нужны.
"""
from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Awaitable, Sequence

from loguru import logger
from telethon.errors import FloodWaitError, RPCError

from app.db import repo
from app.db.database import SessionLocal
from app.telegram_client.filters import FilterConfig, message_text, transform_text
from app.telegram_client.forwarder import send_copy, subscription_active
from app.telegram_client.types import RuleSnapshot

# Слушают все чаты аккаунта, а не один источник: у ЛС нет фиксированного chat_id
FLOATING_KINDS: tuple[str, ...] = ("dialogs",)

# Запускаются вручную и сразу возвращают результат
ONE_SHOT_KINDS: tuple[str, ...] = ("parser", "autosubscribe")

# Запускаются ТОЛЬКО вручную: у таких задач нет обработчика входящих сообщений,
# поэтому они не должны попадать в кэш «слушающих» правил. Иначе каждое
# сообщение в источнике звало бы run_job и писало «Неизвестный тип задачи».
MANUAL_ONLY_KINDS: tuple[str, ...] = ("parser",)

# Живут по расписанию планировщика, а не по входящим сообщениям
SCHEDULED_KINDS: tuple[str, ...] = ("poster", "mailing")

# Складывают находки в collected_items — у них есть кнопка «Результаты»
COLLECTING_KINDS: tuple[str, ...] = ONE_SHOT_KINDS + ("checks",)

KIND_LABELS: dict[str, str] = {
    "forward": "пересылка",
    # «пересылка в чаты», а не «рассылка»: свои сообщения по чатам шлёт mailing,
    # и два одинаковых слова в списке задач читались как одна команда-двойник.
    "broadcast": "пересылка в чаты",
    "baiting": "байтинг",
    "mute": "мут",
    "dialogs": "уведомления из диалогов",
    "checks": "ловец чеков",
    "parser": "парсер аудитории",
    "autosubscribe": "автоподписка",
    # Постинг и рассылка обе шлют ваш текст по чатам, поэтому в ярлык вынесено
    # отличие: у постинга расписание, у рассылки обход чатов по одному.
    "poster": "постинг по расписанию",
    "mailing": "рассылка по очереди",
}

# Ссылки на подарки и чеки, которые ищет «ловец чеков»
GIFT_LINK_RE = re.compile(
    r"(?:https?://)?(?:t|telegram)\.me/(?:giftcode|gifts|nft)/[A-Za-z0-9_\-]+",
    re.IGNORECASE,
)

# Любая ссылка на чат: t.me/username, t.me/+invite, t.me/joinchat/hash
CHAT_LINK_RE = re.compile(
    r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z0-9_+\-/]{5,})",
    re.IGNORECASE,
)

# Служебные пути t.me, которые заведомо не являются чатом. Сравнивается только
# первая часть пути: t.me/addlist/xxx — папка, t.me/c/123/45 — ссылка на сообщение.
NOT_A_CHAT_LINK = frozenset(
    {
        "giftcode", "gifts", "nft",       # подарки и чеки — это не чаты
        "s", "share", "iv", "c", "m",     # расшаренные посты и сообщения
        "proxy", "socks",                 # прокси
        "addstickers", "addemoji", "addlist", "addtheme",  # наборы и папки
        "login", "confirmphone", "boost", # служебные переходы
    }
)

MAX_PARSER_LIMIT = 10_000

# Отказы Telegram, которые не значат «не получилось»: в чате мы и так есть, а
# заявку уже отправили и её рассматривает админ. Исправлять человеку нечего,
# поэтому такие ответы не попадают в причины сбоя на карточке.
JOIN_ALREADY_FINE: frozenset[str] = frozenset(
    {"UserAlreadyParticipantError", "InviteRequestSentError"}
)

# Пауза между вступлениями: без неё Telegram быстро отвечает «подождите».
JOIN_PAUSE_SECONDS = 2


@dataclass
class JoinOutcome:
    """Что вышло из захода в чаты: вступили, уже были, не пустили.

    Одного числа не хватало: «вступили в 0 из 5» человек читал как поломку, хотя
    в четырёх чатах аккаунт уже сидел, а в пятый его не пустил админ — и об этом
    знал только лог службы на сервере.
    """

    joined: int = 0
    already: int = 0
    problems: list[str] = field(default_factory=list)


def task_title(rule: Any) -> str:
    """Заголовок задачи. У разных типов разная геометрия, а не только «A → B»."""
    kind = getattr(rule, "kind", None) or "forward"
    filters = getattr(rule, "filters", None)
    filters = filters if isinstance(filters, dict) else {}
    source = getattr(rule, "source_title", None) or str(getattr(rule, "source_id", "") or "")
    target = getattr(rule, "target_title", None) or str(getattr(rule, "target_id", "") or "")

    if kind == "parser":
        return f"Парсер аудитории: {source}"
    if kind == "autosubscribe":
        channels = len(filters.get("subscribe_to") or [])
        if channels:
            return f"Автоподписка: {channels} кан." + (f" из «{source}»" if source else "")
        return f"Автоподписка: {source or 'все чаты аккаунта'}"
    if kind == "dialogs":
        return f"Уведомления из ЛС → {target}"
    if kind == "checks":
        return f"Ловец чеков: {source} → {target}"
    if kind == "broadcast":
        total = len(chat_recipients(rule))
        if total > 1:
            return f"Пересылка: {source} → {total} чат."
        return f"Пересылка: {source} → {target}"
    if kind in ("baiting", "mute"):
        watched = int(filters.get("target_user_id") or 0)
        head = "Байтинг в" if kind == "baiting" else "Мут в"
        return f"{head} {source}" + (f" · за {watched}" if watched else "")
    # Постинг и рассылка идут в любое число чатов, поэтому в заголовке — счёт,
    # а имя чата показываем только когда он один: перечислять двести имён некуда.
    if kind == "poster":
        total = len(chat_recipients(rule))
        if total > 1:
            return f"Постинг по расписанию: {total} чат."
        return f"Постинг по расписанию → {target}" if target else "Постинг по расписанию"
    if kind == "mailing":
        total = len(chat_recipients(rule))
        if total > 1:
            return f"Рассылка по очереди: {total} чат."
        return f"Рассылка по очереди → {target}" if target else "Рассылка по очереди"
    return f"{source} → {target}"


# ─────────────────────────────── Разбор сообщений ─────────────────────────────


def _normalize_filters(rule: RuleSnapshot) -> None:
    """Приводит настройки правила к FilterConfig.

    Снимок из БД всегда несёт FilterConfig, но обработчики вызывают и вручную
    собранными правилами, где легко передать обычный словарь из JSON. Пусть
    здесь будет одна точка приведения, а не проверки в каждом обработчике.
    """
    if not isinstance(rule.filters, FilterConfig):
        rule.filters = FilterConfig.from_dict(rule.filters or {})


async def run_job(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Проводит одно сообщение через задачу, отличную от пересылки."""
    # Служебные сообщения (вступления, смена аватара) нечего reacting/удалять
    if getattr(message, "action", None) is not None:
        return

    _normalize_filters(rule)

    if not await subscription_active(rule.user_id):
        logger.debug("Задача #{}: у пользователя нет активной подписки", rule.id)
        return

    handler = _HANDLERS.get(rule.kind)
    if handler is None:
        # Задачи ручного запуска и работающие по расписанию сюда долетать не
        # должны: их отсеивает refresh_rules. Если всё же долетели — это не
        # ошибка правила, а неверная маршрутизация: пишем в debug, чтобы не
        # засорять журнал и не пугать пользователя красными ошибками.
        if rule.kind in MANUAL_ONLY_KINDS or rule.kind in SCHEDULED_KINDS:
            logger.debug(
                "Задача #{} ({}): запускается не по сообщениям — пропускаю",
                rule.id,
                rule.kind,
            )
            return
        await record_error(rule, message, f"Неизвестный тип задачи: {rule.kind}")
        return

    if rule.delay_seconds > 0:
        await asyncio.sleep(rule.delay_seconds)

    try:
        await handler(client, message, rule)
    except FloodWaitError as exc:
        wait = int(getattr(exc, "seconds", 5)) + 1
        logger.warning("Задача #{} ({}): FloodWait {} сек — ждём", rule.id, rule.kind, wait)
        await asyncio.sleep(wait)
        try:
            await handler(client, message, rule)
        except RPCError as retry_exc:
            await record_error(rule, message, f"FloodWait повторно: {retry_exc}")
    except RPCError as exc:
        await record_error(rule, message, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 — одна задача не должна ронять аккаунт
        await record_error(rule, message, f"{type(exc).__name__}: {exc}")


def _matches_keywords(config_keywords: list[str], text: str) -> bool:
    """Пустой список ключевых слов означает «подходит любое сообщение»."""
    words = [word.strip().lower() for word in (config_keywords or []) if word and word.strip()]
    if not words:
        return True
    lowered = (text or "").lower()
    return any(word in lowered for word in words)


def _sender_matches(rule: RuleSnapshot, message: Any) -> bool:
    """Проверяет, что сообщение от того человека, за которым следит задача."""
    watched = int(rule.filters.target_user_id or 0)
    if not watched:
        return True
    return int(getattr(message, "sender_id", 0) or 0) == watched


# ─────────────────────────────────── Задачи ──────────────────────────────────


def _conf_value(config: Any, name: str, default: Any) -> Any:
    """Настройка и из FilterConfig, и из «сырого» словаря правила.

    Снимок правила несёт FilterConfig, а строка БД — обычный JSON-словарь.
    Считалкам нужно одно и то же значение, и вторая копия расчёта под словарь
    рано или поздно расходится с первой.
    """
    value = config.get(name, default) if isinstance(config, dict) else getattr(config, name, default)
    return default if value is None else value


def chat_recipients(rule: Any) -> list[int]:
    """Все чаты задачи: приёмник правила плюс дополнительные из настроек.

    Одна геометрия на все задачи «в несколько чатов» — пересылку (broadcast),
    авто-постинг и рассылку: первый чат живёт в обязательной колонке
    ``target_id``, остальные — в ``filters.targets``. Числа их не ограничивает:
    сколько чатов человек отметил, столько и вернётся.

    Повторы убираем (иначе один чат получил бы сообщение дважды за круг), а
    источник исключаем — пересылать пост в тот же канал, откуда он взят, значит
    зациклить задачу. Принимает и снимок правила, и строку БД.
    """
    extra = _conf_value(getattr(rule, "filters", None), "targets", []) or []
    source_id = int(getattr(rule, "source_id", 0) or 0)
    seen: list[int] = []
    for candidate in [getattr(rule, "target_id", 0), *extra]:
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value and value != source_id and value not in seen:
            seen.append(value)
    return seen


# ── Рассылка по чатам (kind="mailing"): считалки для планировщика ──
#
# Логика вынесена из планировщика отдельными чистыми функциями: их видно в
# тестах без Telethon, а планировщик остаётся про порядок вызовов.

MAILING_MIN_GAP = 1  # быстрее секунды между чатами Telegram всё равно не даст
MAILING_MAX_GAP = 7 * 24 * 3600  # неделя: дальше это уже не «пауза», а ошибка ввода

# ── Авто-постинг (kind="poster"): темп обхода чатов ──
#
# Постер шлёт своё сообщение сразу во все выбранные чаты, а чатов может быть
# сколько угодно. Поэтому круг идёт не залпом: за один тик уходит не больше
# POSTER_BATCH сообщений, между чатами держится пауза, а весь проход
# планировщика ограничен POSTER_TICK_BUDGET — иначе одна задача с сотней чатов
# заняла бы цикл целиком и остальные постеры стояли бы в очереди.
POSTER_CHAT_GAP = 2.0
POSTER_BATCH = 8
POSTER_TICK_BUDGET = 15.0

# ── Окно постинга: чьи это часы ──
#
# Окно «с 10:00 до 20:00» человек задаёт по своим часам — он им и объявления
# рассылает. Сервер же живёт по своим: наш стоит в UTC, и московское «окно
# 10:00–20:00» превращалось на нём в 13:00–23:00 по Москве. Смысл окна — не
# писать людям ночью — при этом терялся ровно наоборот: последний круг уходил
# в полночь.
#
# Поэтому рядом с окном задача хранит смещение хозяина от UTC в минутах
# (``window_tz``: Москва — 180, Нью-Йорк — −240), и планировщик сверяется с
# часами хозяина. У задач, созданных до этого, смещения нет: их окно так и
# остаётся по часам сервера, а кабинет об этом честно говорит — иначе правка
# сдвинула бы время рассылки у тех, кто уже подобрал окно под серверные часы.
WINDOW_TZ_LIMIT = 14 * 60  # дальше UTC±14 часовых поясов на Земле не бывает
SECONDS_IN_DAY = 24 * 3600


def window_tz_minutes(value: Any) -> int | None:
    """Смещение хозяина задачи от UTC в минутах. Непонятное значение → ``None``.

    ``None`` означает «часы сервера»: так работали все задачи до появления
    смещения, и так же остаётся, если кабинет прислал ерунду вместо часового
    пояса. Выдумывать за человека пояс нельзя — окно поехало бы молча.
    """
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    if abs(minutes) > WINDOW_TZ_LIMIT:
        return None
    return minutes


def window_now_sec(tz_minutes: int | None, now: float | None = None) -> int:
    """Сколько секунд прошло с полуночи там, где живёт хозяин задачи.

    Смещение известно — считаем от UTC: эпоха Unix начинается ровно в UTC-полночь,
    поэтому остаток от суток и есть время на часах со сдвигом. Смещения нет —
    остаются часы сервера, как было до появления этой настройки.
    """
    moment = time.time() if now is None else float(now)
    if tz_minutes is None:
        local = time.localtime(moment)
        return local.tm_hour * 3600 + local.tm_min * 60 + local.tm_sec
    return int((moment + tz_minutes * 60) % SECONDS_IN_DAY)


def mailing_recipients(rule: Any) -> list[int]:
    """Получатели рассылки — те же чаты задачи, что у пересылки и постинга.

    Отдельное имя оставлено ради читаемости планировщика: «получатели рассылки»
    там понятнее, чем общий ``chat_recipients``. Расчёт один — см. выше.
    """
    return chat_recipients(rule)


def mailing_gap(config: FilterConfig, *, cycle: bool = False) -> float:
    """Пауза перед следующей отправкой: базовая плюс случайная добавка.

    ``cycle=True`` — пауза перед новым кругом. Джиттер именно прибавляется
    (а не разбрасывается вокруг базы): человек задал минимум, и уходить ниже
    него — прямой путь под ограничения Telegram.
    """
    base = int(getattr(config, "cycle_seconds" if cycle else "gap_seconds", 0) or 0)
    spread = int(getattr(config, "cycle_jitter" if cycle else "gap_jitter", 0) or 0)
    gap = max(MAILING_MIN_GAP, base) + (random.uniform(0, spread) if spread > 0 else 0.0)
    return float(min(gap, MAILING_MAX_GAP))


def mailing_next_due(planned: float, now: float, gap: float) -> float:
    """Когда отправлять следующему получателю.

    Отсчёт от планового времени, а не от «сейчас»: иначе задержки RPC копятся и
    темп уползает. Но и не раньше «сейчас» — после простоя (перезапуск, FloodWait)
    накопившийся долг не должен вылиться очередью подряд.
    """
    return max(planned, now) + gap


def mailing_position(done: int, recipients: int) -> tuple[int, int]:
    """Где остановились: (номер получателя в круге, номер круга).

    Считается от ``forwarded_count``, поэтому рассылка продолжается с того же
    места после перезапуска процесса, а отдельная таблица прогресса не нужна.
    """
    if recipients <= 0:
        return 0, 0
    done = max(0, int(done or 0))
    return done % recipients, done // recipients


class MailingMessageGone(RuntimeError):
    """Сохранённое сообщение ссылается на пост, которого уже нет."""


# Сколько «печатать» перед отправкой, когда режим «печатает» включён
MAILING_TYPING_SECONDS = 2


async def load_mailing_library(
    user_id: int, library_ids: Sequence[int] | None = None
) -> list[Any]:
    """Что рассылать: сообщения из библиотеки пользователя.

    Пустой список в настройках означает «все сохранённые» — иначе человеку
    пришлось бы сначала завести библиотеку, а потом заново править задачу.
    Пустые записи (ни текста, ни ссылки на пост) отбрасываем: слать нечего.
    """
    async with SessionLocal() as session:
        if library_ids:
            items = await repo.saved_messages_by_ids(session, user_id, library_ids)
        else:
            # Список в кабинете идёт свежими вперёд, а очередь рассылки — в том
            # порядке, в каком сообщения добавляли: первым уходит первое.
            items = list(reversed(await repo.list_saved_messages(session, user_id)))
    return [
        item
        for item in items
        if (getattr(item, "text", "") or "").strip()
        or (int(getattr(item, "chat_id", 0) or 0) and int(getattr(item, "message_id", 0) or 0))
    ]


def mailing_pick(
    items: Sequence[Any], step: int, *, random_pick: bool = False
) -> Any | None:
    """Какое сообщение уходит следующим: по кругу или наугад."""
    if not items:
        return None
    if random_pick:
        return random.choice(list(items))
    return list(items)[max(0, int(step or 0)) % len(items)]


def own_text_item(text: str) -> SimpleNamespace:
    """Свой текст в виде записи библиотеки — для задач, чей текст ещё не переехал.

    Постинг раньше держал копии своих текстов в настройках задачи
    (``filters.messages``), а теперь их место — библиотека. Отправляет и те и
    другие один ``mailing_send``, а ему нужна запись с полями, а не строка:
    иначе для старых задач пришлось бы держать вторую ветку отправки.
    """
    return SimpleNamespace(id=0, title="", text=text, chat_id=0, message_id=0)


async def mailing_send(client: Any, rule: RuleSnapshot, item: Any, target_id: int) -> None:
    """Отправляет одно сохранённое сообщение в один чат.

    Ошибки не перехватываем: паузы, повторы и запись в журнал — дело
    планировщика (``manager._mailing_tick``), как и у авто-постера.
    """
    filters = rule.filters

    # Сообщение-ссылка: перечитываем пост и копируем его целиком, поэтому
    # медиа и вложенные пересылки доезжают как есть.
    message: Any = None
    chat_id = int(getattr(item, "chat_id", 0) or 0)
    message_id = int(getattr(item, "message_id", 0) or 0)
    if chat_id and message_id:
        message = await client.get_messages(chat_id, ids=message_id)
        if message is None:
            raise MailingMessageGone(
                f"сообщение {message_id} из чата {chat_id} не найдено"
            )
        text = transform_text(message_text(message), filters)
    else:
        text = transform_text(getattr(item, "text", "") or "", filters)

    async def _send() -> None:
        if message is not None:
            await send_copy(
                client, target_id, message, text, link_preview=bool(filters.link_preview)
            )
        else:
            await client.send_message(
                target_id,
                text or "",
                parse_mode=None,
                link_preview=bool(filters.link_preview),
            )

    if filters.typing:
        # «Печатает» видно в чате — так рассылка не выглядит ботом. Пауза
        # внутри блока: вышли из него — индикатор погас.
        async with client.action(target_id, "typing"):
            await asyncio.sleep(MAILING_TYPING_SECONDS)
            await _send()
        return
    await _send()


async def _broadcast(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Одно сообщение из источника уходит в несколько чатов."""
    targets = chat_recipients(rule)
    if not targets:
        await record_error(rule, message, "У рассылки нет получателей")
        return

    text = transform_text(message_text(message), rule.filters)
    sent = 0
    for target in targets:
        try:
            await send_copy(client, target, message, text)
            sent += 1
        except RPCError as exc:
            logger.warning("Рассылка #{}: не ушло в {}: {}", rule.id, target, exc)
    if sent:
        await record_ok(rule, message, count=sent)
    else:
        await record_error(rule, message, "Сообщение не удалось доставить ни в один чат")


async def _baiting(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Ставит реакцию на сообщения нужного человека."""
    if not _sender_matches(rule, message):
        return

    reaction = (rule.filters.reaction or "").strip()
    if not reaction:
        await record_error(rule, message, "Не задана реакция для байтинга")
        return

    await client.send_reaction(message.chat_id, message.id, reaction)
    await record_ok(rule, message)


async def _mute(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Удаляет сообщения нужного человека в общем чате."""
    if not _sender_matches(rule, message):
        return
    if not _matches_keywords(rule.filters.keywords, message_text(message)):
        return

    await client.delete_messages(message.chat_id, [message.id])
    await record_ok(rule, message)


async def _dialogs(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Присылает входящие личные сообщения в выбранный чат."""
    if not getattr(message, "is_private", False):
        return
    if getattr(message, "out", False):  # свои исходящие не пересылаем
        return

    raw_text = message_text(message)
    if not raw_text and getattr(message, "media", None) is None:
        return
    if not _matches_keywords(rule.filters.keywords, raw_text):
        return

    header = await _sender_header(message)
    text = header + transform_text(raw_text, rule.filters)
    await send_copy(client, rule.target_id, message, text)
    await record_ok(rule, message)


async def _checks(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Ловит чеки и подарочные ссылки, складывает их в одно место."""
    raw_text = message_text(message)
    links = [match.group(0) for match in GIFT_LINK_RE.finditer(raw_text)]
    keyword_hit = bool(rule.filters.keywords) and _matches_keywords(
        rule.filters.keywords, raw_text
    )

    if not links and not keyword_hit:
        return

    chat_id = int(getattr(message, "chat_id", 0) or 0)
    message_id = int(getattr(message, "id", 0) or 0)
    if links:
        payloads = [
            {"chat_id": chat_id, "message_id": message_id, "link": link, "text": raw_text[:500]}
            for link in links
        ]
    else:
        payloads = [
            {"chat_id": chat_id, "message_id": message_id, "link": None, "text": raw_text[:500]}
        ]

    if rule.target_id:
        await send_copy(client, rule.target_id, message, transform_text(raw_text, rule.filters))
    await _store(rule, "checks", payloads)
    await record_ok(rule, message)


async def _autosubscribe(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Находит ссылки на каналы в сообщениях источника и вступает в них."""
    targets = _invite_targets(message_text(message))
    if not targets:
        return

    outcome = await _join_all(client, targets)
    if outcome.joined:
        await record_ok(rule, message, count=outcome.joined)
        logger.info("Автоподписка #{}: вступили в {} чат(ов)", rule.id, outcome.joined)


_HANDLERS: dict[str, Callable[[Any, Any, RuleSnapshot], Awaitable[None]]] = {
    "broadcast": _broadcast,
    "baiting": _baiting,
    "mute": _mute,
    "dialogs": _dialogs,
    "checks": _checks,
    "autosubscribe": _autosubscribe,
}


# ──────────────────────────────── Разовые задачи ──────────────────────────────


async def run_oneshot(client: Any, rule: RuleSnapshot) -> dict[str, Any]:
    """Запускает разовую задачу и возвращает сводку для API."""
    _normalize_filters(rule)

    if rule.kind == "parser":
        return await run_parser(client, rule)
    if rule.kind == "autosubscribe":
        return await run_autosubscribe(client, rule)
    return {
        "ok": False,
        "error": f"Задача типа «{KIND_LABELS.get(rule.kind, rule.kind)}» "
        "работает по сообщениям, запускать вручную её не нужно",
    }


async def _known_user_ids(rule: RuleSnapshot) -> set[int]:
    """Люди, уже собранные этой задачей: повторный запуск не должен дублировать их."""
    async with SessionLocal() as session:
        items = await repo.list_collected_items(session, rule.id, limit=MAX_PARSER_LIMIT)

    known: set[int] = set()
    for item in items:
        try:
            value = int((item.payload or {}).get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        if value:
            known.add(value)
    return known


async def run_parser(client: Any, rule: RuleSnapshot) -> dict[str, Any]:
    """Собирает участников чата-источника в ``collected_items``."""
    limit = int(rule.filters.limit or 0)
    limit = max(1, min(limit if limit > 0 else 200, MAX_PARSER_LIMIT))
    known = await _known_user_ids(rule)

    payloads: list[dict] = []
    skipped = 0
    try:
        async for user in client.iter_participants(rule.source_id, limit=limit):
            if getattr(user, "deleted", False) or getattr(user, "bot", False):
                continue
            user_id = int(getattr(user, "id", 0) or 0)
            if not user_id or user_id in known:
                skipped += 1
                continue
            known.add(user_id)
            name = " ".join(
                part
                for part in (getattr(user, "first_name", None), getattr(user, "last_name", None))
                if part
            )
            payloads.append(
                {
                    "user_id": user_id,
                    "username": getattr(user, "username", None),
                    "name": name.strip(),
                    "phone": getattr(user, "phone", None),
                }
            )
    except FloodWaitError as exc:
        return {
            "ok": False,
            "error": f"Telegram просит подождать {int(getattr(exc, 'seconds', 60))} сек",
            "collected": 0,
        }
    except RPCError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "collected": 0}

    added = await _store(rule, "parser", payloads)
    return {"ok": True, "collected": added, "skipped": skipped, "limit": limit}


async def run_autosubscribe(client: Any, rule: RuleSnapshot) -> dict[str, Any]:
    """Вступает в чаты из настроек задачи и в ссылки, найденные в источнике."""
    targets = [
        str(item).strip() for item in (rule.filters.subscribe_to or []) if str(item).strip()
    ]
    outcome = JoinOutcome()

    # Если задан источник — сначала читаем из него последние посты на предмет ссылок
    if rule.source_id:
        try:
            async for message in client.iter_messages(rule.source_id, limit=20):
                targets.extend(_invite_targets(message_text(message)))
        except RPCError as exc:
            # Непрочитанный источник — половина работы: ссылки из его постов
            # задача не увидит. Раньше об этом знал только лог службы.
            logger.warning("Автоподписка #{}: источник не прочитан: {}", rule.id, exc)
            outcome.problems.append(f"источник не прочитан ({type(exc).__name__})")

    # порядок сохраняем, но дубли убираем
    unique: list[str] = []
    for target in targets:
        if target not in unique:
            unique.append(target)

    if not unique:
        return {
            "ok": False,
            "error": "Не указано ни одного канала для подписки",
            **_join_summary(outcome, 0),
        }

    try:
        await _join_all(client, unique, outcome)
    except FloodWaitError as exc:
        # Вступления, сделанные до отказа, остались в outcome — их и показываем:
        # «вступили в 0» после трёх удачных заходов было бы неправдой.
        return {
            "ok": False,
            "error": f"Telegram просит подождать {int(getattr(exc, 'seconds', 60))} сек",
            **_join_summary(outcome, len(unique)),
        }
    return {"ok": True, **_join_summary(outcome, len(unique))}


def _join_summary(outcome: JoinOutcome, total: int) -> dict[str, Any]:
    """Итог захода в чаты — полями ответа кабинету."""
    return {
        "joined": outcome.joined,
        "already": outcome.already,
        "total": total,
        "problems": list(outcome.problems),
    }


def _invite_targets(text: str) -> list[str]:
    """Достаёт из текста ссылки на чаты, отбрасывая служебные t.me-пути."""
    found: list[str] = []
    for match in CHAT_LINK_RE.finditer(text or ""):
        value = match.group(1).strip("/")
        if not value:
            continue
        head = value.split("/", 1)[0].lower()
        if head in NOT_A_CHAT_LINK:
            continue
        if value not in found:
            found.append(value)
    return found


async def _join_all(client: Any, targets: list[str], outcome: JoinOutcome | None = None) -> JoinOutcome:
    """Вступает в перечисленные чаты и рассказывает, что из этого вышло.

    FloodWait пробрасывает наверх: у задачи по сообщениям есть свой повтор после
    паузы (``run_job``). Чтобы при этом не потерялись уже сделанные вступления,
    итог можно передать своим — тогда после исключения в нём остаётся всё, во
    что успели войти до отказа.
    """
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest

    result = outcome if outcome is not None else JoinOutcome()
    for target in targets:
        try:
            if target.startswith("+") or target.lower().startswith("joinchat/"):
                invite_hash = target[1:] if target.startswith("+") else target.split("/", 1)[1]
                await client(ImportChatInviteRequest(invite_hash))
            else:
                await client(JoinChannelRequest(target))
            result.joined += 1
        except FloodWaitError:
            raise
        except RPCError as exc:
            name = type(exc).__name__
            logger.info("Автоподписка: не вступили в {}: {}", target, name)
            if name in JOIN_ALREADY_FINE:
                # «уже участник», «заявка отправлена» — тут нечего исправлять,
                # и краснеть карточке незачем.
                result.already += 1
            else:
                result.problems.append(f"не пустили в {target} ({name})")
        # пауза между вступлениями, иначе Telegram быстро присылает FloodWait
        await asyncio.sleep(JOIN_PAUSE_SECONDS)
    return result


# ─────────────────────────────────── Журнал ──────────────────────────────────


async def _sender_header(message: Any) -> str:
    """Строит шапку «кто написал» для уведомления из диалога."""
    sender_id = getattr(message, "sender_id", None)
    name = ""
    try:
        sender = await message.get_sender()
    except Exception:  # noqa: BLE001 — шапка не обязательна
        sender = None
    if sender is not None:
        name = " ".join(
            part
            for part in (
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            )
            if part
        ).strip() or (getattr(sender, "username", None) or "")
    if not name:
        name = str(sender_id or "неизвестно")
    return f"💬 {name} (id {sender_id}):\n"


async def _store(rule: RuleSnapshot, kind: str, payloads: list[dict]) -> int:
    """Кладёт собранные результаты в БД."""
    if not payloads:
        return 0
    async with SessionLocal() as session:
        added = await repo.add_collected_items(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            kind=kind,
            payloads=payloads,
        )
        await session.commit()
    return added


async def record_ok(rule: RuleSnapshot, message: Any, count: int = 1) -> None:
    """Отмечает успешное срабатывание задачи."""
    async with SessionLocal() as session:
        if count > 0:
            await repo.bump_forwarded(session, rule.id, count)
        await repo.log_forward(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            source_msg_id=int(getattr(message, "id", 0) or 0),
            target_msg_id=int(rule.target_id or 0) or None,
            status="ok",
        )
        await session.commit()
    logger.debug("Задача #{} ({}) сработала", rule.id, rule.kind)


async def record_error(rule: RuleSnapshot, message: Any, error: str) -> None:
    """Пишет ошибку задачи в журнал — чтобы её было видно в статистике."""
    logger.error("Задача #{} ({}): {}", rule.id, rule.kind, error)
    async with SessionLocal() as session:
        await repo.log_forward(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            source_msg_id=int(getattr(message, "id", 0) or 0),
            target_msg_id=None,
            status="error",
            error=error,
        )
        await session.commit()


def batch_error_text(failed: Sequence[str]) -> str:
    """Причина сбоя прохода одной строкой — её человек читает на карточке."""
    first = next((str(item) for item in failed if item), "неизвестная ошибка")
    if len(failed) < 2:
        return f"не ушло в {first}"
    return f"не ушло в {len(failed)} чат(ов), первый — {first}"


async def record_batch(
    rule: RuleSnapshot,
    *,
    sent: int = 0,
    failed: Sequence[str] = (),
    target_id: int | None = None,
) -> None:
    """Итог одного прохода задачи «в несколько чатов»: сколько ушло и что нет.

    Одна запись на проход, а не на чат: постер за круг обходит сотни чатов, и
    строка на каждый превратила бы журнал в поток, в котором ничего не найти.
    Отказ «подождите» (FloodWait) сюда не попадает — это пауза, а не сбой:
    задача вернётся к этим чатам сама.

    Порядок записи важен: сбой пишется раньше успеха, поэтому проход, в котором
    что-то всё же ушло, не считается сломанным (см. ``repo.task_health``).
    Причина при этом остаётся в журнале и видна на карточке — «в три чата не
    ушло» человеку нужно знать, даже когда остальные сто получили.
    """
    if not sent and not failed:
        return
    async with SessionLocal() as session:
        if failed:
            await repo.log_forward(
                session,
                rule_id=rule.id,
                user_id=rule.user_id,
                # У расписанных задач нет входящего сообщения: они его создают.
                source_msg_id=0,
                target_msg_id=None,
                status="error",
                error=batch_error_text(failed),
            )
        if sent:
            await repo.bump_forwarded(session, rule.id, sent)
            await repo.log_forward(
                session,
                rule_id=rule.id,
                user_id=rule.user_id,
                source_msg_id=0,
                target_msg_id=int(target_id) if target_id else None,
                status="ok",
            )
        await session.commit()


def oneshot_problem_text(problems: Sequence[str]) -> str:
    """Помехи разового запуска одной строкой — её человек читает на карточке.

    Причины уже написаны словами («не пустили в @chat (…)», «источник не
    прочитан (…)»), поэтому склеивать их незачем: показываем первую и говорим,
    сколько таких же было ещё. На экране 390 px длинное перечисление всё равно
    не читается.
    """
    first = next((str(item) for item in problems if item), "неизвестная ошибка")
    if len(problems) < 2:
        return first
    return f"{first} — и ещё {len(problems) - 1}"


async def record_oneshot(rule: RuleSnapshot, result: dict[str, Any]) -> None:
    """Итог разового запуска — в журнал задачи, а не только во всплывающий тост.

    Раньше «Telegram просит подождать 40 сек», «аккаунт не в сети» и даже удачный
    сбор жили ровно до закрытия подсказки: на карточке оставались метка «по
    кнопке» и ноль собранных, и по ней нельзя было понять, запускали задачу час
    назад или ни разу. Теперь запуск виден так же, как проход расписанной задачи.

    Порядок записи тот же, что в ``record_batch``: сбой раньше успеха, поэтому
    запуск с помехами не считается сломанным (см. ``repo.task_health``), но
    причина на карточке остаётся. Успехом не дополняем два случая: неудачный
    запуск (у разовой задачи нет следующего прохода, который сам всё исправит, —
    за кнопкой должен вернуться человек) и запуск, который прошёл до конца, но не
    сделал ничего, а помехи назвал: «не пустили ни в один чат» — это сбой, как бы
    гладко ни завершился сам проход.

    Отдельная функция, а не ``record_batch``: тому нужны отправки по чатам, и
    удачный сбор без единой отправки он просто не записал бы.
    """
    ok = bool(result.get("ok"))
    joined = int(result.get("joined") or 0)
    problems = [str(item) for item in (result.get("problems") or ()) if item]
    reason = str(result.get("error") or "") if not ok else oneshot_problem_text(problems)
    # Что запуск успел сделать: вступления, чаты, где мы и так есть, и собранные
    # записи. Ноль при названных помехах — это не «сработала», а «не смогла».
    done = joined + int(result.get("already") or 0) + int(result.get("collected") or 0)

    async with SessionLocal() as session:
        if not ok or problems:
            await repo.log_forward(
                session,
                rule_id=rule.id,
                user_id=rule.user_id,
                # У разовой задачи нет входящего сообщения: её запускают кнопкой.
                source_msg_id=0,
                target_msg_id=None,
                status="error",
                error=reason or None,
            )
        # Вступления считаем и у неудачного запуска: три чата из десяти — это
        # три чата, а полоса выполнения читает именно этот счётчик.
        if joined:
            await repo.bump_forwarded(session, rule.id, joined)
        if ok and (done or not problems):
            await repo.log_forward(
                session,
                rule_id=rule.id,
                user_id=rule.user_id,
                source_msg_id=0,
                target_msg_id=None,
                status="ok",
            )
        await session.commit()
