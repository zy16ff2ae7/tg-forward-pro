"""Клавиатуры бота."""
from __future__ import annotations

from typing import Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app import bonus, paylink, referral
from app.config import settings
from app.db.models import Rule, TelegramAccount
from app.plans import PERIODS, stars_amount


def main_menu(
    is_admin: bool = False,
    *,
    rules_count: int | None = None,
    accounts_online: int | None = None,
    accounts_total: int | None = None,
    sub_active: bool | None = None,
) -> InlineKeyboardMarkup:
    """Главное меню. Счётчики подставляются, когда известны, — так меню сразу
    показывает состояние: сколько правил, сколько аккаунтов в сети, есть ли
    абонемент."""
    builder = InlineKeyboardBuilder()

    # Кнопка мини-аппа — только если известен публичный HTTPS-адрес
    mini_url = settings.mini_app_url
    if mini_url:
        builder.row(
            InlineKeyboardButton(
                text="🖥 Открыть кабинет", web_app=WebAppInfo(url=mini_url)
            )
        )

    rules_label = "📡 Мои правила"
    if rules_count is not None:
        rules_label += f" ({rules_count})"
    accounts_label = "👤 Аккаунты"
    if accounts_total:
        accounts_label += f" ({accounts_online or 0}/{accounts_total})"
    sub_label = "💳 Подписка"
    if sub_active is True:
        sub_label += " ✅"
    elif sub_active is False:
        sub_label += " ❌"

    builder.row(
        InlineKeyboardButton(text=rules_label, callback_data="menu:rules"),
        InlineKeyboardButton(text=accounts_label, callback_data="menu:accounts"),
    )
    builder.row(
        InlineKeyboardButton(text=sub_label, callback_data="menu:sub"),
        InlineKeyboardButton(text="❓ Помощь", callback_data="menu:help"),
    )
    if is_admin:
        builder.row(
            InlineKeyboardButton(text="🛠 Панель владельца", callback_data="menu:admin")
        )
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


def rule_menu(rule: Rule, *, collected: int = 0) -> InlineKeyboardMarkup:
    """Меню задачи. ``collected`` — сколько она уже насобирала.

    Число нужно ровно для одной кнопки: «⬇️ Файлом» показываем только когда файл
    получится непустым. Кнопка, которая честно отвечает «выгружать нечего», в
    меню не нужна — её место занимает сам список.
    """
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
        results = [
            InlineKeyboardButton(text="📄 Результаты", callback_data=f"rule:results:{rule.id}")
        ]
        if collected > 0:
            results.append(
                InlineKeyboardButton(
                    text="⬇️ Файлом", callback_data=f"rule:export:{rule.id}"
                )
            )
        builder.row(*results)
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


def payment_menu(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Способы оплаты: показываем только те, что реально работают.

    Раньше кнопки карты и USDT рисовались всегда, а при нажатии пользователь
    получал «временно недоступно». Теперь ненастроенные способы либо скрыты,
    либо помечены как «скоро» — меню не обещает того, чего сервис не умеет.

    Внутри Telegram остаются звёзды и заявка администратору. Карта и USDT в
    режиме ``PAY_MODE=external`` уходят на обычную веб-страницу: кнопка-ссылка
    открывает её во внешнем браузере. Ссылка подписана и живёт час, поэтому
    её приходится собирать под конкретного пользователя — отсюда ``user_id``.
    """
    builder = InlineKeyboardBuilder()
    labels = {
        "stars": "⭐ Telegram Stars",
        "yookassa": "💳 Карта / СБП",
        "usdt": "🪙 USDT (TRC-20)",
        "manual": "👤 Через администратора",
    }
    # Бесплатные дни — первой строкой: это самый дешёвый для человека способ
    # получить абонемент, и прятать его под кнопками оплаты нечестно.
    if bonus.enabled():
        builder.row(
            InlineKeyboardButton(text="🎁 " + bonus.offer(), callback_data="bonus:open")
        )
    # Друг по ссылке — тоже бесплатные дни, поэтому рядом с подарком.
    if referral.enabled():
        builder.row(
            InlineKeyboardButton(
                text="👥 Пригласить друга — обоим выгода",
                callback_data="ref:open",
            )
        )
    # Промокод — тоже бесплатные дни, поэтому в одном ряду с подарком.
    builder.row(
        InlineKeyboardButton(text="🎟 Ввести промокод", callback_data="promo:open")
    )
    # Подарок другу — звёздами, как себе: счёт тот же, получатель другой.
    if "stars" in settings.inline_payment_methods():
        builder.row(
            InlineKeyboardButton(text="🎁 Подарить абонемент", callback_data="pay:gift")
        )
    inline_methods = settings.inline_payment_methods()
    for method in inline_methods:
        builder.row(
            InlineKeyboardButton(text=labels[method], callback_data=f"pay:{method}")
        )

    external = settings.external_payment_methods()
    link = paylink.pay_url(user_id) if (external and user_id) else None
    if link:
        names = " / ".join(labels[method].split(" ", 1)[-1] for method in external)
        builder.row(InlineKeyboardButton(text=f"🌐 {names} — на сайте", url=link))

    for method in ("yookassa", "usdt"):
        if not settings.method_available(method):
            builder.row(
                InlineKeyboardButton(
                    text=labels[method] + " — скоро",
                    callback_data=f"pay:soon:{method}",
                )
            )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="menu:main"))
    return builder.as_markup()


def bonus_menu(claimed: bool = False) -> InlineKeyboardMarkup:
    """Подарок за подписку: открыть канал и проверить подписку.

    Кнопки «Проверить» после выдачи нет: подарок разовый, и повторное нажатие
    может ответить только «уже получено» — такую кнопку лучше не рисовать.
    """
    builder = InlineKeyboardBuilder()
    url = settings.bonus_url
    if url:
        builder.row(InlineKeyboardButton(text="📣 Открыть канал", url=url))
    if not claimed:
        builder.row(
            InlineKeyboardButton(
                text="🔄 Проверить подписку", callback_data="bonus:check"
            )
        )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="menu:sub"))
    return builder.as_markup()


def referral_menu(link: str) -> InlineKeyboardMarkup:
    """Реферальный экран: поделиться ссылкой и назад к абонементу."""
    from urllib.parse import quote

    builder = InlineKeyboardBuilder()
    if link:
        share = "https://t.me/share/url?url=" + quote(link, safe="") + "&text=" + quote(
            "ДОЧА — автоматизации Telegram 24/7. Приходи по моей ссылке — нам обоим дадут дни!",
            safe="",
        )
        builder.row(InlineKeyboardButton(text="📤 Поделиться", url=share))
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="menu:sub"))
    return builder.as_markup()


def stars_periods(
    *, prefix: str = "pay:stars", with_autorenew: bool = True
) -> InlineKeyboardMarkup:
    """Выбор срока оплаты звёздами: на кнопке сразу итоговая сумма.

    Сроки берём из каталога, а цену считаем на месте — если тариф поменяли,
    кнопки не расходятся с тем, что реально уедет в инвойс. Подарок пользуется
    той же клавиатурой с другим префиксом и без автопродления: дарить чужому
    человеку списание каждый месяц нельзя.
    """
    builder = InlineKeyboardBuilder()
    for months in PERIODS:
        builder.row(
            InlineKeyboardButton(
                text=f"{months} мес. — {stars_amount(months)} ⭐",
                callback_data=f"{prefix}:{months}",
            )
        )
    if with_autorenew:
        # Автопродление — помесячно: период подписки в звёздах всегда 30 дней.
        builder.row(
            InlineKeyboardButton(
                text=f"🔁 Автопродление — {stars_amount(1)} ⭐/мес",
                callback_data="pay:stars:auto",
            )
        )
    builder.row(InlineKeyboardButton(text="◀️ Назад", callback_data="menu:sub"))
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


def login_code_kb() -> InlineKeyboardMarkup:
    """Шаг кода: повтор другим способом доставки + отмена.

    Повтор — это auth.ResendCode (приложение → SMS → звонок), а не новый вход:
    код из прошлого сообщения после него мёртв, вводить надо новый.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📩 Код не пришёл — прислать ещё раз", callback_data="acc:resend")
    )
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="nav:cancel"))
    return builder.as_markup()


def back_to_main() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main"))
    return builder.as_markup()


def relogin_notice() -> InlineKeyboardMarkup:
    """Кнопки под сообщением «аккаунт выпал»: вход сразу, а не поиск по меню.

    ``acc:add`` ведёт на шаг номера и сам подхватывает незавершённый вход, если
    человек его уже начал.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🔑 Подключить заново", callback_data="acc:add")
    )
    builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main"))
    return builder.as_markup()


def admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
        InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users"),
    )
    builder.row(
        InlineKeyboardButton(text="💳 Выдать абонемент", callback_data="admin:grant"),
        InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast"),
    )
    builder.row(
        InlineKeyboardButton(text="🔄 Перезапустить аккаунты", callback_data="admin:restart"),
    )
    builder.row(InlineKeyboardButton(text="◀️ В меню", callback_data="menu:main"))
    return builder.as_markup()


def grant_months_kb() -> InlineKeyboardMarkup:
    """Срок абонемента — те же периоды, что в тарифах."""
    from app.plans import PERIODS

    builder = InlineKeyboardBuilder()
    builder.row(
        *[
            InlineKeyboardButton(
                text=f"{months} мес.", callback_data=f"admin:grant:{months}"
            )
            for months in PERIODS
        ]
    )
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="nav:cancel"))
    return builder.as_markup()


def forget_confirm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🗑 Да, удалить всё", callback_data="forget:yes"),
        InlineKeyboardButton(text="◀️ Оставить", callback_data="forget:no"),
    )
    return builder.as_markup()


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Отправить всем", callback_data="admin:bcast:send"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="admin:bcast:cancel"),
    )
    return builder.as_markup()
