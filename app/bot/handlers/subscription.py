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

from app import bonus
from app.bot import keyboards as kb
from app.bot import texts
from app.bot.utils import ensure_user, smart_edit
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.errors import AppError
from app.payments import crypto, service, yookassa
from app.plans import (
    DEFAULT_MONTHS,
    STARS_DESCRIPTION,
    months_from_callback,
    periods_text,
    stars_amount,
)

router = Router(name="subscription")

# Срок по умолчанию для способов оплаты без выбора срока
# (карта, USDT, ручная выдача) — звёзды предлагают срок отдельным шагом.
MONTHS = DEFAULT_MONTHS


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


# ───────────────────── Подарок за подписку на канал ──────────────────────


async def _bonus_claimed(user_id: int) -> bool:
    async with SessionLocal() as session:
        user = await repo.get_user(session, user_id)
    return bool(user is not None and user.channel_bonus_at)


async def show_bonus_message(message: Message) -> None:
    """Экран подарка. Сюда ведут /start bonus и кнопка из кабинета."""
    await ensure_user(message)
    assert message.from_user is not None
    claimed = await _bonus_claimed(message.from_user.id)
    await message.answer(
        texts.bonus_card(claimed), reply_markup=kb.bonus_menu(claimed)
    )


@router.message(Command("bonus"))
async def cmd_bonus(message: Message) -> None:
    await show_bonus_message(message)


@router.callback_query(F.data == "bonus:open")
async def open_bonus(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    claimed = await _bonus_claimed(callback.from_user.id)
    if callback.message is not None:
        await smart_edit(
            callback.message,
            texts.bonus_card(claimed),
            reply_markup=kb.bonus_menu(claimed),
        )


@router.callback_query(F.data == "bonus:check")
async def check_bonus(callback: CallbackQuery) -> None:
    """Проверяет подписку на канал и начисляет дни — один раз на аккаунт.

    Проверка и начисление — те же, что в кабинете (``app/bonus.py``): бот и
    мини-апп не могут разойтись в ответе, сколько бы раз человек ни переходил
    из одного в другой.
    """
    await callback.answer()
    assert callback.from_user is not None
    user_id = callback.from_user.id

    async with SessionLocal() as session:
        result = await bonus.claim(session, callback.bot, user_id)
        if result.granted:
            await session.commit()
        else:
            await session.rollback()

    text = bonus.message(result)
    if not result.granted:
        # Отказ показываем всплывашкой: экран с инструкцией остаётся на месте,
        # и человеку не приходится открывать его заново, чтобы дойти до канала.
        await callback.answer(text, show_alert=True)
        return
    if callback.message is not None:
        await smart_edit(
            callback.message,
            "🎁 <b>Подарок начислен</b>\n\n"
            + text
            + "\n\n"
            + await _status_text(user_id),
            reply_markup=kb.payment_menu(user_id),
        )


async def show_subscription_message(message: Message) -> None:
    """Карточка абонемента. Используется и из /sub, и из диплинка мини-аппа."""
    await ensure_user(message)
    assert message.from_user is not None
    text = await _status_text(message.from_user.id)
    await message.answer(text, reply_markup=kb.payment_menu(message.from_user.id))


async def show_subscription(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    text = await _status_text(callback.from_user.id)
    if callback.message is not None:
        await smart_edit(
            callback.message, text, reply_markup=kb.payment_menu(callback.from_user.id)
        )


@router.callback_query(F.data.startswith("pay:soon:"))
async def pay_soon(callback: CallbackQuery) -> None:
    """Способ оплаты ещё не подключён администратором.

    Кнопка помечена «скоро», но её всё равно могут нажать — объясняем, что
    делать, вместо молчаливого «временно недоступно».
    """
    await callback.answer()
    method = (callback.data or "").split(":")[-1]
    names = {
        "yookassa": "Оплата картой / СБП",
        "usdt": "Оплата USDT (TRC-20)",
    }
    name = names.get(method, "Этот способ оплаты")
    if callback.message is not None:
        await smart_edit(
            callback.message,
            f"{name} пока не подключена.\n\n"
            "Сейчас доступны звёзды и ручная выдача через администратора — "
            "этого хватит, чтобы абонемент заработал прямо сейчас.",
            reply_markup=kb.payment_menu(callback.from_user.id),
        )


# ───────────────────── Оплата вне Telegram (карта и крипта) ───────────────────

METHOD_NAMES = {
    "yookassa": "Карта / СБП",
    "usdt": "USDT (TRC-20)",
}


async def _offer_external(callback: CallbackQuery, method: str) -> None:
    """Объясняет, что этим способом платят не в боте, а на странице сервиса.

    Нужно для старых сообщений: кнопка «Карта / СБП» осталась в чате с тех
    времён, когда счёт выставлялся прямо здесь. Молча выставить его снова
    нельзя — правила Telegram (ToS, п. 6.2) оставляют внутри Telegram только
    звёзды. Ничего не скрываем: прямо говорим, где платить и почему.
    """
    name = METHOD_NAMES.get(method, "Этот способ оплаты")
    if method in settings.external_payment_methods():
        text = (
            f"🌐 <b>{name} — на сайте</b>\n\n"
            "Внутри Telegram абонемент продаётся за звёзды. "
            f"{name} работает на нашей странице оплаты: кнопка ниже открывает "
            "её в браузере.\n\n"
            "После оплаты доступ включится сам — уведомление придёт сюда."
        )
    else:
        text = (
            f"{name} сейчас отключена.\n\n"
            "Внутри Telegram остаются звёзды и заявка администратору."
        )
    if callback.message is not None:
        await smart_edit(
            callback.message, text, reply_markup=kb.payment_menu(callback.from_user.id)
        )


# ─────────────────────────────── Telegram Stars ───────────────────────────────


@router.callback_query(F.data == "pay:stars")
async def pay_stars(callback: CallbackQuery) -> None:
    """Шаг 1: выбрать срок. Счёт высылается следующим шагом."""
    await callback.answer()
    assert callback.from_user is not None

    text = (
        "⭐ <b>Оплата звёздами</b>\n\n"
        f"Абонемент: <b>{settings.price_stars} ⭐</b> за месяц.\n"
        "Выберите срок — чем больше срок, тем меньше хлопот с продлением.\n\n"
        f"Доступные сроки: {periods_text()}."
    )
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=kb.stars_periods())


@router.callback_query(F.data.startswith("pay:stars:"))
async def pay_stars_period(callback: CallbackQuery) -> None:
    """Шаг 2: выставить счёт на выбранный срок."""
    await callback.answer()
    assert callback.from_user is not None and callback.data is not None

    months = months_from_callback(callback.data)
    if months is None:
        # Левый срок в callback_data: молча брать месяц нельзя — иначе
        # пользователь заплатит не за то, что выбирал.
        logger.warning("Stars: непонятный срок в {}", callback.data)
        if callback.message is not None:
            await smart_edit(
                callback.message,
                "Не удалось разобрать срок. Выберите его заново.",
                reply_markup=kb.stars_periods(),
            )
        return

    amount = stars_amount(months)
    title = f"Абонемент на {months} мес."
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=title,
        description=STARS_DESCRIPTION,
        # Формат читает хендлер successful_payment — менять нельзя.
        payload=f"sub:{callback.from_user.id}:{months}",
        provider_token="",  # для Stars токен не нужен
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=amount)],
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

    # Внутри бота карта работает только в режиме PAY_MODE=inline. В остальных
    # случаях кнопка могла достаться из старого сообщения — уводим на страницу.
    if "yookassa" not in settings.inline_payment_methods():
        await _offer_external(callback, "yookassa")
        return

    if not yookassa.is_configured():
        if callback.message is not None:
            await smart_edit(
                callback.message,
                "💳 Оплата картой временно недоступна.\n"
                "Воспользуйтесь Stars, USDT или напишите администратору.",
                reply_markup=kb.payment_menu(callback.from_user.id),
            )
        return

    # Счёт выставляет общий сервис — тот же, что и страница оплаты. Так у бота
    # и страницы одна цена, одна проверка срока и один лимит висящих счетов.
    try:
        invoice = await service.start_yookassa(callback.from_user.id, MONTHS)
    except AppError as exc:
        if callback.message is not None:
            await smart_edit(
                callback.message,
                f"❌ {exc.message}",
                reply_markup=kb.payment_menu(callback.from_user.id),
            )
        return

    payment_id = invoice["payment_id"]
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить", url=invoice["url"])],
            [InlineKeyboardButton(text="🔄 Я оплатил", callback_data=f"pay:check:{payment_id}")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="menu:sub")],
        ]
    )
    if callback.message is not None:
        await smart_edit(callback.message,
            f"💳 Счёт на <b>{invoice['amount']} ₽</b> создан.\n\n"
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
                await smart_edit(
                    callback.message,
                    "Платёж не найден или уже обработан.",
                    reply_markup=kb.payment_menu(callback.from_user.id),
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

        # Закрываем платёж переводом состояния pending → paid: нажать «Я оплатил»
        # можно дважды, и оба нажатия успевают увидеть pending. Начисляет тот,
        # кому строка досталась.
        if not await repo.claim_payment(session, payment):
            await session.commit()
            if callback.message is not None:
                await smart_edit(
                    callback.message,
                    "✅ Этот платёж уже зачтён — абонемент продлён.",
                    reply_markup=kb.back_to_main(),
                )
            return
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

    if "usdt" not in settings.inline_payment_methods():
        await _offer_external(callback, "usdt")
        return

    if not crypto.is_configured():
        if callback.message is not None:
            await smart_edit(
                callback.message,
                "🪙 Оплата USDT не настроена администратором.",
                reply_markup=kb.payment_menu(callback.from_user.id),
            )
        return

    try:
        invoice = await service.start_usdt(callback.from_user.id, MONTHS)
    except AppError as exc:
        if callback.message is not None:
            await smart_edit(
                callback.message,
                f"❌ {exc.message}",
                reply_markup=kb.payment_menu(callback.from_user.id),
            )
        return

    text = (
        "🪙 <b>Оплата USDT (TRC-20)</b>\n\n"
        f"Сеть: <b>{invoice['network']}</b>\n"
        f"Кошелёк: <code>{invoice['wallet']}</code>\n"
        f"Сумма: <b>{invoice['amount']:.3f} USDT</b>\n\n"
        "⚠️ Отправьте ровно эту сумму — копейки служат идентификатором платежа. "
        "Доступ включится автоматически после подтверждения в сети (обычно 1–3 минуты)."
    )
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=kb.back_to_main())


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
