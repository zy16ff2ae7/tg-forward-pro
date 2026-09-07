"""Создание счёта: одна реализация на бота и на внешнюю страницу оплаты.

Счёт выставляется из двух мест — кнопкой в боте (режим ``PAY_MODE=inline``) и
страницей ``/pay`` вне Telegram. Логика одна и та же: создать строку платежа,
получить у провайдера ссылку или реквизиты, вернуть их вызывающему. Если
держать её в двух местах, они разъезжаются — в одном месте появится проверка
срока или уникальная метка, в другом нет.

Цена считается здесь же, по каталогу сроков: клиент передаёт только срок,
сумму назначает сервер.
"""
from __future__ import annotations

from loguru import logger

from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.errors import ConflictError, FeatureUnavailable, ValidationError
from app.payments import crypto, yookassa
from app.plans import (
    apply_discount,
    is_valid_period,
    periods_text,
    rub_amount,
    usdt_amount,
)


def _check_period(months: int) -> int:
    if not is_valid_period(months):
        raise ValidationError(f"Срок — {periods_text()}")
    return months


# Больше пяти неоплаченных счетов подряд — это не покупка, а перебор кнопки
# (или утёкшая ссылка на страницу оплаты). Каждый счёт USDT занимает уникальную
# метку-сумму, поэтому бесконечно их плодить нельзя.
MAX_PENDING_PER_METHOD = 5


async def _pending_percent(user_id: int) -> int:
    """Скидка человека, ждущая оплаты. Нет ожидания — ноль."""
    async with SessionLocal() as session:
        pending = await repo.pending_discount(session, user_id)
    return int(pending.percent or 0) if pending is not None else 0


async def _check_not_flooding(user_id: int, provider: str) -> None:
    async with SessionLocal() as session:
        waiting = await repo.count_pending_payments(session, user_id, provider)
    if waiting >= MAX_PENDING_PER_METHOD:
        raise ConflictError(
            "Слишком много неоплаченных счетов. Оплатите или подождите, "
            "пока прежние закроются."
        )


async def start_yookassa(user_id: int, months: int = 1) -> dict:
    """Счёт на карту/СБП. Возвращает ссылку на оплату и сумму.

    Порядок такой: сначала строка в БД, потом счёт у провайдера. Если ЮKassa
    не ответит, останется висящий pending-платёж — он никому не мешает и виден
    в админке, а вот оплата без строки в БД была бы потерянными деньгами.
    """
    _check_period(months)
    if not yookassa.is_configured():
        raise FeatureUnavailable(
            "Оплата картой не настроена", feature="yookassa", status="not_configured"
        )
    await _check_not_flooding(user_id, "yookassa")

    discount = await _pending_percent(user_id)
    amount = int(apply_discount(rub_amount(months), discount))
    async with SessionLocal() as session:
        payment = await repo.create_payment(
            session,
            user_id=user_id,
            provider="yookassa",
            amount=float(amount),
            currency="RUB",
            months=months,
        )
        await session.commit()
        payment_id = payment.id

    try:
        url, external_id = await yookassa.create_invoice(
            user_id=user_id,
            amount_rub=float(amount),
            months=months,
            local_payment_id=payment_id,
        )
    except Exception as exc:  # noqa: BLE001 — подробности провайдера в лог
        logger.warning("ЮKassa: не создали счёт для {}: {}", user_id, exc)
        raise FeatureUnavailable(
            "Не удалось создать счёт. Попробуйте позже.",
            feature="yookassa",
            status="invoice_failed",
        ) from exc

    async with SessionLocal() as session:
        for item in await repo.pending_payments(session, "yookassa"):
            if item.id == payment_id:
                item.external_id = external_id
        await session.commit()

    return {
        "method": "yookassa",
        "payment_id": payment_id,
        "url": url,
        "amount": amount,
        "currency": "RUB",
        "months": months,
        "discount_percent": discount,
    }


async def start_usdt(user_id: int, months: int = 1) -> dict:
    """Реквизиты для перевода USDT: кошелёк и сумма с уникальной меткой."""
    _check_period(months)
    if not crypto.is_configured():
        raise FeatureUnavailable(
            "Оплата USDT не настроена", feature="usdt", status="not_configured"
        )
    await _check_not_flooding(user_id, "usdt")

    discount = await _pending_percent(user_id)
    base = usdt_amount(months)
    if discount:
        base = float(apply_discount(base, discount))
    async with SessionLocal() as session:
        payment = await repo.create_payment(
            session,
            user_id=user_id,
            provider="usdt",
            amount=float(base),
            currency="USDT",
            months=months,
        )
        # Метку подбираем с оглядкой на уже висящие счёта: одинаковая сумма у
        # двух платежей — это зачёт перевода не тому, кто платил.
        memo = await crypto.reserve_memo(session, float(base), payment.id)
        payment.memo = memo
        await session.commit()
        payment_id = payment.id

    return {
        "method": "usdt",
        "payment_id": payment_id,
        "wallet": settings.usdt_wallet,
        "network": "TRC-20 (Tron)",
        "amount": float(memo),
        "memo": memo,
        "currency": "USDT",
        "months": months,
        "discount_percent": discount,
    }


async def start(method: str, user_id: int, months: int = 1) -> dict:
    """Счёт по названию способа. ValidationError — способ незнакомый."""
    if method == "yookassa":
        return await start_yookassa(user_id, months)
    if method == "usdt":
        return await start_usdt(user_id, months)
    raise ValidationError("Неизвестный способ оплаты")
