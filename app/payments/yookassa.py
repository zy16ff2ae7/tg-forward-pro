"""Приём оплат картами и через СБП (ЮKassa).

Модуль не импортируется в обязательном порядке: если ключи не заданы,
кнопка «Карта / СБП» просто скажет, что способ временно недоступен.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from loguru import logger

from app.config import settings

# Сколько ждём оплату счёта. Ссылка ЮKassa живёт около часа, поэтому суточный
# счёт заведомо мёртв: держать его в pending — значит впустую опрашивать
# провайдера и занимать место в лимите висящих счетов.
PENDING_TTL = timedelta(hours=24)


def is_configured() -> bool:
    # Единый источник правды — settings.yookassa_ready (нужна полная пара ключей).
    return settings.yookassa_ready


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


async def check_pending(bot) -> int:
    """Зачисляет оплаченные счёта картой. Возвращает количество зачисленных.

    В боте есть кнопка «Я оплатил», но на странице сервиса её нет: человек
    уходит на страницу банка и обратно может не вернуться. Вебхук ЮKassa требует
    отдельной настройки в личном кабинете магазина и публичного адреса, поэтому
    доступ включаем опросом — тем же способом, что и по USDT.
    """
    from app.db import repo
    from app.db.database import SessionLocal, session_scope
    from app.db.models import Payment

    if not is_configured():
        return 0

    # Сначала закрываем просроченное: по мёртвым ссылкам провайдера не спрашиваем.
    async with session_scope() as session:
        dropped = await repo.expire_stale_payments(session, "yookassa", older_than=PENDING_TTL)
    if dropped:
        logger.info("ЮKassa: закрыли {} брошенных счетов", dropped)

    async with SessionLocal() as session:
        payments = list(await repo.pending_payments(session, "yookassa"))

    activated = 0
    for payment in payments:
        if not payment.external_id:
            # Счёт создан, а ответ провайдера потерялся — проверять нечего.
            continue
        if not await is_paid(payment.external_id):
            continue

        # Каждый платёж — отдельная транзакция БД: сбой на одном не должен
        # откатывать зачисление остальных.
        until = None
        try:
            async with session_scope() as session:
                fresh = await session.get(Payment, payment.id)
                if fresh is None or fresh.status != "pending":
                    continue
                # tx_id — id платежа у провайдера: уникальный индекс по нему не
                # даст зачесть один и тот же счёт дважды, даже если в этот же
                # момент человек нажал в боте «Я оплатил».
                if not await repo.claim_payment(session, fresh, tx_id=fresh.external_id):
                    logger.info("Платёж #{} закрыт кем-то другим — пропускаем", payment.id)
                    continue
                until = await repo.activate_subscription(session, fresh.user_id, fresh.months)
        except Exception as exc:  # noqa: BLE001 — один платёж не рушит проверку
            logger.exception("Платёж #{}: не смогли зачислить: {}", payment.id, exc)
            continue

        if until is None:
            continue
        activated += 1
        try:
            await bot.send_message(
                payment.user_id,
                "✅ <b>Оплата получена</b>\n\n"
                f"Абонемент активен до <b>{until:%d.%m.%Y %H:%M}</b> (UTC).\n"
                "Пересылка продолжает работать.",
            )
        except Exception:  # noqa: BLE001
            logger.debug("Не смогли уведомить {} об оплате", payment.user_id)
    return activated
