"""Тексты сообщений бота."""
from __future__ import annotations

from datetime import datetime

from app.config import settings
from app.timeutil import utcnow


def welcome(name: str) -> str:
    from app import bonus

    status_line = (
        "\n\n⚙️ Вход аккаунтов по телефону сейчас на настройке. "
        "Кабинет, меню, подписки и платежи уже можно проверять."
        if not settings.public_login_enabled
        else ""
    )
    # Подарок за подписку — сразу в приветствии: это первое, что человеку
    # стоит знать, если он ещё не платил.
    bonus_line = (
        f"\n\n🎁 <b>{settings.bonus_days} дн. бесплатно</b> за подписку на "
        f"{bonus.channel()} — /bonus"
        if bonus.enabled()
        else ""
    )
    return (
        f"Привет, {name}! 👋\n\n"
        "Я — ДОЧА, папина дочка на связи: помощница автоматизаций в Telegram. "
        "Задачи работают 24/7, даже когда ваш компьютер выключен.\n\n"
        "<b>Что умею:</b>\n"
        "• <b>Копирование канала</b> — без метки «Переслано от»;\n"
        "• <b>Пересылка в несколько чатов</b> — один пост из источника сразу во все;\n"
        "• <b>Постинг по расписанию</b> — свой текст во все чаты каждые N минут в окне времени;\n"
        "• <b>Рассылка по очереди</b> — свой текст по чатам поштучно: чат, пауза, следующий;\n"
        "• <b>Парсер аудитории</b> — собираю участников чужого чата;\n"
        "• <b>Автоподписка</b> — вступаю в каналы из списка и по ссылкам;\n"
        "• <b>Ловец чеков</b> — ловлю подарочные ссылки и складываю в одно место;\n"
        "• <b>Уведомления из диалогов</b> — пересылаю входящие ЛС в выбранный чат;\n"
        "• <b>Байтинг</b> — ставлю реакцию на сообщения выбранного человека;\n"
        "• <b>Мут</b> — удаляю сообщения нарушителя, где вы администратор.\n\n"
        "<b>В каждой задаче:</b>\n"
        "• фильтры по словам и типам медиа, задержка, автозамены текста;\n"
        "• несколько аккаунтов, неограниченные правила, абонемент на месяц.\n\n"
        "Откройте кабинет кнопкой ниже или начните с раздела 👤 Аккаунты."
        f"{bonus_line}"
        f"{status_line}"
    )


HELP = (
    "❓ <b>Как пользоваться</b>\n\n"
    "1️⃣ <b>Подключите аккаунт.</b> «Аккаунты» → «Подключить аккаунт» → номер телефона "
    "в формате +79001234567 → код из Telegram → пароль 2FA, если включён. "
    "Если кнопка показывает «нужен MTProto-вход», кабинет уже можно смотреть, а вход аккаунтов "
    "откроется после заполнения API_ID/API_HASH в настройках сервиса.\n\n"
    "2️⃣ <b>Создайте задачу.</b> «Правила» → «Создать правило» → выберите аккаунт, "
    "тип задачи и заполните поля: источник, приёмник, получатели, слова-триггеры и т.д. "
    "Источник можно прислать как @username канала или ссылку t.me/..., "
    "либо выбрать из списка («Чаты аккаунта»).\n\n"
    "3️⃣ <b>Настройте.</b> В каждой задаче доступны: режим (копия/форвард), задержка, "
    "стоп-слова, белый список, типы медиа, чистка ссылок и @упоминаний, "
    "автозамена текста и подпись в конце поста.\n\n"
    "⚠️ <b>Важно:</b> аккаунт должен быть подписан на канал-источник, "
    "а в приёмнике — иметь право публиковать сообщения.\n\n"
    "💳 Задачи работают только при активном абонементе (раздел «Подписка»)."
)


def price_line() -> str:
    """Строка со стоимостью: только те способы, что реально принимают оплату.

    Незачем показывать цену в рублях, если карты не подключены, — это
    выглядит как обман и рождает вопросы в поддержку.
    """
    parts: list[str] = []
    if settings.stars_ready:
        parts.append(f"{settings.price_stars} ⭐")
    if settings.yookassa_ready:
        parts.append(f"{settings.price_rub} ₽")
    if settings.usdt_ready:
        parts.append(f"{settings.price_usdt:g} USDT")
    if not parts:
        return ""
    return "Стоимость: " + "  ·  ".join(parts)


def subscription_status(active_until: datetime | None, rules_count: int) -> str:
    if active_until is None:
        price = price_line()
        return (
            "💳 <b>Абонемент не активен</b>\n\n"
            "Сейчас пересылка остановлена. Оплатите месяц, и правила снова заработают."
            + (f"\n{price}" if price else "")
        )
    days = (active_until - utcnow()).days
    return (
        "💳 <b>Абонемент активен</b>\n\n"
        f"Действует до: <b>{active_until:%d.%m.%Y %H:%M}</b> (UTC)\n"
        f"Осталось дней: <b>{max(days, 0)}</b>\n"
        f"Правил у вас: {rules_count}"
    )


def bonus_card(claimed: bool) -> str:
    """Экран подарка за подписку на канал сервиса."""
    from app import bonus

    if not bonus.enabled():
        return (
            "🎁 <b>Подарок за подписку</b>\n\n"
            "Сейчас подарок не действует — канал не настроен."
        )
    channel = bonus.channel()
    if claimed:
        return (
            "🎁 <b>Подарок за подписку</b>\n\n"
            f"Дни за подписку на {channel} уже начислены. "
            "Подарок даётся один раз на аккаунт."
        )
    return (
        "🎁 <b>Подарок за подписку</b>\n\n"
        f"Подпишитесь на {channel} — и получите "
        f"<b>{settings.bonus_days} дн.</b> работы задач бесплатно.\n\n"
        "1️⃣ «Открыть канал» и подписаться\n"
        "2️⃣ «Проверить подписку» — дни начислятся сразу\n\n"
        "Подарок один на аккаунт. Дни складываются с текущим абонементом, "
        "так что ничего не сгорит."
    )


def rule_card(rule) -> str:
    """Карточка задачи. Состав строк зависит от типа задачи."""
    from app.telegram_client.jobs import KIND_LABELS, chat_recipients, task_title

    kind = rule.kind or "forward"
    filters = rule.filters or {}
    if rule.archived:
        state = "в архиве 📦"
    else:
        state = "работает ✅" if rule.enabled else "на паузе ⏸"

    lines = [
        f"📡 <b>Задача #{rule.id}</b> · {KIND_LABELS.get(kind, kind)}",
        "",
        f"<b>{task_title(rule)}</b>",
        f"Состояние: {state}",
    ]

    if kind in ("forward", "broadcast", "checks"):
        lines.append(f"Источник: <b>{rule.source_title or rule.source_id}</b>")
    if kind in ("forward", "checks"):
        lines.append(f"Приёмник: <b>{rule.target_title or rule.target_id}</b>")
    if kind == "forward":
        mode = "копия (без метки)" if rule.mode == "copy" else "обычный форвард"
        lines.append(f"Режим: {mode}")
    # Пересылка в чаты, постинг и рассылка ходят в любое число чатов: считаем их
    # одним счётом. Раньше пересылка писала «дополнительных получателей» и
    # теряла из счёта первый чат, а постинг с рассылкой не писали ничего.
    if kind in ("broadcast", "poster", "mailing"):
        chats = chat_recipients(rule)
        if len(chats) == 1 and (rule.target_title or rule.target_id):
            lines.append(f"Чат: <b>{rule.target_title or rule.target_id}</b>")
        else:
            lines.append(f"Чатов: <b>{len(chats)}</b>")
    if kind in ("baiting", "mute"):
        watched = int(filters.get("target_user_id") or 0)
        lines.append(f"Следим за: {watched or 'всеми подряд'}")
        if kind == "baiting":
            lines.append(f"Реакция: {filters.get('reaction') or '👍'}")
    if kind == "parser":
        lines.append(f"Лимит за запуск: {filters.get('limit') or 200}")
    if kind == "autosubscribe":
        channels = filters.get("subscribe_to") or []
        if channels:
            lines.append(f"Каналов в списке: {len(channels)}")

    keywords = filters.get("keywords") or []
    if keywords:
        lines.append(f"Ключевые слова: {', '.join(str(word) for word in keywords)}")

    lines.append(f"Задержка: {rule.delay_seconds} сек")
    lines.append(f"Сработало раз: {rule.forwarded_count}")

    blacklist = filters.get("blacklist") or []
    whitelist = filters.get("whitelist") or []
    if blacklist:
        lines.append(f"Стоп-слова: {', '.join(blacklist)}")
    if whitelist:
        lines.append(f"Только со словами: {', '.join(whitelist)}")
    if filters.get("append_text"):
        lines.append(f"Текст в конце: {filters['append_text']}")
    return "\n".join(lines)
