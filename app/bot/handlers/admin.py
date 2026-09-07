"""Панель владельца: сводка, пользователи, выдача, рассылка, перезапуск."""
from __future__ import annotations

import asyncio
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger

from app.bot import keyboards as kb
from app.bot.states import OwnerStates
from app.bot.utils import is_admin, smart_edit
from app.db import repo
from app.db.database import SessionLocal
from app.plans import is_valid_period
from app.telegram_client.manager import manager

router = Router(name="admin")

# Пауза между отправками рассылки: Bot API не любит очередь без пауз.
BROADCAST_PAUSE = 0.05
USERS_SHOWN = 15


async def _denied(callback: CallbackQuery) -> bool:
    """Чужой нажал кнопку панели — говорим об этом вслух и ничего не делаем."""
    assert callback.from_user is not None
    if is_admin(callback.from_user.id):
        return False
    await callback.answer("Нет доступа", show_alert=True)
    return True


async def _dashboard() -> str:
    """Сводка одной строкой на раздел — её владелец видит сразу при входе."""
    async with SessionLocal() as session:
        users = await repo.count_users(session)
        active_subs = await repo.count_active_subscriptions(session)
        rules = await repo.count_rules_all(session)
        accounts = await repo.count_accounts_all(session)
        forwarded = await repo.total_forwarded(session)
        day = await repo.forward_stats(session, None, 1)
    online = len(list(manager.online_ids()))
    return (
        "🛠 <b>Панель владельца</b>\n\n"
        f"👥 Пользователей: <b>{users}</b> · абонементов: <b>{active_subs}</b>\n"
        f"📡 Задач: <b>{rules}</b> · аккаунтов: <b>{accounts}</b> (в сети: <b>{online}</b>)\n"
        f"📨 Переслано: <b>{forwarded}</b> · за 24 ч: <b>{day['total']}</b>\n"
    )


_CURRENCY_LABELS = {"XTR": "⭐", "RUB": "₽", "USDT": "USDT"}


def _money_line(revenue: dict[str, float]) -> str:
    """Выручка одной строкой: «500 ⭐ · 990 ₽». Пустая — честное «пока нет»."""
    parts = []
    for currency in ("XTR", "RUB", "USDT"):
        total = revenue.get(currency, 0)
        if total > 0:
            shown = int(total) if total == int(total) else round(total, 2)
            parts.append(f"{shown} {_CURRENCY_LABELS[currency]}")
    return " · ".join(parts) if parts else "пока нет"


def _conversion(bonus: int, paid: int) -> str:
    """Конверсия подарка в оплату — в скобках, если есть из чего считать."""
    if bonus <= 0:
        return ""
    return f" ({paid * 100 // bonus}%)"


# Сколько общих кодов влезает в экран: дальше — счётчик остатка.
PROMO_SHOWN = 10


def _promo_text(rows: list[dict]) -> str:
    """Конверсия кодов: активации → платящие, у скидочных — выручка."""
    public = [row for row in rows if row.get("code")]
    personal = next((row for row in rows if not row.get("code")), None)
    if not public and not personal:
        return "🎟 <b>Промокоды</b>\n\nПока нет ни одного кода."
    lines = []
    for row in public[:PROMO_SHOWN]:
        kind = f"−{row['percent']}%" if row["percent"] else f"{row['days']} дн."
        uses = f"{row['used']}/{row['max_uses']}" if row["max_uses"] else str(row["used"])
        line = f"• <code>{row['code']}</code> ({kind}): активаций <b>{uses}</b> → платят <b>{row['payers']}</b>"
        money = _money_line(row["revenue"])
        if money != "пока нет":
            line += f" · {money}"
        if not row["active"]:
            line += " (выкл)"
        lines.append(line)
    if len(public) > PROMO_SHOWN:
        lines.append(f"…и ещё {len(public) - PROMO_SHOWN}.")
    if personal:
        lines.append(
            f"• личные: <b>{personal['codes']}</b> шт · активаций <b>{personal['used']}</b> "
            f"→ платят <b>{personal['payers']}</b>"
        )
    return "🎟 <b>Промокоды</b>\n\n" + "\n".join(lines)


def _user_line(user_id: int, name: str, sub_until, created) -> str:
    sub = f"до {sub_until:%d.%m.%Y}" if sub_until else "—"
    return (
        f"• <code>{user_id}</code> {escape(name)} — аб: <b>{sub}</b> "
        f"({created:%d.%m.%Y})"
    )


async def _users_text(limit: int = USERS_SHOWN) -> str:
    """Свежие пользователи с состоянием абонемента — свежие вперёд."""
    async with SessionLocal() as session:
        total = await repo.count_users(session)
        users = list(await repo.recent_users(session, limit))
        lines = []
        for user in users:
            until = await repo.subscription_until(session, user.id)
            name = (
                f"@{user.username}"
                if user.username
                else (user.full_name or "без имени")
            )
            lines.append(_user_line(user.id, name, until, user.created_at))
    body = "\n".join(lines) if lines else "Пока пусто."
    return f"👥 <b>Пользователи</b> (всего: {total})\n\n{body}"


async def _do_grant(bot, user_id: int, months: int):
    """Выдаёт абонемент и пробует сообщить человеку. Возвращает (до, уведомлён)."""
    async with SessionLocal() as session:
        await repo.get_or_create_user(session, user_id)
        until = await repo.activate_subscription(session, user_id, months)
        await session.commit()
    try:
        await bot.send_message(
            user_id,
            "🎉 <b>Доступ открыт</b>\n\n"
            f"Абонемент активен до <b>{until:%d.%m.%Y %H:%M}</b> (UTC).\n"
            "Можно создавать правила и запускать пересылку.",
        )
    except Exception:  # noqa: BLE001 — человек мог заблокировать бота
        logger.debug("Не смогли уведомить пользователя {}", user_id)
        return until, False
    return until, True


def _parse_user_id(raw: str) -> int | None:
    """ID из текста владельца: только цифры, иначе None."""
    cleaned = (raw or "").strip()
    return int(cleaned) if cleaned.isdigit() else None


async def _send_broadcast(bot, user_ids, text: str) -> tuple[int, int]:
    """Шлёт текст всем по списку. Возвращает (дошло, не дошло).

    Один заблокировавший бота не останавливает остальных: Bot API отвечает
    ошибкой на каждое такое сообщение, и её глотаем поштучно.
    """
    sent = failed = 0
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text)
            sent += 1
        except Exception:  # noqa: BLE001 — адрес недоступен, идём дальше
            failed += 1
        await asyncio.sleep(BROADCAST_PAUSE)
    return sent, failed


async def _show_panel(message: Message, text: str | None = None) -> None:
    await smart_edit(
        message, text or await _dashboard(), reply_markup=kb.admin_menu()
    )


@router.callback_query(F.data == "admin:panel")
async def show_admin(callback: CallbackQuery) -> None:
    await callback.answer()
    if await _denied(callback):
        return
    if callback.message is not None:
        await _show_panel(callback.message)


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    """Панель владельца отдельным сообщением."""
    assert message.from_user is not None
    if not is_admin(message.from_user.id):
        return
    await message.answer(await _dashboard(), reply_markup=kb.admin_menu())


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    if await _denied(callback):
        return

    async with SessionLocal() as session:
        users = await repo.count_users(session)
        forwarded = await repo.total_forwarded(session)
        active_subs = await repo.count_active_subscriptions(session)
        rules = await repo.count_rules_all(session)
        accounts = await repo.count_accounts_all(session)
        day = await repo.forward_stats(session, None, 1)
        revenue = await repo.revenue_since(session, 30)
        bonus = await repo.count_bonus_claimed(session)
        paid = await repo.count_ever_paid(session)
        ended = await repo.count_ended_subscriptions(session)
        new_week = await repo.count_new_users(session, 7)

    online = len(list(manager.online_ids()))
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Пользователей: <b>{users}</b> (новых за 7 дней: <b>{new_week}</b>)\n"
        f"Активных абонементов: <b>{active_subs}</b> · кончились: <b>{ended}</b>\n"
        f"Задач: <b>{rules}</b>\n"
        f"Аккаунтов: <b>{accounts}</b> (в сети: <b>{online}</b>)\n"
        f"Переслано всего: <b>{forwarded}</b>\n"
        f"Переслано за 24 ч: <b>{day['total']}</b>\n"
        f"💰 Выручка за 30 дней: <b>{_money_line(revenue)}</b>\n"
        f"📉 Воронка: подарков <b>{bonus}</b> → платили <b>{paid}</b>"
        f"{_conversion(bonus, paid)} → активны <b>{active_subs}</b>\n"
    )
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=kb.admin_menu())


@router.callback_query(F.data == "admin:promo")
async def admin_promo(callback: CallbackQuery) -> None:
    await callback.answer()
    if await _denied(callback):
        return
    async with SessionLocal() as session:
        rows = await repo.promo_stats(session)
    if callback.message is not None:
        await smart_edit(callback.message, _promo_text(rows), reply_markup=kb.promo_menu())


@router.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery) -> None:
    await callback.answer()
    if await _denied(callback):
        return
    if callback.message is not None:
        await smart_edit(
            callback.message, await _users_text(), reply_markup=kb.admin_menu()
        )


@router.callback_query(F.data == "admin:restart")
async def admin_restart(callback: CallbackQuery) -> None:
    await callback.answer("Перезапускаю аккаунты…")
    if await _denied(callback):
        return
    await manager.stop_all()
    await manager.start_all()
    if callback.message is not None:
        await smart_edit(
            callback.message,
            "🔄 Аккаунты перезапущены.",
            reply_markup=kb.admin_menu(),
        )


# ────────────────────────── Выдача абонемента ──────────────────────────


@router.callback_query(F.data == "admin:grant")
async def admin_grant_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if await _denied(callback):
        return
    await state.set_state(OwnerStates.grant_user)
    if callback.message is not None:
        await smart_edit(
            callback.message,
            "💳 <b>Выдача абонемента</b>\n\nПришлите Telegram ID пользователя (цифры):",
            reply_markup=kb.cancel_kb(),
        )


@router.message(OwnerStates.grant_user)
async def admin_grant_user(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    if not is_admin(message.from_user.id):
        return
    user_id = _parse_user_id(message.text or "")
    if user_id is None:
        await message.answer(
            "Нужны только цифры — например <code>123456789</code>. Попробуйте ещё раз:",
            reply_markup=kb.cancel_kb(),
        )
        return
    await state.update_data(grant_user_id=user_id)
    await state.set_state(None)
    await message.answer(
        f"На сколько выдать пользователю <code>{user_id}</code>?",
        reply_markup=kb.grant_months_kb(),
    )


@router.callback_query(F.data.startswith("admin:grant:"))
async def admin_grant_months(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if await _denied(callback):
        return
    try:
        months = int((callback.data or "").split(":")[2])
    except (IndexError, ValueError):
        return
    if not is_valid_period(months):
        await callback.answer("Такого периода нет", show_alert=True)
        return
    data = await state.get_data()
    user_id = data.get("grant_user_id")
    if not user_id:
        # Состояние слетело (рестарт между шагами) — начинаем заново.
        if callback.message is not None:
            await smart_edit(
                callback.message,
                "Начните выдачу заново — я забыл, кому выдаём.",
                reply_markup=kb.admin_menu(),
            )
        return
    await state.clear()
    until, notified = await _do_grant(callback.bot, int(user_id), months)
    note = "человек уведомлён" if notified else "уведомить не вышло (возможно, бан бота)"
    if callback.message is not None:
        await smart_edit(
            callback.message,
            f"✅ Абонемент на {months} мес. выдан <code>{user_id}</code> "
            f"до {until:%d.%m.%Y %H:%M} (UTC), {note}.",
            reply_markup=kb.admin_menu(),
        )


@router.message(Command("grant"))
async def grant_access(message: Message) -> None:
    """Выдаёт абонемент: /grant <user_id> [месяцев]"""
    assert message.from_user is not None
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) < 2 or _parse_user_id(parts[1]) is None:
        await message.answer("Использование: <code>/grant 123456789 1</code>")
        return
    user_id = int(parts[1])
    months = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    if not is_valid_period(months):
        await message.answer("Период — 1, 3, 6 или 12 месяцев.")
        return

    until, _notified = await _do_grant(message.bot, user_id, months)
    await message.answer(
        f"✅ Доступ выдан пользователю {user_id} до {until:%d.%m.%Y %H:%M} (UTC)."
    )


def _parse_percent(raw: str) -> int | None:
    """Процент из «20%» и «−20%»: 1–90, иначе None (не скидка)."""
    cleaned = (raw or "").strip().lstrip("-−").rstrip()
    if not cleaned.endswith("%"):
        return None
    digits = cleaned[:-1]
    if not digits.isdigit():
        return None
    percent = int(digits)
    return percent if 1 <= percent <= 90 else None


@router.message(Command("promo_new"))
async def promo_new(message: Message) -> None:
    """Создаёт промокод: /promo_new КОД ДНИ [ЛИМИТ] [СРОК_ДНЕЙ].

    Лимит — сколько человек успеют активировать (по умолчанию без лимита),
    срок — сколько дней код живёт (по умолчанию бессрочно). Вместо дней
    можно дать скидку: /promo_new КОД 20% [ЛИМИТ] [СРОК_ДНЕЙ] — общий код
    на −N% к оплате для акций в канале.
    """
    from sqlalchemy.exc import IntegrityError

    assert message.from_user is not None
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    usage = (
        "Использование: <code>/promo_new LETO 7 100 3</code> — код, дни, лимит, "
        "срок в днях. Скидка: <code>/promo_new SALE 20% 500 3</code> — код, "
        "процент, лимит, срок."
    )
    if len(parts) < 3:
        await message.answer(usage)
        return
    percent = _parse_percent(parts[2])
    days = 0
    if percent is None:
        if not parts[2].isdigit() or int(parts[2]) < 1:
            await message.answer(usage)
            return
        days = int(parts[2])
    limit = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
    ttl = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
    try:
        async with SessionLocal() as session:
            promo = await repo.create_promo_code(
                session,
                parts[1],
                days,
                max_uses=limit,
                ttl_days=ttl or None,
                created_by=message.from_user.id,
                percent=percent or 0,
            )
            await session.commit()
    except IntegrityError:
        await message.answer(f"Код <code>{repo.normalize_promo_code(parts[1])}</code> уже существует.")
        return
    tail = f", лимит {limit}" if limit else ", без лимита"
    tail += f", срок {ttl} дн." if ttl else ""
    if percent:
        await message.answer(
            f"🎟 Промокод <code>{promo.code}</code> на −{percent}% к оплате{tail}."
        )
    else:
        await message.answer(
            f"🎟 Промокод <code>{promo.code}</code> на {promo.days} дн.{tail}."
        )


# ───────────────────── Мастер промокодов кнопками ─────────────────────


def _promo_reward(data: dict) -> str:
    """Награда кода словами: «−20% к оплате» или «7 дней доступа»."""
    if data.get("promo_type") == "percent":
        return f"−{data.get('promo_value')}% к оплате"
    return f"{data.get('promo_value')} дн. доступа"


def _promo_draft(data: dict) -> str:
    """Итог мастера перед созданием — всё выбранное одним экраном."""
    limit = data.get("promo_limit") or 0
    ttl = data.get("promo_ttl") or 0
    return (
        "🎟 <b>Новый промокод</b>\n\n"
        f"Код: <code>{data.get('promo_code')}</code>\n"
        f"Даёт: <b>{_promo_reward(data)}</b>\n"
        f"Лимит: <b>{'без лимита' if not limit else limit}</b>\n"
        f"Срок: <b>{'бессрочно' if not ttl else f'{ttl} дн.'}</b>"
    )


@router.callback_query(F.data.startswith("admin:promo:"))
async def admin_promo_step(callback: CallbackQuery, state: FSMContext) -> None:
    """Шаги мастера: тип → значение → код → лимит → срок → создать.

    Тип и значение живут в кнопках, код — текстом: набирать латиницу
    кнопками не выйдет. «Отмена» на каждом шаге — общий nav:cancel.
    """
    await callback.answer()
    if await _denied(callback):
        return
    message = callback.message
    if message is None:
        return
    parts = (callback.data or "").split(":")
    action = parts[2] if len(parts) > 2 else ""
    arg = parts[3] if len(parts) > 3 else ""

    if action == "new":
        await state.clear()
        await smart_edit(
            message,
            "🎟 <b>Новый промокод</b>\n\nЧто даёт код?",
            reply_markup=kb.promo_type_kb(),
        )
        return
    if action == "type":
        if arg not in ("percent", "days"):
            return
        await state.update_data(promo_type=arg)
        await smart_edit(
            message,
            "Размер скидки?" if arg == "percent" else "Сколько дней даёт код?",
            reply_markup=kb.promo_value_kb(arg),
        )
        return
    if action == "value":
        data = await state.get_data()
        kind = data.get("promo_type")
        if kind not in ("percent", "days"):
            await smart_edit(
                message,
                "Начните заново — я забыл, что даёт код.",
                reply_markup=kb.promo_menu(),
            )
            return
        if arg == "custom":
            await state.set_state(OwnerStates.promo_custom)
            await smart_edit(
                message,
                "Пришлите процент числом (1–90):"
                if kind == "percent"
                else "Пришлите число дней:",
                reply_markup=kb.cancel_kb(),
            )
            return
        if not arg.isdigit():
            return
        value = int(arg)
        if kind == "percent" and not 1 <= value <= 90:
            return
        if kind == "days" and value < 1:
            return
        await state.update_data(promo_value=value)
        await state.set_state(OwnerStates.promo_code)
        await smart_edit(
            message,
            f"Даёт: <b>{_promo_reward({**data, 'promo_value': value})}</b>.\n"
            "Пришлите текст кода (латиница и цифры):",
            reply_markup=kb.cancel_kb(),
        )
        return
    if action == "limit":
        if not arg.isdigit():
            return
        await state.update_data(promo_limit=int(arg))
        await smart_edit(
            message, "Сколько живёт код?", reply_markup=kb.promo_ttl_kb()
        )
        return
    if action == "ttl":
        if not arg.isdigit():
            return
        await state.update_data(promo_ttl=int(arg))
        data = await state.get_data()
        if not data.get("promo_code") or not data.get("promo_value"):
            # Состояние слетело (рестарт между шагами) — начинаем заново.
            await smart_edit(
                message,
                "Начните заново — я забыл, что создаём.",
                reply_markup=kb.promo_menu(),
            )
            return
        await smart_edit(
            message, _promo_draft(data), reply_markup=kb.promo_confirm_kb()
        )
        return
    if action == "make":
        data = await state.get_data()
        if not data.get("promo_code") or not data.get("promo_value"):
            await smart_edit(
                message,
                "Начните заново — я забыл, что создаём.",
                reply_markup=kb.promo_menu(),
            )
            return
        from sqlalchemy.exc import IntegrityError

        assert callback.from_user is not None
        try:
            async with SessionLocal() as session:
                promo = await repo.create_promo_code(
                    session,
                    str(data["promo_code"]),
                    0 if data.get("promo_type") == "percent" else int(data["promo_value"]),
                    max_uses=int(data.get("promo_limit") or 0),
                    ttl_days=int(data.get("promo_ttl") or 0) or None,
                    created_by=callback.from_user.id,
                    percent=int(data["promo_value"])
                    if data.get("promo_type") == "percent"
                    else 0,
                )
                await session.commit()
        except IntegrityError:
            # Код заняли, пока мастер шёл: возвращаемся на шаг кода.
            await state.set_state(OwnerStates.promo_code)
            await smart_edit(
                message,
                f"Код <code>{data['promo_code']}</code> уже заняли. Пришлите другой:",
                reply_markup=kb.cancel_kb(),
            )
            return
        await state.clear()
        await smart_edit(
            message,
            f"🎟 Промокод <code>{promo.code}</code> создан: "
            f"<b>{_promo_reward(data)}</b>.",
            reply_markup=kb.promo_menu(),
        )


@router.message(OwnerStates.promo_custom)
async def admin_promo_custom(message: Message, state: FSMContext) -> None:
    """Своё значение награды числом: процент 1–90 или дни от 1."""
    assert message.from_user is not None
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    kind = data.get("promo_type")
    if kind not in ("percent", "days"):
        await state.clear()
        await message.answer(
            "Начните заново — я забыл, что даёт код.",
            reply_markup=kb.admin_menu(),
        )
        return
    raw = (message.text or "").strip()
    percent = _parse_percent(raw) if kind == "percent" else None
    if percent is None and kind == "percent" and raw.isdigit():
        number = int(raw)
        percent = number if 1 <= number <= 90 else None
    if kind == "percent" and percent is None:
        await message.answer(
            "Нужен процент от 1 до 90 — например <code>25</code>. Попробуйте ещё раз:",
            reply_markup=kb.cancel_kb(),
        )
        return
    if kind == "days" and (not raw.isdigit() or int(raw) < 1):
        await message.answer(
            "Нужно число дней от 1 — например <code>7</code>. Попробуйте ещё раз:",
            reply_markup=kb.cancel_kb(),
        )
        return
    value = percent if kind == "percent" else int(raw)
    await state.update_data(promo_value=value)
    await state.set_state(OwnerStates.promo_code)
    await message.answer(
        f"Даёт: <b>{_promo_reward({**data, 'promo_value': value})}</b>.\n"
        "Пришлите текст кода (латиница и цифры):",
        reply_markup=kb.cancel_kb(),
    )


@router.message(OwnerStates.promo_code)
async def admin_promo_code(message: Message, state: FSMContext) -> None:
    """Текст кода: приводим к виду, занятый отклоняем — и дальше к лимиту."""
    assert message.from_user is not None
    if not is_admin(message.from_user.id):
        return
    code = repo.normalize_promo_code(message.text or "")
    if not code:
        await message.answer(
            "Код не может быть пустым. Пришлите текст кода:",
            reply_markup=kb.cancel_kb(),
        )
        return
    async with SessionLocal() as session:
        taken = await repo.get_promo_code(session, code)
    if taken is not None:
        await message.answer(
            f"Код <code>{code}</code> уже существует. Пришлите другой:",
            reply_markup=kb.cancel_kb(),
        )
        return
    await state.update_data(promo_code=code)
    await state.set_state(None)
    await message.answer(
        "Сколько человек успеют активировать?",
        reply_markup=kb.promo_limit_kb(),
    )


# ─────────────────────────────── Рассылка ───────────────────────────────


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if await _denied(callback):
        return
    async with SessionLocal() as session:
        total = len(await repo.list_user_ids(session))
    await state.set_state(OwnerStates.broadcast_text)
    if callback.message is not None:
        await smart_edit(
            callback.message,
            f"📣 <b>Рассылка</b> (получателей: {total})\n\n"
            "Пришлите текст одним сообщением — разметка отправится как есть, "
            "без форматирования:",
            reply_markup=kb.cancel_kb(),
        )


@router.message(OwnerStates.broadcast_text)
async def admin_broadcast_text(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer(
            "Пусто — пришлите текст рассылки:", reply_markup=kb.cancel_kb()
        )
        return
    # Экранируем: бот шлёт в HTML, а кривой тег уронил бы всю рассылку.
    await state.update_data(broadcast_text=escape(text))
    await state.set_state(None)
    async with SessionLocal() as session:
        total = len(await repo.list_user_ids(session))
    await message.answer(
        f"Так дойдёт до <b>{total}</b>:\n\n{escape(text)}",
        reply_markup=kb.broadcast_confirm_kb(),
    )


@router.callback_query(F.data == "admin:bcast:cancel")
async def admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отменено")
    if await _denied(callback):
        return
    await state.clear()
    if callback.message is not None:
        await _show_panel(callback.message)


@router.callback_query(F.data == "admin:bcast:send")
async def admin_broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Отправляю…")
    if await _denied(callback):
        return
    data = await state.get_data()
    text = data.get("broadcast_text") or ""
    await state.clear()
    if not text:
        if callback.message is not None:
            await smart_edit(
                callback.message,
                "Текст потерян (возможно, рестарт) — начните рассылку заново.",
                reply_markup=kb.admin_menu(),
            )
        return
    async with SessionLocal() as session:
        user_ids = list(await repo.list_user_ids(session))
    sent, failed = await _send_broadcast(callback.bot, user_ids, text)
    logger.info("Рассылка владельца: дошло {}, не дошло {}", sent, failed)
    if callback.message is not None:
        await smart_edit(
            callback.message,
            f"📣 Рассылка готова: дошло <b>{sent}</b>, не дошло <b>{failed}</b>.",
            reply_markup=kb.admin_menu(),
        )


@router.message(Command("users"))
async def list_users(message: Message) -> None:
    assert message.from_user is not None
    if not is_admin(message.from_user.id):
        return
    await message.answer(await _users_text())
