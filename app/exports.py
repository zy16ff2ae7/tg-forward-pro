"""Собранное — одним файлом.

Список участников или пойманных чеков лежал в базе, а забрать его было нельзя:
в шторке кабинета видна первая сотня, в боте — двадцать строк, и всё. Здесь
собирается CSV, который бот присылает в чат: внутри Telegram это единственный
надёжный способ отдать файл — WebView не сохраняет скачанное.

Формат — под русский Excel: BOM в начале (без него кириллица открывается
кракозябрами) и точка с запятой вместо запятой (в русской локали запятая —
десятичный разделитель, и строка расползается по колонкам).
"""
from __future__ import annotations

import codecs
import csv
import io
import json
import re
from datetime import datetime, timedelta
from html import escape
from typing import Any, Sequence

from app.timeutil import tz_suffix as _tz_suffix
from app.timeutil import utcnow

# Заголовок — теми же словами, что и в шторке кабинета: человек нажал
# «Результаты», получил файл и должен узнать в подписи то же самое.
EXPORT_TITLES: dict[str, str] = {
    "parser": "Собранная аудитория",
    "checks": "Пойманные чеки",
}
EXPORT_FALLBACK_TITLE = "Собранное"

# Имя файла — латиницей: часть настольных клиентов и файловых систем спотыкается
# о кириллицу во вложении и подсовывает вместо имени набор процентов.
_FILE_STEMS: dict[str, str] = {"parser": "audience", "checks": "checks"}

# Ячейка, начинающаяся с этих знаков, в Excel и Google Таблицах считается
# формулой. Имя в Telegram человек пишет себе сам — то есть в него можно
# положить «=HYPERLINK(...)», и таблица выполнит это при открытии файла.
# Поэтому такие значения помечаем апострофом: он делает ячейку текстом.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")

# Подпись к документу в Telegram — не длиннее 1024 знаков, поэтому длинное имя
# задачи режем сами: иначе Bot API отклонит весь файл целиком.
CAPTION_TITLE_LIMIT = 120


def _cell(value: Any) -> str:
    """Текст в ячейку: одной строкой, без переводов, без формул."""
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if text[:1] in _FORMULA_START:
        text = "'" + text
    return text


def _number(value: Any) -> str:
    """Число как есть: id чата бывает отрицательным, и это не формула."""
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return _cell(value)


def _phone(value: Any) -> str:
    """Только цифры: «+7…» таблица приняла бы за формулу и съела бы плюс."""
    return re.sub(r"\D", "", str(value or ""))


def tz_suffix(tz_minutes: int | None) -> str:
    """Чем подписать колонку времени: «UTC+3» или просто «UTC».

    В базе всё в UTC, а человек читает файл по своим часам. Без подписи он бы
    решил, что парсер работал ночью, и не поверил бы собранному. Сама подпись
    живёт в ``app.timeutil``: теми же словами названо окно постинга в карточке.
    """
    return _tz_suffix(tz_minutes)


def _when(value: Any, tz_minutes: int | None) -> str:
    """Дата и время по часам хозяина задачи. В базе — UTC без tzinfo."""
    if not isinstance(value, datetime):
        return ""
    return (value + timedelta(minutes=int(tz_minutes or 0))).strftime("%d.%m.%Y %H:%M")


def _table(
    kind: str, items: Sequence[Any], tz_minutes: int | None
) -> tuple[list[str], list[list[str]]]:
    """Заголовки и строки для одного вида сборщика."""
    when = f"когда ({tz_suffix(tz_minutes)})"
    rows: list[list[str]] = []

    if kind == "parser":
        # Комментаторы несут счётчик — им отдельная колонка. Остальным она ни
        # к чему: пустая колонка в тысяче строк — мусор, а не информация.
        with_comments = any(
            (getattr(item, "payload", None) or {}).get("comments") for item in items
        )
        head = ["id", "ник", "имя", "телефон"]
        if with_comments:
            head.append("комментариев")
        head.append(when)
        for item in items:
            payload = getattr(item, "payload", None) or {}
            row = [
                _number(payload.get("user_id")),
                _cell(payload.get("username")),
                _cell(payload.get("name")),
                _phone(payload.get("phone")),
            ]
            if with_comments:
                row.append(_number(payload.get("comments")))
            row.append(_when(getattr(item, "created_at", None), tz_minutes))
            rows.append(row)
        return head, rows

    if kind == "checks":
        head = [when, "ссылка", "текст", "чат", "сообщение"]
        for item in items:
            payload = getattr(item, "payload", None) or {}
            rows.append(
                [
                    _when(getattr(item, "created_at", None), tz_minutes),
                    _cell(payload.get("link")),
                    _cell(payload.get("text")),
                    _number(payload.get("chat_id")),
                    _number(payload.get("message_id")),
                ]
            )
        return head, rows

    # Незнакомый сборщик: отдаём payload как есть, но файл всё равно отдаём —
    # потерять собранное из-за нового типа задачи хуже, чем показать JSON.
    head = [when, "данные"]
    for item in items:
        rows.append(
            [
                _when(getattr(item, "created_at", None), tz_minutes),
                _cell(json.dumps(getattr(item, "payload", None) or {}, ensure_ascii=False)),
            ]
        )
    return head, rows


def collected_csv(kind: str, items: Sequence[Any], tz_minutes: int | None = None) -> bytes:
    """Собранное → байты CSV: BOM, «;» и переводы строк как у Windows."""
    head, rows = _table(kind, items, tz_minutes)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(head)
    writer.writerows(rows)
    return codecs.BOM_UTF8 + buffer.getvalue().encode("utf-8")


def export_filename(kind: str, rule_id: int, when: datetime | None = None) -> str:
    """Имя вложения: по чему собрано, из какой задачи и когда выгружено."""
    stamp = (when or utcnow()).strftime("%Y-%m-%d-%H%M")
    return f"{_FILE_STEMS.get(kind, 'collected')}-{int(rule_id)}-{stamp}.csv"


def export_caption(kind: str, *, rule_title: str, sent: int, total: int) -> str:
    """Подпись к файлу: что внутри, из какой задачи и сколько строк.

    ``sent`` меньше ``total`` — значит выгрузили не всё (упёрлись в потолок), и
    промолчать нельзя: человек считал бы, что забрал всю аудиторию.
    """
    title = EXPORT_TITLES.get(kind, EXPORT_FALLBACK_TITLE)
    name = str(rule_title or "").strip()
    if len(name) > CAPTION_TITLE_LIMIT:
        name = name[:CAPTION_TITLE_LIMIT].rstrip() + "…"
    count = f"{sent} из {total}" if sent < total else str(sent)
    tail = f"\n\nЗадача «{escape(name, quote=False)}»." if name else ""
    return f"📄 <b>{title}</b>: {count}{tail}"
