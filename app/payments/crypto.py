"""Приём USDT (TRC-20) с автопроверкой через TronGrid.

Схема без генерации адресов: каждому платежу выдаётся уникальная сумма
(например 12.017 вместо 12.00), по ней и находим перевод на общем кошельке.
"""
from __future__ import annotations

from datetime import datetime

import aiohttp
from loguru import logger

from app.config import settings

TRONGRID_URL = "https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
USDT_DECIMALS = 6


def is_configured() -> bool:
    return bool(settings.usdt_wallet)


def unique_amount(base_amount: float, payment_id: int) -> float:
    """Уникальная сумма к оплате: к базовой цене прибавляем номер платежа/1000."""
    return round(base_amount + payment_id / 1000.0, 3)


async def _fetch_transactions(session: aiohttp.ClientSession, since_ms: int = 0) -> list[dict]:
    url = TRONGRID_URL.format(address=settings.usdt_wallet)
    params = {
        "limit": "100",
        "only_to": "true",
        "contract_address": USDT_CONTRACT,
        "only_confirmed": "true",
    }
    if since_ms:
        params["min_timestamp"] = str(since_ms)
    headers = {}
    if settings.trongrid_api_key:
        headers["TRON-PRO-API-KEY"] = settings.trongrid_api_key

    async with session.get(url, params=params, headers=headers, timeout=30) as response:
        if response.status != 200:
            logger.warning("TronGrid вернул статус {}", response.status)
            return []
        payload = await response.json(content_type=None)
    return payload.get("data") or []


async def find_incoming(expected_amount: float, since: datetime | None = None) -> dict | None:
    """Ищет входящий перевод на сумму expected_amount (с допуском 0.001 USDT)."""
    if not is_configured():
        return None

    since_ms = int(since.timestamp() * 1000) if since else 0
    try:
        async with aiohttp.ClientSession() as session:
            transactions = await _fetch_transactions(session, since_ms)
    except Exception as exc:  # noqa: BLE001
        logger.warning("TronGrid недоступен: {}", exc)
        return None

    for tx in transactions:
        token = (tx.get("token_info") or {}).get("symbol", "")
        if token.upper() != "USDT":
            continue
        try:
            value = int(tx.get("value", 0)) / (10**USDT_DECIMALS)
        except (TypeError, ValueError):
            continue
        if abs(value - expected_amount) < 0.001:
            return tx
    return None


async def check_pending(bot) -> int:
    """Проверяет ожидающие крипто-платежи. Возвращает количество зачисленных."""
    from app.db import repo
    from app.db.database import SessionLocal

    if not is_configured():
        return 0

    activated = 0
    async with SessionLocal() as session_local:
        payments = list(await repo.pending_payments(session_local, "usdt"))
        for payment in payments:
            if not payment.memo:
                continue
            try:
                expected = float(payment.memo)
            except ValueError:
                continue
            tx = await find_incoming(expected, since=payment.created_at)
            if tx is None:
                continue

            await repo.mark_payment_paid(session_local, payment)
            until = await repo.activate_subscription(session_local, payment.user_id, payment.months)
            activated += 1
            try:
                await bot.send_message(
                    payment.user_id,
                    "✅ <b>Оплата USDT получена</b>\n\n"
                    f"Абонемент активен до <b>{until:%d.%m.%Y %H:%M}</b> (UTC).\n"
                    "Пересылка продолжает работать.",
                )
            except Exception:  # noqa: BLE001
                logger.debug("Не смогли уведомить {} об оплате", payment.user_id)
        if activated:
            await session_local.commit()
    return activated
