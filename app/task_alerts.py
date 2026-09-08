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
async def alert_pause_started(user_id: int, phone: str, until_text: str) -> None:
    """Пишет, что аккаунт встал на паузу после спам-ограничения. Не падает никогда.

    В отличие от писем о больных задачах, это письмо — не про третью ошибку, а
    про рубильник: он встаёт один раз на много часов, и человек должен узнать
    об этом сразу из лички, а не когда-нибудь из карточки. Настройку «alerts»
    задачи не смотрим: пауза — событие аккаунта, а не задачи.
    """
    try:
        if _bot is None or not user_id:
            return
        from app.bot.keyboards import cabinet_button

        await _bot.send_message(
            user_id,
            f"🛡 <b>Безопасный режим: {escape(phone)}</b>\n"
            "Telegram ограничил аккаунт за спам — отправки всех его задач "
            f"на паузе до {escape(until_text)}.\n"
            "Не запускайте задачи вручную: ограничение спадёт само.",
            reply_markup=cabinet_button(),
        )
    except Exception as exc:  # noqa: BLE001 — алерт не должен ронять доставку
        logger.warning("Не удалось отправить письмо о паузе аккаунта: {}", exc)


async def alert_account_dead(user_id: int, phone: str, reason: str) -> None:
    """Пишет, что сессия аккаунта мертва и он выведен из работы. Не падает никогда."""
    try:
        if _bot is None or not user_id:
            return
        from app.bot.keyboards import cabinet_button

        await _bot.send_message(
            user_id,
            f"🔌 <b>Аккаунт отключён: {escape(phone)}</b>\n"
            f"{escape(reason)}\n"
            "Подключите его заново в «👤 Аккаунты».",
            reply_markup=cabinet_button(),
        )
    except Exception as exc:  # noqa: BLE001 — алерт не должен ронять доставку
        logger.warning("Не удалось отправить письмо о мёртвом аккаунте: {}", exc)
