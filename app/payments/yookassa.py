"""Приём оплат картами и через СБП (ЮKassa).

Модуль не импортируется в обязательном порядке: если ключи не заданы,
кнопка «Карта / СБП» просто скажет, что способ временно недоступен.
"""
from __future__ import annotations

import asyncio
import uuid

from loguru import logger

from app.config import settings


def is_configured() -> bool:
    return bool(settings.yookassa_shop_id and settings.yookassa_secret_key)


def _configure() -> None:
    from yookassa import Configuration

    Configuration.configure(
        account_id=settings.yookassa_shop_id,
        secret_key=settings.yookassa_secret_key,
    )


async def create_invoice(user_id: int, amount_rub: float, months: int, local_payment_id: int):
    """Создаёт платёж в ЮKassa. Возвращает (ссылка_на_оплату, id_платежа_в_ЮKassa)."""
    if not is_configured():
        raise RuntimeError("ЮKassa не настроена: пустые YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY")

    def _create() -> tuple[str, str]:
        from yookassa import Payment as YooPayment

        _configure()
        payment = YooPayment.create(
            {
                "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": settings.yookassa_return_url or "https://t.me",
                },
                "capture": True,
                "description": f"Абонемент на {months} мес. — автопересылка Telegram",
                "metadata": {
                    "user_id": str(user_id),
                    "local_payment_id": str(local_payment_id),
                    "months": str(months),
                },
            },
            uuid.uuid4(),
        )
        return payment.confirmation.confirmation_url, payment.id

    return await asyncio.to_thread(_create)


async def is_paid(external_id: str) -> bool:
    """Проверяет статус платежа в ЮKassa."""
    if not is_configured():
        return False

    def _check() -> bool:
        from yookassa import Payment as YooPayment

        _configure()
        payment = YooPayment.find_one(external_id)
        return bool(payment.status == "succeeded")

    try:
        return await asyncio.to_thread(_check)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ЮKassa: не проверили платёж {}: {}", external_id, exc)
        return False
