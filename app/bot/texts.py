"""Тексты сообщений бота."""
from __future__ import annotations

from datetime import datetime
from html import escape

from app.config import settings
from app.timeutil import time_ago, tz_suffix, utcnow


def welcome(name: str) -> str:
    from app import bonus

    status_line = (
        "\n\n⚙️ Вход аккаунтов по телефону сейчас на настройке. "
        "Кабинет, меню, подписки и платежи уже можно проверять."
        if not settings.public_login_enabled
        else ""
    )
    # Пробного периода «просто так» нет: бесплатные дни — только за подписку
    # на канал (см. /bonus). Строка живёт здесь, а не в тарифах, — это первое,
    # что человеку стоит знать, если он ещё не платил.
    bonus_line = (
        f"\n\n🎁 <b>Пробный период — {settings.bonus_days} дн. бесплатно</b> "
        f"за подписку на {bonus.channel()} — /bonus"
        if bonus.enabled()
        else ""
    )
    return (
        f"👑 {name}, привет! Это ДОЧА — папина дочка на связи.\n\n"
        "Я кручу ваши Telegram-автоматизации <b>24/7</b>: задачи работают, "
        "даже когда ваш компьютер выключен.\n\n"
        "<b>Что умею:</b>\n"
        "📤 <b>Постинг и рассылка</b> — ваш текст по чатам: по расписанию или по очереди;\n"
        "📋 <b>Копировать канал</b> — без метки «Переслано от»;\n"
        "📣 <b>Слать в несколько чатов</b> — один пост сразу во все;\n"
        "🔍 <b>Парсить аудиторию</b> — собираю участников чужого чата;\n"
        "➕ <b>Автоподписка</b> — вступаю в каналы из списка и по ссылкам;\n"
        "🎁 <b>Ловец чеков</b> — подарочные ссылки складываю в одно место;\n"
        "💬 <b>Уведомления из ЛС</b> — входящие диалоги в выбранный чат;\n"
        "❤️ <b>Байтинг</b> — реакции на сообщения выбранного человека;\n"
        "🔇 <b>Мут</b> — удаляю сообщения нарушителя, где вы админ.\n\n"
        "<b>В каждой задаче:</b> фильтры по словам и медиа, задержка, автозамены.\n"
        "Несколько аккаунтов — один абонемент на всё сразу.\n\n"
        "Кабинет уже ждёт — кнопка ниже 👇"
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


def subscription_status(
    active_until: datetime | None, rules_count: int, *, autorenew: bool = False
) -> str:
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
        + (
            "\n🔁 Автопродление Stars включено (отмена — в настройках Telegram)."
            if autorenew
            else ""
        )
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


def referral_card(link: str, code: str, days: int, invited: int, earned: int) -> str:
    """Экран реферальной программы: ссылка, условия и счёт."""
    if not days:
        return (
            "👥 <b>Пригласи друга</b>\n\n"
            "Программа сейчас выключена — загляните позже."
        )
    return (
        "👥 <b>Пригласи друга — обоим +дни</b>\n\n"
        f"Друг приходит по вашей ссылке — вы оба получаете "
        f"<b>+{days} дн.</b> к абонементу. Приглашений без лимита.\n\n"
        f"🔗 Ваша ссылка:\n<code>{link or code}</code>\n\n"
        f"Пришло друзей: <b>{invited}</b>. Заработано дней: <b>{earned}</b>."
        + ("" if link else "\n\nСсылка соберётся, когда владелец укажет юзернейм бота.")
    )


def rule_card(
    rule,
    *,
    collected: int = 0,
    health: dict | None = None,
    online: bool = True,
    subscription_active: bool = True,
) -> str:
    """Карточка задачи. Состав строк зависит от типа задачи.

    ``collected`` — сколько задача уже нашла (строки в ``collected_items``).
    Число приходит снаружи: у парсера счётчик отправок ``forwarded_count``
    остаётся нулём навсегда — он считает отправленные сообщения, а парсер
    ничего не отправляет, и карточка годами говорила «сработало раз: 0» после
    собранных тысяч.

    ``health`` (запись из ``repo.task_health``), ``online`` и
    ``subscription_active`` отвечают на главный вопрос: работает ли задача
    **прямо сейчас**. Без них «Состояние: работает ✅» стояло и у задачи, которая
    сутки падает с ошибкой, и у задачи с отключённым аккаунтом, и у задачи без
    абонемента: причину было видно только в кабинете. Значения по умолчанию —
    «всё хорошо», чтобы карточку можно было собрать и без походов в базу.
    """
    from app.task_health import chat_names, error_text
    from app.telegram_client.filters import FilterConfig
    from app.telegram_client.jobs import (
        COLLECTING_KINDS,
        KIND_LABELS,
        ONE_SHOT_KINDS,
        chat_recipients,
        task_title,
    )

    kind = rule.kind or "forward"
    filters = rule.filters or {}
    health = health or {}
    conf = FilterConfig.from_dict(filters)
    # Разовые задачи запускает кнопка, а её абонемент не сторожит (``run_oneshot``
    # проверки не делает) — писать им «нет абонемента» было бы неправдой.
    one_shot = kind in ONE_SHOT_KINDS
    # Тот же порядок причин, что у значка задачи в кабинете (``taskBadge``):
    # сначала то, что человек выключил сам, потом то, что сломалось. Кончившийся
    # абонемент важнее связи с аккаунтом: работа выключена целиком, и связь тут
    # уже ничего не меняет.
    if rule.archived:
        state = "в архиве 📦"
    elif not rule.enabled:
        state = "на паузе ⏸"
    elif not subscription_active and not one_shot:
        state = "нет абонемента ⛔"
    elif not online:
        state = "нет связи 🔌"
    elif health.get("failing"):
        state = "сбой ⚠️"
    elif one_shot:
        state = "по кнопке 🖐"
    else:
        state = "работает ✅"
    # Сказало ли состояние, что задачу запускает кнопка: если да, отдельная
    # строка «Запуск: по кнопке» ниже была бы дубляжом.
    state_says_button = state.startswith("по кнопке")

    lines = [
        f"📡 <b>Задача #{rule.id}</b> · {KIND_LABELS.get(kind, kind)}",
        "",
        f"<b>{task_title(rule)}</b>",
        f"Состояние: {state}",
    ]
    # Из значка не видно, что делать, — поэтому под ним строка с причиной. Архив
    # и пауза стоят по своей причине: там объяснять нечего.
    if not rule.archived and rule.enabled:
        if not subscription_active and not one_shot:
            lines.append(
                "⛔ Абонемент закончился — задача стоит. Продлите его в «💳 Подписка», "
                "и она пойдёт сама: настройки на месте."
            )
        elif not online:
            lines.append(
                "🔌 Аккаунт не в сети — задача ждёт связи. Перезапустите его "
                "в «👤 Аккаунты»."
            )
    # Причина сбоя — сразу под состоянием: без неё «сбой ⚠️» ничего не
    # объясняет, а в журнал службы человек не полезет. Починенный сбой тоже
    # называем: «в три чата не ушло» надо знать, даже когда остальные сто
    # получили, — но словами поспокойнее.
    reason = error_text(health.get("error"), chat_names(rule))
    if reason:
        when = time_ago(health.get("error_at"))
        head = "⚠️" if health.get("failing") else "Прошлый сбой:"
        lines.append(f"{head} {escape(reason, quote=False)}{f' · {when}' if when else ''}")

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
        pruned = int(filters.get("chats_pruned") or 0)
        if pruned:
            lines.append(f"🧹 Мёртвых чатов вычищено: {pruned}")
    if kind == "baiting":
        watched = int(filters.get("target_user_id") or 0)
        lines.append(f"Следим за: {watched or 'всеми подряд'}")
        lines.append(f"Реакция: {filters.get('reaction') or '👍'}")
    elif kind == "mute":
        watched = int(filters.get("target_user_id") or 0)
        if watched:
            lines.append(f"Следим за: {watched}")
        words = [str(w).strip() for w in (filters.get("banned_words") or [])]
        words = [w for w in words if w]
        if words:
            shown = ", ".join(words[:5]) + ("…" if len(words) > 5 else "")
            lines.append(f"🚫 Слова под запретом: {escape(shown, quote=False)}")
        if filters.get("block_links"):
            lines.append("🔗 Ссылки: удаляются")
        max_warns = max(0, int(conf.max_warns or 0))
        if max_warns:
            lines.append(
                f"🔇 Мут: после {max_warns}-го нарушения "
                f"на {max(1, int(conf.mute_hours or 1))} ч"
            )
        else:
            lines.append("🔇 Мут выключен — только удаляем")
    if kind == "parser":
        lines.append(f"Лимит за запуск: {filters.get('limit') or 200}")
    if kind == "autosubscribe":
        channels = filters.get("subscribe_to") or []
        if channels:
            lines.append(f"Каналов в списке: {len(channels)}")

    keywords = filters.get("keywords") or []
    if keywords:
        lines.append(f"Ключевые слова: {', '.join(str(word) for word in keywords)}")

    # Чем задача живёт: расписанием, кнопкой или входящими сообщениями. Раньше
    # тут у всех стояла «Задержка: N сек», хотя постинг, рассылку и парсер она не
    # касается вовсе (её отрабатывает только путь входящего сообщения), а
    # настоящее расписание — интервал, окно, паузу и круги — в боте было не
    # видно: за ним приходилось идти в кабинет.
    if kind == "poster":
        lines.append(f"Раз в {max(1, int(conf.interval_seconds) // 60)} мин")
        window = f"{conf.window_start}–{conf.window_end}"
        clock = "по часам сервера" if conf.window_tz is None else tz_suffix(conf.window_tz)
        lines.append(f"Окно: {window} {clock}")
        slots = [s for s in (conf.scheduled_posts or []) if isinstance(s, dict)]
        if slots:
            sent = sum(1 for s in slots if s.get("sent"))
            full = round(sent / len(slots) * 8)
            bar = "▓" * full + "░" * (8 - full)
            lines.append(f"Расписание: {bar} {sent}/{len(slots)}")
    elif kind == "mailing":
        lines.append(f"Пауза между чатами: {conf.gap_seconds} сек")
        lines.append(f"Кругов: {conf.repeats}" if conf.repeats else "Кругов: без конца")
    elif kind == "parser":
        # «Запуск: по кнопке» повторяло бы состояние — строка нужна только там,
        # где состояние занято другой причиной (пауза, сбой, нет связи, архив).
        if not state_says_button:
            lines.append("Запуск: по кнопке")
    elif kind == "autosubscribe":
        # Автоподписка живёт двумя путями сразу: кнопкой по списку каналов и по
        # ссылкам, которые находит в источнике. Второй путь и есть тот случай,
        # когда задержка работает, — о ней говорим только там.
        if rule.source_id:
            lines.append("Запуск: по кнопке и по ссылкам из источника")
            if rule.delay_seconds:
                lines.append(f"Задержка: {rule.delay_seconds} сек")
        elif not state_says_button:
            lines.append("Запуск: по кнопке")
    else:
        lines.append(f"Задержка: {rule.delay_seconds} сек")

    # Сделанное. У собирающих задач это найденные записи (их же показывает
    # кнопка «Результаты»), у автоподписки — вступления, у остальных — отправки.
    # Рядом — когда задача сработала последний раз: по одному счётчику не
    # понять, идёт работа прямо сейчас или встала неделю назад.
    done = collected if kind in COLLECTING_KINDS else int(rule.forwarded_count or 0)
    ago = time_ago(health.get("ok_at"))
    if ago:
        tail = f" · {ago}"
    elif not done:
        # Журнал пуст и счётчик нулевой — задача действительно ещё не работала.
        # При непустом счётчике молчим: старые задачи журнала не знают, и
        # «ещё ни разу» поверх тысячи отправок было бы ложью.
        tail = " · ещё ни разу"
    else:
        tail = ""
    if kind in COLLECTING_KINDS:
        lines.append(f"{'Поймано' if kind == 'checks' else 'Собрано'}: {collected}{tail}")
    elif kind in ONE_SHOT_KINDS:
        lines.append(f"Вступили в чаты: {rule.forwarded_count}{tail}")
    else:
        lines.append(f"Сработало раз: {rule.forwarded_count}{tail}")

    blacklist = filters.get("blacklist") or []
    whitelist = filters.get("whitelist") or []
    if blacklist:
        lines.append(f"Стоп-слова: {', '.join(blacklist)}")
    if whitelist:
        lines.append(f"Только со словами: {', '.join(whitelist)}")
    if filters.get("append_text"):
        lines.append(f"Текст в конце: {filters['append_text']}")
    return "\n".join(lines)
