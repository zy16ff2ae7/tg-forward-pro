"""Общие типы юзебот-слоя (лежат отдельно, чтобы не было циклических импортов)."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.telegram_client.filters import FilterConfig


@dataclass(slots=True)
class RuleSnapshot:
    """Снимок правила для быстрой работы в обработчике сообщений."""

    id: int
    user_id: int
    target_id: int
    account_id: int
    mode: str
    delay_seconds: int
    filters: FilterConfig = field(default_factory=FilterConfig)
    # forward — обычная пересылка; остальные значения — задачи из jobs.py
    kind: str = "forward"
    source_id: int = 0
    # Названия чатов нужны для понятных логов и заголовков: по одним id не скажешь,
    # что за канал, а в моменты ошибок видеть название важнее экономии памяти.
    source_title: str = ""
    target_title: str = ""
    # Актуальное состояние правила (нужно планировщику авто-постера, чтобы
    # не слать в паузу/архив и не держать кэш лишних правил).
    enabled: bool = True
    archived: bool = False
