"""Письма о больных задачах: третья ошибка подряд — в личку через бота.

Задача может сломаться тихо: чат удалили, аккаунт вылетел, ссылку отозвали.
Журнал это покажет, но журнал надо открыть. Поэтому третья ошибка подряд без
успеха между ними уходит человеку личным сообщением — один раз за серию:
четвёртая и дальше молчат, иначе больной чат завалил бы личку. Выздоровление
(любой успех) обнуляет серию, и следующая болезнь снова пишет один раз.

Состояния не храним: серия считается по журналу, так что рестарт ничего не
теряет и не задваивает. Бота может не быть (кабинет без ботовой части) —
тогда молча пропускаем: алерты не должны ронять доставку.
"""
from __future__ import annotations

from html import escape
from typing import Any

from loguru import logger

from app.db import repo
from app.db.database import SessionLocal

# Сколько ошибок подряд без успеха — повод написать человеку.
ALERT_STREAK = 3

_bot: Any = None


def set_alert_bot(bot: Any) -> None:
    """Бот для писем. Ставит точка входа, где бот уже создан."""
    global _bot
    _bot = bot


async def maybe_alert_problem(rule: Any, reason: str) -> None:
    """Пишет о третьей ошибке подряд. Не падает никогда."""
    try:
        if not getattr(getattr(rule, "filters", None), "alerts", True):
            return
        if _bot is None:
            return
        async with SessionLocal() as session:
            streak = await repo.count_trailing_errors(
                session, rule.id, ALERT_STREAK + 1
            )
        # Ровно три: меньше — рано, больше — уже писали на третьей.
        if streak != ALERT_STREAK:
            return
        from app.telegram_client.jobs import task_title
        from app.bot.keyboards import cabinet_button

        title = escape(str(task_title(rule)))
        text = str(reason or "").strip()
        if len(text) > 200:
            text = text[:200].rstrip() + "…"
        await _bot.send_message(
            rule.user_id,
            f"⚠️ <b>{title}</b>\n{escape(text)}\nОшибка третья подряд — "
            "откройте задачу, в журнале подробности.",
            reply_markup=cabinet_button(),
        )
    except Exception as exc:  # noqa: BLE001 — алерт не должен ронять доставку
        logger.warning("Не удалось отправить алерт задачи #{}: {}", getattr(rule, "id", "?"), exc)
