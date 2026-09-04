"""Главное меню и навигация."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.media import WELCOME_PHOTO
from app.bot.states import LoginStates
from app.bot.utils import ensure_user, is_admin, smart_edit
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal

router = Router(name="menu")

# Диплинки из мини-аппа: /start <ключ>
DEEP_LINKS = {
    "referrals": (
        "👥 Рефералы",
        "Ссылка, зеркала и выплаты появятся здесь после запуска партнёрской программы.",
    ),
    "messages": (
        "💬 Сообщения",
        "Сохранённые тексты, медиа и репосты настраиваются внутри правила: "
        "«Замены» и «Текст в конце».",
    ),
    "library": (
        "📚 Библиотека сообщений",
        "Публикации из вашего приватного канала. Режим копирования уже умеет брать оттуда посты.",
    ),
    "language": ("🌐 Язык", "Сейчас интерфейс на русском. Другие языки — в планах."),
    "guides": (
        "📖 Гайды",
        "Короткие инструкции: /help — основы, /rules — создание правил, "
        "/accounts — подключение аккаунта.",
    ),
    "resources": ("🛟 Ресурсы", "Чат поддержки и новостной канал — ссылки добавит администратор."),
}


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    user = await ensure_user(message)
    parts = (message.text or "").split(maxsplit=1)
    deep_link = parts[1].strip() if len(parts) > 1 else ""

    if deep_link == "distribute":
        from app.bot.handlers.subscription import show_bank_message

        await show_bank_message(message)
        return

    if deep_link in ("subscribe", "trial"):
        from app.bot.handlers.subscription import show_subscription_message

        await show_subscription_message(message)
        return

    if deep_link in ("add_account", "resume_login"):
        # Из кабинета нажали «Подключить аккаунт» — человек должен попасть
        # на сам шаг входа, а не в список аккаунтов: иначе он оказывается в
        # чате бота перед меню и не понимает, что делать дальше.
        from app.bot.handlers.accounts import begin_login_message

        await begin_login_message(message, state)
        return

    if deep_link == "accounts":
        from app.bot.handlers.accounts import show_accounts_message

        await show_accounts_message(message)
        return

    if deep_link in DEEP_LINKS:
        title, body = DEEP_LINKS[deep_link]
        await message.answer(f"<b>{title}</b>\n\n{body}", reply_markup=kb.back_to_main())
        return

    caption = texts.welcome(message.from_user.full_name or "друг")
    reply = kb.main_menu(is_admin(user.id))
    if WELCOME_PHOTO.exists():
        await message.answer_photo(FSInputFile(WELCOME_PHOTO), caption=caption, reply_markup=reply)
    else:
        await message.answer(caption, reply_markup=reply)


@router.message(Command("app"))
async def cmd_app(message: Message) -> None:
    """Кнопка для открытия мини-аппа."""
    url = settings.mini_app_url
    if not url:
        await message.answer(
            "Мини-апп ещё не привязан к публичному адресу.\n\n"
            "Задайте <code>WEBAPP_URL</code> (или <code>WEBHOOK_URL</code>) в .env — "
            "тогда кнопка появится.",
            reply_markup=kb.back_to_main(),
        )
        return

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🖥 Открыть кабинет", web_app=WebAppInfo(url=url))]
        ]
    )
    await message.answer(
        "🖥 <b>Кабинет</b>\n\nЗадачи, чаты, аккаунты и подписка — в одном окне.",
        reply_markup=markup,
    )


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    user = await ensure_user(message)
    await message.answer("Главное меню:", reply_markup=kb.main_menu(is_admin(user.id)))


@router.callback_query(F.data == "menu:main")
async def back_to_main(callback: CallbackQuery) -> None:
    user = await ensure_user(callback)
    await callback.answer()
    if callback.message is None:
        return
    await smart_edit(callback.message, 
        "Главное меню:", reply_markup=kb.main_menu(is_admin(user.id))
    )


@router.callback_query(F.data == "menu:help")
async def show_help(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is not None:
        await smart_edit(callback.message, texts.HELP, reply_markup=kb.back_to_main())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP, reply_markup=kb.back_to_main())


@router.callback_query(F.data == "nav:cancel")
async def cancel_action(callback: CallbackQuery, state: FSMContext) -> None:
    # Вход по номеру держит своё состояние в БД, а не во FSM: без этого «Отмена»
    # чистила бы только шаг в памяти, а кабинет и бот продолжали бы предлагать
    # «продолжить вход» на номер, от которого человек уже отказался.
    current = await state.get_state()
    await state.clear()
    if current in {LoginStates.phone.state, LoginStates.code.state, LoginStates.password.state}:
        from app import accounts_login

        if callback.from_user is not None:
            await accounts_login.cancel(callback.from_user.id)
    await callback.answer("Отменено")
    if callback.message is not None:
        await smart_edit(callback.message, "Действие отменено.", reply_markup=kb.back_to_main())


@router.callback_query(F.data == "menu:sub")
async def open_subscription(callback: CallbackQuery) -> None:
    """Раздел подписки — живёт в handlers/subscription.py, здесь только мост."""
    from app.bot.handlers.subscription import show_subscription

    await show_subscription(callback)


@router.callback_query(F.data == "menu:accounts")
async def open_accounts(callback: CallbackQuery) -> None:
    from app.bot.handlers.accounts import show_accounts

    await show_accounts(callback)


@router.callback_query(F.data == "menu:rules")
async def open_rules(callback: CallbackQuery) -> None:
    from app.bot.handlers.rules import show_rules

    await show_rules(callback)


@router.callback_query(F.data == "menu:admin")
async def open_admin(callback: CallbackQuery) -> None:
    from app.bot.handlers.admin import show_admin

    await show_admin(callback)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Короткая сводка по пользователю (без прав админа)."""
    if message.from_user is None:
        return
    async with SessionLocal() as session:
        until = await repo.subscription_until(session, message.from_user.id)
        rules_count = await repo.count_rules(session, message.from_user.id)
    status = (
        f"до {until:%d.%m.%Y}" if until else "не активен"
    )
    await message.answer(
        f"📊 Ваша статистика\n\nАбонемент: {status}\nПравил: {rules_count}",
        reply_markup=kb.back_to_main(),
    )
