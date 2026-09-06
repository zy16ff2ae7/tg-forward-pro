"""Непосредственно пересылка: фильтры → отправка без метки «Переслано от»."""
from __future__ import annotations

import os
import tempfile
from typing import Any

from loguru import logger

from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.telegram_client.filters import (
    FilterConfig,
    media_kind,
    message_text,
    should_forward,
    transform_text,
)
from app.telegram_client.types import (
    SENT,
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


async def subscription_active(user_id: int) -> bool:
    async with SessionLocal() as session:
        return await repo.has_active_subscription(session, user_id)


async def send_copy(
    client: Any, target_id: int, message: Any, text: str, link_preview: bool = True
) -> Any:
    """Публикует сообщение как своё — без метки «Переслано от».

    ``link_preview=False`` убирает блок предпросмотра у ссылок в тексте.
    Относится только к текстовым сообщениям: у медиа предпросмотра нет, зато у
    ``send_file`` такого аргумента и вовсе может не быть.
    """
    media = getattr(message, "media", None)
    if media is None:
        return await client.send_message(
            target_id, text or "", parse_mode=None, link_preview=link_preview
        )

    size = getattr(media, "size", None)
    if size is None:
        document = getattr(media, "document", None)
        size = getattr(document, "size", None) if document else None

    if size and size > MAX_MEDIA_BYTES:
        logger.info("Медиа слишком большое ({} байт) — отправляем форвардом", size)
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
            return await client.forward_messages(target_id, message)

        # Ошибки отправки НЕ ловим: повторы — дело очереди доставки.
        return await client.send_file(
            target_id,
            tmp_path,
            caption=text or None,
            file_name=file_name,
            parse_mode=None,
            supports_streaming=True,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _send_once(client: Any, rule: RuleSnapshot, message: Any, text: str) -> Any:
    if rule.mode == "forward":
        return await client.forward_messages(rule.target_id, message)
    return await send_copy(client, rule.target_id, message, text)


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
    if rule.kind != "forward":
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

    text = transform_text(raw_text, filters)

    # Задержку из правила отрабатывает очередь — до постановки в работу.
    sent = await _send_once(client, rule, message, text)

    target_msg_id = getattr(sent, "id", None)
    async with session_scope() as session:
        await repo.bump_forwarded(session, rule.id)
        await repo.log_forward(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            source_msg_id=int(getattr(message, "id", 0) or 0),
            target_msg_id=int(target_msg_id) if target_msg_id else None,
            status="ok",
        )
    logger.debug(
        "Правило #{}: переслано {} ({})", rule.id, target_msg_id, media_kind(message)
    )
    return SENT


async def log_delivery_error(
    client: Any, message: Any, rule: RuleSnapshot, error: BaseException
) -> None:
    """Пишет в журнал окончательную ошибку доставки (все повторы исчерпаны)."""
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
