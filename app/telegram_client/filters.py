"""Фильтры и преобразования текста перед пересылкой.

Настройки хранятся в Rule.filters (JSON). Неизвестные ключи игнорируются,
поэтому старые правила продолжают работать после добавления новых полей.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

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
    # Настройки задач из app.telegram_client.jobs
    targets: list[int] = field(default_factory=list)
    subscribe_to: list[str] = field(default_factory=list)
    reaction: str = "👍"
    target_user_id: int = 0
    keywords: list[str] = field(default_factory=list)
    limit: int = 200
    # ── Настройки авто-постера (планировщик собственных сообщений) ──
    messages: list[str] = field(default_factory=list)  # тексты сообщений (по одному в строке)
    interval_seconds: int = 120  # интервал между отправками
    window_start: str = "00:00"  # начало окна ЧЧ:ММ
    window_end: str = "23:59"  # конец окна ЧЧ:ММ

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
            "targets": self.targets,
            "subscribe_to": self.subscribe_to,
            "reaction": self.reaction,
            "target_user_id": self.target_user_id,
            "keywords": self.keywords,
            "limit": self.limit,
            "messages": self.messages,
            "interval_seconds": self.interval_seconds,
            "window_start": self.window_start,
            "window_end": self.window_end,
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
    return result.strip()


def parse_words(raw: str) -> list[str]:
    """Разбирает ввод пользователя: «слово1, слово2» или по строкам."""
    chunks = re.split(r"[,\n;]+", raw or "")
    return [chunk.strip() for chunk in chunks if chunk.strip()]
