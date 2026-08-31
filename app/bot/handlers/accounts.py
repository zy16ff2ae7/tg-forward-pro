"""Подключение личных Telegram-аккаунтов (вход по номеру телефона)."""
from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from loguru import logger
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from app.bot import keyboards as kb
from app.bot.states import LoginStates
from app.bot.utils import ensure_user, smart_edit
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.db.models import TelegramAccount
from app.security import decrypt_session, encrypt_session
from app.telegram_client.manager import manager

router = Router(name="accounts")

PHONE_RE = re.compile(r"^\+?\d{10,15}$")


@router.message(Command("accounts"))
async def cmd_accounts(message: Message) -> None:
    await show_accounts_message(message)


async def show_accounts_message(message: Message) -> None:
    """Список аккаунтов. Используется и из /accounts, и из диплинка мини-аппа."""
    await ensure_user(message)
    assert message.from_user is not None
    async with SessionLocal() as session:
        accounts = await repo.list_accounts(session, message.from_user.id)
        pending = await repo.get_pending_login(session, message.from_user.id)
    text = (
        "👤 <b>Ваши аккаунты</b>\n\n"
        "Подключённые номера читают источники и пересылают посты."
    )
    if not settings.public_login_enabled:
        text += (
            "\n\n⚙️ <b>Подключение аккаунтов временно на настройке</b>\n"
            "Кабинет, меню, подписки и платежи работают. "
            "Вход по номеру откроется после подключения MTProto-шлюза сервиса."
        )
    await message.answer(text, reply_markup=kb.accounts_menu(accounts, pending_login=pending is not None))


async def show_accounts(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    async with SessionLocal() as session:
        accounts = await repo.list_accounts(session, callback.from_user.id)
        pending = await repo.get_pending_login(session, callback.from_user.id)
    text = (
        "👤 <b>Ваши аккаунты</b>\n\n"
        "Здесь подключаются личные аккаунты Telegram — именно они читают каналы-источники.\n"
        f"Подключено: {len(accounts)}"
    )
    if not settings.public_login_enabled:
        text += (
            "\n\n⚙️ <b>Подключение аккаунтов временно на настройке</b>\n"
            "Кабинет, меню, подписки и платежи уже доступны. "
            "Вход по номеру откроется после подключения MTProto-шлюза сервиса."
        )
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=kb.accounts_menu(accounts, pending_login=pending is not None))


@router.callback_query(F.data == "acc:add")
async def add_account_start(callback: CallbackQuery, state: FSMContext) -> None:
    if not settings.public_login_enabled:
        await callback.answer("Вход по номеру пока на настройке", show_alert=True)
        if callback.message is not None:
            await smart_edit(callback.message, 
                "⚙️ <b>Подключение аккаунта пока недоступно</b>\n\n"
                "Я продолжаю собирать сервис как публичный платный бот. Сейчас можно "
                "проверять кабинет, меню, подписку и платежи, но вход личных аккаунтов "
                "по телефону требует MTProto-шлюз на стороне сервиса.\n\n"
                "Как только шлюз будет подключён, пользовательский сценарий будет таким: "
                "номер телефона → код из Telegram → облачный пароль 2FA, если он включён.",
                reply_markup=kb.back_to_main(),
            )
        return

    await callback.answer()
    await state.set_state(LoginStates.phone)
    if callback.message is not None:
        await smart_edit(callback.message, 
            "📱 Введите номер телефона в международном формате:\n\n"
            "<code>+79001234567</code>\n\n"
            "Код подтверждения придёт в официальном приложении Telegram.",
            reply_markup=kb.cancel_kb(),
        )


async def _resume_account_login(user_id: int, state: FSMContext, send) -> None:
    """Общая логика восстановления незавершённого входа."""
    async with SessionLocal() as session:
        pending = await repo.get_pending_login(session, user_id)

    if pending is None:
        await send(
            "Незавершённого входа не найдено. Начните заново: «Подключить аккаунт».",
            kb.back_to_main(),
        )
        return

    try:
        session_string = decrypt_session(pending.session_encrypted)
    except Exception:  # noqa: BLE001
        async with SessionLocal() as session:
            await repo.delete_pending_login(session, user_id)
            await session.commit()
        await send(
            "Не удалось восстановить временную сессию. Начните подключение заново.",
            kb.back_to_main(),
        )
        return

    await state.update_data(
        phone=pending.phone,
        session=session_string,
        hash=pending.phone_code_hash,
    )

    if pending.stage == "waiting_password":
        await state.set_state(LoginStates.password)
        text = "🔐 Введите облачный пароль (2FA), чтобы завершить подключение аккаунта."
    else:
        await state.set_state(LoginStates.code)
        text = (
            f"▶️ Продолжаем вход для <b>{pending.phone}</b>.\n\n"
            "Введите код из Telegram подряд без пробелов."
        )

    await send(text, kb.cancel_kb())


@router.message(Command("resume_login"))
async def cmd_resume_login(message: Message, state: FSMContext) -> None:
    """Команда на случай, если пользователь потерял кнопку в меню."""
    if message.from_user is None:
        return
    await _resume_account_login(
        user_id=message.from_user.id,
        state=state,
        send=lambda text, markup: message.answer(text, reply_markup=markup),
    )


@router.callback_query(F.data == "acc:resume")
async def resume_account_login(callback: CallbackQuery, state: FSMContext) -> None:
    """Восстанавливает незавершённый вход после перезапуска бота или потери FSM."""
    await callback.answer()
    if callback.from_user is None or callback.message is None:
        return
    await _resume_account_login(
        user_id=callback.from_user.id,
        state=state,
        send=lambda text, markup: smart_edit(callback.message, text, reply_markup=markup),
    )


@router.message(LoginStates.phone)
async def process_phone(message: Message, state: FSMContext) -> None:
    phone = (message.text or "").strip().replace(" ", "")
    if not PHONE_RE.match(phone):
        await message.answer(
            "Похоже, это не номер. Нужно в формате <code>+79001234567</code>. Попробуйте ещё раз:",
            reply_markup=kb.cancel_kb(),
        )
        return

    if not phone.startswith("+"):
        phone = "+" + phone

    assert message.from_user is not None
    wait_msg = await message.answer("⏳ Отправляю код…")

    try:
        session_string, phone_code_hash = await manager.send_code(phone)
    except PhoneNumberInvalidError:
        await wait_msg.edit_text("❌ Telegram не знает такой номер. Проверьте и введите заново:")
        return
    except PhoneNumberBannedError:
        await state.clear()
        await wait_msg.edit_text(
            "❌ Этот номер заблокирован в Telegram. Подключите другой.",
            reply_markup=kb.back_to_main(),
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Не удалось отправить код")
        await state.clear()
        await wait_msg.edit_text(
            f"❌ Не удалось отправить код: {type(exc).__name__}: {exc}",
            reply_markup=kb.back_to_main(),
        )
        return

    async with SessionLocal() as session:
        await repo.save_pending_login(
            session,
            user_id=message.from_user.id,
            phone=phone,
            session_encrypted=encrypt_session(session_string),
            phone_code_hash=phone_code_hash,
            stage="waiting_code",
        )
        await session.commit()

    await state.update_data(phone=phone, session=session_string, hash=phone_code_hash)
    await state.set_state(LoginStates.code)
    await wait_msg.edit_text(
        "✅ Код отправлен в Telegram.\n\n"
        "Введите его подряд без пробелов. Если код вида <code>1 2 3 4 5</code> — "
        "пришлите <code>12345</code>.",
        reply_markup=kb.cancel_kb(),
    )


@router.message(LoginStates.code)
async def process_code(message: Message, state: FSMContext) -> None:
    code = re.sub(r"\D", "", message.text or "")
    if not code:
        await message.answer("Нужны только цифры кода. Попробуйте ещё раз:")
        return

    data = await state.get_data()
    phone: str = data.get("phone", "")
    session_string: str = data.get("session", "")
    phone_code_hash: str = data.get("hash", "")
    assert message.from_user is not None

    wait_msg = await message.answer("⏳ Проверяю код…")
    try:
        session_string = await manager.sign_in_code(
            phone=phone, code=code, session_string=session_string,
            phone_code_hash=phone_code_hash,
        )
    except (PhoneCodeInvalidError, PhoneCodeExpiredError):
        async with SessionLocal() as session:
            await repo.delete_pending_login(session, message.from_user.id)
            await session.commit()
        await wait_msg.edit_text(
            "❌ Код не подошёл или устарел. Запросите новый: /accounts → «Подключить аккаунт».",
            reply_markup=kb.back_to_main(),
        )
        await state.clear()
        return
    except SessionPasswordNeededError:
        await state.update_data(session=session_string)
        async with SessionLocal() as session:
            await repo.save_pending_login(
                session,
                user_id=message.from_user.id,
                phone=phone,
                session_encrypted=encrypt_session(session_string),
                phone_code_hash=phone_code_hash,
                stage="waiting_password",
            )
            await session.commit()
        await state.set_state(LoginStates.password)
        await wait_msg.edit_text(
            "🔐 На аккаунте включён облачный пароль (2FA). Введите его:",
            reply_markup=kb.cancel_kb(),
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка входа по коду")
        async with SessionLocal() as session:
            await repo.delete_pending_login(session, message.from_user.id)
            await session.commit()
        await state.clear()
        await wait_msg.edit_text(
            f"❌ Ошибка входа: {type(exc).__name__}: {exc}",
            reply_markup=kb.back_to_main(),
        )
        return

    await _finish_login(message, state, session_string, phone, wait_msg)


@router.message(LoginStates.password)
async def process_password(message: Message, state: FSMContext) -> None:
    password = (message.text or "").strip()
    data = await state.get_data()
    session_string: str = data.get("session", "")
    phone: str = data.get("phone", "")
    assert message.from_user is not None

    if not session_string or not phone:
        async with SessionLocal() as session:
            pending = await repo.get_pending_login(session, message.from_user.id)
        if pending is not None:
            try:
                session_string = decrypt_session(pending.session_encrypted)
                phone = pending.phone
            except Exception:  # noqa: BLE001
                session_string = ""

    if not session_string or not phone:
        await state.clear()
        await message.answer(
            "Не удалось восстановить вход. Начните заново: /accounts → «Подключить аккаунт».",
            reply_markup=kb.back_to_main(),
        )
        return

    wait_msg = await message.answer("⏳ Проверяю пароль…")
    try:
        session_string = await manager.sign_in_password(password, session_string)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка ввода облачного пароля")
        await wait_msg.edit_text(
            f"❌ Пароль не подошёл: {type(exc).__name__}. Попробуйте снова:",
            reply_markup=kb.cancel_kb(),
        )
        return

    await _finish_login(message, state, session_string, phone, wait_msg)


async def _finish_login(
    message: Message,
    state: FSMContext,
    session_string: str,
    phone: str,
    wait_msg: Message,
) -> None:
    """Сохраняет аккаунт, поднимает клиент и показывает результат."""
    assert message.from_user is not None
    ok, name, error = await manager.check_session(session_string)

    if not ok:
        await state.clear()
        await wait_msg.edit_text(
            f"❌ Аккаунт не подтверждён: {error}\n\nПопробуйте подключить заново.",
            reply_markup=kb.back_to_main(),
        )
        return

    async with SessionLocal() as session:
        await repo.delete_pending_login(session, message.from_user.id)
        account = await repo.add_account(
            session,
            user_id=message.from_user.id,
            phone=phone,
            session_encrypted=encrypt_session(session_string),
        )
        await session.commit()
        account_id = account.id
        db_account = await session.get(TelegramAccount, account_id)
        assert db_account is not None
        started = await manager.start_account(db_account, session_string)
        await repo.set_account_error(session, db_account, None if started else "Не запустился")
        await session.commit()

    await manager.refresh_rules()
    await state.clear()
    await wait_msg.edit_text(
        f"✅ Аккаунт <b>{phone}</b> подключён ({name}).\n\n"
        "Теперь создайте правило: 📡 Мои правила → ➕ Создать правило.",
        reply_markup=kb.back_to_main(),
    )


@router.callback_query(F.data.startswith("acc:open:"))
async def open_account(callback: CallbackQuery) -> None:
    await callback.answer()
    account_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None
    async with SessionLocal() as session:
        account = await repo.get_account(session, account_id, callback.from_user.id)
    if account is None:
        await callback.answer("Аккаунт не найден", show_alert=True)
        return
    online = manager.is_online(account_id)
    text = (
        f"👤 <b>{account.phone}</b>\n\n"
        f"Статус: {'🟢 на связи' if online else '🔴 не в сети'}\n"
        f"Ошибка: {account.last_error or 'нет'}"
    )
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=kb.account_menu(account_id))


@router.callback_query(F.data.startswith("acc:chats:"))
async def account_chats(callback: CallbackQuery) -> None:
    await callback.answer()
    account_id = int(callback.data.split(":")[2])
    dialogs = await manager.list_dialogs(account_id, limit=30)
    if not dialogs:
        await callback.answer("Список чатов недоступен — аккаунт не в сети", show_alert=True)
        return
    lines = [
        f"{'📢' if d['is_channel'] else ('👥' if d['is_group'] else '💬')} "
        f"<b>{d['title']}</b> — <code>{d['id']}</code>"
        for d in dialogs
    ]
    text = "📋 <b>Чаты аккаунта</b>\n\n" + "\n".join(lines)
    text += "\n\nID пригодится, если не хотите вводить @username при создании правила."
    if callback.message is not None:
        await smart_edit(callback.message, 
            text, reply_markup=kb.account_menu(account_id)
        )


@router.callback_query(F.data.startswith("acc:delete:"))
async def delete_account(callback: CallbackQuery) -> None:
    await callback.answer()
    account_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None
    await manager.stop_account(account_id)
    async with SessionLocal() as session:
        account = await repo.get_account(session, account_id, callback.from_user.id)
        if account is not None:
            await session.delete(account)
            await session.commit()
    await manager.refresh_rules()
    if callback.message is not None:
        await smart_edit(callback.message, 
            "🗑 Аккаунт отключён.", reply_markup=kb.back_to_main()
        )
