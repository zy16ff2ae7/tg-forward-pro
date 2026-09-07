"""Реферальная программа: друг пришёл — ему скидка, оплатил — вам дни.

Правила против фарма: ссылка срабатывает, только если друг — новичок (аккаунт
создан не раньше десяти минут назад) и ни к кому ещё не привязан. За саму
регистрацию дней не даётся никому: их фармили пачками фейков, и они ломали
правило «бесплатно — только за подписку на канал». Друг за приход получает
личный промокод на скидку к первой оплате, а пригласивший — скромные дни
и свой код, когда друг оплатит первый абонемент. Чтобы нафармить награду,
надо сначала заплатить за месяц, — фарм убыточен сам по себе.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import repo


@dataclass(frozen=True)
class Referral:
    """Итог захода по реферальной ссылке."""

    status: str  # granted | self | stranger | stale | already | unknown | disabled
    days: int = 0  # награда пригласившему — для текстов, не выдача
    # Личный код друга на скидку. Пустой — скидки выключены или заход не сработал.
    friend_code: str = ""

    @property
    def granted(self) -> bool:
        return self.status == "granted"


def enabled() -> bool:
    """Включена ли программа вообще."""
    return settings.referral_enabled


def days() -> int:
    """Сколько дней получает пригласивший за оплатившего друга."""
    return settings.referral_days if settings.referral_enabled else 0


def code(user_id: int) -> str:
    """Хвост ссылки-приглашения: ``ref_<id>``."""
    return f"ref_{user_id}"


def link(user_id: int) -> str:
    """Ссылка-приглашение. Пустая — юзернейм бота не задан в .env."""
    if not settings.referral_enabled or not settings.bot_username:
        return ""
    return f"https://t.me/{settings.bot_username}?start={code(user_id)}"


def discount_percent() -> int:
    """Размер скидки за друга. Ноль — скидок нет, только дни за оплату."""
    if not settings.referral_enabled:
        return 0
    return max(0, settings.referral_discount_percent)


async def apply(
    session: AsyncSession, user_id: int, referrer_id: int
) -> Referral:
    """Применяет реферальную ссылку к новичку. Сессию не коммитит."""
    if not settings.referral_enabled:
        return Referral("disabled")
    status = await repo.apply_referral(session, user_id, referrer_id)
    friend_code = ""
    percent = discount_percent()
    if status == "granted" and percent > 0:
        friend_code = (
            await repo.mint_referral_discount(session, user_id, percent)
        ).code
    return Referral(status, days=settings.referral_days, friend_code=friend_code)


async def info(session: AsyncSession, user_id: int) -> dict[str, Any]:
    """Блок ``referral`` в ``/api/me``: по нему кабинет рисует карточку."""
    on = enabled()
    invited = await repo.count_referrals(session, user_id) if on else 0
    rewarded = await repo.count_active_referrals(session, user_id) if on else 0
    pending = await repo.pending_discount(session, user_id) if on else None
    codes = await repo.owner_discount_codes(session, user_id) if on else []
    return {
        "enabled": on,
        "link": link(user_id),
        "code": code(user_id),
        "days": days(),
        "invited": invited,
        "rewarded": rewarded,
        "earned_days": rewarded * days(),
        "discount_percent": discount_percent(),
        "discount_codes": [promo.code for promo in codes],
        "pending_discount": int(pending.percent or 0) if pending else 0,
    }


def message(result: Referral, name: str = "друг") -> str:
    """Что показать новичку после захода по ссылке."""
    if result.status == "granted":
        text = f"Вас пригласил {name}! 🎉"
        if result.friend_code:
            text += (
                "\n\nВаш личный промокод на скидку "
                f"{discount_percent()}%: <code>{result.friend_code}</code>\n"
                "Введите его в «Промокод» — первая оплата станет дешевле."
            )
        if result.days:
            text += (
                f"\n\nОформите абонемент — и {name} получит "
                f"+{result.days} дн. Спасибо, что вы с нами!"
            )
        return text
    if result.status == "self":
        return "Это ваша собственная ссылка: награды за неё нет, зовите друзей 🙂"
    if result.status == "already":
        return "Пригласивший у вас уже записан: ссылка срабатывает один раз."
    if result.status == "stale":
        return "Ссылка действует только для новичков — ваш аккаунт уже не первый день с нами."
    if result.status == "disabled":
        return "Реферальная программа сейчас выключена."
    return "Не получилось применить ссылку: пригласивший не найден."


def referrer_pending_message(name: str) -> str:
    """Что уходит пригласившему, когда друг зашёл, но ещё не оплатил."""
    text = f"👋 {name} пришёл по вашей ссылке!"
    if days():
        text += f" Когда он оформит первый абонемент — вам +{days()} дн."
    if discount_percent():
        text += f" и промокод на −{discount_percent()}%"
    return text + "."


def referrer_reward_message(name: str, reward_days: int, code: str = "") -> str:
    """Что уходит пригласившему, когда друг оплатил первый абонемент."""
    text = f"🎉 {name} оформил абонемент! Вам +{reward_days} дн. к абонементу."
    if code:
        text += (
            "\n\nВаш личный промокод на скидку "
            f"{discount_percent()}%: <code>{code}</code>\n"
            "Введите его в «Промокод» — ближайшая оплата станет дешевле."
        )
    return text
