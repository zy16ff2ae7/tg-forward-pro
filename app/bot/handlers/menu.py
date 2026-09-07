"""Главное меню и навигация."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from app.bot import keyboards as kb
from app.bot import texts
from app.bot.media import WELCOME_PHOTO
from app.bot.states import LoginStates
from app.bot.utils import answer_with_banner, ensure_user, is_admin, smart_edit
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.telegram_client.jobs import task_title
from app.telegram_client.manager import manager

router = Router(name="menu")

_BARS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[int]) -> str:
    """Мини-график одной строкой: ▁▂▃… по максимуму ряда."""
    if not values or max(values) <= 0:
        return "—"
    peak = max(values)
    return "".join(
        _BARS[min(len(_BARS) - 1, round(v / peak * (len(_BARS) - 1)))] for v in values
    )


async def _menu_counts(user_id: int) -> dict:
    """Счётчики для кнопок главного меню: правила, аккаунты в сети, абонемент."""
    async with SessionLocal() as session:
        rules_count = await repo.count_rules(session, user_id, include_archived=False)
        accounts = list(await repo.list_accounts(session, user_id))
        until = await repo.subscription_until(session, user_id)
    online = sum(1 for a in accounts if manager.is_online(a.id))
    return {
        "rules_count": rules_count,
        "accounts_online": online,
        "accounts_total": len(accounts),
        "sub_active": until is not None,
    }

# Диплинки из мини-аппа: /start <ключ>
# (referrals тут больше нет: партнёрская программа запущена, и ссылка ведёт
# на настоящий экран — см. ветку ниже.)
DEEP_LINKS = {
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

    if deep_link == "bonus":
        # «3 дня за подписку» из кабинета: сразу на экран подарка, а не в меню
        # оплаты — иначе человек ищет обещанный подарок среди кнопок цен.
        from app.bot.handlers.subscription import show_bonus_message

        await show_bonus_message(message)
        return

    if deep_link == "referrals":
        from app.bot.handlers.subscription import show_referral_message

        await show_referral_message(message)
        return

    referral_note = ""
    if deep_link.startswith("ref_"):
        # Друг пришёл по ссылке: дни обоим — но только если друг новичок
        # (это проверяет сам apply). Итог дописываем к приветствию: ссылка —
        # повод зайти, а не отдельный экран.
        from app import referral as referral_program

        try:
            referrer_id = int(deep_link[4:])
        except ValueError:
            referrer_id = 0
        if referrer_id:
            async with SessionLocal() as session:
                result = await referral_program.apply(session, user.id, referrer_id)
                referrer = await repo.get_user(session, referrer_id)
                if result.granted:
                    await session.commit()
                else:
                    await session.rollback()
            friend = (message.from_user.full_name if message.from_user else "") or "друг"
            referral_note = "\n\n" + referral_program.message(
                result, referrer.mention if referrer else "друг"
            )
            if result.granted:
                # Пригласившему — радость сразу: иначе он узнает о днях,
                # только открыв абонемент.
                try:
                    await message.bot.send_message(
                        referrer_id,
                        referral_program.referrer_message(friend, result.days),
                    )
                except Exception:  # noqa: BLE001 — друг свои дни уже получил
                    pass

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

    caption = texts.welcome(message.from_user.full_name or "друг") + referral_note
    reply = kb.main_menu(is_admin(user.id), **await _menu_counts(user.id))
    await answer_with_banner(message, WELCOME_PHOTO, caption, reply_markup=reply)


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
    reply = kb.main_menu(is_admin(user.id), **await _menu_counts(user.id))
    await message.answer("Главное меню:", reply_markup=reply)


@router.callback_query(F.data == "menu:main")
async def back_to_main(callback: CallbackQuery) -> None:
    user = await ensure_user(callback)
    await callback.answer()
    if callback.message is None:
        return
    reply = kb.main_menu(is_admin(user.id), **await _menu_counts(user.id))
    await smart_edit(callback.message, "Главное меню:", reply_markup=reply)


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
    user_id = message.from_user.id
    async with SessionLocal() as session:
        until = await repo.subscription_until(session, user_id)
        rules = list(await repo.list_rules(session, user_id, include_archived=False))
        accounts = list(await repo.list_accounts(session, user_id))
        agg = await repo.forward_stats(session, user_id)
    ordered = [c for _, c in sorted(agg["per_day"].items())]
    online = sum(1 for a in accounts if manager.is_online(a.id))
    status = f"до {until:%d.%m.%Y}" if until else "не активен"
    top = sorted(rules, key=lambda r: r.forwarded_count or 0, reverse=True)[:3]
    lines = [
        "📊 <b>Ваша статистика</b>",
        "",
        f"Абонемент: {status}",
        f"Правил: <b>{len(rules)}</b> · аккаунтов в сети: <b>{online}/{len(accounts)}</b>",
        f"Переслано за неделю: <b>{agg['total']}</b>",
        f"<code>{_sparkline(ordered)}</code>",
    ]
    if top:
        lines.append("")
        lines.append("Топ задач:")
        for rule in top:
            lines.append(f"• {task_title(rule)} — {rule.forwarded_count or 0}")
    await message.answer("\n".join(lines), reply_markup=kb.back_to_main())


@router.message(Command("forget"))
async def cmd_forget(message: Message) -> None:
    """«Удалить мои данные» — сначала предупреждение, удаление по кнопке."""
    await message.answer(
        "🗑 <b>Удаление данных</b>\n\n"
        "Уберу всё, что вы оставляли в сервисе: задачи, подключённые аккаунты "
        "(сессии сгорят), журнал, результаты парсера, библиотеку и абонемент.\n\n"
        "Аккаунты Telegram при этом не пострадают — удалятся только их копии "
        "в сервисе. Действие необратимое.",
        reply_markup=kb.forget_confirm_kb(),
    )


@router.callback_query(F.data == "forget:no")
async def forget_cancel(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message is not None:
        await smart_edit(callback.message, "Хорошо, ничего не трогаю. 🙂")


@router.callback_query(F.data == "forget:yes")
async def forget_confirm(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    removed = await manager.forget_user(callback.from_user.id)
    tasks = removed.get("rules", 0)
    accounts = removed.get("accounts", 0)
    if callback.message is not None:
        await smart_edit(
            callback.message,
            "🗑 <b>Готово, всё удалено</b>\n\n"
            f"Задач: {tasks}, аккаунтов: {accounts}, плюс журнал, результаты "
            "и библиотека.\n\n"
            "Если передумаете — /start начнёт всё с чистого листа.",
        )
