"""Фильтры и преобразования текста перед пересылкой.

Настройки хранятся в Rule.filters (JSON). Неизвестные ключи игнорируются,
поэтому старые правила продолжают работать после добавления новых полей.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.errors import ValidationError

URL_RE = re.compile(r"(https?://\S+|t\.me/\S+|telegram\.me/\S+)", re.IGNORECASE)
MENTION_RE = re.compile(r"@[A-Za-z0-9_]{3,}")

MEDIA_KINDS = ("text", "photo", "video", "document", "audio", "voice", "sticker", "animation")


def default_filters() -> dict:
    """Значения по умолчанию для нового правила."""
    return {
        "whitelist": [],        # если не пусто — пропускаем только с этими словами
        "blacklist": [],        # если есть хотя бы одно слово — пост отбрасываем
        "media_types": list(MEDIA_KINDS),  # какие типы сообщений пересылать
        "min_length": 0,        # минимальная длина текста
        "skip_forwards": False, # не пересылать уже пересланные сообщения
        "remove_links": False,  # вырезать ссылки
        "remove_mentions": False,  # вырезать @упоминания
        "replace": [],          # [{"from": "старое", "to": "новое"}, ...]
        "append_text": "",      # дописать в конец поста
        # ── Ниже — настройки задач из app.telegram_client.jobs ──
        "targets": [],          # рассылка: доп. получатели (кроме rule.target_id)
        "subscribe_to": [],     # автоподписка: @username или ссылки, куда вступаем
        "reaction": "👍",       # байтинг: чем реагируем
        "target_user_id": 0,    # байтинг/мут: за кем следим (0 — за всеми)
        "keywords": [],         # ловец чеков и уведомления: слова-триггеры
        "limit": 200,           # парсер: сколько участников собрать за запуск
        # ── Рассылка по чатам (kind="mailing") ──
        "gap_seconds": 5,       # пауза между получателями
        "gap_jitter": 0,        # к паузе между получателями добавляем 0..N секунд
        "cycle_seconds": 10,    # пауза перед следующим кругом рассылки
        "cycle_jitter": 0,      # к паузе между кругами добавляем 0..N секунд
        "repeats": 0,           # сколько кругов сделать (0 — без лимита)
        "typing": False,        # показывать «печатает» перед отправкой
        "link_preview": False,  # оставлять блок предпросмотра ссылки
        "random_pick": False,   # брать из набора случайное сообщение, а не по кругу
        "library_ids": [],      # id сохранённых сообщений (таблица saved_messages)
    }


@dataclass(slots=True)
class FilterConfig:
    """Типизированная обёртка над JSON-настройками правила."""

    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=lambda: list(MEDIA_KINDS))
    min_length: int = 0
    skip_forwards: bool = False
    remove_links: bool = False
    remove_mentions: bool = False
    replace: list[dict[str, str]] = field(default_factory=list)
    append_text: str = ""
    # Кнопки-ссылки под постом: [{'text', 'url'}]. Форвард их не умеет —
    # прикладываются только в режиме «копия» (см. send_copy).
    buttons: list = field(default_factory=list)
    # Перевод чужих постов: код языка («ru») или пусто — не переводить.
    translate_to: str = ""
    # Уникализация чужого текста: омоглифы + безопасные синонимы.
    uniquify: bool = False
    # Клон канала: сколько постов истории забрать и что уже забрали.
    clone_history: int = 0
    clone_ids: list = field(default_factory=list)
    clone_listed: bool = False
    clone_done: bool = False
    # Парсер: куда звать собранных людей (ссылка или id чата).
    invite_to: str = ""
    # Письма о третьей ошибке подряд. Выключается галочкой в задаче.
    alerts: bool = True
    # Счётчик сбоев по чатам {chat_id: {fails, error}} и сколько уже убрано.
    chat_strikes: dict = field(default_factory=dict)
    chats_pruned: int = 0
    # Модерация: запретные слова для всех, блок ссылок, лесенка варнов.
    banned_words: list = field(default_factory=list)
    block_links: bool = False
    max_warns: int = 3
    mute_hours: int = 24
    mod_strikes: dict = field(default_factory=dict)
    # Настройки задач из app.telegram_client.jobs
    targets: list[int] = field(default_factory=list)
    subscribe_to: list[str] = field(default_factory=list)
    reaction: str = "👍"
    target_user_id: int = 0
    keywords: list[str] = field(default_factory=list)
    limit: int = 200
    # ── Парсер аудитории (kind="parser"): limit — сколько сохранить ──
    parser_mode: str = "participants"  # participants | history (авторы сообщений)
    require_username: bool = True  # без @username участник бесполезен для рассылки
    exclude_admins: bool = True  # админы источника в базу не попадают
    only_premium: bool = False  # только с Telegram Premium
    only_with_photo: bool = False  # только с аватаркой
    active_only: bool = False  # только живые: онлайн или заходили в последние 3 суток
    online_within_hours: int = 0  # заходили не раньше N часов назад (0 — не важно)
    scan_limit: int = 1000  # сколько просмотреть (участников или сообщений)
    api_delay: int = 0  # пауза между запросами к Telegram, сек
    # ── Уведомления из диалогов (kind="dialogs") ──
    ignore_bots: bool = True  # не уведомлять о сообщениях ботов
    ignore_archived: bool = True  # не уведомлять из архивных чатов
    ignore_muted: bool = True  # не уведомлять из заглушённых чатов
    # ── Автоподписка (kind="autosubscribe") ──
    join_limit: int = 0  # вступить за один запуск (0 — во все)
    join_gap: int = 2  # пауза между вступлениями, сек
    join_retries: int = 0  # повторы вступления при коротком FloodWait
    daily_join_limit: int = 0  # вступлений в сутки на задачу (0 — без лимита)
    # ── Настройки авто-постера (планировщик собственных сообщений) ──
    messages: list[str] = field(default_factory=list)  # тексты сообщений (по одному в строке)
    interval_seconds: int = 120  # интервал между отправками
    window_start: str = "00:00"  # начало окна ЧЧ:ММ
    window_end: str = "23:59"  # конец окна ЧЧ:ММ
    # Чьи это часы: смещение хозяина задачи от UTC в минутах (Москва — 180).
    # None — часы сервера, как у задач, созданных до появления настройки
    # (см. jobs.window_now_sec).
    window_tz: int | None = None
    # Только по расписанию: круги по интервалу выключены, шлём слоты из
    # scheduled_posts (каждый — {"id", "at" (UTC ISO), "text"/"library_id",
    # "sent", "sent_to"}). Прошедшие даты уходят на ближайшем проходе.
    schedule_only: bool = False
    scheduled_posts: list = field(default_factory=list)
    # ── Рассылка по чатам (kind="mailing") ──
    gap_seconds: int = 5  # пауза между получателями
    gap_jitter: int = 0  # случайная добавка к паузе между получателями
    cycle_seconds: int = 10  # пауза перед следующим кругом
    cycle_jitter: int = 0  # случайная добавка к паузе между кругами
    # Кругов по умолчанию нет предела: настройка не задана — значит рассылка
    # крутится, пока её не остановят. Число кругов приходит из кабинета явно.
    repeats: int = 0  # сколько кругов (0 — без лимита)
    typing: bool = False  # показывать «печатает»
    link_preview: bool = False  # оставлять предпросмотр ссылки
    random_pick: bool = False  # случайное сообщение из набора
    library_ids: list[int] = field(default_factory=list)  # id из saved_messages

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "FilterConfig":
        raw = dict(raw or {})
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {key: value for key, value in raw.items() if key in known}
        return cls(**clean)

    def to_dict(self) -> dict:
        return {
            "whitelist": self.whitelist,
            "blacklist": self.blacklist,
            "media_types": self.media_types,
            "min_length": self.min_length,
            "skip_forwards": self.skip_forwards,
            "remove_links": self.remove_links,
            "remove_mentions": self.remove_mentions,
            "replace": self.replace,
            "append_text": self.append_text,
            "buttons": self.buttons,
            "translate_to": self.translate_to,
            "uniquify": self.uniquify,
            "clone_history": self.clone_history,
            "clone_ids": self.clone_ids,
            "clone_listed": self.clone_listed,
            "clone_done": self.clone_done,
            "invite_to": self.invite_to,
            "alerts": self.alerts,
            "chat_strikes": self.chat_strikes,
            "chats_pruned": self.chats_pruned,
            "banned_words": self.banned_words,
            "block_links": self.block_links,
            "max_warns": self.max_warns,
            "mute_hours": self.mute_hours,
            "mod_strikes": self.mod_strikes,
            "targets": self.targets,
            "subscribe_to": self.subscribe_to,
            "reaction": self.reaction,
            "target_user_id": self.target_user_id,
            "keywords": self.keywords,
            "limit": self.limit,
            "parser_mode": self.parser_mode,
            "require_username": self.require_username,
            "exclude_admins": self.exclude_admins,
            "only_premium": self.only_premium,
            "only_with_photo": self.only_with_photo,
            "active_only": self.active_only,
            "online_within_hours": self.online_within_hours,
            "scan_limit": self.scan_limit,
            "api_delay": self.api_delay,
            "ignore_bots": self.ignore_bots,
            "ignore_archived": self.ignore_archived,
            "ignore_muted": self.ignore_muted,
            "join_limit": self.join_limit,
            "join_gap": self.join_gap,
            "join_retries": self.join_retries,
            "daily_join_limit": self.daily_join_limit,
            "messages": self.messages,
            "interval_seconds": self.interval_seconds,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "window_tz": self.window_tz,
            "schedule_only": self.schedule_only,
            "scheduled_posts": self.scheduled_posts,
            "gap_seconds": self.gap_seconds,
            "gap_jitter": self.gap_jitter,
            "cycle_seconds": self.cycle_seconds,
            "cycle_jitter": self.cycle_jitter,
            "repeats": self.repeats,
            "typing": self.typing,
            "link_preview": self.link_preview,
            "random_pick": self.random_pick,
            "library_ids": self.library_ids,
        }


def media_kind(message: Any) -> str:
    """Определяет тип сообщения: text / photo / video / document / ..."""
    media = getattr(message, "media", None)
    if media is None:
        return "text"
    for kind in ("photo", "video", "document", "audio", "voice", "sticker", "animation"):
        if getattr(media, kind, None) is not None:
            return kind
    # web_page, geo, poll и пр. считаем документом-«прочим»
    return "document"


def message_text(message: Any) -> str:
    """Текст сообщения с учётом подписи к медиа."""
    text = getattr(message, "message", None) or getattr(message, "text", None) or ""
    return text


def _meaningful(words: list[str]) -> list[str]:
    """Отбрасывает пустые и пробельные записи списков слов.

    Без этого список из одних пустых строк считался бы заданным фильтром и
    отсеивал все сообщения подряд — пользователь видит мёртвое правило без
    видимой причины.
    """
    return [word.strip() for word in words if word and word.strip()]


def should_forward(message: Any, config: FilterConfig) -> bool:
    """Проверяет, проходит ли сообщение через фильтры правила."""
    if config.skip_forwards and getattr(message, "forward", None) is not None:
        return False

    kind = media_kind(message)
    if config.media_types and kind not in config.media_types:
        return False

    text = message_text(message)
    if config.min_length and len(text) < config.min_length:
        return False

    lowered = text.lower()
    for bad in _meaningful(config.blacklist):
        if bad.lower() in lowered:
            return False

    whitelist = _meaningful(config.whitelist)
    if whitelist and not any(word.lower() in lowered for word in whitelist):
        return False

    return True


def transform_text(text: str, config: FilterConfig) -> str:
    """Применяет к тексту замены, вырезание ссылок/упоминаний и суффикс."""
    result = text

    for pair in config.replace:
        src = (pair or {}).get("from") or ""
        dst = (pair or {}).get("to") or ""
        if src:
            result = result.replace(src, dst)

    if config.remove_links:
        result = URL_RE.sub("", result)
    if config.remove_mentions:
        result = MENTION_RE.sub("", result)

    if config.append_text:
        result = (result.rstrip() + "\n" + config.append_text).strip()

    # подчищаем тройные переводы строк, которые часто появляются после вырезаний
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = result.strip()
    if config.uniquify:
        # Последней: замены и подпись заданы обычными буквами и должны лечь на
        # текст до подмены символов, иначе их паттерны не совпадут.
        result = uniquify_text(result)
    return result


def parse_words(raw: str) -> list[str]:
    """Разбирает ввод пользователя: «слово1, слово2» или по строкам."""
    chunks = re.split(r"[,\n;]+", raw or "")
    return [chunk.strip() for chunk in chunks if chunk.strip()]


# Кириллица → неотличимая на глаз латиница. Набор маленький специально: каждая
# замена видна поиску, но не читателю, а тотальная подмена всех букв подряд —
# уже след для спам-фильтров, а не маскировка.
_HOMOGLYPHS = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H",
    "О": "O", "Р": "P", "С": "C", "Т": "T", "Х": "X",
}

# Синонимы, которые не ломают грамматику: наречия и прилагательные в начальной
# форме. Замена детерминированная — один и тот же пост всегда даёт один и тот
# же текст, иначе повторы плодили бы разные версии одного поста.
_SYNONYMS = {
    "очень": "весьма", "быстро": "стремительно", "сейчас": "прямо сейчас",
    "потом": "затем", "также": "к тому же", "иногда": "время от времени",
    "всегда": "постоянно", "никогда": "ни разу", "сразу": "тотчас",
    "большой": "крупный", "маленький": "небольшой", "хороший": "отличный",
    "плохой": "скверный", "новый": "новейший", "важный": "значимый",
    "главный": "ключевой", "известный": "знаменитый", "сильный": "мощный",
    "красивый": "прекрасный", "начать": "стартовать", "купить": "приобрести",
    "получить": "обрести", "сделать": "выполнить", "использовать": "применять",
}
_SYNONYM_RE = re.compile(
    r"\b(" + "|".join(sorted(_SYNONYMS, key=len, reverse=True)) + r")\b",
    re.IGNORECASE | re.UNICODE,
)


def uniquify_text(text: str) -> str:
    """Делает чужой текст непохожим на исходник для поиска, не для читателя."""
    if not text:
        return text

    def synonym(match: re.Match) -> str:
        word = match.group(0)
        replacement = _SYNONYMS[word.lower()]
        if word[0].isupper():
            replacement = replacement[0].upper() + replacement[1:]
        return replacement

    return "".join(
        _HOMOGLYPHS.get(char, char) for char in _SYNONYM_RE.sub(synonym, text)
    )


# Кнопок под постом: больше — уже меню, а не подпись.
BUTTONS_CAP = 6
BUTTON_TEXT_CAP = 64


def normalize_buttons(raw: Any) -> list[dict]:
    """Кнопки из формы — в хранимый вид [{'text', 'url'}].

    Мусор отклоняется с номером кнопки: молча выкинутая кнопка — это пост без
    ссылки «Купить», о чём человек узнает от клиентов, а не от нас. Короткие
    ссылки вида t.me/имя дописываются до полного https.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [line for line in raw.splitlines() if line.strip()]
    if not isinstance(raw, list):
        raise ValidationError("Кнопки — списком «текст — ссылка»")
    buttons: list[dict] = []
    for pos, entry in enumerate(raw, start=1):
        if len(buttons) >= BUTTONS_CAP:
            raise ValidationError(f"Не больше {BUTTONS_CAP} кнопок под постом")
        if isinstance(entry, str) and "|" in entry:
            # Строка из кабинета: «Подписаться | https://t.me/канал».
            name, _, link = entry.partition("|")
            entry = {"text": name.strip(), "url": link.strip()}
        if not isinstance(entry, dict):
            raise ValidationError(f"Кнопка №{pos}: нужны текст и ссылка")
        name = str(entry.get("text") or "").strip()
        link = str(entry.get("url") or "").strip()
        if not name:
            raise ValidationError(f"Кнопка №{pos}: пустой текст")
        if len(name) > BUTTON_TEXT_CAP:
            raise ValidationError(f"Кнопка №{pos}: текст длиннее {BUTTON_TEXT_CAP}")
        if link.startswith("t.me/"):
            link = "https://" + link
        if not re.match(r"^https?://\S+$", link):
            raise ValidationError(f"Кнопка №{pos}: ссылка должна начинаться с http")
        buttons.append({"text": name, "url": link})
    return buttons
