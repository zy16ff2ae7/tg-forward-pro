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
