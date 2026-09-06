"""Админ-панель: статистика, ручная выдача абонемента, перезапуск аккаунтов."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from loguru import logger

from app.bot import keyboards as kb
from app.bot.utils import is_admin, smart_edit
from app.db import repo
from app.db.database import SessionLocal
from app.telegram_client.manager import manager

router = Router(name="admin")


async def show_admin(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return
    if callback.message is not None:
        await smart_edit(callback.message, "🛠 Админка", reply_markup=kb.admin_menu())


@router.callback_query(F.data == "admin:stats")
async def admin_stats(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    if not is_admin(callback.from_user.id):
        return

    async with SessionLocal() as session:
        users = await repo.count_users(session)
        forwarded = await repo.total_forwarded(session)
        active_subs = await repo.count_active_subscriptions(session)
        day = await repo.forward_stats(session, None, 1)

    online = len(list(manager.online_ids()))
    text = (
        "📊 <b>Статистика</b>\n\n"
        f"Пользователей: <b>{users}</b>\n"
        f"Активных абонементов: <b>{active_subs}</b>\n"
        f"Аккаунтов в сети: <b>{online}</b>\n"
        f"Переслано всего: <b>{forwarded}</b>\n"
        f"Переслано за 24 ч: <b>{day['total']}</b>\n"
    )
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=kb.admin_menu())


@router.callback_query(F.data == "admin:restart")
async def admin_restart(callback: CallbackQuery) -> None:
    await callback.answer("Перезапускаю аккаунты…")
    assert callback.from_user is not None
    if not is_admin(callback.from_user.id):
        return
    await manager.stop_all()
    await manager.start_all()
    if callback.message is not None:
        await smart_edit(callback.message, 
            "🔄 Аккаунты перезапущены.", reply_markup=kb.admin_menu()
        )


@router.message(Command("grant"))
async def grant_access(message: Message) -> None:
    """Выдаёт абонемент: /grant <user_id> [месяцев]"""
    assert message.from_user is not None
    if not is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await message.answer("Использование: <code>/grant 123456789 1</code>")
        return
    user_id = int(parts[1])
    months = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1

    async with SessionLocal() as session:
        await repo.get_or_create_user(session, user_id)
        until = await repo.activate_subscription(session, user_id, months)
        await session.commit()

    await message.answer(
        f"✅ Доступ выдан пользователю {user_id} до {until:%d.%m.%Y %H:%M} (UTC)."
    )
    try:
        await message.bot.send_message(
            user_id,
            "🎉 <b>Доступ открыт</b>\n\n"
            f"Абонемент активен до <b>{until:%d.%m.%Y %H:%M}</b> (UTC).\n"
            "Можно создавать правила и запускать пересылку.",
        )
    except Exception:  # noqa: BLE001
        logger.debug("Не смогли уведомить пользователя {}", user_id)


@router.message(Command("users"))
async def list_users(message: Message) -> None:
    assert message.from_user is not None
    if not is_admin(message.from_user.id):
        return
    async with SessionLocal() as session:
        user_ids = list(await repo.list_user_ids(session))
    await message.answer(f"Всего пользователей: {len(user_ids)}\n" + ", ".join(map(str, user_ids[:100])))
