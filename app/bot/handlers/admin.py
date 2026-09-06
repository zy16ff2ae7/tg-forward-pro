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

    online = len(list(manager.online_ids()))
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Пользователей: <b>{users}</b>\n"
        f"Активных абонементов: <b>{active_subs}</b>\n"
        f"Задач: <b>{rules}</b>\n"
        f"Аккаунтов: <b>{accounts}</b> (в сети: <b>{online}</b>)\n"
        f"Переслано всего: <b>{forwarded}</b>\n"
        f"Переслано за 24 ч: <b>{day['total']}</b>\n"
    )
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=kb.admin_menu())


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
