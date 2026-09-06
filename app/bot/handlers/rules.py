"""Правила пересылки: создание, настройки, фильтры."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from loguru import logger

from app import exports
from app.bot import keyboards as kb
from app.bot import texts
from app.bot.states import EditStates, RuleStates
from app.bot.utils import ensure_user, smart_edit
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.telegram_client.filters import default_filters, parse_words
from app.telegram_client.jobs import COLLECTING_KINDS, MAX_PARSER_LIMIT, task_title
from app.telegram_client.manager import manager

router = Router(name="rules")

# Сколько строк собранного помещается в одно сообщение бота. Больше не влезает
# по лимиту в 4096 знаков, поэтому весь список отдаётся файлом.
RESULTS_PREVIEW = 20


async def _collected_count(rule) -> int:
    """Сколько задача уже нашла. У несобирающих задач — ноль без запроса."""
    if (rule.kind or "forward") not in COLLECTING_KINDS:
        return 0
    async with SessionLocal() as session:
        return await repo.count_collected_items(session, rule.id)


async def _rule_view(rule) -> tuple[str, InlineKeyboardMarkup]:
    """Карточка задачи и её меню — одним куском.

    Обоим нужно одно число: сколько задача уже нашла. Карточке — чтобы не
    показывать «сработало раз: 0» у парсера, собравшего тысячи (счётчик отправок
    он не трогает), меню — чтобы «⬇️ Файлом» появлялась там, где файл получится
    непустым.

    Кроме числа карточке нужно то, что раньше знал только кабинет: чем
    закончилось последнее срабатывание, жив ли абонемент и на связи ли аккаунт.
    Без этого бот писал «Состояние: работает ✅» задаче, которая сутки падает.
    Один сеанс на все три вопроса: карточка открывается на каждое нажатие, и
    лишние подключения к базе тут заметны.
    """
    kind = rule.kind or "forward"
    async with SessionLocal() as session:
        collected = (
            await repo.count_collected_items(session, rule.id)
            if kind in COLLECTING_KINDS
            else 0
        )
        health = (await repo.task_health(session, [rule.id])).get(rule.id)
        subscription_active = await repo.has_active_subscription(session, rule.user_id)
    card = texts.rule_card(
        rule,
        collected=collected,
        health=health,
        online=manager.is_online(rule.account_id),
        subscription_active=subscription_active,
    )
    return card, kb.rule_menu(rule, collected=collected)


@router.message(Command("rules"))
async def cmd_rules(message: Message) -> None:
    await ensure_user(message)
    assert message.from_user is not None
    async with SessionLocal() as session:
        rules = await repo.list_rules(session, message.from_user.id)
    await message.answer("📡 <b>Ваши правила</b>", reply_markup=kb.rules_menu(rules))


async def show_rules(callback: CallbackQuery) -> None:
    await callback.answer()
    assert callback.from_user is not None
    async with SessionLocal() as session:
        rules = await repo.list_rules(session, callback.from_user.id)
        text = (
            "📡 <b>Ваши правила</b>\n\n"
            f"Всего правил: {len(rules)}\n\n"
            "Правило — это пара «источник → приёмник». Как только в источнике "
            "появляется пост, он уходит в приёмник."
        )
    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=kb.rules_menu(rules))


@router.callback_query(F.data == "rule:create")
async def create_rule_choose_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    assert callback.from_user is not None
    async with SessionLocal() as session:
        accounts = await repo.list_accounts(session, callback.from_user.id)

    if not accounts:
        if callback.message is not None:
            await smart_edit(callback.message, 
                "Сначала подключите аккаунт: 👤 Аккаунты → ➕ Подключить аккаунт.",
                reply_markup=kb.back_to_main(),
            )
        return

    builder = kb.InlineKeyboardBuilder()
    for account in accounts:
        builder.row(
            kb.InlineKeyboardButton(
                text=f"👤 {account.phone}", callback_data=f"rule:acc:{account.id}"
            )
        )
    builder.row(kb.InlineKeyboardButton(text="◀️ Назад", callback_data="menu:rules"))
    if callback.message is not None:
        await smart_edit(callback.message, 
            "Выберите аккаунт, который будет читать источник:",
            reply_markup=builder.as_markup(),
        )


@router.callback_query(F.data.startswith("rule:acc:"))
async def choose_account(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    account_id = int(callback.data.split(":")[2])
    await state.set_state(RuleStates.source)
    await state.update_data(account_id=account_id)
    if callback.message is not None:
        await smart_edit(callback.message, 
            "📥 <b>Источник</b>\n\n"
            "Пришлите @username канала, ссылку t.me/... или его точное название "
            "(аккаунт должен быть на него подписан).\n\n"
            "ID чата тоже подойдёт — их можно посмотреть в «Чаты аккаунта».",
            reply_markup=kb.cancel_kb(),
        )


async def _resolve(callback_or_message, account_id: int, query: str):
    return await manager.resolve_chat(account_id, query)


@router.message(RuleStates.source)
async def set_source(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    account_id: int = data["account_id"]
    query = (message.text or "").strip()

    wait = await message.answer("🔎 Ищу чат…")
    found = await _resolve(message, account_id, query)
    if found is None:
        await wait.edit_text(
            "❌ Не нашёл такой чат у этого аккаунта.\n\n"
            "Проверьте, что аккаунт подписан на канал, и пришлите @username или ссылку ещё раз:",
            reply_markup=kb.cancel_kb(),
        )
        return

    source_id, source_title = found
    await state.update_data(source_id=source_id, source_title=source_title)
    await state.set_state(RuleStates.target)
    await wait.edit_text(
        f"✅ Источник: <b>{source_title}</b>\n\n"
        "📤 Теперь пришлите приёмник — куда пересылать посты "
        "(@username, ссылка или ID).\n"
        "В приёмнике у аккаунта должно быть право писать.",
        reply_markup=kb.cancel_kb(),
    )


@router.message(RuleStates.target)
async def set_target(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    account_id: int = data["account_id"]
    query = (message.text or "").strip()

    wait = await message.answer("🔎 Ищу чат…")
    found = await _resolve(message, account_id, query)
    if found is None:
        await wait.edit_text(
            "❌ Не нашёл такой чат. Пришлите @username, ссылку или ID ещё раз:",
            reply_markup=kb.cancel_kb(),
        )
        return

    target_id, target_title = found
    assert message.from_user is not None

    # Приёмник совпал с источником — такое правило молча ничего не делает:
    # свои же форварды аккаунт не читает. Ловим здесь, как в кабинете.
    if int(target_id) == int(data["source_id"]):
        await wait.edit_text(
            "❌ Приёмник совпадает с источником — пересылать будет некуда.\n\n"
            "Пришлите другой чат:",
            reply_markup=kb.cancel_kb(),
        )
        return

    async with SessionLocal() as session:
        # Архив в лимит не входит — так же считает кабинет.
        rules_count = await repo.count_rules(
            session, message.from_user.id, include_archived=False
        )
        subscribed = await repo.has_active_subscription(session, message.from_user.id)
        if not subscribed and rules_count >= settings.max_rules_free:
            await state.clear()
            await wait.edit_text(
                f"🔒 На бесплатном режиме доступно только {settings.max_rules_free} правила.\n"
                "Оформите абонемент, чтобы снять ограничение.",
                reply_markup=kb.payment_menu(message.from_user.id),
            )
            return

        rule = await repo.add_rule(
            session,
            user_id=message.from_user.id,
            account_id=account_id,
            source_id=data["source_id"],
            source_title=data["source_title"],
            target_id=target_id,
            target_title=target_title,
        )
        rule.filters = default_filters()
        await session.commit()
        rule_id = rule.id
        rule = await repo.get_rule(session, rule_id, message.from_user.id)

    await manager.refresh_rules()
    await state.clear()
    if rule is None:
        await wait.edit_text(
            "❌ Не удалось сохранить правило. Попробуйте создать заново.",
            reply_markup=kb.back_to_main(),
        )
        return
    card, menu = await _rule_view(rule)
    await wait.edit_text(f"🎉 Правило создано!\n\n{card}", reply_markup=menu)


@router.callback_query(F.data.startswith("rule:open:"))
async def open_rule(callback: CallbackQuery) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None
    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
    if rule is None:
        await callback.answer("Правило не найдено", show_alert=True)
        return
    if callback.message is not None:
        card, menu = await _rule_view(rule)
        await smart_edit(callback.message, card, reply_markup=menu)


@router.callback_query(F.data.startswith("rule:toggle:"))
async def toggle_rule(callback: CallbackQuery) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None
    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
        if rule is None:
            await callback.answer("Правило не найдено", show_alert=True)
            return
        rule.enabled = not rule.enabled
        await session.commit()
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
    await manager.refresh_rules()
    if callback.message is not None and rule is not None:
        card, menu = await _rule_view(rule)
        await smart_edit(callback.message, card, reply_markup=menu)


@router.callback_query(F.data.startswith("rule:mode:"))
async def switch_mode(callback: CallbackQuery) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None
    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
        if rule is None:
            return
        rule.mode = "forward" if rule.mode == "copy" else "copy"
        await session.commit()
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
    await manager.refresh_rules()
    if callback.message is not None and rule is not None:
        card, menu = await _rule_view(rule)
        await smart_edit(callback.message, card, reply_markup=menu)


@router.callback_query(F.data.startswith("rule:delete:"))
async def delete_rule(callback: CallbackQuery) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None
    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
        if rule is not None:
            await repo.delete_rule(session, rule)
            await session.commit()
    await manager.refresh_rules()
    if callback.message is not None:
        await smart_edit(callback.message, 
            "🗑 Правило удалено.", reply_markup=kb.back_to_main()
        )


# ───────────────────────────────── Настройки ──────────────────────────────────


@router.callback_query(F.data.startswith("rule:settings:"))
async def open_settings(callback: CallbackQuery) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    if callback.message is not None:
        await smart_edit(callback.message, 
            "⚙️ <b>Настройки правила</b>\n\n"
            "Задержка — пауза перед публикацией (имитирует живого человека).\n"
            "Стоп-слова — посты с такими словами отбрасываются.\n"
            "Только со словами — пропускаются только посты с этими словами.\n"
            "Типы медиа — что вообще пересылать.\n"
            "Чистка текста — вырезание ссылок, @упоминаний, пропуск репостов.\n"
            "Замены — автозамена слов и ссылок на свои.",
            reply_markup=kb.settings_menu(rule_id),
        )


@router.callback_query(F.data.startswith("set:delay:"))
async def ask_delay(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    await state.set_state(EditStates.delay)
    await state.update_data(rule_id=rule_id)
    if callback.message is not None:
        await smart_edit(callback.message, 
            "⏱ Задержка перед публикацией в секундах (0 — без задержки, максимум 3600):",
            reply_markup=kb.cancel_kb(),
        )


@router.message(EditStates.delay)
async def set_delay(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rule_id: int = data["rule_id"]
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Нужно число в секундах. Например: 30")
        return
    delay = max(0, min(3600, int(raw)))
    assert message.from_user is not None
    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, message.from_user.id)
        if rule is None:
            await state.clear()
            return
        rule.delay_seconds = delay
        await session.commit()
        rule = await repo.get_rule(session, rule_id, message.from_user.id)
    await manager.refresh_rules()
    await state.clear()
    card, _ = await _rule_view(rule)
    await message.answer(
        f"✅ Задержка: {delay} сек\n\n{card}",
        reply_markup=kb.settings_menu(rule_id),
    )


@router.callback_query(F.data.startswith("set:blacklist:"))
async def ask_blacklist(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    await state.set_state(EditStates.blacklist)
    await state.update_data(rule_id=rule_id)
    if callback.message is not None:
        await smart_edit(callback.message, 
            "🚫 Стоп-слова через запятую или с новой строки.\n"
            "Пост с любым из этих слов не будет переслан.\n"
            "Отправьте «-» чтобы очистить.",
            reply_markup=kb.cancel_kb(),
        )


@router.message(EditStates.blacklist)
async def set_blacklist(message: Message, state: FSMContext) -> None:
    await _save_wordlist(message, state, "blacklist")


@router.callback_query(F.data.startswith("set:whitelist:"))
async def ask_whitelist(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    await state.set_state(EditStates.whitelist)
    await state.update_data(rule_id=rule_id)
    if callback.message is not None:
        await smart_edit(callback.message, 
            "✅ Слова, которые должны быть в посте.\n"
            "Если список не пуст, пересылаются только посты с хотя бы одним из этих слов.\n"
            "Отправьте «-» чтобы очистить.",
            reply_markup=kb.cancel_kb(),
        )


@router.message(EditStates.whitelist)
async def set_whitelist(message: Message, state: FSMContext) -> None:
    await _save_wordlist(message, state, "whitelist")


async def _save_wordlist(message: Message, state: FSMContext, key: str) -> None:
    data = await state.get_data()
    rule_id: int = data["rule_id"]
    raw = (message.text or "").strip()
    words = [] if raw == "-" else parse_words(raw)
    assert message.from_user is not None

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, message.from_user.id)
        if rule is None:
            await state.clear()
            return
        filters = dict(rule.filters or default_filters())
        filters[key] = words
        rule.filters = filters
        await session.commit()

    await manager.refresh_rules()
    await state.clear()
    preview = ", ".join(words) if words else "пусто"
    await message.answer(
        f"✅ Сохранено ({key}): {preview}",
        reply_markup=kb.settings_menu(rule_id),
    )


@router.callback_query(F.data.startswith("set:media:"))
async def media_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None
    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
        if rule is None:
            return
        selected = (rule.filters or {}).get("media_types") or []
    if callback.message is not None:
        await smart_edit(callback.message, 
            "🎛 Какие типы сообщений пересылать:",
            reply_markup=kb.media_types_menu(rule_id, selected),
        )


@router.callback_query(F.data.startswith("media:"))
async def toggle_media(callback: CallbackQuery) -> None:
    _, rule_id_raw, kind = callback.data.split(":")
    rule_id = int(rule_id_raw)
    assert callback.from_user is not None
    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
        if rule is None:
            await callback.answer()
            return
        filters = dict(rule.filters or default_filters())
        selected = list(filters.get("media_types") or [])
        if kind in selected:
            selected.remove(kind)
        else:
            selected.append(kind)
        filters["media_types"] = selected
        rule.filters = filters
        await session.commit()
    await manager.refresh_rules()
    await callback.answer()
    if callback.message is not None:
        await smart_edit(callback.message, 
            "🎛 Какие типы сообщений пересылать:",
            reply_markup=kb.media_types_menu(rule_id, selected),
        )


@router.callback_query(F.data.startswith("set:clean:"))
async def clean_menu(callback: CallbackQuery) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None
    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
        if rule is None:
            return
        filters = dict(rule.filters or default_filters())
    if callback.message is not None:
        await smart_edit(callback.message, 
            "✂️ <b>Чистка текста</b>", reply_markup=kb.clean_menu(rule_id, filters)
        )


@router.callback_query(F.data.startswith("clean:") & ~F.data.startswith("clean:append:"))
async def toggle_clean(callback: CallbackQuery) -> None:
    _, action, rule_id_raw = callback.data.split(":")
    rule_id = int(rule_id_raw)
    key_map = {
        "links": "remove_links",
        "mentions": "remove_mentions",
        "forwards": "skip_forwards",
    }
    key = key_map.get(action)
    if key is None:
        await callback.answer()
        return
    assert callback.from_user is not None
    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
        if rule is None:
            await callback.answer()
            return
        filters = dict(rule.filters or default_filters())
        filters[key] = not filters.get(key, False)
        rule.filters = filters
        await session.commit()
    await manager.refresh_rules()
    await callback.answer("Сохранено")
    if callback.message is not None:
        await smart_edit(callback.message, 
            "✂️ <b>Чистка текста</b>", reply_markup=kb.clean_menu(rule_id, filters)
        )


@router.callback_query(F.data.startswith("clean:append:"))
async def ask_append(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    await state.set_state(EditStates.append)
    await state.update_data(rule_id=rule_id)
    if callback.message is not None:
        await smart_edit(callback.message, 
            "➕ Текст, который будет добавлен в конец каждого поста "
            "(подпись, ссылка на канал).\nОтправьте «-» чтобы очистить.",
            reply_markup=kb.cancel_kb(),
        )


@router.message(EditStates.append)
async def set_append(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rule_id: int = data["rule_id"]
    raw = (message.text or "").strip()
    value = "" if raw == "-" else raw
    assert message.from_user is not None
    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, message.from_user.id)
        if rule is None:
            await state.clear()
            return
        filters = dict(rule.filters or default_filters())
        filters["append_text"] = value
        rule.filters = filters
        await session.commit()
    await manager.refresh_rules()
    await state.clear()
    await message.answer(
        f"✅ Сохранено. Подпись: {value or 'нет'}",
        reply_markup=kb.settings_menu(rule_id),
    )


@router.callback_query(F.data.startswith("set:replace:"))
async def ask_replace(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    await state.set_state(EditStates.replace)
    await state.update_data(rule_id=rule_id)
    if callback.message is not None:
        await smart_edit(callback.message, 
            "🔁 Замены текста. Каждая пара — с новой строки, старое и новое через <code>=></code>:\n\n"
            "<code>конкурент => я</code>\n"
            "<code>t.me/other => t.me/my</code>\n\n"
            "Отправьте «-» чтобы очистить.",
            reply_markup=kb.cancel_kb(),
        )


@router.message(EditStates.replace)
async def set_replace(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    rule_id: int = data["rule_id"]
    raw = (message.text or "").strip()
    pairs: list[dict[str, str]] = []
    if raw != "-":
        for line in raw.splitlines():
            if "=>" not in line:
                continue
            src, dst = line.split("=>", 1)
            src, dst = src.strip(), dst.strip()
            if src:
                pairs.append({"from": src, "to": dst})
    assert message.from_user is not None
    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, message.from_user.id)
        if rule is None:
            await state.clear()
            return
        filters = dict(rule.filters or default_filters())
        filters["replace"] = pairs
        rule.filters = filters
        await session.commit()
    await manager.refresh_rules()
    await state.clear()
    await message.answer(
        f"✅ Сохранено замен: {len(pairs)}", reply_markup=kb.settings_menu(rule_id)
    )


@router.callback_query(F.data.startswith("rule:new:"))
async def quick_rule_from_account(callback: CallbackQuery, state: FSMContext) -> None:
    """Быстрый старт правила прямо из карточки аккаунта."""
    account_id = int(callback.data.split(":")[2])
    await choose_account_impl(callback, state, account_id)


async def choose_account_impl(
    callback: CallbackQuery, state: FSMContext, account_id: int
) -> None:
    await state.set_state(RuleStates.source)
    await state.update_data(account_id=account_id)
    await callback.answer()
    if callback.message is not None:
        await smart_edit(callback.message, 
            "📥 <b>Источник</b>\n\nПришлите @username канала, ссылку t.me/... или ID.",
            reply_markup=kb.cancel_kb(),
        )


# ────────────────────────── Архив, разовые задачи, результаты ─────────────────


@router.callback_query(F.data.startswith("rule:archive:"))
async def archive_rule(callback: CallbackQuery) -> None:
    """Убирает задачу в архив или возвращает из него."""
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
        if rule is None:
            await callback.answer("Задача не найдена", show_alert=True)
            return
        archived = not rule.archived
        await repo.set_rule_archived(session, rule, archived)
        await session.commit()
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)

    await manager.refresh_rules()
    if callback.message is not None and rule is not None:
        note = "📦 Задача убрана в архив.\n\n" if archived else "↩️ Задача возвращена из архива.\n\n"
        card, menu = await _rule_view(rule)
        await smart_edit(callback.message, note + card, reply_markup=menu)


@router.callback_query(F.data.startswith("rule:run:"))
async def run_rule_now(callback: CallbackQuery) -> None:
    """Запускает разовую задачу: парсер аудитории или автоподписку."""
    await callback.answer("Запускаю…")
    rule_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
    if rule is None:
        await callback.answer("Задача не найдена", show_alert=True)
        return

    if callback.message is not None:
        await smart_edit(
            callback.message,
            "⏳ <b>Выполняется…</b>\n\nЭто может занять пару минут — результат появится в этом же сообщении.",
        )
    result = await manager.run_task_now(rule)
    if callback.message is not None:
        # Собранное считаем после запуска: парсер только что дописал находки, и
        # «⬇️ Файлом» нужна здесь же — иначе за файлом пришлось бы идти через
        # «Результаты», хотя человек стоит ровно на итоге сбора.
        menu = kb.rule_menu(rule, collected=await _collected_count(rule))
        await smart_edit(callback.message, _run_result_text(rule, result), reply_markup=menu)


def _run_result_text(rule, result: dict) -> str:
    """Разворачивает сводку запуска в понятное сообщение."""
    if not result.get("ok"):
        return f"❌ Не получилось: {result.get('error') or 'неизвестная ошибка'}"
    if rule.kind == "parser":
        return (
            f"🕵️ Парсер собрал <b>{result.get('collected', 0)}</b> участников.\n\n"
            "Список — кнопкой «📄 Результаты»."
        )
    return (
        f"🤝 Автоподписка: вступили в <b>{result.get('joined', 0)}</b> "
        f"из {result.get('total', 0)} каналов."
    )


@router.callback_query(F.data.startswith("rule:results:"))
async def show_rule_results(callback: CallbackQuery) -> None:
    """Показывает, что насобирала задача — участников или чеки."""
    await callback.answer()
    rule_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
        if rule is None:
            await callback.answer("Задача не найдена", show_alert=True)
            return
        items = list(await repo.list_collected_items(session, rule_id, limit=RESULTS_PREVIEW))
        total = await repo.count_collected_items(session, rule_id)

    if not items:
        text = (
            "📄 <b>Результатов пока нет</b>\n\n"
            "Запустите задачу кнопкой «▶️ Запустить сейчас» — после этого "
            "собранное появится здесь."
        )
    else:
        lines = []
        for item in items:
            payload = item.payload or {}
            if rule.kind == "parser":
                name = (
                    payload.get("name")
                    or payload.get("username")
                    or str(payload.get("user_id") or "")
                )
                username = f" (@{payload['username']})" if payload.get("username") else ""
                lines.append(f"• {name}{username}")
            else:
                link = payload.get("link")
                lines.append(f"• {link or str(payload.get('text') or '')[:80]}")
        # Сообщение бота вмещает десятки строк, а не тысячи, поэтому остальное
        # достаётся кнопкой «⬇️ Файлом» — и об этом надо сказать прямо, иначе
        # «показаны последние 20 из 4000» выглядит как потерянная работа.
        more = (
            f"\n\nПоказаны последние {len(items)} из {total}."
            "\nВесь список — кнопкой «⬇️ Файлом»."
            if total > len(items)
            else ""
        )
        text = f"📄 <b>Результаты: {total}</b>\n\n" + "\n".join(lines) + more

    if callback.message is not None:
        await smart_edit(callback.message, text, reply_markup=kb.rule_menu(rule, collected=total))


@router.callback_query(F.data.startswith("rule:export:"))
async def export_rule_results(callback: CallbackQuery) -> None:
    """Присылает собранное файлом — тем же CSV, что и кабинет.

    Отвечаем на нажатие ровно один раз и в самом конце: у нажатия один ответ, и
    если истратить его на «готовлю файл», то отказ («бота заблокировали»)
    показать уже нечем — человек нажмёт и не узнает, почему файла нет. Пока
    файл собирается, кнопка и так подсвечена ожиданием.
    """
    rule_id = int(callback.data.split(":")[2])
    assert callback.from_user is not None

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, rule_id, callback.from_user.id)
        if rule is None:
            await callback.answer("Задача не найдена", show_alert=True)
            return
        kind = rule.kind or "forward"
        title = task_title(rule)
        items = list(await repo.list_collected_items(session, rule_id, limit=MAX_PARSER_LIMIT))
        total = await repo.count_collected_items(session, rule_id)

    if not items:
        await callback.answer("Выгружать пока нечего", show_alert=True)
        return

    document = BufferedInputFile(
        exports.collected_csv(kind, items), filename=exports.export_filename(kind, rule_id)
    )
    try:
        await callback.bot.send_document(
            callback.from_user.id,
            document,
            caption=exports.export_caption(kind, rule_title=title, sent=len(items), total=total),
        )
    except Exception as exc:  # noqa: BLE001 — причина в лог, человеку внятный отказ
        logger.warning("Не удалось отправить выгрузку задачи {}: {}", rule_id, exc)
        await callback.answer("Телеграм не принял файл. Попробуйте ещё раз.", show_alert=True)
        return

    await callback.answer(f"Файл отправлен: {len(items)} стр.")
