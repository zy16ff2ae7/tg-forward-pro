"""Непосредственно пересылка: фильтры → отправка без метки «Переслано от»."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger
from telethon import Button

from app.translate import maybe_translate

from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.telegram_client.filters import (
    FilterConfig,
    autodelete_hours,
    media_kind,
    message_text,
    should_forward,
    transform_text,
)
from app.telegram_client.types import (
    SENT,
    SKIP_DAILY_CAP,
    SKIP_EMPTY,
    SKIP_FILTER,
    SKIP_FILTER_ERROR,
    SKIP_JOB,
    SKIP_NO_SUBSCRIPTION,
    SKIP_SERVICE,
    DeliveryResult,
    RuleSnapshot,
    skipped,
)

# Если медиа весит больше — не качаем, а делаем обычный форвард
MAX_MEDIA_BYTES = 50 * 1024 * 1024


# Отправок в сутки на прогретый аккаунт. Лимит — предохранитель, а не цель:
# при нормальном темпе его не видно, а разогнавшуюся рассылку он останавливает
# раньше, чем аккаунт заметит Telegram.
DAILY_CAP_DEFAULT = 1000
# Прогрев новичка: (возраст аккаунта в днях включительно, лимит). Свежий
# аккаунт, выстреливший тысячей сообщений в первый день, живёт недолго.
WARMUP_CAPS: tuple[tuple[int, int], ...] = ((1, 50), (3, 150), (7, 400))


def send_cap_for(created_at, override: int = 0) -> int:
    """Дневной лимит отправок: свой из задачи или сервисный с прогревом."""
    override = max(0, int(override or 0))
    if override:
        return override
    if created_at is None:
        return DAILY_CAP_DEFAULT
    try:
        age_days = max(0, (datetime.now(timezone.utc) - created_at).days)
    except TypeError:
        # Наивная дата из БД — считаем её UTC.
        naive_now = datetime.now(timezone.utc).replace(tzinfo=None)
        age_days = max(0, (naive_now - created_at).days)
    for max_age, cap in WARMUP_CAPS:
        if age_days <= max_age:
            return cap
    return DAILY_CAP_DEFAULT


def tomorrow_ts() -> float:
    """Полночь UTC: когда дневной лимит обнулится."""
    now = datetime.now(timezone.utc)
    midnight = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return midnight.timestamp()


async def check_send_cap(rule) -> tuple[bool, int, int]:
    """Исчерпан ли дневной лимит аккаунта. Возвращает (пора_стоять, ушло, лимит)."""
    async with session_scope() as session:
        account = await repo.get_account(session, rule.account_id, rule.user_id)
        created = getattr(account, "created_at", None) if account is not None else None
        cap = send_cap_for(created, getattr(rule.filters, "daily_cap", 0))
        used = await repo.send_count_today(session, rule.account_id)
        return used >= cap, used, cap


# Кому уже сказали про лимит сегодня: (rule_id, дата). Письмо одно в сутки —
# дальше задача тихо стоит до полуночи, а не красит журнал каждой отправкой.
_cap_warned: set[tuple[int, str]] = set()
_cap_warned_day: str = ""


async def note_cap_hit(rule, used: int, cap: int, source_msg_id: int = 0) -> None:
    """Пишет в журнал, что задача встала по лимиту. Раз в сутки на задачу."""
    global _cap_warned_day
    today = datetime.now(timezone.utc).date().isoformat()
    if today != _cap_warned_day:
        _cap_warned.clear()
        _cap_warned_day = today
    key = (int(rule.id), today)
    if key in _cap_warned:
        return
    _cap_warned.add(key)
    async with session_scope() as session:
        await repo.log_forward(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            source_msg_id=int(source_msg_id or 0),
            target_msg_id=None,
            status="error",
            error=(
                f"Дневной лимит отправок исчерпан ({used}/{cap}). "
                "Продолжим после полуночи UTC."
            ),
        )


async def subscription_active(user_id: int) -> bool:
    async with SessionLocal() as session:
        return await repo.has_active_subscription(session, user_id)


async def send_copy(
    client: Any,
    target_id: int,
    message: Any,
    text: str,
    link_preview: bool = True,
    buttons: Any = None,
    topic_id: int = 0,
    entities: Any = None,
) -> Any:
    """Публикует сообщение как своё — без метки «Переслано от».

    ``link_preview=False`` убирает блок предпросмотра у ссылок в тексте.
    Относится только к текстовым сообщениям: у медиа предпросмотра нет, зато у
    ``send_file`` такого аргумента и вовсе может не быть.

    ``buttons`` — кнопки-ссылки из настроек ([{'text', 'url'}]): каждая своей
    строкой. Форвард кнопок не умеет, поэтому слишком большое медиа уходит без
    них — предупредили в форме, а не промолчали.

    ``topic_id`` — тема форума в приёмнике (0 — корень чата). Форвард тем
    не умеет на уровне Telegram API, поэтому огромное медиа и нескачанное
    уходят в корень обычным форвардом — с пометкой в логе, а не молча.
    """
    rows = [
        [Button.url(str(item.get("text") or ""), str(item.get("url") or ""))]
        for item in (buttons or [])
        if isinstance(item, dict) and item.get("text") and item.get("url")
    ] or None
    thread = int(topic_id or 0) or None
    media = getattr(message, "media", None)
    if media is None:
        return await client.send_message(
            target_id, text or "", parse_mode=None, link_preview=link_preview,
            buttons=rows, comment_to=thread, formatting_entities=entities,
        )

    size = getattr(media, "size", None)
    if size is None:
        document = getattr(media, "document", None)
        size = getattr(document, "size", None) if document else None

    if size and size > MAX_MEDIA_BYTES:
        logger.info("Медиа слишком большое ({} байт) — отправляем форвардом", size)
        if thread is not None:
            logger.warning(
                "Топик {}: огромное медиа уходит в корень — форвард тем не умеет",
                thread,
            )
        return await client.forward_messages(target_id, message)

    file_name = None
    document = getattr(media, "document", None)
    if document is not None:
        for attr in getattr(document, "attributes", []) or []:
            name = getattr(attr, "file_name", None)
            if name:
                file_name = name
                break

    # Качаем во временный файл, а не в память: при 8 параллельных отправках
    # file=bytes давал бы пик в сотни мегабайт RAM под флудом.
    fd, tmp_path = tempfile.mkstemp(prefix="tgfwd-")
    os.close(fd)
    try:
        try:
            await client.download_media(message, file=tmp_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не скачали медиа ({}), уходим в форвард: {}", type(exc).__name__, exc)
            if thread is not None:
                logger.warning(
                    "Топик {}: нескачанное медиа уходит в корень — форвард тем не умеет",
                    thread,
                )
            return await client.forward_messages(target_id, message)

        # Ошибки отправки НЕ ловим: повторы — дело очереди доставки.
        return await client.send_file(
            target_id,
            tmp_path,
            caption=text or None,
            file_name=file_name,
            parse_mode=None,
            supports_streaming=True,
            buttons=rows,
            comment_to=thread,
            formatting_entities=entities,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def pin_sent(
    client: Any, target_id: int, message_id: int, *, rule_id: int = 0
) -> bool:
    """Закрепляет отправленное сообщение. Закреп — украшение, а не доставка:
    не вышло (нет прав админа в приёмнике) — сообщение всё равно ушло."""
    try:
        await client.pin_message(target_id, message_id)
        return True
    except Exception as exc:  # noqa: BLE001 — закреп не роняет отправку
        logger.warning(
            "Правило #{}: не закрепили {} в {} ({}): {}",
            rule_id, message_id, target_id, type(exc).__name__, exc,
        )
        return False


async def _send_once(client: Any, rule: RuleSnapshot, message: Any, text: str) -> Any:
    topic = int(getattr(rule.filters, "topic_id", 0) or 0)
    if rule.mode == "forward":
        if topic:
            # Сюда проходят только задачи, созданные мимо кабинета: форма
            # топик в режиме «форвард» не даёт сохранить. Пост не теряем —
            # уходит в корень, а в логе видно, чья настройка кривая.
            logger.warning(
                "Правило #{}: топик {} работает только в режиме «копия» — шлём в корень",
                rule.id, topic,
            )
        return await client.forward_messages(rule.target_id, message)
    return await send_copy(
        client, rule.target_id, message, text,
        buttons=getattr(rule.filters, "buttons", None),
        topic_id=topic,
    )


async def deliver(client: Any, message: Any, rule: RuleSnapshot) -> DeliveryResult:
    """Обрабатывает одно сообщение по одному правилу и отправляет его.

    Возвращает ``DeliveryResult``: отправлено или пропущено и почему
    (служебное, не прошло фильтр, нет подписки). Для очереди доставки пропуск —
    не ошибка: повторять его не надо, но в счётчиках он виден отдельно, иначе
    «ничего не пересылается» невозможно отличить от «всё отфильтровано».

    Ошибки отправки **не перехватываются**: повторы, паузы при FloodWait и
    запись в журнал ошибок — дело очереди (``app.telegram_client.queue``).
    Разделение нужно, чтобы повтор не прогонял фильтры и проверку подписки
    заново, а пауза не занимала слот отправки.

    Задачи, отличные от пересылки (см. ``app.telegram_client.jobs``), уходят
    туда — у них свой порядок обработки и свои журналы.
    """
    # Клон — та же пересылка-копия, только с догрузкой истории по таймеру:
    # новые посты идут общим путём со всеми фильтрами и оформлением.
    if rule.kind not in ("forward", "clone"):
        from app.telegram_client.jobs import run_job

        await run_job(client, message, rule)
        return skipped(SKIP_JOB)

    # Служебные сообщения (вступления, смена аватара) не пересылаем
    if getattr(message, "action", None) is not None:
        return skipped(SKIP_SERVICE)

    raw_text = message_text(message)
    if not raw_text and getattr(message, "media", None) is None:
        return skipped(SKIP_EMPTY)

    filters: FilterConfig = rule.filters
    try:
        if not should_forward(message, filters):
            return skipped(SKIP_FILTER)
    except Exception as exc:  # noqa: BLE001 — фильтр не должен ронять пересылку
        logger.warning("Ошибка фильтра в правиле #{}: {}", rule.id, exc)
        return skipped(SKIP_FILTER_ERROR)

    if not await subscription_active(rule.user_id):
        logger.debug("Правило #{}: у пользователя нет активной подписки", rule.id)
        return skipped(SKIP_NO_SUBSCRIPTION)

    hit, used, cap = await check_send_cap(rule)
    if hit:
        await note_cap_hit(rule, used, cap, int(getattr(message, "id", 0) or 0))
        return skipped(SKIP_DAILY_CAP)

    if rule.mode != "forward" and filters.translate_to:
        # Переводим исходник, а не готовый текст: подпись и замены уже на
        # языке читателя, гонять их туда-обратно не надо. Фильтры при этом
        # смотрят исходник: они подбирают посты, а не их отображение.
        raw_text = await maybe_translate(raw_text, filters.translate_to)
    text = transform_text(raw_text, filters)

    # Задержку из правила отрабатывает очередь — до постановки в работу.
    sent = await _send_once(client, rule, message, text)

    target_msg_id = getattr(sent, "id", None)
    if filters.pin_on_send and target_msg_id:
        await pin_sent(client, rule.target_id, int(target_msg_id), rule_id=rule.id)
    async with session_scope() as session:
        await repo.bump_forwarded(session, rule.id)
        await repo.bump_send_count(session, rule.account_id)
        await repo.log_forward(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            source_msg_id=int(getattr(message, "id", 0) or 0),
            target_msg_id=int(target_msg_id) if target_msg_id else None,
            status="ok",
        )
        hours = autodelete_hours(filters)
        if hours > 0 and target_msg_id:
            from datetime import timedelta

            from app.timeutil import utcnow

            await repo.schedule_delete(
                session,
                rule_id=rule.id,
                user_id=rule.user_id,
                account_id=rule.account_id,
                chat_id=int(rule.target_id),
                msg_id=int(target_msg_id),
                delete_at=utcnow() + timedelta(hours=hours),
            )
    logger.debug(
        "Правило #{}: переслано {} ({})", rule.id, target_msg_id, media_kind(message)
    )
    return SENT


async def log_delivery_error(
    client: Any, message: Any, rule: RuleSnapshot, error: BaseException
) -> None:
    """Пишет в журнал окончательную ошибку доставки (все повторы исчерпаны)."""
    from app.telegram_client.antispam import dead_session_kind, is_peer_flood

    logger.error("Правило #{}: не удалось переслать — {}", rule.id, error)
    async with session_scope() as session:
        await repo.log_forward(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            source_msg_id=int(getattr(message, "id", 0) or 0),
            target_msg_id=None,
            status="error",
            error=f"{type(error).__name__}: {error}",
        )
    if is_peer_flood(error):
        # Спамблок встаёт на весь аккаунт, а не на одно правило — иначе соседние
        # задачи продолжат слать и продлят ограничение. Импорт отложенный:
        # менеджер импортирует этот модуль.
        from app.telegram_client.manager import manager

        await manager.note_peer_flood(rule.account_id, rule, f" ({rule.kind})")
    elif dead_session_kind(error):
        # Ключ мёртв или номера нет: ретраить нечего, аккаунт гасится с
        # причиной «подключите заново», а не долбится вечно.
        from app.telegram_client.manager import ACCOUNT_BANNED, SESSION_REVOKED, manager

        await manager.kill_dead_account(
            rule.account_id,
            ACCOUNT_BANNED
            if dead_session_kind(error) == "banned"
            else SESSION_REVOKED,
        )
    from app.task_alerts import maybe_alert_problem

    await maybe_alert_problem(rule, f"{type(error).__name__}: {error}")
