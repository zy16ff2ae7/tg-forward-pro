"""Единое «сейчас» для всего сервиса.

Все даты в БД хранятся в UTC без tzinfo: SQLite не умеет таймзоны, а сравнивать
и сортировать такие значения можно напрямую. Раньше «сейчас» считалось в двух
местах (модели и репозиторий) — расхождение было бы незаметным, пока не станет
фатальным, поэтому источник ровно один.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Текущее время в UTC, без tzinfo — под формат колонок БД."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def tz_suffix(tz_minutes: int | None) -> str:
    """Чьи это часы: «UTC+3», «UTC-5:30» или просто «UTC».

    Одна подпись на весь сервис: колонка времени в выгрузке и окно постинга в
    карточке задачи должны называть смещение одинаково, иначе человек читает два
    разных обозначения одних и тех же часов.
    """
    minutes = int(tz_minutes or 0)
    if not minutes:
        return "UTC"
    hours, rest = divmod(abs(minutes), 60)
    tail = f":{rest:02d}" if rest else ""
    return f"UTC{'+' if minutes > 0 else '-'}{hours}{tail}"


def plural_ru(count: int, one: str, few: str, many: str) -> str:
    """Русский счёт: 1 минуту / 2 минуты / 5 минут."""
    tail = abs(int(count)) % 100
    if 11 <= tail <= 14:
        return many
    last = tail % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def time_ago(moment: datetime | None, *, now: datetime | None = None) -> str:
    """«5 минут назад» — теми же словами, что и в кабинете (``timeAgo`` в app.js).

    Давно ли задача что-то делала — единственное, что человеку нужно от журнала:
    по одному счётчику не понять, идёт работа прямо сейчас или встала неделю
    назад. Пустое время даёт пустую строку: что сказать вместо неё, решает сам
    вызывающий — «ещё ни разу» и «нет сбоев» это разные вещи.

    Округляем половину вверх, как ``Math.round`` в браузере: иначе одно и то же
    время в кабинете и в боте называлось бы по-разному.
    """
    if not isinstance(moment, datetime):
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    seconds = max(0, int(((now or utcnow()) - moment).total_seconds() + 0.5))
    if seconds < 60:
        return "только что"
    minutes = int(seconds / 60 + 0.5)
    if minutes < 60:
        return f"{minutes} {plural_ru(minutes, 'минуту', 'минуты', 'минут')} назад"
    hours = int(minutes / 60 + 0.5)
    if hours < 24:
        return f"{hours} {plural_ru(hours, 'час', 'часа', 'часов')} назад"
    days = int(hours / 24 + 0.5)
    return f"{days} {plural_ru(days, 'день', 'дня', 'дней')} назад"
