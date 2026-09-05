"""Подарок за подписку на канал сервиса.

Одна проверка на два входа: карточка в кабинете и кнопка в боте зовут
``claim()`` и показывают ``message()``. Поэтому «не вижу вас в канале»
звучит там одинаково, а правило «подарок разовый» живёт в одном месте, а не
в двух копиях, которые разъезжаются на первой же правке.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import repo

# Кто считается подписчиком. Ограниченный участник («restricted») — тоже
# участник, если он не вышел: у такого Telegram отдельно держит флаг is_member.
MEMBER_STATUSES = frozenset({"creator", "administrator", "member"})

# Итог → код ответа мини-аппа. Отказы намеренно разные: 403 значит «подпишитесь»,
# 409 — «уже получено», 503 — «виноваты мы». По одному общему коду кабинет не
# смог бы выбрать, что показать человеку.
HTTP_STATUS = {
    "granted": 200,
    "already": 409,
    "not_member": 403,
    "unavailable": 503,
    "disabled": 503,
}


@dataclass(frozen=True)
class Bonus:
    """Итог попытки забрать подарок."""

    status: str  # granted | already | not_member | unavailable | disabled
    days: int = 0
    until: datetime | None = None

    @property
    def granted(self) -> bool:
        return self.status == "granted"


def enabled() -> bool:
    """Настроен ли подарок вообще."""
    return settings.bonus_enabled


def channel() -> str:
    """Как называть канал в текстах: ``@имя`` либо числовой id."""
    return settings.bonus_chat or ""


def offer() -> str:
    """Короткое предложение для кнопки и заголовка карточки."""
    if not settings.bonus_enabled:
        return ""
    return f"{settings.bonus_days} дн. за подписку на канал"


def info(claimed_at: datetime | None) -> dict[str, Any]:
    """Блок ``bonus`` в ``/api/me``: по нему кабинет рисует и прячет карточку."""
    return {
        "enabled": settings.bonus_enabled,
        "channel": settings.bonus_chat or "",
        "url": settings.bonus_url or "",
        "days": settings.bonus_days if settings.bonus_enabled else 0,
        "claimed": claimed_at is not None,
        "claimed_at": claimed_at.isoformat() if claimed_at else None,
    }


async def is_member(bot: Any, user_id: int) -> bool | None:
    """Подписан ли человек на канал. ``None`` — спросить не удалось.

    Разница между «не подписан» и «не смогли проверить» принципиальна: в
    первом случае человеку надо подписаться, во втором виноваты мы — бот не
    администратор канала, канал переименован, Telegram недоступен, — и
    предлагать «подпишитесь ещё раз» бессмысленно и обидно.
    """
    chat = settings.bonus_chat
    if bot is None or not chat:
        return None
    try:
        member = await bot.get_chat_member(chat_id=chat, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — причина в лог, наружу «не смогли»
        logger.warning("Не смог проверить подписку {} на {}: {}", user_id, chat, exc)
        return None

    # У aiogram статус — строковый Enum: str() дал бы «ChatMemberStatus.MEMBER»,
    # поэтому сначала берём .value, и только потом сравниваем.
    raw = getattr(member, "status", "")
    status = str(getattr(raw, "value", raw) or "").lower()
    if status in MEMBER_STATUSES:
        return True
    if status == "restricted":
        return bool(getattr(member, "is_member", False))
    return False


async def claim(session: AsyncSession, bot: Any, user_id: int) -> Bonus:
    """Проверяет подписку и начисляет дни — один раз на аккаунт.

    Сессию не коммитит: вызывающий сам решает, когда закрывать транзакцию.
    """
    if not settings.bonus_enabled:
        return Bonus("disabled")

    user = await repo.get_user(session, user_id)
    if user is not None and user.channel_bonus_at is not None:
        return Bonus("already", days=settings.bonus_days)

    member = await is_member(bot, user_id)
    if member is None:
        return Bonus("unavailable")
    if not member:
        return Bonus("not_member")

    until = await repo.claim_channel_bonus(session, user_id, settings.bonus_days)
    if until is None:
        # Либо кто-то успел раньше (два нажатия подряд), либо пользователя ещё
        # нет в базе. И то и другое — «подарка не будет», а не ошибка сервера.
        return Bonus("already", days=settings.bonus_days)
    return Bonus("granted", days=settings.bonus_days, until=until)


def message(result: Bonus) -> str:
    """Что показать человеку. Один текст на кабинет и на бота."""
    if result.status == "granted":
        until = result.until
        tail = f" Абонемент действует до {until:%d.%m.%Y %H:%M} (UTC)." if until else ""
        return f"Готово! Подарок за подписку — {result.days} дн.{tail}"
    if result.status == "already":
        return "Подарок за подписку уже получен: он даётся один раз на аккаунт."
    if result.status == "not_member":
        name = channel() or "канал"
        return (
            f"Не вижу вас в канале {name}. Подпишитесь и нажмите «Проверить "
            "подписку» ещё раз."
        )
    if result.status == "unavailable":
        return (
            "Не могу проверить подписку прямо сейчас. Попробуйте позже — "
            "дни не потеряются."
        )
    return "Подарок за подписку сейчас не действует."
