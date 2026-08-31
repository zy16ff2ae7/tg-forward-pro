"""Непосредственно пересылка: фильтры → задержка → отправка без метки «Переслано от»."""
from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from telethon.errors import FloodWaitError, RPCError

from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.telegram_client.filters import (
    FilterConfig,
    media_kind,
    message_text,
    should_forward,
    transform_text,
)
from app.telegram_client.types import RuleSnapshot

# Если медиа весит больше — не качаем, а делаем обычный форвард
MAX_MEDIA_BYTES = 50 * 1024 * 1024


async def subscription_active(user_id: int) -> bool:
    async with SessionLocal() as session:
        return await repo.has_active_subscription(session, user_id)


async def send_copy(client: Any, target_id: int, message: Any, text: str) -> Any:
    """Публикует сообщение как своё — без метки «Переслано от»."""
    media = getattr(message, "media", None)
    if media is None:
        return await client.send_message(target_id, text or "", parse_mode=None)

    size = getattr(media, "size", None)
    if size is None:
        document = getattr(media, "document", None)
        size = getattr(document, "size", None) if document else None

    if size and size > MAX_MEDIA_BYTES:
        logger.info("Медиа слишком большое ({} байт) — отправляем форвардом", size)
        return await client.forward_messages(target_id, message)

    try:
        payload = await client.download_media(message, file=bytes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не скачали медиа ({}), уходим в форвард: {}", type(exc).__name__, exc)
        return await client.forward_messages(target_id, message)

    file_name = None
    document = getattr(media, "document", None)
    if document is not None:
        for attr in getattr(document, "attributes", []) or []:
            name = getattr(attr, "file_name", None)
            if name:
                file_name = name
                break

    return await client.send_file(
        target_id,
        payload,
        caption=text or None,
        file_name=file_name,
        parse_mode=None,
        supports_streaming=True,
    )


async def _send_once(client: Any, rule: RuleSnapshot, message: Any, text: str) -> Any:
    if rule.mode == "forward":
        return await client.forward_messages(rule.target_id, message)
    return await send_copy(client, rule.target_id, message, text)


async def deliver(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Полный цикл обработки одного сообщения по одному правилу.

    Задачи, отличные от пересылки (см. ``app.telegram_client.jobs``), уходят
    туда, а сюда попадает только классическая пара «источник → приёмник» —
    чтобы её поведение осталось прежним.
    """
    if rule.kind != "forward":
        from app.telegram_client.jobs import run_job

        await run_job(client, message, rule)
        return

    # Служебные сообщения (вступления, смена аватара) не пересылаем
    if getattr(message, "action", None) is not None:
        return

    raw_text = message_text(message)
    if not raw_text and getattr(message, "media", None) is None:
        return

    filters: FilterConfig = rule.filters
    try:
        if not should_forward(message, filters):
            return
    except Exception as exc:  # noqa: BLE001 — фильтр не должен ронять пересылку
        logger.warning("Ошибка фильтра в правиле #{}: {}", rule.id, exc)
        return

    if not await subscription_active(rule.user_id):
        logger.debug("Правило #{}: у пользователя нет активной подписки", rule.id)
        return

    text = transform_text(raw_text, filters)

    if rule.delay_seconds > 0:
        await asyncio.sleep(rule.delay_seconds)

    try:
        sent = await _send_once(client, rule, message, text)
    except FloodWaitError as exc:
        wait = int(getattr(exc, "seconds", 5)) + 1
        logger.warning("FloodWait {} сек по правилу #{} — ждём", wait, rule.id)
        await asyncio.sleep(wait)
        try:
            sent = await _send_once(client, rule, message, text)
        except RPCError as retry_exc:
            await _log_error(rule, message, f"FloodWait повторно: {retry_exc}")
            return
    except RPCError as exc:
        await _log_error(rule, message, f"{type(exc).__name__}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        await _log_error(rule, message, f"{type(exc).__name__}: {exc}")
        return

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


async def _log_error(rule: RuleSnapshot, message: Any, error: str) -> None:
    logger.error("Правило #{}: не удалось переслать — {}", rule.id, error)
    async with session_scope() as session:
        await repo.log_forward(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            source_msg_id=int(getattr(message, "id", 0) or 0),
            target_msg_id=None,
            status="error",
            error=error[:1000],
        )
