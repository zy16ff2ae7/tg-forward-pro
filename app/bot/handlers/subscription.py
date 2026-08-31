"""Абонемент и оплата: Stars, карта/СБП, USDT, ручная выдача."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from loguru import logger

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.utils import ensure_user, smart_edit
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.payments import crypto, yookassa

router = Router(name="subscription")

MONTHS = 1


async def _status_text(user_id: int) -> str:
    async with SessionLocal() as session:
        until = await repo.subscription_until(session, user_id)
        rules_count = await repo.count_rules(session, user_id)
    return texts.subscription_status(until, rules_count)


@router.message(Command("sub"))
async def cmd_sub(message: Message) -> None:
    await show_subscription_message(message)


# ────────────────────────────── Копилка подписок ─────────────────────────────


async def _bank_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и клавиатура копилки: сколько дней активно и сколько заморожено."""
    async with SessionLocal() as session:
        until = await repo.subscription_until(session, user_id)
        sub = await repo.get_subscription(session, user_id)

    banked = int(sub.banked_days or 0) if sub is not None else 0
    days_left = max((until - repo.utcnow()).days, 0) if until else 0
    active_until = f"{until:%d.%m.%Y}" if until else "—"

    text = (
        "🐖 <b>Копилка подписок</b>\n\n"
        f"Абонемент активен до: <b>{active_until}</b> ({days_left} дн.)\n"
        f"Заморожено в копилке: <b>{banked} дн.</b>\n\n"
        "Дни в копилке не сгорают. Заморозьте часть срока, пока пересылка "
        "не нужна, и верните её, когда понадобится."
    )
    return text, kb.bank_menu(banked, days_left)


async def show_bank_message(message: Message) -> None:
    """Копилка. Сюда ведёт кнопка «Распределить подписку» из мини-аппа."""
    await ensure_user(message)
    assert message.from_user is not None
    text, markup = await _bank_view(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("bank"))
async def cmd_bank(message: Message) -> None:
    await show_bank_message(message)


@router.callback_query(F.data == "menu:bank")
async def open_bank(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    text, markup = await _bank_view(callback.from_user.id)
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=markup)


@router.callback_query(F.data.startswith("bank:freeze:"))
async def bank_freeze(callback: CallbackQuery) -> None:
    """Замораживает дни: снимает с активного периода и кладёт в копилку."""
    await callback.answer()
    days = int(callback.data.split(":")[2])
    assert callback.from_user is not None

    async with SessionLocal() as session:
        until = await repo.subscription_until(session, callback.from_user.id)
        if until is None:
            await callback.answer("Активной подписки нет", show_alert=True)
            return
        moved = await repo.bank_days(session, callback.from_user.id, days)
        if not moved:
            await callback.answer(
                "Столько заморозить нельзя — оставьте хотя бы сутки активными", show_alert=True
            )
            return
        await session.commit()

    text, markup = await _bank_view(callback.from_user.id)
    if callback.message is not None:
        await smart_edit(callback.message, f"❄️ Заморожено дней: <b>{moved}</b>\n\n" + text,
                         reply_markup=markup)


@router.callback_query(F.data.startswith("bank:give:"))
async def bank_give(callback: CallbackQuery) -> None:
    """Возвращает дни из копилки в активный период (0 — вернуть всё)."""
    await callback.answer()
    days = int(callback.data.split(":")[2])
    assert callback.from_user is not None

    async with SessionLocal() as session:
        moved = await repo.unbank_days(session, callback.from_user.id, days)
        if not moved:
            await callback.answer("В копилке нет дней", show_alert=True)
            return
        await session.commit()

    text, markup = await _bank_view(callback.from_user.id)
    if callback.message is not None:
        await smart_edit(callback.message, f"↩️ Добавлено дней: <b>{moved}</b>\n\n" + text,
                         reply_markup=markup)


async def show_subscription_message(message: Message) -> None:
    """Карточка абонемента. Используется и из /sub, и из диплинка мини-аппа."""
    await ensure_user(message)
    assert message.from_user is not None
    text = await _status_text(message.from_user.id)
    await message.answer(text, reply_markup=kb.payment_menu())


async def show_subscription(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    text = await _status_text(callback.from_user.id)
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=kb.payment_menu())


# ─────────────────────────────── Telegram Stars ───────────────────────────────


@router.callback_query(F.data == "pay:stars")
async def pay_stars(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    payload = f"sub:{callback.from_user.id}:{MONTHS}"
    prices = [LabeledPrice(label=f"Абонемент на {MONTHS} мес.", amount=settings.price_stars)]
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Абонемент на 1 месяц",
        description="Автоматическая пересылка сообщений: безлимит правил, 24/7, без метки «Переслано от».",
        payload=payload,
        provider_token="",  # для Stars токен не нужен
        currency="XTR",
        prices=prices,
    )


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_stars_paid(message: Message) -> None:
    payment = message.successful_payment
    assert payment is not None and message.from_user is not None
    try:
        _, user_id_raw, months_raw = payment.invoice_payload.split(":")
        user_id, months = int(user_id_raw), int(months_raw)
    except ValueError:
        user_id, months = message.from_user.id, MONTHS

    async with SessionLocal() as session:
        await repo.create_payment(
            session,
            user_id=user_id,
            provider="stars",
            amount=payment.total_amount,
            currency=payment.currency,
            months=months,
            external_id=payment.telegram_payment_charge_id,
        )
        until = await repo.activate_subscription(session, user_id, months)
        await session.commit()

    await message.answer(
        "🎉 <b>Оплата прошла</b>\n\n"
        f"Абонемент активен до <b>{until:%d.%m.%Y %H:%M}</b> (UTC).\n"
        "Правила уже работают.",
        reply_markup=kb.back_to_main(),
    )


# ────────────────────────────── Карта / СБП (ЮKassa) ──────────────────────────


@router.callback_query(F.data == "pay:yookassa")
async def pay_yookassa(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None

    if not yookassa.is_configured():
        if callback.message is not None:
            await smart_edit(callback.message, 
                "💳 Оплата картой временно недоступна.\n"
                "Воспользуйтесь Stars, USDT или напишите администратору.",
                reply_markup=kb.payment_menu(),
            )
        return

    async with SessionLocal() as session:
        payment = await repo.create_payment(
            session,
            user_id=callback.from_user.id,
            provider="yookassa",
            amount=float(settings.price_rub),
            currency="RUB",
            months=MONTHS,
        )
        await session.commit()
        payment_id = payment.id

    try:
        url, external_id = await yookassa.create_invoice(
            user_id=callback.from_user.id,
            amount_rub=float(settings.price_rub),
            months=MONTHS,
            local_payment_id=payment_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ЮKassa: не создали платёж")
        if callback.message is not None:
            await smart_edit(callback.message, 
                f"❌ Не удалось создать счёт: {exc}", reply_markup=kb.payment_menu()
            )
        return

    async with SessionLocal() as session:
        pending = await repo.pending_payments(session, "yookassa")
        for item in pending:
            if item.id == payment_id:
                item.external_id = external_id
        await session.commit()

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=url)],
            [InlineKeyboardButton(text="🔄 Я оплатил", callback_data=f"pay:check:{payment_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:sub")],
        ]
    )
    if callback.message is not None:
        await smart_edit(callback.message, 
            f"💳 Счёт на <b>{settings.price_rub} ₽</b> создан.\n\n"
            "После оплаты нажмите «Я оплатил» — доступ включится сразу.",
            reply_markup=markup,
        )


@router.callback_query(F.data.startswith("pay:check:"))
async def check_yookassa(callback: CallbackQuery) -> None:
    await callback.answer()
    payment_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None

    async with SessionLocal() as session:
        payments = await repo.pending_payments(session, "yookassa")
        payment = next((p for p in payments if p.id == payment_id), None)
        if payment is None or not payment.external_id:
            if callback.message is not None:
                await smart_edit(callback.message, 
                    "Платёж не найден или уже обработан.", reply_markup=kb.payment_menu()
                )
            return

        if not await yookassa.is_paid(payment.external_id):
            if callback.message is not None:
                await smart_edit(callback.message, 
                    "⏳ Оплата ещё не поступила. Подождите минуту и нажмите «Я оплатил» снова.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="🔄 Проверить снова",
                                    callback_data=f"pay:check:{payment_id}",
                                )
                            ],
                            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:sub")],
                        ]
                    ),
                )
            return

        await repo.mark_payment_paid(session, payment)
        until = await repo.activate_subscription(session, payment.user_id, payment.months)
        await session.commit()

    if callback.message is not None:
        await smart_edit(callback.message, 
            "🎉 <b>Оплата прошла</b>\n\n"
            f"Абонемент активен до <b>{until:%d.%m.%Y %H:%M}</b> (UTC).",
            reply_markup=kb.back_to_main(),
        )


# ───────────────────────────────────── USDT ───────────────────────────────────


@router.callback_query(F.data == "pay:usdt")
async def pay_usdt(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None

    if not crypto.is_configured():
        if callback.message is not None:
            await smart_edit(callback.message, 
                "🪙 Оплата USDT не настроена администратором.",
                reply_markup=kb.payment_menu(),
            )
        return

    async with SessionLocal() as session:
        payment = await repo.create_payment(
            session,
            user_id=callback.from_user.id,
            provider="usdt",
            amount=float(settings.price_usdt),
            currency="USDT",
            months=MONTHS,
        )
        await session.commit()
        amount = crypto.unique_amount(float(settings.price_usdt), payment.id)
        payment.memo = f"{amount:.3f}"
        await session.commit()

    text = (
        "🪙 <b>Оплата USDT (TRC-20)</b>\n\n"
        f"Сеть: <b>Tron (TRC-20)</b>\n"
        f"Кошелёк: <code>{settings.usdt_wallet}</code>\n"
        f"Сумма: <b>{amount:.3f} USDT</b>\n\n"
        "⚠️ Отправьте ровно эту сумму — копейки служат идентификатором платежа. "
        "Доступ включится автоматически после подтверждения в сети (обычно 1–3 минуты)."
    )
    if callback.message is not None:
        await smart_edit(callback.message, 
            text, reply_markup=kb.back_to_main()
        )


# ─────────────────────────────── Через администратора ─────────────────────────


@router.callback_query(F.data == "pay:manual")
async def pay_manual(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None

    async with SessionLocal() as session:
        await repo.create_payment(
            session,
            user_id=callback.from_user.id,
            provider="manual",
            amount=float(settings.price_rub),
            currency="RUB",
            months=MONTHS,
        )
        await session.commit()

    for admin_id in settings.admin_ids:
        try:
            await callback.bot.send_message(
                admin_id,
                "🆕 <b>Заявка на абонемент</b>\n\n"
                f"Пользователь: {callback.from_user.id} "
                f"({callback.from_user.username or callback.from_user.full_name})\n\n"
                "Выдать доступ: <code>/grant {id} 1</code>".replace(
                    "{id}", str(callback.from_user.id)
                ),
            )
        except Exception:  # noqa: BLE001
            logger.debug("Не смогли отправить заявку админу {}", admin_id)

    if callback.message is not None:
        await smart_edit(callback.message, 
            "✅ Заявка отправлена администратору.\n"
            "Обычно доступ выдаётся в течение нескольких минут — придёт уведомление.",
            reply_markup=kb.back_to_main(),
        )
