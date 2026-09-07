"""Абонемент и оплата: Stars, карта/СБП, USDT, ручная выдача."""
from __future__ import annotations

from html import escape as html_escape

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
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

from app import bonus, promocode, referral
from app.bot import keyboards as kb
from app.bot import texts
from app.bot.states import GiftStates, PromoStates
from app.bot.utils import ensure_user, smart_edit
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.errors import AppError
from app.payments import crypto, service, yookassa
from app.plans import (
    DEFAULT_MONTHS,
    STARS_DESCRIPTION,
    STARS_SUBSCRIPTION_PERIOD,
    apply_discount,
    is_valid_period,
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
        autorenew = await repo.stars_autorenew(session, user_id)
    return texts.subscription_status(until, rules_count, autorenew=autorenew)


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


# ───────────────────── Реферальная программа ──────────────────────


async def _referral_card(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with SessionLocal() as session:
        stats = await referral.info(session, user_id)
    text = texts.referral_card(
        stats["link"], stats["code"], stats["days"], stats["invited"], stats["earned_days"],
        rewarded=int(stats["rewarded"] or 0),
        discount_percent=int(stats["discount_percent"] or 0),
        discount_codes=tuple(stats["discount_codes"] or ()),
        pending_discount=int(stats["pending_discount"] or 0),
    )
    return text, kb.referral_menu(stats["link"])


async def show_referral_message(message: Message) -> None:
    """Экран «Пригласи друга». Сюда ведут /ref и диплинк referrals."""
    await ensure_user(message)
    assert message.from_user is not None
    text, markup = await _referral_card(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.message(Command("ref"))
async def cmd_ref(message: Message) -> None:
    await show_referral_message(message)


@router.callback_query(F.data == "ref:open")
async def open_referral(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    text, markup = await _referral_card(callback.from_user.id)
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=markup)


# ───────────────────── Промокод ──────────────────────


async def _redeem_code_message(message: Message, raw: str, state: FSMContext) -> None:
    """Активирует введённый код и показывает итог с меню оплаты."""
    assert message.from_user is not None
    await state.clear()
    async with SessionLocal() as session:
        result = await promocode.redeem(session, message.from_user.id, raw)
        if result.granted:
            await session.commit()
        else:
            await session.rollback()
    await message.answer(
        promocode.message(result),
        reply_markup=kb.payment_menu(message.from_user.id),
    )


@router.message(Command("promo"))
async def cmd_promo(message: Message, state: FSMContext) -> None:
    """Активирует код: ``/promo КОД`` — или спрашивает код следующим сообщением."""
    await ensure_user(message)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        await _redeem_code_message(message, parts[1], state)
        return
    await state.set_state(PromoStates.waiting_code)
    await message.answer(
        "🎟 <b>Промокод</b>\n\nПришлите код следующим сообщением:",
        reply_markup=kb.cancel_kb(),
    )


@router.message(PromoStates.waiting_code)
async def promo_code_entered(message: Message, state: FSMContext) -> None:
    await _redeem_code_message(message, message.text or "", state)


@router.callback_query(F.data == "promo:open")
async def open_promo(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.set_state(PromoStates.waiting_code)
    if callback.message is not None:
        await smart_edit(
            callback.message,
            "🎟 <b>Промокод</b>\n\nПришлите код следующим сообщением:",
            reply_markup=kb.cancel_kb(),
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
    description = STARS_DESCRIPTION
    async with SessionLocal() as session:
        pending = await repo.pending_discount(session, callback.from_user.id)
    if pending is not None:
        # Скидка применяется здесь, а гаснет при зачёте платежа: неоплаченный
        # счёт её не сжигает, и кнопки сроков врут в меньшую сторону осознанно —
        # точную цену человек видит в самом счёте.
        amount = int(apply_discount(amount, int(pending.percent or 0)))
        description = f"{STARS_DESCRIPTION} Скидка {pending.percent}% по промокоду."
    title = f"Абонемент на {months} мес."
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=title,
        description=description,
        # Формат читает хендлер successful_payment — менять нельзя.
        payload=f"sub:{callback.from_user.id}:{months}",
        provider_token="",  # для Stars токен не нужен
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=amount)],
    )


@router.callback_query(F.data == "pay:stars:auto")
async def pay_stars_autorenew(callback: CallbackQuery) -> None:
    """Шаг 2-авто: счёт-подписка — Telegram списывает месяц сам.

    Payload того же формата, что у разовых счетов: каждое списание продлевает
    абонемент на месяц тем же хендлером successful_payment. Отличается только
    subscription_period — и флаг is_recurring в приходящих апдейтах.
    """
    await callback.answer()
    assert callback.from_user is not None

    # Автопродление скидок не знает: Telegram списывает по условиям первого
    # счёта каждый месяц, и разовая скидка стала бы вечной. Хотите дешевле —
    # платите разовыми счетами.
    amount = stars_amount(1)
    title = "Абонемент с автопродлением"
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=title,
        description=STARS_DESCRIPTION,
        # Формат читает хендлер successful_payment — менять нельзя.
        payload=f"sub:{callback.from_user.id}:1",
        provider_token="",  # для Stars токен не нужен
        currency="XTR",
        prices=[LabeledPrice(label=f"{title} — {amount} ⭐/мес", amount=amount)],
        subscription_period=STARS_SUBSCRIPTION_PERIOD,
    )


@router.callback_query(F.data == "pay:gift")
async def pay_gift(callback: CallbackQuery, state: FSMContext) -> None:
    """Подарок, шаг 1: кому дарим."""
    await callback.answer()
    await state.set_state(GiftStates.waiting_friend)
    if callback.message is not None:
        await smart_edit(
            callback.message,
            "🎁 <b>Подарок другу</b>\n\n"
            "Пришлите id или @username друга следующим сообщением. Друг должен "
            "хотя бы раз запустить бота — незнакомцу дарить нечего.",
            reply_markup=kb.cancel_kb(),
        )


@router.message(GiftStates.waiting_friend)
async def gift_friend_entered(message: Message, state: FSMContext) -> None:
    """Подарок, шаг 2: нашли друга — выбираем срок."""
    assert message.from_user is not None
    raw = (message.text or "").strip()
    async with SessionLocal() as session:
        if raw.isdigit():
            target = await repo.get_user(session, int(raw))
        else:
            target = await repo.get_user_by_username(session, raw)
    if target is None:
        await message.answer(
            "Не нашли такого: друг сначала должен запустить бота (/start). "
            "Пришлите другой id или @username:",
            reply_markup=kb.cancel_kb(),
        )
        return
    if target.id == message.from_user.id:
        await state.clear()
        await message.answer(
            "Себе дарить не надо — оформите абонемент как обычно 🙂",
            reply_markup=kb.payment_menu(message.from_user.id),
        )
        return
    await state.update_data(gift_to=target.id)
    await state.set_state(None)
    nick = f"@{target.username}" if target.username else f"id {target.id}"
    await message.answer(
        f"🎁 Подарок для <b>{nick}</b> — выберите срок "
        f"({settings.price_stars} ⭐ за месяц):",
        reply_markup=kb.stars_periods(prefix="pay:gift", with_autorenew=False),
    )


@router.callback_query(F.data.startswith("pay:gift:"))
async def pay_gift_period(callback: CallbackQuery, state: FSMContext) -> None:
    """Подарок, шаг 3: счёт на выбранный срок. Платит даритель."""
    await callback.answer()
    assert callback.from_user is not None and callback.data is not None
    # Тот же разбор срока, что у своих счетов: три части, срок — последний.
    months = months_from_callback(callback.data)
    data = await state.get_data()
    friend_id = data.get("gift_to")
    if months is None or not friend_id:
        logger.warning("Подарок: непонятные данные {} / {}", callback.data, data)
        if callback.message is not None:
            await smart_edit(
                callback.message,
                "Не удалось разобрать подарок. Начните заново: «💝 Подарить абонемент».",
                reply_markup=kb.payment_menu(callback.from_user.id),
            )
        return
    async with SessionLocal() as session:
        friend = await repo.get_user(session, int(friend_id))
        pending = await repo.pending_discount(session, callback.from_user.id)
    if friend is None or friend.id == callback.from_user.id:
        await state.clear()
        if callback.message is not None:
            await smart_edit(
                callback.message,
                "Получатель потерялся. Начните подарок заново.",
                reply_markup=kb.payment_menu(callback.from_user.id),
            )
        return
    await state.clear()
    amount = stars_amount(months)
    description = STARS_DESCRIPTION
    if pending is not None:
        # Скидка дарителя действует и на подарок: платит-то он.
        amount = int(apply_discount(amount, int(pending.percent or 0)))
        description = f"{STARS_DESCRIPTION} Скидка {pending.percent}% по промокоду."
    title = f"Подарок: абонемент на {months} мес."
    await callback.bot.send_invoice(
        chat_id=callback.from_user.id,
        title=title,
        description=description,
        # Формат читает хендлер successful_payment — менять нельзя.
        payload=f"gift:{callback.from_user.id}:{friend.id}:{months}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=title, amount=amount)],
    )


def _parse_payload(payload: str) -> tuple[str, int | None, int | None, int | None]:
    """Разбирает payload счёта: ``sub:<кто>:<срок>`` или ``gift:<кто>:<кому>:<срок>``.

    Возвращает (вид, плательщик, получатель, срок). Получатель — только у
    подарка. Мусор — вид ``""``: оба сверяющих хендлера ответят отказом.
    """
    parts = (payload or "").split(":")
    try:
        if len(parts) == 3 and parts[0] == "sub":
            return "sub", int(parts[1]), None, int(parts[2])
        if len(parts) == 4 and parts[0] == "gift":
            return "gift", int(parts[1]), int(parts[2]), int(parts[3])
    except ValueError:
        pass
    return "", None, None, None


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    """Проверяем счёт до списания: срок из каталога, сумма наша, плательщик тот.

    Без этого пользователь мог бы оплатить чужую (пересланную) ссылку — деньги
    ушли бы, а подписка включилась бы не ему.
    """
    payload = query.invoice_payload or ""
    kind, owner_id, friend_id, months = _parse_payload(payload)

    accepted = {stars_amount(months)} if months is not None else set()
    if months is not None and is_valid_period(months):
        async with SessionLocal() as session:
            pending = await repo.pending_discount(session, query.from_user.id)
        if pending is not None:
            # Счёт выставили со скидкой — списываем тоже со скидкой. Проверка
            # повторяется здесь, а не только при создании счёта: между ними
            # скидку могли уже потратить другим платежом.
            accepted.add(int(apply_discount(stars_amount(months), int(pending.percent or 0))))
    gift_ok = True
    if kind == "gift":
        # Подарок себе — подделка payload: честный путь это запрещает раньше.
        gift_ok = friend_id is not None and friend_id != query.from_user.id
        if gift_ok:
            async with SessionLocal() as session:
                gift_ok = await repo.get_user(session, friend_id) is not None
    if (
        kind not in ("sub", "gift")
        or months is None
        or not is_valid_period(months)
        or owner_id != query.from_user.id
        or query.currency != "XTR"
        or query.total_amount not in accepted
        or not gift_ok
    ):
        logger.warning(
            "Stars pre_checkout отклонён: payload={!r} amount={} {} от {}",
            payload,
            query.total_amount,
            query.currency,
            query.from_user.id,
        )
        await query.answer(
            ok=False,
            error_message="Этот счёт выписан не вам или устарел. Создайте новый из бота.",
        )
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_stars_paid(message: Message) -> None:
    payment = message.successful_payment
    assert payment is not None and message.from_user is not None
    kind, owner_id, friend_id, months = _parse_payload(payment.invoice_payload or "")
    user_id = owner_id if owner_id is not None else message.from_user.id
    months = months if months is not None else MONTHS

    # Финальная сверка уже после списания: pre_checkout мог пройти до смены
    # тарифа, а апдейт — приехать дважды. Молча активировать «что-то» нельзя.
    full_price = stars_amount(months) if is_valid_period(months) else None
    async with SessionLocal() as session:
        stars_pending = await repo.pending_discount(session, message.from_user.id)
        gift_ok = True
        if kind == "gift":
            gift_ok = (
                friend_id is not None
                and friend_id != message.from_user.id
                and await repo.get_user(session, friend_id) is not None
            )
    accepted_amounts = {full_price} if full_price is not None else set()
    if stars_pending is not None and full_price is not None:
        accepted_amounts.add(
            int(apply_discount(full_price, int(stars_pending.percent or 0)))
        )
    if (
        kind not in ("sub", "gift")
        or user_id != message.from_user.id
        or payment.currency != "XTR"
        or not is_valid_period(months)
        or payment.total_amount not in accepted_amounts
        or not gift_ok
    ):
        logger.error(
            "Stars-платёж не сошёлся: payload={!r} amount={} {} payer={}",
            payment.invoice_payload,
            payment.total_amount,
            payment.currency,
            message.from_user.id,
        )
        await message.answer(
            "⚠️ Оплата прошла, но счёт не совпал с тарифом — подписка не включилась "
            "автоматически. Напишите администратору, разберёмся вручную.",
            reply_markup=kb.back_to_main(),
        )
        return

    async with SessionLocal() as session:
        # Повторная доставка того же апдейта (ретраи Telegram) не должна
        # продлевать подписку второй раз за один платёж.
        duplicate = await repo.get_payment_by_external_id(
            session, "stars", payment.telegram_payment_charge_id
        )
        if duplicate is not None:
            logger.warning(
                "Stars: повторный апдейт {} — уже зачислен, пропускаем",
                payment.telegram_payment_charge_id,
            )
            await session.commit()
            return
        await repo.create_payment(
            session,
            user_id=user_id,
            provider="stars",
            amount=payment.total_amount,
            currency=payment.currency,
            months=months,
            external_id=payment.telegram_payment_charge_id,
        )
        # Подарок включает абонемент не плательщику, а другу. Строка платежа
        # пишется на плательщика: платил он, ему и чек.
        recipient_id = friend_id if kind == "gift" else user_id
        assert recipient_id is not None
        until = await repo.activate_subscription(session, recipient_id, months)
        # Уплачено меньше тарифа — сработала ожидавшая скидка: гасим её.
        # Повторный апдейт сюда не доходит (проверка дубликата выше), поэтому
        # дважды скидка не гаснет, а чужой платёж её не трогает.
        await repo.consume_pending_discount(
            session,
            user_id,
            provider="stars",
            paid_amount=float(payment.total_amount),
            months=months,
        )
        # Первый оплаченный абонемент — награда пригласившему (если друга
        # приводили по ссылке). Повторный апдейт сюда не доходит, а второй
        # платёж того же друга награды не даёт: один друг — одна награда.
        # Подарок награды не даёт: «оплатил» — значит, сам заплатил.
        stars_reward = None
        if kind == "sub":
            stars_reward = await repo.reward_referrer(
                session, user_id,
                settings.referral_days, referral.discount_percent(),
            )
        # Рекуррентное списание — признак живой подписки: взводим флаг.
        # Разовый платёж флага не касается: подписка могла остаться с прошлого
        # раза, а могла и не быть — гадать по одному платежу нельзя.
        recurring = bool(
            getattr(payment, "is_recurring", False)
            or getattr(payment, "is_first_recurring", False)
        )
        if recurring and kind == "sub":
            await repo.set_stars_autorenew(session, user_id, True)
        await session.commit()

    if stars_reward is not None:
        referrer_id, deal_code = stars_reward
        try:
            await message.bot.send_message(
                referrer_id,
                referral.referrer_reward_message(
                    message.from_user.full_name or "друг",
                    settings.referral_days,
                    deal_code,
                ),
            )
        except Exception:  # noqa: BLE001 — награда начислена, весть вторична
            logger.debug("Не смогли уведомить {} о награде", referrer_id)

    if kind == "gift":
        # Друг узнаёт о подарке сразу — иначе сюрприз раскроется, только когда
        # он сам откроет абонемент. Имя дарителя — из апдейта: в базу за ним
        # ходить не надо, а разметку из чужого имени экранируем.
        giver = message.from_user
        giver_name = (
            f"@{giver.username}" if giver.username else html_escape(giver.full_name or "друг")
        )
        assert friend_id is not None
        try:
            await message.bot.send_message(
                friend_id,
                "🎁 <b>Вам подарили абонемент!</b>\n\n"
                f"{giver_name} оплатил вам {months} мес. — "
                f"доступен до <b>{until:%d.%m.%Y %H:%M}</b> (UTC).\n"
                "Можно создавать правила и запускать пересылку.",
            )
        except Exception:  # noqa: BLE001 — подарок уже включён, весть вторична
            logger.debug("Не смогли уведомить {} о подарке", friend_id)
        await message.answer(
            "🎁 <b>Подарок оплачен</b>\n\n"
            f"Абонемент друга активен до <b>{until:%d.%m.%Y %H:%M}</b> (UTC).",
            reply_markup=kb.back_to_main(),
        )
        return

    head = (
        "🔁 <b>Автопродление сработало</b>"
        if recurring
        else "🎉 <b>Оплата прошла</b>"
    )
    await message.answer(
        head + "\n\n"
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
        # Чужой счёт проверять нельзя: id последовательный и легко перебирается.
        if payment is None or payment.user_id != callback.from_user.id:
            found = False
        else:
            found = bool(payment.external_id)
        if not found:
            if callback.message is not None:
                await smart_edit(
                    callback.message,
                    "Платёж не найден или уже обработан.",
                    reply_markup=kb.payment_menu(callback.from_user.id),
                )
            return
        assert payment is not None and payment.external_id

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
        check_reward = await repo.reward_referrer(
            session, payment.user_id,
            settings.referral_days, referral.discount_percent(),
        )
        await session.commit()

    if check_reward is not None:
        referrer_id, deal_code = check_reward
        friend = callback.from_user
        try:
            await callback.bot.send_message(
                referrer_id,
                referral.referrer_reward_message(
                    (friend.full_name if friend else "") or "друг",
                    settings.referral_days,
                    deal_code,
                ),
            )
        except Exception:  # noqa: BLE001 — награда начислена, весть вторична
            logger.debug("Не смогли уведомить {} о награде", referrer_id)

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
