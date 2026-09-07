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
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Awaitable, Sequence

from loguru import logger
from telethon.errors import (
    ChannelInvalidError,
    ChannelPrivateError,
    ChatIdInvalidError,
    ChatWriteForbiddenError,
    FloodWaitError,
    RPCError,
    UserBannedInChannelError,
)
from telethon.tl.functions.channels import (
    GetFullChannelRequest,
    InviteToChannelRequest,
)
from telethon.tl.types import ChannelParticipantsAdmins

from app.db import repo
from app.errors import ValidationError
from app.db.database import SessionLocal
from app.telegram_client.filters import URL_RE, FilterConfig, message_text, transform_text
from app.telegram_client.forwarder import send_copy, subscription_active
from app.telegram_client.types import RuleSnapshot
from app.translate import maybe_translate

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

# Складывают находки в collected_items — у них есть кнопка «Результаты».
# Ровно те типы, которые зовут _store: автоподписка тоже разовая, но она
# вступает в чаты и ничего не собирает — её кнопка всегда отвечала «Пока пусто».
COLLECTING_KINDS: tuple[str, ...] = ("parser", "checks")

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
    "clone": "клон канала",
    "listener": "слушатель слов",
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
# Ответов на один пост при сборе комментаторов: дальше — шум, а не люди.
COMMENTS_PER_POST = 200
# Приглашений за один нажим кнопки: инвайт — тяжёлая операция, пачками по
# многу Telegram режет и заодно банит аккаунт за спам.
INVITE_BATCH = 20

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
    # Остановились не потому, что кончились чаты, а потому, что кончился
    # дневной лимит: остаток — завтра, а не «не пустили».
    limited: bool = False


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
    if kind == "listener":
        words = len([word for word in (filters.get("keywords") or []) if str(word).strip()])
        head = f"Слушатель: {source} → {target}"
        return head + (f" · {words} сл." if words else "")
    if kind == "clone":
        if filters.get("clone_done"):
            return f"Клон: {source} → {target}"
        left = len(filters.get("clone_ids") or [])
        total = int(filters.get("clone_history") or 0)
        if filters.get("clone_listed") and total:
            return f"Клон: {source} → {target} · история {total - left}/{total}"
        return f"Клон: {source} → {target} · забираю историю"
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
        raw_filters = getattr(rule, "filters", None)
        dated = (
            raw_filters.get("schedule_only")
            if isinstance(raw_filters, dict)
            else getattr(raw_filters, "schedule_only", False)
        )
        head = "Постинг по датам" if dated else "Постинг по расписанию"
        if total > 1:
            return f"{head}: {total} чат."
        return f"{head} → {target}" if target else head
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

    # Задержку rule.delay_seconds здесь НЕ спим: очередь доставки уже отработала
    # её в submit() до постановки в работу. Второй сон не только удваивал бы
    # паузу, но и держал бы воркер и слот отправки всё это время.
    try:
        await handler(client, message, rule)
    except FloodWaitError:
        # FloodWait отдаём наверх очереди: она подождёт ровно столько, сколько
        # просит Telegram, ВНЕ слота отправки и повторит. Спать здесь — значит
        # блокировать воркер и остальные правила (см. queue.DeliveryQueue._run).
        # Порядок важен: FloodWaitError — подкласс RPCError, ловим его первым.
        raise
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


def hhmm_to_sec(value: Any) -> int:
    """«ЧЧ:ММ» → секунды от начала суток. Невалидное значение → 0."""
    try:
        hours, minutes = str(value).split(":")
        return max(0, min(23, int(hours))) * 3600 + max(0, min(59, int(minutes))) * 60
    except Exception:  # noqa: BLE001
        return 0


def in_window(now_sec: int, start: int, end: int) -> bool:
    """Попадает ли момент в окно. Окно через полночь (23:00→01:00) тоже ок."""
    if start <= end:
        return start <= now_sec <= end
    return now_sec >= start or now_sec <= end


def window_allows(config: Any, *, now: float | None = None) -> bool:
    """Окно отправки открыто прямо сейчас?

    Окно одно на всех: постер ждёт его кругами, рассылка — тиком, пересылка —
    задержкой. Часы — хозяина задачи (``window_tz``), а не сервера.
    """
    start = hhmm_to_sec(getattr(config, "window_start", "00:00"))
    end = hhmm_to_sec(getattr(config, "window_end", "23:59"))
    now_sec = window_now_sec(window_tz_minutes(getattr(config, "window_tz", None)), now)
    return in_window(now_sec, start, end)


def quiet_wait_seconds(config: Any, *, now: float | None = None) -> int:
    """Сколько секунд ждать до открытия окна отправки. 0 — слать сейчас.

    Нужна пересылке: она событийная, «пропустить тик» ей нечего — сообщение
    уже пришло, и его надо отложить до утра, а не выбросить.
    """
    tz = window_tz_minutes(getattr(config, "window_tz", None))
    now_sec = window_now_sec(tz, now)
    start = hhmm_to_sec(getattr(config, "window_start", "00:00"))
    end = hhmm_to_sec(getattr(config, "window_end", "23:59"))
    if in_window(now_sec, start, end):
        return 0
    if start <= end:
        # Закрыто до старта сегодня — или до старта завтра, если он прошёл.
        return (start - now_sec) % SECONDS_IN_DAY
    # Ночное окно: закрытая зона — между концом и стартом, старт сегодня.
    return start - now_sec


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


async def mailing_send(client: Any, rule: RuleSnapshot, item: Any, target_id: int) -> int | None:
    """Отправляет одно сохранённое сообщение в один чат.

    Возвращает id отправленного (нужен закрепу и автоудалению) или None.
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

    async def _send() -> Any:
        if message is not None:
            return await send_copy(
                client, target_id, message, text,
                link_preview=bool(filters.link_preview),
                buttons=getattr(filters, "buttons", None),
            )
        return await client.send_message(
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
            sent = await _send()
    else:
        sent = await _send()
    sent_id = getattr(sent, "id", None)
    return int(sent_id) if sent_id else None


# ─────────── Запланированные посты: слоты с датой вместо кругов ───────────

# Слотов на задачу: календарь, а не склад. Кому мало — вторая задача.
SCHEDULED_SLOT_CAP = 50


def parse_slot_at(raw: Any) -> datetime | None:
    """Дата слота → наивный UTC. Наивная считается UTC: кабинет шлёт ISO с зоной."""
    if not raw:
        return None
    text = str(raw).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def normalize_scheduled_posts(raw: Any) -> list[dict]:
    """Слоты из формы — в хранимый вид, отсортированные по дате.

    Мусор не чиним, а отклоняем с понятной причиной: молча выкинутая дата —
    это пост, который человек ждёт, а задача о нём «не знает». Прошедшие даты
    разрешены: такой слот уйдёт на ближайшем проходе, а не потеряется.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValidationError("Расписание — списком дат")
    slots: list[dict] = []
    for pos, entry in enumerate(raw, start=1):
        if len(slots) >= SCHEDULED_SLOT_CAP:
            raise ValidationError(f"Не больше {SCHEDULED_SLOT_CAP} дат на задачу")
        if not isinstance(entry, dict):
            raise ValidationError(f"Дата №{pos}: нужна дата и текст")
        moment = parse_slot_at(entry.get("at"))
        if moment is None:
            raise ValidationError(f"Дата №{pos}: не разобрать «{entry.get('at')}»")
        text = str(entry.get("text") or "").strip()
        library_id: int | None = None
        if entry.get("library_id") is not None:
            try:
                library_id = int(entry.get("library_id"))
            except (TypeError, ValueError):
                raise ValidationError(f"Дата №{pos}: неверная запись библиотеки")
            if library_id <= 0:
                library_id = None
        if not text and library_id is None:
            raise ValidationError(f"Дата №{pos}: нужен текст или запись библиотеки")
        slot_id = str(entry.get("id") or "").strip() or uuid.uuid4().hex[:8]
        slots.append(
            {
                "id": slot_id,
                "at": moment.isoformat(timespec="minutes"),
                "text": text,
                "library_id": library_id,
                "sent": False,
                "sent_to": [],
            }
        )
    slots.sort(key=lambda slot: slot["at"])
    return slots


def merge_scheduled_state(
    old: Sequence[dict] | None, fresh: list[dict]
) -> list[dict]:
    """Переносит состояние отправки на пересохранённые слоты.

    Правка формы присылает все даты заново, и normalize помечает их
    неотправленными — без слияния уже ушедшие посты воскресали бы и уходили
    по второму кругу. Слоты стыкуются по id: совпал — забираем sent/sent_to.
    """
    known = {
        slot.get("id"): slot
        for slot in (old or [])
        if isinstance(slot, dict) and slot.get("id")
    }
    for slot in fresh:
        prev = known.get(slot.get("id"))
        if not prev:
            continue
        if prev.get("sent"):
            slot["sent"] = True
            slot["sent_to"] = list(prev.get("sent_to") or [])
            if prev.get("skipped"):
                slot["skipped"] = prev["skipped"]
    return fresh


def due_scheduled_slot(slots: Sequence[dict], now: datetime) -> dict | None:
    """Первый слот, которому пора: не отправлен и дата прошла."""
    for slot in slots:
        if not isinstance(slot, dict) or slot.get("sent"):
            continue
        moment = parse_slot_at(slot.get("at"))
        if moment is not None and moment <= now:
            return slot
    return None


def scheduled_pending(slots: Sequence[dict]) -> list[dict]:
    """Слоты, которые ещё не ушли (для карточки и формы правки)."""
    return [slot for slot in slots if isinstance(slot, dict) and not slot.get("sent")]


async def load_scheduled_item(user_id: int, slot: dict) -> Any | None:
    """Что уходит по слоту: запись библиотеки — или свой текст строкой.

    Отправка дальше общая (``mailing_send``): сохранённый пост с медиа слот
    тоже умеет, отдельным путём не ходим.
    """
    library_id = slot.get("library_id")
    if library_id:
        items = await load_mailing_library(user_id, [int(library_id)])
        return items[0] if items else None
    return own_text_item(str(slot.get("text") or ""))


async def _broadcast(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Одно сообщение из источника уходит в несколько чатов."""
    targets = chat_recipients(rule)
    if not targets:
        await record_error(rule, message, "У рассылки нет получателей")
        return

    raw_text = message_text(message)
    if rule.filters.translate_to:
        raw_text = await maybe_translate(raw_text, rule.filters.translate_to)
    text = transform_text(raw_text, rule.filters)
    sent = 0
    struck: dict[int, str] = {}
    delivered: list[int] = []
    for target in targets:
        try:
            await send_copy(
                client, target, message, text,
                buttons=getattr(rule.filters, "buttons", None),
            )
            sent += 1
            delivered.append(target)
        except FloodWaitError:
            # «Подождите» — не отказ чата, а пауза всего задания: отдаём её
            # наверх очереди, она подождёт вне слота отправки и повторит всё
            # задание. Глотать её здесь — значит молча потерять оставшиеся чаты.
            raise
        except RPCError as exc:
            logger.warning("Рассылка #{}: не ушло в {}: {}", rule.id, target, exc)
            if is_hopeless_chat_error(exc):
                struck[target] = type(exc).__name__
    if sent:
        await record_ok(rule, message, count=sent)
    else:
        await record_error(rule, message, "Сообщение не удалось доставить ни в один чат")
    # Мёртвые чаты копятся в счётчике и уходят из получателей сами — иначе
    # каждое сообщение спотыкалось бы об один и тот же недоступный чат.
    if struck or (delivered and rule.filters.chat_strikes):
        async with SessionLocal() as session:
            pruned, strikes = await repo.register_chat_strikes(
                session, rule.id, failed=struck, succeeded=delivered
            )
            await session.commit()
        rule.filters.chat_strikes = strikes
        if pruned:
            rule.filters.targets = [
                chat_id for chat_id in rule.filters.targets if chat_id not in pruned
            ]
            await record_pruned_chats(rule, pruned)


# Безнадёжные ошибки отправки в чат: сами не пройдут, сколько ни повторяй.
# Всё остальное (сеть, слоумод, временные отказы) — повод подождать, а не
# вычёркивать: transient-ошибка три круга подряд — ещё не мёртвый чат.
HOPELESS_CHAT_ERRORS: tuple[type, ...] = (
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    ChannelPrivateError,
    ChannelInvalidError,
    ChatIdInvalidError,
)


def is_hopeless_chat_error(exc: BaseException) -> bool:
    """Чат не примет сообщение никогда: выгнали, снесли, закрыли."""
    return isinstance(exc, HOPELESS_CHAT_ERRORS)


# История забирается порциями: залп в сотни постов — верный FloodWait.
CLONE_BATCH = 20
# Больше — уже не клон, а архив: качать тысячи постов через API — часы работы.
CLONE_HISTORY_CAP = 500


async def clone_backfill_tick(client: Any, rule: RuleSnapshot) -> str:
    """Один проход догрузки истории клона: не больше CLONE_BATCH постов.

    Возвращает "done", когда забирать больше нечего, иначе "progress".
    Порядок — от старых к новым: читатель нового канала листает историю как
    она выходила. Удалённый или служебный пост не держит очередь: его id
    считается обработанным, иначе один такой пост встал бы пробкой навсегда.
    FloodWait отдаётся наверх: менеджер ставит задачу на паузу до срока.
    """
    filters = rule.filters
    want = max(0, min(CLONE_HISTORY_CAP, int(filters.clone_history or 0)))
    ids = [int(item) for item in (filters.clone_ids or [])]

    async def persist(
        rest: list[int] | None = None,
        listed: bool | None = None,
        done: bool | None = None,
    ) -> None:
        async with SessionLocal() as session:
            await repo.update_clone_progress(
                session, rule.id, ids=rest, listed=listed, done=done
            )
            await session.commit()
        # Снимок менеджера правим руками: следующий тик читает его же, а не базу.
        if rest is not None:
            filters.clone_ids = list(rest)
        if listed is not None:
            filters.clone_listed = listed
        if done is not None:
            filters.clone_done = done

    if filters.clone_done or want <= 0:
        if not filters.clone_done:
            await persist([], True, True)
        return "done"
    if not ids and not filters.clone_listed:
        # Опись: забираем id последних постов одним запросом и разворачиваем —
        # дальше очередь всегда идёт от старых к новым.
        found = await client.get_messages(rule.source_id, limit=want)
        if not isinstance(found, list):
            found = [found] if found else []
        ids = sorted({int(msg.id) for msg in found if msg and msg.id})
        await persist(ids, True, None)
    if not ids:
        await persist([], True, True)
        return "done"

    chunk, rest = ids[:CLONE_BATCH], ids[CLONE_BATCH:]
    found = await client.get_messages(rule.source_id, ids=chunk)
    if not isinstance(found, list):
        found = [found] if found else []
    by_id = {int(msg.id): msg for msg in found if msg and msg.id}
    sent_count = 0
    try:
        for pos, mid in enumerate(chunk):
            msg = by_id.get(mid)
            raw_text = message_text(msg) if msg is not None else ""
            if (
                msg is None
                or getattr(msg, "action", None) is not None
                or (not raw_text and getattr(msg, "media", None) is None)
            ):
                continue
            if filters.translate_to:
                raw_text = await maybe_translate(raw_text, filters.translate_to)
            await send_copy(
                client,
                rule.target_id,
                msg,
                transform_text(raw_text, filters),
                buttons=getattr(filters, "buttons", None),
            )
            await record_ok(rule, msg)
            sent_count = pos + 1
    except FloodWaitError:
        # Успевшее — в базу, остаток — в очередь: повтор начнётся с места
        # остановки, а не с начала порции.
        await persist(chunk[sent_count:] + rest, None, None)
        raise
    rest = chunk[sent_count:] + rest if sent_count < len(chunk) else rest
    await persist(rest, None, not rest)
    return "done" if not rest else "progress"


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


# Админы чата для модерации — с кэшем на 10 минут: спрашивать состав при
# каждом сообщении значит упереться во FloodWait на первом же живом чате.
_mod_admins: dict[tuple[int, int], tuple[float, set[int]]] = {}
_MOD_ADMINS_TTL = 600


async def _mod_admin_ids(client: Any, account_id: int, chat_id: int) -> set[int]:
    """Админы чата: свои под модерацию не попадают."""
    now = time.time()
    hit = _mod_admins.get((account_id, chat_id))
    if hit is not None and now - hit[0] < _MOD_ADMINS_TTL:
        return hit[1]
    try:
        ids = {
            int(getattr(user, "id", 0) or 0)
            async for user in client.iter_participants(
                chat_id, filter=ChannelParticipantsAdmins
            )
        }
    except Exception:  # noqa: BLE001 — нет прав/чата: считаем, что админов нет
        return set()
    ids.discard(0)
    _mod_admins[(account_id, chat_id)] = (now, ids)
    return ids


async def record_mod_action(rule: RuleSnapshot, user_id: int, action: str) -> None:
    """Серьёзное действие модерации (мут) — строкой в журнал, а не только в лог."""
    async with SessionLocal() as session:
        await repo.log_forward(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            source_msg_id=0,
            target_msg_id=None,
            status="ok",
            error=f"🔇 {user_id}: {action}",
        )
        await session.commit()


async def _mute(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Модерирует чат: цель, запретные слова и ссылки — удалением, рецидив — мутом.

    Три повода удалить: сообщение поднадзорного (с учётом слов), запретное
    слово от кого угодно и ссылка при включённом блоке. Админы от слов и ссылок
    освобождены — но явно поднадзорного это не касается: раз человека заказали,
    значит так надо. Каждое удаление — варн автору; набрал ``max_warns`` —
    получает мут на ``mute_hours`` и счёт обнуляется.
    """
    conf = rule.filters
    text = message_text(message)
    sender_id = int(getattr(message, "sender_id", 0) or 0)
    chat_id = int(message.chat_id)

    if getattr(conf, "target_user_id", 0):
        targeted = _sender_matches(rule, message) and _matches_keywords(
            conf.keywords, text
        )
    else:
        # Цели нет (старые задачи): как раньше — по словам на всех.
        targeted = _matches_keywords(conf.keywords, text) and bool(
            [word for word in (conf.keywords or []) if str(word).strip()]
        )
    banned = [word.strip().lower() for word in (conf.banned_words or []) if word and word.strip()]
    word_hit = bool(banned) and any(word in text.lower() for word in banned)
    link_hit = bool(conf.block_links) and bool(text) and URL_RE.search(text) is not None
    if not targeted and not word_hit and not link_hit:
        return
    if (word_hit or link_hit) and sender_id:
        admins = await _mod_admin_ids(client, rule.account_id, chat_id)
        if sender_id in admins and not targeted:
            return

    await client.delete_messages(chat_id, [message.id])
    await record_ok(rule, message)

    max_warns = max(0, int(conf.max_warns or 0))
    if max_warns <= 0 or not sender_id:
        return
    async with SessionLocal() as session:
        count = await repo.bump_mod_strike(session, rule.id, sender_id)
        await session.commit()
    if count < max_warns:
        return
    hours = min(720, max(1, int(conf.mute_hours or 24)))
    try:
        await client.edit_permissions(
            chat_id,
            sender_id,
            send_messages=False,
            until_date=datetime.now(timezone.utc) + timedelta(hours=hours),
        )
    except Exception as exc:  # noqa: BLE001 — удаление уже сработало
        logger.warning(
            "Мут #{}: не смог ограничить {} ({}): проверьте права админа",
            rule.id, sender_id, exc,
        )
    else:
        await record_mod_action(rule, sender_id, f"мут {hours} ч после {count} нарушений")
    async with SessionLocal() as session:
        await repo.clear_mod_strikes(session, rule.id, sender_id)
        await session.commit()


async def _dialog_flags(account_id: int, chat_id: int) -> dict[str, Any]:
    """Флаги чата (архив, мут) — из кэша диалогов, не из Telegram.

    Импорт менеджера отложенный: manager импортирует этот модуль, и импорт
    наверху закольцевался бы. Пустой кэш — «чат обычный»: уведомлениям лучше
    прийти лишний раз, чем потеряться из-за недоступного списка.
    """
    from app.telegram_client.manager import manager

    try:
        dialogs = await manager.list_dialogs(account_id)
    except Exception:
        return {}
    for chat in dialogs:
        if int(chat.get("id", 0) or 0) == chat_id:
            return chat
    return {}


async def _dialogs(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Присылает входящие личные сообщения в выбранный чат."""
    if not getattr(message, "is_private", False):
        return
    if getattr(message, "out", False):  # свои исходящие не пересылаем
        return
    conf = rule.filters
    if getattr(conf, "ignore_bots", True):
        sender = getattr(message, "sender", None)
        if getattr(sender, "bot", False):
            return
    chat_id = int(getattr(message, "chat_id", 0) or 0)
    if chat_id and (getattr(conf, "ignore_archived", True) or getattr(conf, "ignore_muted", True)):
        flags = await _dialog_flags(rule.account_id, chat_id)
        if getattr(conf, "ignore_archived", True) and flags.get("archived"):
            return
        if getattr(conf, "ignore_muted", True) and flags.get("muted"):
            return

    raw_text = message_text(message)
    if not raw_text and getattr(message, "media", None) is None:
        return
    if not _matches_keywords(rule.filters.keywords, raw_text):
        return

    header = await _sender_header(message)
    text = header + transform_text(raw_text, rule.filters)
    await send_copy(
        client, rule.target_id, message, text,
        buttons=getattr(rule.filters, "buttons", None),
    )
    await record_ok(rule, message)


async def _listener(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Ловит ключевые слова в источнике и присылает совпадения в чат.

    От уведомлений из диалогов отличается источником: те слушают все ЛС
    аккаунта, а слушатель — один указанный чат. Пустых слов не бывает: задача
    без слов — это пересылка, и API её не создаёт.
    """
    raw_text = message_text(message)
    if not raw_text:
        return
    if not _matches_keywords(rule.filters.keywords, raw_text):
        return
    where = rule.source_title or "Источник"
    text = f"🔔 {where}\n\n" + transform_text(raw_text, rule.filters)
    await send_copy(
        client, rule.target_id, message, text,
        buttons=getattr(rule.filters, "buttons", None),
    )
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

    # Сначала в хранилище, потом в чат: если отправка упадёт, находка уже
    # сохранена и не потеряется вместе с ошибкой.
    _, capped = await _store(rule, "checks", payloads)
    if rule.target_id:
        await send_copy(
            client, rule.target_id, message, transform_text(raw_text, rule.filters),
            buttons=getattr(rule.filters, "buttons", None),
        )
    if capped:
        logger.warning(
            "Ловец чеков #{}: хранилище переполнено ({}), новые находки отброшены",
            rule.id,
            MAX_PARSER_LIMIT,
        )
    await record_ok(rule, message)


async def _autosubscribe(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Находит ссылки на каналы в сообщениях источника и вступает в них."""
    targets = _invite_targets(message_text(message))
    if not targets:
        return
    conf = rule.filters
    gap = max(0, int(getattr(conf, "join_gap", JOIN_PAUSE_SECONDS) or 0))
    retries = max(0, int(getattr(conf, "join_retries", 0) or 0))
    # Дневной лимит действует и здесь; исчерпанный — тихий пропуск, а не
    # ошибка: журнал не должен краснеть каждый вечер.
    stop_at: int | None = None
    daily = max(0, int(getattr(conf, "daily_join_limit", 0) or 0))
    if daily > 0:
        async with SessionLocal() as session:
            already_today = await repo.count_joins_today(session, rule.id)
        if already_today >= daily:
            return
        stop_at = daily - already_today

    outcome = await _join_all(
        client, targets, rule=rule, gap=gap, retries=retries, stop_at=stop_at
    )
    if outcome.joined:
        await record_ok(rule, message, count=outcome.joined)
        logger.info("Автоподписка #{}: вступили в {} чат(ов)", rule.id, outcome.joined)


_HANDLERS: dict[str, Callable[[Any, Any, RuleSnapshot], Awaitable[None]]] = {
    "broadcast": _broadcast,
    "baiting": _baiting,
    "mute": _mute,
    "dialogs": _dialogs,
    "listener": _listener,
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


def _user_last_seen(user: Any) -> datetime | None:
    """Когда пользователь был в сети — по его статусу, в UTC.

    Класс статуса читаем по имени, а не импортом типов Telethon: так тесты
    обходятся SimpleNamespace, а не конструкторами реальных статусов.
    «Недавно» Telegram показывает вместо точного времени — считаем его двумя
    сутками назад: для фильтра «заходил не раньше N часов» это честная оценка.
    """
    status = getattr(user, "status", None)
    name = type(status).__name__ if status is not None else ""
    now = datetime.now(timezone.utc)
    if name == "UserStatusOnline":
        return now
    if name == "UserStatusOffline":
        seen = getattr(status, "was_online", None)
        if isinstance(seen, datetime):
            return seen if seen.tzinfo else seen.replace(tzinfo=timezone.utc)
        return None
    if name == "UserStatusRecently":
        return now - timedelta(hours=48)
    if name == "UserStatusLastWeek":
        return now - timedelta(days=7)
    if name == "UserStatusLastMonth":
        return now - timedelta(days=30)
    return None


def _user_passes_filters(user: Any, conf: Any, admin_ids: set[int]) -> bool:
    """Участник проходит фильтры парсера (удалённые и боты уже отсеяны)."""
    user_id = int(getattr(user, "id", 0) or 0)
    if not user_id:
        return False
    if getattr(conf, "require_username", True) and not getattr(user, "username", None):
        return False
    if getattr(conf, "exclude_admins", True) and user_id in admin_ids:
        return False
    if getattr(conf, "only_premium", False) and not getattr(user, "premium", False):
        return False
    if getattr(conf, "only_with_photo", False) and getattr(user, "photo", None) is None:
        return False
    within = int(getattr(conf, "online_within_hours", 0) or 0)
    if getattr(conf, "active_only", False) and not within:
        within = 72  # «живой» — заходил в последние трое суток
    if within > 0:
        seen = _user_last_seen(user)
        if seen is None or datetime.now(timezone.utc) - seen > timedelta(hours=within):
            return False
    return True


def _screen_user(
    user: Any, conf: Any, admin_ids: set[int], known: set[int]
) -> tuple[dict | None, str]:
    """Отсев собранного человека: удалён/бот, уже есть, не прошёл фильтры.

    Возвращает (payload, причина): "ok" — забираем, "skipped" — уже собран,
    "filtered" — отсеян. Один на режимы «авторы» и «комментарии»: проверки
    одинаковые, а расходились они уже дважды — правили в одном месте и забывали
    во втором.
    """
    if (
        getattr(user, "deleted", False)
        or getattr(user, "bot", False)
        or getattr(user, "broadcast", False)
    ):
        return None, "filtered"
    user_id = int(getattr(user, "id", 0) or 0)
    if not user_id or user_id in known:
        return None, "skipped"
    if not _user_passes_filters(user, conf, admin_ids):
        return None, "filtered"
    return _parser_payload(user), "ok"


def _parser_payload(user: Any) -> dict:
    """Участник — в запись хранилища (те же поля, что ждёт выгрузка в файл)."""
    name = " ".join(
        part
        for part in (getattr(user, "first_name", None), getattr(user, "last_name", None))
        if part
    )
    return {
        "user_id": int(getattr(user, "id", 0) or 0),
        "username": getattr(user, "username", None),
        "name": name.strip(),
        "phone": getattr(user, "phone", None),
    }


async def _discussion_id(client: Any, source_id: int) -> int | None:
    """Где живут комментарии источника: у группы — она сама, у канала — группа
    обсуждений из полного описания. Нет группы — негде собирать (None)."""
    try:
        entity = await client.get_entity(source_id)
    except Exception:  # noqa: BLE001 — источник недоступен, скажет вызывающий
        return None
    if not getattr(entity, "broadcast", False):
        return int(source_id)
    try:
        full = await client(GetFullChannelRequest(entity))
    except Exception:  # noqa: BLE001 — приватный канал без доступа
        return None
    linked = getattr(getattr(full, "full_chat", None), "linked_chat_id", None)
    return int(linked) if linked else None


async def invite_collected(client: Any, rule: RuleSnapshot) -> dict[str, Any]:
    """Зовёт собранных парсером людей в чат из настроек задачи.

    Идёт пачкой INVITE_BATCH за вызов: инвайт упирается в лимиты Telegram, и
    гнать тысячу зараз — верный бан аккаунта. Каждый приглашённый помечается в
    хранилище, повторный вызов берёт следующих. Чужая приватность уважается:
    кого позвать нельзя, тот помечается причиной и больше не трогается.
    FloodWait останавливает пачку: недоприглашённые ждут следующего вызова.
    """
    conf = rule.filters
    target_ref = str(getattr(conf, "invite_to", "") or "").strip()
    if not target_ref:
        return {"ok": False, "error": "Укажите чат для приглашений в настройках задачи"}
    try:
        target = await client.get_entity(target_ref)
    except Exception as exc:  # noqa: BLE001 — ссылка битая или нет доступа
        return {"ok": False, "error": f"Чат для приглашений недоступен: {exc}"}
    delay = max(2, min(int(getattr(conf, "api_delay", 0) or 0), 60))

    async with SessionLocal() as session:
        items = await repo.list_collected_items(
            session, rule.id, limit=MAX_PARSER_LIMIT
        )
    pending = [
        item
        for item in items
        if not (getattr(item, "payload", None) or {}).get("invited")
        and not (getattr(item, "payload", None) or {}).get("invite_error")
    ]
    invited = 0
    failed = 0
    for item in pending[:INVITE_BATCH]:
        payload = getattr(item, "payload", None) or {}
        user_id = int(payload.get("user_id") or 0)
        if invited:
            await asyncio.sleep(delay)
        if not user_id:
            failed += 1
            continue
        try:
            await client(InviteToChannelRequest(target, [user_id]))
        except FloodWaitError as exc:
            seconds = int(getattr(exc, "seconds", 60))
            return {
                "ok": False,
                "error": f"Telegram просит подождать {seconds} сек",
                "invited": invited,
                "failed": failed,
                "pending": len(pending) - invited - failed,
                "wait_seconds": seconds,
                "partial": True,
            }
        except RPCError as exc:
            failed += 1
            async with SessionLocal() as session:
                await repo.update_collected_payload(
                    session, item.id, {"invite_error": f"{type(exc).__name__}"}
                )
                await session.commit()
            continue
        invited += 1
        async with SessionLocal() as session:
            await repo.update_collected_payload(session, item.id, {"invited": True})
            await session.commit()
    return {
        "ok": True,
        "invited": invited,
        "failed": failed,
        "pending": max(0, len(pending) - invited - failed),
    }


async def run_parser(client: Any, rule: RuleSnapshot) -> dict[str, Any]:
    """Собирает участников чата-источника в ``collected_items``.

    Три режима: ``participants`` листает состав чата, ``history`` — авторов
    последних сообщений (живая аудитория вместо мёртвых душ), ``comments`` —
    комментаторов последних постов (самая вовлечённая часть). Результат
    ограничен ``limit`` (сколько сохранить), просмотр — ``scan_limit``
    (сколько перебрать: фильтры отсеивают, и смотреть приходится больше).
    """
    conf = rule.filters
    mode = getattr(conf, "parser_mode", "participants")
    if mode not in ("participants", "history", "comments"):
        mode = "participants"
    wanted = int(getattr(conf, "limit", 0) or 0)
    result_limit = max(1, min(wanted if wanted > 0 else 200, MAX_PARSER_LIMIT))
    scan = int(getattr(conf, "scan_limit", 0) or 0)
    scan_limit = max(1, min(scan if scan > 0 else 1000, MAX_PARSER_LIMIT))
    delay = max(0, min(int(getattr(conf, "api_delay", 0) or 0), 60))
    known = await _known_user_ids(rule)

    # Админов узнаём одним запросом — списком, а не проверкой каждого:
    # дёргать GetParticipant ради каждого участника значит упереться во
    # FloodWait на первом же большом чате.
    admin_ids: set[int] = set()
    if getattr(conf, "exclude_admins", True):
        try:
            async for admin in client.iter_participants(
                rule.source_id, filter=ChannelParticipantsAdmins
            ):
                admin_ids.add(int(getattr(admin, "id", 0) or 0))
        except (RPCError, TypeError):
            # TypeError — мок без параметра filter: админов не знаем, собираем
            # всех. Живой Telethon filter понимает всегда.
            admin_ids = set()

    payloads: list[dict] = []
    scanned = 0
    skipped = 0
    filtered = 0
    try:
        if mode == "history":
            seen_authors: set[int] = set()
            async for message in client.iter_messages(rule.source_id, limit=scan_limit):
                if len(payloads) >= result_limit:
                    break
                sender_id = int(getattr(message, "sender_id", 0) or 0)
                if not sender_id or sender_id in seen_authors:
                    continue
                seen_authors.add(sender_id)
                # Автор — отдельным запросом: в сообщении есть только его id,
                # а фильтрам нужны юзернейм, премиум и статус. Пауза — перед
                # каждым таким запросом.
                if delay:
                    await asyncio.sleep(delay)
                user = await client.get_entity(sender_id)
                scanned += 1
                payload, reason = _screen_user(user, conf, admin_ids, known)
                if reason != "ok":
                    if reason == "filtered":
                        filtered += 1
                    else:
                        skipped += 1
                    continue
                known.add(int(payload["user_id"]))
                payloads.append(payload)
        elif mode == "comments":
            discussion_id = await _discussion_id(client, rule.source_id)
            if discussion_id is None:
                return {
                    "ok": False,
                    "error": "У канала нет группы обсуждений — собирать негде",
                    "collected": 0,
                    "mode": mode,
                    "scanned": 0,
                    "skipped": 0,
                    "filtered": 0,
                    "limit": result_limit,
                    "scan_limit": scan_limit,
                    "capped": False,
                }
            # Сначала считаем всех: лимит должен резать молчунов, а не первых
            # попавшихся. Комментарии — в группе обсуждений, а посты — в самом
            # канале: идём по постам, ответы добираем внизу.
            counts: dict[int, int] = {}
            async for post in client.iter_messages(rule.source_id, limit=scan_limit):
                if getattr(post, "replies", None) is None:
                    continue
                async for reply in client.iter_messages(
                    discussion_id, reply_to=int(post.id), limit=COMMENTS_PER_POST
                ):
                    sender_id = int(getattr(reply, "sender_id", 0) or 0)
                    if sender_id:
                        counts[sender_id] = counts.get(sender_id, 0) + 1
            # Сначала самые разговорчивые.
            for sender_id, total in sorted(counts.items(), key=lambda item: -item[1]):
                if len(payloads) >= result_limit:
                    break
                if delay:
                    await asyncio.sleep(delay)
                user = await client.get_entity(sender_id)
                scanned += 1
                payload, reason = _screen_user(user, conf, admin_ids, known)
                if reason != "ok":
                    if reason == "filtered":
                        filtered += 1
                    else:
                        skipped += 1
                    continue
                known.add(int(payload["user_id"]))
                payload["comments"] = total
                payloads.append(payload)
        else:
            async for user in client.iter_participants(rule.source_id, limit=scan_limit):
                if len(payloads) >= result_limit:
                    break
                scanned += 1
                if scanned % 200 == 0 and delay:
                    # Telethon тянет участников пачками: пауза раз в пачку и
                    # есть «пауза между запросами», а не сон после каждого.
                    await asyncio.sleep(delay)
                if getattr(user, "deleted", False) or getattr(user, "bot", False):
                    filtered += 1
                    continue
                user_id = int(getattr(user, "id", 0) or 0)
                if not user_id or user_id in known:
                    skipped += 1
                    continue
                if not _user_passes_filters(user, conf, admin_ids):
                    filtered += 1
                    continue
                known.add(user_id)
                payloads.append(_parser_payload(user))
    except FloodWaitError as exc:
        # Собранное до отказа сохраняем: иначе 180 найденных участников
        # пропадали вместе с ошибкой и следующий запуск начинал с нуля.
        added, capped = await _store(rule, "parser", payloads)
        return {
            "ok": False,
            "error": f"Telegram просит подождать {int(getattr(exc, 'seconds', 60))} сек",
            "collected": added,
            "partial": True,
            "capped": capped,
            "mode": mode,
            "scanned": scanned,
            "skipped": skipped,
            "filtered": filtered,
        }
    except RPCError as exc:
        added, capped = await _store(rule, "parser", payloads)
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "collected": added,
            "partial": True,
            "capped": capped,
            "mode": mode,
            "scanned": scanned,
            "skipped": skipped,
            "filtered": filtered,
        }

    added, capped = await _store(rule, "parser", payloads)
    return {
        "ok": True,
        "mode": mode,
        "collected": added,
        "scanned": scanned,
        "skipped": skipped,
        "filtered": filtered,
        "limit": result_limit,
        "scan_limit": scan_limit,
        "capped": capped,
    }


async def run_autosubscribe(client: Any, rule: RuleSnapshot) -> dict[str, Any]:
    """Вступает в чаты из настроек задачи и в ссылки, найденные в источнике."""
    targets = [
        str(item).strip() for item in (rule.filters.subscribe_to or []) if str(item).strip()
    ]
    outcome = JoinOutcome()
    conf = rule.filters
    gap = max(0, int(getattr(conf, "join_gap", JOIN_PAUSE_SECONDS) or 0))
    retries = max(0, int(getattr(conf, "join_retries", 0) or 0))

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

    per_run = max(0, int(getattr(conf, "join_limit", 0) or 0))
    if per_run > 0:
        unique = unique[:per_run]
    # Дневной лимит: сколько уже вступили сегодня — столько мест занято.
    # Исчерпанный лимит — не ошибка: остаток вступит завтра.
    stop_at: int | None = None
    daily = max(0, int(getattr(conf, "daily_join_limit", 0) or 0))
    if daily > 0:
        async with SessionLocal() as session:
            already_today = await repo.count_joins_today(session, rule.id)
        if already_today >= daily:
            outcome.limited = True
            outcome.problems.append(f"дневной лимит вступлений исчерпан ({daily} в сутки)")
            return {"ok": True, **_join_summary(outcome, len(unique))}
        stop_at = daily - already_today

    try:
        await _join_all(client, unique, outcome, rule=rule, gap=gap, retries=retries, stop_at=stop_at)
    except FloodWaitError as exc:
        # Вступления, сделанные до отказа, остались в outcome — их и показываем:
        # «вступили в 0» после трёх удачных заходов было бы неправдой.
        return {
            "ok": False,
            "error": f"Telegram просит подождать {int(getattr(exc, 'seconds', 60))} сек",
            **_join_summary(outcome, len(unique)),
        }
    if outcome.limited:
        outcome.problems.append(f"дневной лимит вступлений исчерпан ({daily} в сутки)")
    return {"ok": True, **_join_summary(outcome, len(unique))}


def _join_summary(outcome: JoinOutcome, total: int) -> dict[str, Any]:
    """Итог захода в чаты — полями ответа кабинету."""
    summary: dict[str, Any] = {
        "joined": outcome.joined,
        "already": outcome.already,
        "total": total,
        "problems": list(outcome.problems),
    }
    if outcome.limited:
        summary["limited"] = True
    return summary


async def _log_join(rule: RuleSnapshot) -> None:
    """Вступление — строкой в свой учёт: по ним считается дневной лимит."""
    async with SessionLocal() as session:
        await repo.log_join(session, rule.id, rule.user_id)
        await session.commit()


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


async def _join_all(
    client: Any,
    targets: list[str],
    outcome: JoinOutcome | None = None,
    *,
    rule: RuleSnapshot | None = None,
    gap: int = JOIN_PAUSE_SECONDS,
    retries: int = 0,
    stop_at: int | None = None,
) -> JoinOutcome:
    """Вступает в перечисленные чаты и рассказывает, что из этого вышло.

    FloodWait пробрасывает наверх: у задачи по сообщениям есть свой повтор после
    паузы (``run_job``). Чтобы при этом не потерялись уже сделанные вступления,
    итог можно передать своим — тогда после исключения в нём остаётся всё, во
    что успели войти до отказа.

    ``rule`` — чьё вступление писать в журнал (по записям считается дневной
    лимит); ``stop_at`` — остановиться, когда столько уже вступили в этом
    запуске (дневной лимит). Повторы (``retries``) переживают только короткий
    FloodWait — до минуты: часовое «подождите» сном не леча.
    """
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest

    result = outcome if outcome is not None else JoinOutcome()
    for target in targets:
        if stop_at is not None and result.joined >= stop_at:
            result.limited = True
            break
        attempts = max(0, retries) + 1
        while True:
            try:
                if target.startswith("+") or target.lower().startswith("joinchat/"):
                    invite_hash = target[1:] if target.startswith("+") else target.split("/", 1)[1]
                    await client(ImportChatInviteRequest(invite_hash))
                else:
                    await client(JoinChannelRequest(target))
                result.joined += 1
                if rule is not None:
                    await _log_join(rule)
                break
            except FloodWaitError as exc:
                attempts -= 1
                wait = int(getattr(exc, "seconds", 60))
                if attempts <= 0 or wait > 60:
                    raise
                logger.info("Автоподписка: FloodWait {} сек — ждём и повторяем {}", wait, target)
                await asyncio.sleep(wait + 1)
            except RPCError as exc:
                name = type(exc).__name__
                logger.info("Автоподписка: не вступили в {}: {}", target, name)
                if name in JOIN_ALREADY_FINE:
                    # «уже участник», «заявка отправлена» — тут нечего исправлять,
                    # и краснеть карточке незачем.
                    result.already += 1
                else:
                    result.problems.append(f"не пустили в {target} ({name})")
                break
        # пауза между вступлениями, иначе Telegram быстро присылает FloodWait
        if gap > 0:
            await asyncio.sleep(gap)
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


async def _store(rule: RuleSnapshot, kind: str, payloads: list[dict]) -> tuple[int, bool]:
    """Кладёт собранные результаты в БД. Возвращает (записано, упёрлись в лимит).

    Больше MAX_PARSER_LIMIT записей на правило не храним: иначе повторные
    запуски растят таблицу бесконечно, а множество «уже собранных» (оно
    читается с тем же лимитом) перестаёт их всех покрывать — и старые находки
    начинают дублироваться.
    """
    if not payloads:
        return 0, False
    async with SessionLocal() as session:
        existing = await repo.count_collected_items(session, rule.id)
        room = MAX_PARSER_LIMIT - existing
        if room <= 0:
            return 0, True
        trimmed = payloads[:room]
        added = await repo.add_collected_items(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            kind=kind,
            payloads=trimmed,
        )
        await session.commit()
    return added, len(payloads) > room


# Сносов за проход уборщика: сотня удалений — уже заметная пачка запросов,
# остальное подождёт следующего круга (20 секунд — не срок для часов жизни).
AUTODELETE_BATCH = 100
# Неудачных попыток снести одно сообщение: дальше строка считается мёртвой.
AUTODELETE_ATTEMPTS = 10


async def schedule_autodelete(rule: RuleSnapshot, chat_id: int, msg_id: int) -> None:
    """Планирует снос отправленного, если в задаче стоят часы жизни.

    Рассылка и постер зовут после каждой отправки. Своя сессия и best effort:
    планировщик шлёт дальше, даже если запись не легла (сообщение тогда
    останется — об этом скажет лог службы, а не молчание).
    """
    from app.db.database import session_scope
    from app.telegram_client.filters import autodelete_hours

    hours = autodelete_hours(getattr(rule, "filters", None))
    if hours <= 0 or not msg_id:
        return
    try:
        async with session_scope() as session:
            await repo.schedule_delete(
                session,
                rule_id=rule.id,
                user_id=rule.user_id,
                account_id=rule.account_id,
                chat_id=int(chat_id),
                msg_id=int(msg_id),
                delete_at=repo.utcnow() + timedelta(hours=hours),
            )
    except Exception as exc:  # noqa: BLE001 — планировщик шлёт дальше
        logger.warning(
            "Задача #{}: не записали снос {} в {}: {}", rule.id, msg_id, chat_id, exc
        )


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
    # Третья подряд — письмом человеку, а не только строкой в журнал.
    from app.task_alerts import maybe_alert_problem

    await maybe_alert_problem(rule, error)


async def record_pruned_chats(rule: RuleSnapshot, pruned: list[int]) -> None:
    """Мёртвые получатели убраны из задачи — одной строкой в журнал.

    Статус «успех»: задача самовылечилась, а не сломалась. Текст — в поле
    причины: карточка его не покажет (там только несчастья), а в журнале
    уборка видна — молча выкидывать чаты из чужой задачи нельзя.
    """
    if not pruned:
        return
    chats = ", ".join(str(chat_id) for chat_id in sorted(pruned))
    async with SessionLocal() as session:
        await repo.log_forward(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            source_msg_id=0,
            target_msg_id=None,
            status="ok",
            error=f"🧹 Убраны мёртвые чаты ({repo.DEAD_CHAT_STRIKES} сбоя подряд): {chats}",
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
    # Проход, где не ушло ничего: частичные сбои («в три чата не ушло, а сто
    # получили») остаются в журнале и на карточке — письмом о каждом таком
    # проходе мы бы завалили личку.
    if failed and not sent:
        from app.task_alerts import maybe_alert_problem

        await maybe_alert_problem(rule, batch_error_text(failed))


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
