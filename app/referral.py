"""Реферальная программа: друг пришёл по ссылке — обоим плюс дни.

Правило одно на всех: ссылка срабатывает, только если друг — новичок (аккаунт
создан не раньше десяти минут назад) и ни к кому ещё не привязан. Самому себе,
по чужой ссылке со старого аккаунта и по второму кругу дни не начисляются —
иначе программа превратилась бы в обмен днями по кругу.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import repo


@dataclass(frozen=True)
class Referral:
    """Итог захода по реферальной ссылке."""

    status: str  # granted | self | stranger | stale | already | unknown | disabled
    days: int = 0
    until: datetime | None = None

    @property
    def granted(self) -> bool:
        return self.status == "granted"


def enabled() -> bool:
    """Включена ли программа вообще."""
    return settings.referral_enabled


def days() -> int:
    """Сколько дней дарит одно приглашение — каждому из двоих."""
    return settings.referral_days if settings.referral_enabled else 0


def code(user_id: int) -> str:
    """Хвост ссылки-приглашения: ``ref_<id>``."""
    return f"ref_{user_id}"


def link(user_id: int) -> str:
    """Ссылка-приглашение. Пустая — юзернейм бота не задан в .env."""
    if not settings.referral_enabled or not settings.bot_username:
        return ""
    return f"https://t.me/{settings.bot_username}?start={code(user_id)}"


async def apply(
    session: AsyncSession, user_id: int, referrer_id: int
) -> Referral:
    """Применяет реферальную ссылку к новичку. Сессию не коммитит."""
    if not settings.referral_enabled:
        return Referral("disabled")
    status, until = await repo.apply_referral(
        session, user_id, referrer_id, settings.referral_days
    )
    return Referral(status, days=settings.referral_days, until=until)


async def info(session: AsyncSession, user_id: int) -> dict[str, Any]:
    """Блок ``referral`` в ``/api/me``: по нему кабинет рисует карточку."""
    invited = await repo.count_referrals(session, user_id) if enabled() else 0
    return {
        "enabled": enabled(),
        "link": link(user_id),
        "code": code(user_id),
        "days": days(),
        "invited": invited,
        "earned_days": invited * days(),
    }


def message(result: Referral, name: str = "друг") -> str:
    """Что показать новичку после захода по ссылке."""
    if result.status == "granted":
        until = result.until
        tail = f" Абонемент действует до {until:%d.%m.%Y %H:%M} (UTC)." if until else ""
        return (
            f"Вас пригласил {name} — вам обоим +{result.days} дн.! 🎉{tail}"
        )
    if result.status == "self":
        return "Это ваша собственная ссылка: себе дни не начисляются, зовите друзей 🙂"
    if result.status == "already":
        return "Пригласивший у вас уже записан: ссылка срабатывает один раз."
    if result.status == "stale":
        return "Ссылка действует только для новичков — ваш аккаунт уже не первый день с нами."
    if result.status == "disabled":
        return "Реферальная программа сейчас выключена."
    return "Не получилось применить ссылку: пригласивший не найден."


def referrer_message(name: str, days: int) -> str:
    """Что уходит пригласившему, когда друг зашёл по его ссылке."""
    return f"🎉 {name} пришёл по вашей ссылке! Вам +{days} дн. к абонементу."
