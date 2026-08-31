"""Клавиатуры бота."""
from __future__ import annotations

from typing import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import settings
from app.db.models import Rule, TelegramAccount


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    # Кнопка мини-аппа — только если известен публичный HTTPS-адрес
    mini_url = settings.mini_app_url
    if mini_url:
        builder.row(
            InlineKeyboardButton(
                text="🖥 Открыть кабинет", web_app=WebAppInfo(url=mini_url)
            )
        )

    builder.row(
        InlineKeyboardButton(text="📡 Мои правила", callback_data="menu:rules"),
        InlineKeyboardButton(text="👤 Аккаунты", callback_data="menu:accounts"),
    )
    builder.row(
        InlineKeyboardButton(text="💳 Подписка", callback_data="menu:sub"),
        InlineKeyboardButton(text="❓ Помощь", callback_data="menu:help"),
    )
    if is_admin:
        builder.row(InlineKeyboardButton(text="🛠 Админка", callback_data="menu:admin"))
    return builder.as_markup()


def accounts_menu(
    accounts: Sequence[TelegramAccount], pending_login: bool = False
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for account in accounts:
        status = "🟢" if account.is_active else "🔴"
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {account.phone}", callback_data=f"acc:open:{account.id}"
            )
        )
    if pending_login and settings.public_login_enabled:
        builder.row(InlineKeyboardButton(text="▶️ Продолжить вход", callback_data="acc:resume"))
    add_text = "➕ Подключить аккаунт" if settings.public_login_enabled else "⚙️ Нужен MTProto-вход"
    builder.row(InlineKeyboardButton(text=add_text, callback_data="acc:add"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main"))
    return builder.as_markup()


def account_menu(account_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📋 Чаты аккаунта", callback_data=f"acc:chats:{account_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="➕ Правило с этим аккаунтом", callback_data=f"rule:new:{account_id}"
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🗑 Отключить аккаунт", callback_data=f"acc:delete:{account_id}"
        )
    )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="menu:accounts"))
    return builder.as_markup()


def rules_menu(rules: Sequence[Rule]) -> InlineKeyboardMarkup:
    from app.telegram_client.jobs import task_title

    builder = InlineKeyboardBuilder()
    for rule in rules:
        mark = "📦" if rule.archived else ("✅" if rule.enabled else "⏸")
        builder.row(
            InlineKeyboardButton(
                text=f"{mark} {task_title(rule)}",
                callback_data=f"rule:open:{rule.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="➕ Создать правило", callback_data="rule:create"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main"))
    return builder.as_markup()


def rule_menu(rule: Rule) -> InlineKeyboardMarkup:
    from app.telegram_client.jobs import COLLECTING_KINDS, KIND_LABELS, ONE_SHOT_KINDS

    kind = rule.kind or "forward"
    builder = InlineKeyboardBuilder()

    if rule.archived:
        builder.row(
            InlineKeyboardButton(text="↩️ Из архива", callback_data=f"rule:archive:{rule.id}")
        )
        builder.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"rule:delete:{rule.id}"))
        builder.row(InlineKeyboardButton(text="◀️ К правилам", callback_data="menu:rules"))
        return builder.as_markup()

    toggle = "⏸ Остановить" if rule.enabled else "▶️ Запустить"
    builder.row(
        InlineKeyboardButton(text=toggle, callback_data=f"rule:toggle:{rule.id}"),
        InlineKeyboardButton(text="⚙️ Настройки", callback_data=f"rule:settings:{rule.id}"),
    )
    # Режим «копия/форвард» существует только у обычной пересылки
    if kind == "forward":
        builder.row(
            InlineKeyboardButton(
                text="🔄 Режим: " + ("копия" if rule.mode == "copy" else "форвард"),
                callback_data=f"rule:mode:{rule.id}",
            )
        )
    elif kind in ONE_SHOT_KINDS:
        builder.row(
            InlineKeyboardButton(text="▶️ Запустить сейчас", callback_data=f"rule:run:{rule.id}")
        )
    # Ловец чеков собирает находки сам, по сообщениям — кнопка запуска ему не нужна
    if kind in COLLECTING_KINDS:
        builder.row(
            InlineKeyboardButton(text="📄 Результаты", callback_data=f"rule:results:{rule.id}")
        )
    builder.row(
        InlineKeyboardButton(
            text=f"📦 В архив ({KIND_LABELS.get(kind, kind)})",
            callback_data=f"rule:archive:{rule.id}",
        )
    )
    builder.row(InlineKeyboardButton(text="🗑 Удалить", callback_data=f"rule:delete:{rule.id}"))
    builder.row(InlineKeyboardButton(text="◀️ К правилам", callback_data="menu:rules"))
    return builder.as_markup()


def settings_menu(rule_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="⏱ Задержка", callback_data=f"set:delay:{rule_id}"),
        InlineKeyboardButton(text="🚫 Стоп-слова", callback_data=f"set:blacklist:{rule_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="✅ Только со словами", callback_data=f"set:whitelist:{rule_id}"),
        InlineKeyboardButton(text="🎛 Типы медиа", callback_data=f"set:media:{rule_id}"),
    )
    builder.row(
        InlineKeyboardButton(text="✂️ Чистка текста", callback_data=f"set:clean:{rule_id}"),
        InlineKeyboardButton(text="🔁 Замены", callback_data=f"set:replace:{rule_id}"),
    )
    builder.row(InlineKeyboardButton(text="◀️ К правилу", callback_data=f"rule:open:{rule_id}"))
    return builder.as_markup()


def media_types_menu(rule_id: int, selected: Sequence[str]) -> InlineKeyboardMarkup:
    from app.telegram_client.filters import MEDIA_KINDS

    labels = {
        "text": "Текст",
        "photo": "Фото",
        "video": "Видео",
        "document": "Файлы",
        "audio": "Аудио",
        "voice": "Голос",
        "sticker": "Стикеры",
        "animation": "Гифки",
    }
    builder = InlineKeyboardBuilder()
    for kind in MEDIA_KINDS:
        mark = "✅" if kind in selected else "⬜️"
        builder.button(
            text=f"{mark} {labels.get(kind, kind)}",
            callback_data=f"media:{rule_id}:{kind}",
        )
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="Готово ◀️", callback_data=f"rule:settings:{rule_id}"))
    return builder.as_markup()


def clean_menu(rule_id: int, filters: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text=("✅" if filters.get("remove_links") else "⬜️") + " Удалять ссылки",
            callback_data=f"clean:links:{rule_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=("✅" if filters.get("remove_mentions") else "⬜️") + " Удалять @упоминания",
            callback_data=f"clean:mentions:{rule_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=("✅" if filters.get("skip_forwards") else "⬜️") + " Пропускать репосты",
            callback_data=f"clean:forwards:{rule_id}",
        )
    )
    builder.row(
        InlineKeyboardButton(text="➕ Текст в конце", callback_data=f"clean:append:{rule_id}")
    )
    builder.row(InlineKeyboardButton(text="Готово ◀️", callback_data=f"rule:settings:{rule_id}"))
    return builder.as_markup()


def payment_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="pay:stars"))
    builder.row(InlineKeyboardButton(text="💳 Карта / СБП", callback_data="pay:yookassa"))
    builder.row(InlineKeyboardButton(text="🪙 USDT (TRC-20)", callback_data="pay:usdt"))
    builder.row(InlineKeyboardButton(text="👤 Через администратора", callback_data="pay:manual"))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main"))
    return builder.as_markup()


def bank_menu(banked_days: int, active_days: int) -> InlineKeyboardMarkup:
    """Клавиатура копилки: заморозить дни или вернуть их в подписку."""
    builder = InlineKeyboardBuilder()
    if active_days > 1:
        builder.row(
            InlineKeyboardButton(text="❄️ Заморозить 7 дней", callback_data="bank:freeze:7")
        )
    if banked_days > 0:
        builder.row(
            InlineKeyboardButton(text="↩️ Вернуть 7 дней", callback_data="bank:give:7"),
            InlineKeyboardButton(
                text=f"↩️ Вернуть все {banked_days}", callback_data="bank:give:0"
            ),
        )
    builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main"))
    return builder.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="nav:cancel"))
    return builder.as_markup()


def back_to_main() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main"))
    return builder.as_markup()


def admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
        InlineKeyboardButton(text="🔄 Перезапустить аккаунты", callback_data="admin:restart"),
    )
    builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main"))
    return builder.as_markup()
