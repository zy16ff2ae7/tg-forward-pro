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
