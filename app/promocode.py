"""Промокоды на дни абонемента: акции вида «код на выходных».

Один код — много человек (лимит — ``max_uses``), один человек — один раз на
код. Выключенный код неотличим от несуществующего: перебору подсказывать
нечего. Кабинет и бот зовут один ``redeem()`` и показывают один ``message()``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import repo

# Итог → код ответа мини-аппа. Истёкший и разобранный — разные коды: первый
# зовёт ждать следующий код, второй — торопиться в следующий раз.
HTTP_STATUS = {
    "granted": 200,
    "already": 409,
    "unknown": 404,
    "expired": 410,
    "exhausted": 409,
}


@dataclass(frozen=True)
class Promo:
    """Итог активации промокода."""

    status: str  # granted | already | unknown | expired | exhausted
    days: int = 0
    until: datetime | None = None

    @property
    def granted(self) -> bool:
        return self.status == "granted"


async def redeem(session: AsyncSession, user_id: int, code: str) -> Promo:
    """Активирует код. Сессию не коммитит: решает вызывающий."""
    status, days, until = await repo.redeem_promo_code(session, user_id, code)
    return Promo(status, days=days, until=until)


def message(result: Promo) -> str:
    """Что показать человеку. Один текст на кабинет и на бота."""
    if result.status == "granted":
        until = result.until
        tail = f" Абонемент действует до {until:%d.%m.%Y %H:%M} (UTC)." if until else ""
        return f"Готово! Промокод дал +{result.days} дн.{tail}"
    if result.status == "already":
        return "Этот код вы уже активировали: один код — один раз на человека."
    if result.status == "expired":
        return "Срок этого кода вышел. Следите за каналом — будут новые."
    if result.status == "exhausted":
        return "Код уже разобрали: лимит активаций исчерпан."
    return "Такого кода нет. Проверьте буквы и попробуйте ещё раз."
