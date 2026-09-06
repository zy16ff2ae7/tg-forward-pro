"""Подключение личных Telegram-аккаунтов в боте (вход по номеру телефона).

Сами шаги входа живут в ``app/accounts_login.py`` — тот же сценарий работает в
кабинете, а состояние шага лежит в БД, а не в памяти процесса. Здесь остаётся
только разговор: какой текст показать и какого ввода ждать дальше.

Отсюда же правило обработки ошибок, одинаковое на всех шагах:

* ``ValidationError`` — ввод не подошёл, но шаг остаётся тем же. Опечатка в
  одной цифре кода больше не сбрасывает вход и не заставляет ждать новый код;
* ``ConflictError`` и ``FeatureUnavailable`` — продолжать нечего (код устарел,
  сессия побилась, шлюз выключен), поэтому FSM чистим и уводим в меню.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import accounts_login as login
from app.bot import keyboards as kb
from app.bot.states import LoginStates
from app.bot.utils import ensure_user, smart_edit
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.errors import AppError, ValidationError
from app.telegram_client.manager import manager

router = Router(name="accounts")

PHONE_PROMPT = (
    "📱 Введите номер телефона в международном формате:\n\n"
    "<code>+79001234567</code>\n\n"
    "Код подтверждения придёт в официальном приложении Telegram."
)
CODE_PROMPT = (
    "Введите код подряд без пробелов: код вида <code>1 2 3 4 5</code> — "
    "это <code>12345</code>."
)

# Куда Telegram положил код — человеческими словами. Ключи — это ``delivery.via``
# из app/accounts_login.py; неизвестное будущее не врёт, а молчит.
DELIVERY_HINTS = {
    "app": "смотрите чат «Telegram» в приложении",
    "sms": "смотрите SMS на этом номере",
    "call": "сейчас позвонит Telegram и продиктует код",
    "flashcall": "сейчас придёт дозвон-сброс от Telegram",
    "firebase": "смотрите SMS на этом номере",
    "missed": "сейчас придёт пропущенный звонок от Telegram",
}


def _delivery_hint(delivery: dict | None) -> str:
    """Где искать код — одна строка для сообщений бота и кабинета."""
    if not delivery:
        return "Код подтверждения придёт в официальном приложении Telegram."
    hint = DELIVERY_HINTS.get(str(delivery.get("via") or ""))
    if hint:
        return f"Код отправлен: {hint}."
    return "Код подтверждения придёт в официальном приложении Telegram."
PASSWORD_PROMPT = "🔐 На аккаунте включён облачный пароль (2FA). Введите его:"


async def _delete_secret(message: Message) -> None:
    """Стирает сообщение с кодом/паролем: секретам не место в истории чата."""
    try:
        await message.delete()
    except Exception:  # noqa: BLE001 — удаление best effort
        pass

SETUP_TEXT = (
    "⚙️ <b>Подключение аккаунта пока недоступно</b>\n\n"
    "Кабинет, меню, подписка и платежи уже работают. Вход личных аккаунтов по "
    "телефону включится, когда сервис подключит MTProto-шлюз.\n\n"
    "Сценарий будет такой: номер телефона → код из Telegram → облачный пароль "
    "2FA, если он включён."
)

async def _accounts_text(user_id: int) -> tuple[str, object]:
    """Текст и клавиатура списка аккаунтов — общие для команды и для кнопки."""
    async with SessionLocal() as session:
        accounts = list(await repo.list_accounts(session, user_id))
        pending = await repo.get_pending_login(session, user_id)

    lines = [
        "👤 <b>Ваши аккаунты</b>",
        "",
        "Подключённые номера читают источники и пересылают посты.",
        f"Подключено: {len(accounts)}",
    ]
    if pending is not None:
        step = "код из Telegram" if pending.stage == login.STAGE_CODE else "облачный пароль"
        lines += ["", f"▶️ Незавершённый вход {pending.phone}: ждём {step}."]
    if not settings.public_login_enabled:
        lines += [
            "",
            "⚙️ <b>Подключение аккаунтов временно на настройке</b>",
            "Кабинет, меню, подписки и платежи работают. Вход по номеру "
            "откроется после подключения MTProto-шлюза сервиса.",
        ]
    return "\n".join(lines), kb.accounts_menu(accounts, pending_login=pending is not None)


@router.message(Command("accounts"))
async def cmd_accounts(message: Message) -> None:
    await show_accounts_message(message)


async def show_accounts_message(message: Message) -> None:
    """Список аккаунтов. Используется и из /accounts, и из диплинка мини-аппа."""
    await ensure_user(message)
    assert message.from_user is not None
    text, markup = await _accounts_text(message.from_user.id)
    await message.answer(text, reply_markup=markup)


async def show_accounts(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    text, markup = await _accounts_text(callback.from_user.id)
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=markup)


# ─────────────────────────────── Начало входа ─────────────────────────────────


async def _open_login(user_id: int, state: FSMContext) -> tuple[str, object]:
    """Куда поставить человека: на новый вход или на середину незавершённого."""
    if not settings.public_login_enabled:
        await state.clear()
        return SETUP_TEXT, kb.back_to_main()

    pending = await login.pending(user_id)
    if pending is None:
        await state.set_state(LoginStates.phone)
        return PHONE_PROMPT, kb.cancel_kb()

    if pending.stage == "password":
        await state.set_state(LoginStates.password)
        return PASSWORD_PROMPT, kb.cancel_kb()

    await state.set_state(LoginStates.code)
    return (
        f"▶️ Продолжаем вход для <b>{pending.phone}</b>.\n\n"
        "Введите код из Telegram подряд без пробелов.\n"
        f"Осталось попыток: {pending.attempts_left}.",
        kb.login_code_kb(),
    )


@router.callback_query(F.data == "acc:add")
async def add_account_start(callback: CallbackQuery, state: FSMContext) -> None:
    assert callback.from_user is not None
    text, markup = await _open_login(callback.from_user.id, state)
    if not settings.public_login_enabled:
        await callback.answer("Вход по номеру пока на настройке", show_alert=True)
    else:
        await callback.answer()
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=markup)


async def begin_login_message(message: Message, state: FSMContext) -> None:
    """Вход из кабинета: диплинк ``/start add_account`` ведёт прямо на шаг.

    Раньше диплинк открывал список аккаунтов, и человек, нажавший в мини-аппе
    «Подключить аккаунт», оказывался в чате бота перед меню — без подсказки,
    что делать дальше. Теперь бот сразу спрашивает номер (или продолжает
    начатый вход).
    """
    await ensure_user(message)
    assert message.from_user is not None
    text, markup = await _open_login(message.from_user.id, state)
    await message.answer(text, reply_markup=markup)


@router.message(Command("resume_login"))
async def cmd_resume_login(message: Message, state: FSMContext) -> None:
    """Команда на случай, если пользователь потерял кнопку в меню."""
    await begin_login_message(message, state)


@router.callback_query(F.data == "acc:resume")
async def resume_account_login(callback: CallbackQuery, state: FSMContext) -> None:
    """Продолжает вход после перезапуска бота или потери FSM."""
    await add_account_start(callback, state)


# ──────────────────────────────── Шаги входа ──────────────────────────────────


async def _step_failed(
    error: AppError, wait_msg: Message, state: FSMContext, *, stay: bool
) -> None:
    """Показывает отказ и решает, остаётся ли человек на этом шаге.

    ``stay`` — шаг тот же (ошибка ввода), иначе вход закончился и FSM чистим.
    """
    if stay:
        await wait_msg.edit_text(f"❌ {error.message}", reply_markup=kb.cancel_kb())
        return
    await state.clear()
    await wait_msg.edit_text(f"❌ {error.message}", reply_markup=kb.back_to_main())


def _finish_text(step: login.LoginStep) -> str:
    return (
        f"✅ Аккаунт <b>{step.phone}</b> подключён ({step.name}).\n\n"
        "Дальше — правило: 📡 Мои правила → ➕ Создать правило. "
        "Или всё то же в кабинете: /app."
    )


@router.message(LoginStates.phone)
async def process_phone(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    wait_msg = await message.answer("⏳ Отправляю код…")
    try:
        step = await login.start(message.from_user.id, message.text)
    except AppError as exc:
        # Отказ может сам сказать, где человеку теперь место: код на этот номер
        # уже ушёл — значит, ждём код, а не номер. Раньше такой отказ чистил FSM,
        # и введённый код улетал в никуда: человек оставался в чате с ботом, а
        # вход надо было начинать заново.
        stage = str(exc.details.get("stage") or "")
        if stage == "code":
            await state.set_state(LoginStates.code)
            await _step_failed(exc, wait_msg, state, stay=True)
            return
        # Неверный формат номера или пауза перед новым кодом — остаёмся на шаге.
        # Всё остальное (номер заблокирован, шлюз выключен, Telegram не ответил)
        # — конец попытки.
        stay = isinstance(exc, ValidationError) or stage == "phone"
        await _step_failed(exc, wait_msg, state, stay=stay)
        return

    await state.set_state(LoginStates.code)
    await wait_msg.edit_text(
        f"✅ Код отправлен на <b>{step.phone}</b>.\n\n"
        f"{_delivery_hint(step.delivery)}\n\n{CODE_PROMPT}",
        reply_markup=kb.login_code_kb(),
    )


@router.callback_query(F.data == "acc:resend")
async def resend_code(callback: CallbackQuery, state: FSMContext) -> None:
    """«Код не пришёл» — повтор тем же способом, каким Telegram шлёт дальше.

    Обычно это переключение «приложение → SMS → звонок». Код из прошлого
    сообщения после повтора мёртв — вводить надо новый.
    """
    assert callback.from_user is not None
    pending = await login.pending(callback.from_user.id)
    if pending is None or pending.stage != "code":
        await callback.answer("Незавершённого входа нет — введите номер заново.")
        await state.set_state(LoginStates.phone)
        if callback.message is not None:
            await smart_edit(
                callback.message, PHONE_PROMPT, reply_markup=kb.cancel_kb()
            )
        return
    await callback.answer("Запрашиваю повтор…")
    try:
        step = await login.start(callback.from_user.id, pending.phone, resend=True)
    except AppError as exc:
        # Повтор не убивает вход: остаёмся на шаге кода при любом отказе,
        # у которого известно место (а у повтора оно известно всегда).
        if isinstance(exc, ValidationError) or str(exc.details.get("stage") or ""):
            await state.set_state(LoginStates.code)
            markup: object = kb.login_code_kb()
            tail = CODE_PROMPT
        else:
            await state.clear()
            markup = kb.back_to_main()
            tail = ""
        if callback.message is not None:
            await smart_edit(
                callback.message,
                f"❌ {exc.message}" + (f"\n\n{tail}" if tail else ""),
                reply_markup=markup,
            )
        return
    await state.set_state(LoginStates.code)
    if callback.message is not None:
        await smart_edit(
            callback.message,
            f"✅ Новый код отправлен на <b>{step.phone}</b>.\n\n"
            f"{_delivery_hint(step.delivery)}\n"
            "Код из прошлого сообщения больше не действует — вводите новый.\n\n"
            f"{CODE_PROMPT}",
            reply_markup=kb.login_code_kb(),
        )


@router.message(LoginStates.code)
async def process_code(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    await _delete_secret(message)
    wait_msg = await message.answer("⏳ Проверяю код…")
    try:
        step = await login.submit_code(message.from_user.id, message.text)
    except AppError as exc:
        await _step_failed(exc, wait_msg, state, stay=isinstance(exc, ValidationError))
        return

    if step.stage == "password":
        await state.set_state(LoginStates.password)
        await wait_msg.edit_text(PASSWORD_PROMPT, reply_markup=kb.cancel_kb())
        return

    await state.clear()
    await wait_msg.edit_text(_finish_text(step), reply_markup=kb.back_to_main())


@router.message(LoginStates.password)
async def process_password(message: Message, state: FSMContext) -> None:
    assert message.from_user is not None
    await _delete_secret(message)
    wait_msg = await message.answer("⏳ Проверяю пароль…")
    try:
        step = await login.submit_password(message.from_user.id, message.text or "")
    except AppError as exc:
        await _step_failed(exc, wait_msg, state, stay=isinstance(exc, ValidationError))
        return

    await state.clear()
    await wait_msg.edit_text(_finish_text(step), reply_markup=kb.back_to_main())


# ────────────────────────── Подключённый аккаунт ──────────────────────────────


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
        await smart_edit(callback.message, text, reply_markup=kb.account_menu(account_id))


@router.callback_query(F.data.startswith("acc:delete:"))
async def delete_account(callback: CallbackQuery) -> None:
    await callback.answer()
    account_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None
    try:
        phone = await login.disconnect(callback.from_user.id, account_id)
    except AppError as exc:
        await callback.answer(exc.message, show_alert=True)
        return
    if callback.message is not None:
        await smart_edit(
            callback.message,
            f"🗑 Аккаунт <b>{phone}</b> отключён. Сохранённая сессия удалена.",
            reply_markup=kb.back_to_main(),
        )
