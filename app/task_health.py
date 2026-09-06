"""Сбой задачи человеческими словами — одинаково в кабинете и в боте.

Причина сбоя приходит из журнала (``repo.task_health``) такой, какой её записал
планировщик: с id чатов и без ограничения длины. Показывать её так нельзя —
«не ушло в -1001234567890» человеку ничего не говорит, а полный текст ошибки
Telegram занимает пол-экрана. Раньше это чинил только кабинет, поэтому бот
показать причину не мог вообще: карточка задачи молчала о том, что задача сутки
падает.
"""
from __future__ import annotations

import re
from typing import Any

# id чата в тексте сбоя: «-1001234567890» человеку ничего не говорит, а название
# у задачи уже запомнено. Пять цифр и больше — чтобы не трогать номера ошибок.
_CHAT_ID_RE = re.compile(r"-?\d{5,}")

# Причина сбоя на карточке: длиннее в узкий экран не влезает, а полный текст
# остаётся в журнале.
ERROR_TEXT_LIMIT = 160


def chat_names(rule: Any) -> dict[str, str]:
    """Названия чатов задачи: ``{"-1001": "Театр у моря"}``.

    В колонках правила есть имя только первого чата, остальные — числа, поэтому
    имена запоминаются в настройках при создании и правке. Старые задачи их не
    знают — там честно останется id.
    """
    names = dict((getattr(rule, "filters", None) or {}).get("chat_titles") or {})
    if getattr(rule, "source_id", None) and getattr(rule, "source_title", None):
        names.setdefault(str(rule.source_id), rule.source_title)
    if getattr(rule, "target_id", None) and getattr(rule, "target_title", None):
        names.setdefault(str(rule.target_id), rule.target_title)
    return names


def error_text(
    error: Any, names: dict[str, str] | None = None, *, limit: int = ERROR_TEXT_LIMIT
) -> str:
    """Причина сбоя для карточки: с названиями чатов вместо id и без простыни."""
    text = str(error or "").strip()
    if not text:
        return ""
    known = names or {}
    text = _CHAT_ID_RE.sub(lambda m: known.get(m.group(0)) or m.group(0), text)
    if len(text) > limit:
        text = f"{text[:limit].rstrip()}…"
    return text
