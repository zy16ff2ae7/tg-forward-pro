"""Задачи, отличные от обычной пересылки.

Тип задачи лежит в ``Rule.kind``. Всё, что не ``forward``, приходит сюда из
``forwarder.deliver`` — на том же контракте: подключённый Telethon-клиент,
сообщение и снимок правила. Классическая пересылка от этого не меняется.

Задачи делятся на два класса:

* **потоковые** — реагируют на каждое сообщение (broadcast, baiting, mute,
  dialogs, checks, autosubscribe);
* **разовые** — запускаются по команде пользователя и сразу отдают результат
  (parser, autosubscribe).
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Awaitable

from loguru import logger
from telethon.errors import FloodWaitError, RPCError

from app.db import repo
from app.db.database import SessionLocal
from app.telegram_client.filters import FilterConfig, message_text, transform_text
from app.telegram_client.forwarder import send_copy, subscription_active
from app.telegram_client.types import RuleSnapshot

# Слушают все чаты аккаунта, а не один источник: у ЛС нет фиксированного chat_id
FLOATING_KINDS: tuple[str, ...] = ("dialogs",)

# Запускаются вручную и сразу возвращают результат
ONE_SHOT_KINDS: tuple[str, ...] = ("parser", "autosubscribe")

# Складывают находки в collected_items — у них есть кнопка «Результаты»
COLLECTING_KINDS: tuple[str, ...] = ONE_SHOT_KINDS + ("checks",)

KIND_LABELS: dict[str, str] = {
    "forward": "пересылка",
    "broadcast": "рассылка",
    "baiting": "байтинг",
    "mute": "мут",
    "dialogs": "уведомления из диалогов",
    "checks": "ловец чеков",
    "parser": "парсер аудитории",
    "autosubscribe": "автоподписка",
}

# Ссылки на подарки и чеки, которые ищет «ловец чеков»
GIFT_LINK_RE = re.compile(
    r"(?:https?://)?(?:t|telegram)\.me/(?:giftcode|gifts|nft)/[A-Za-z0-9_\-]+",
    re.IGNORECASE,
)

# Любая ссылка на чат: t.me/username, t.me/+invite, t.me/joinchat/hash
CHAT_LINK_RE = re.compile(
    r"(?:https?://)?(?:t|telegram)\.me/([A-Za-z0-9_+\-/]{5,})",
    re.IGNORECASE,
)

# Служебные пути t.me, которые заведомо не являются чатом. Сравнивается только
# первая часть пути: t.me/addlist/xxx — папка, t.me/c/123/45 — ссылка на сообщение.
NOT_A_CHAT_LINK = frozenset(
    {
        "giftcode", "gifts", "nft",       # подарки и чеки — это не чаты
        "s", "share", "iv", "c", "m",     # расшаренные посты и сообщения
        "proxy", "socks",                 # прокси
        "addstickers", "addemoji", "addlist", "addtheme",  # наборы и папки
        "login", "confirmphone", "boost", # служебные переходы
    }
)

MAX_PARSER_LIMIT = 10_000


def task_title(rule: Any) -> str:
    """Заголовок задачи. У разных типов разная геометрия, а не только «A → B»."""
    kind = getattr(rule, "kind", None) or "forward"
    filters = getattr(rule, "filters", None)
    filters = filters if isinstance(filters, dict) else {}
    source = getattr(rule, "source_title", None) or str(getattr(rule, "source_id", "") or "")
    target = getattr(rule, "target_title", None) or str(getattr(rule, "target_id", "") or "")

    if kind == "parser":
        return f"Парсер аудитории: {source}"
    if kind == "autosubscribe":
        channels = len(filters.get("subscribe_to") or [])
        if channels:
            return f"Автоподписка: {channels} кан." + (f" из «{source}»" if source else "")
        return f"Автоподписка: {source or 'все чаты аккаунта'}"
    if kind == "dialogs":
        return f"Уведомления из ЛС → {target}"
    if kind == "checks":
        return f"Ловец чеков: {source} → {target}"
    if kind == "broadcast":
        extra = len(filters.get("targets") or [])
        return f"Рассылка: {source} → {target}" + (f" (+{extra})" if extra else "")
    if kind in ("baiting", "mute"):
        watched = int(filters.get("target_user_id") or 0)
        head = "Байтинг в" if kind == "baiting" else "Мут в"
        return f"{head} {source}" + (f" · за {watched}" if watched else "")
    return f"{source} → {target}"


# ─────────────────────────────── Разбор сообщений ─────────────────────────────


def _normalize_filters(rule: RuleSnapshot) -> None:
    """Приводит настройки правила к FilterConfig.

    Снимок из БД всегда несёт FilterConfig, но обработчики вызывают и вручную
    собранными правилами, где легко передать обычный словарь из JSON. Пусть
    здесь будет одна точка приведения, а не проверки в каждом обработчике.
    """
    if not isinstance(rule.filters, FilterConfig):
        rule.filters = FilterConfig.from_dict(rule.filters or {})


async def run_job(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Проводит одно сообщение через задачу, отличную от пересылки."""
    # Служебные сообщения (вступления, смена аватара) нечего reacting/удалять
    if getattr(message, "action", None) is not None:
        return

    _normalize_filters(rule)

    if not await subscription_active(rule.user_id):
        logger.debug("Задача #{}: у пользователя нет активной подписки", rule.id)
        return

    handler = _HANDLERS.get(rule.kind)
    if handler is None:
        await record_error(rule, message, f"Неизвестный тип задачи: {rule.kind}")
        return

    if rule.delay_seconds > 0:
        await asyncio.sleep(rule.delay_seconds)

    try:
        await handler(client, message, rule)
    except FloodWaitError as exc:
        wait = int(getattr(exc, "seconds", 5)) + 1
        logger.warning("Задача #{} ({}): FloodWait {} сек — ждём", rule.id, rule.kind, wait)
        await asyncio.sleep(wait)
        try:
            await handler(client, message, rule)
        except RPCError as retry_exc:
            await record_error(rule, message, f"FloodWait повторно: {retry_exc}")
    except RPCError as exc:
        await record_error(rule, message, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 — одна задача не должна ронять аккаунт
        await record_error(rule, message, f"{type(exc).__name__}: {exc}")


def _matches_keywords(config_keywords: list[str], text: str) -> bool:
    """Пустой список ключевых слов означает «подходит любое сообщение»."""
    words = [word.strip().lower() for word in (config_keywords or []) if word and word.strip()]
    if not words:
        return True
    lowered = (text or "").lower()
    return any(word in lowered for word in words)


def _sender_matches(rule: RuleSnapshot, message: Any) -> bool:
    """Проверяет, что сообщение от того человека, за которым следит задача."""
    watched = int(rule.filters.target_user_id or 0)
    if not watched:
        return True
    return int(getattr(message, "sender_id", 0) or 0) == watched


# ─────────────────────────────────── Задачи ──────────────────────────────────


def _broadcast_targets(rule: RuleSnapshot) -> list[int]:
    """Кому уходит рассылка: приёмник правила плюс доп. получатели."""
    seen: list[int] = []
    for candidate in [rule.target_id, *(rule.filters.targets or [])]:
        try:
            value = int(candidate)
        except (TypeError, ValueError):
            continue
        if value and value != rule.source_id and value not in seen:
            seen.append(value)
    return seen


async def _broadcast(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Одно сообщение из источника уходит в несколько чатов."""
    targets = _broadcast_targets(rule)
    if not targets:
        await record_error(rule, message, "У рассылки нет получателей")
        return

    text = transform_text(message_text(message), rule.filters)
    sent = 0
    for target in targets:
        try:
            await send_copy(client, target, message, text)
            sent += 1
        except RPCError as exc:
            logger.warning("Рассылка #{}: не ушло в {}: {}", rule.id, target, exc)
    if sent:
        await record_ok(rule, message, count=sent)
    else:
        await record_error(rule, message, "Сообщение не удалось доставить ни в один чат")


async def _baiting(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Ставит реакцию на сообщения нужного человека."""
    if not _sender_matches(rule, message):
        return

    reaction = (rule.filters.reaction or "").strip()
    if not reaction:
        await record_error(rule, message, "Не задана реакция для байтинга")
        return

    await client.send_reaction(message.chat_id, message.id, reaction)
    await record_ok(rule, message)


async def _mute(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Удаляет сообщения нужного человека в общем чате."""
    if not _sender_matches(rule, message):
        return
    if not _matches_keywords(rule.filters.keywords, message_text(message)):
        return

    await client.delete_messages(message.chat_id, [message.id])
    await record_ok(rule, message)


async def _dialogs(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Присылает входящие личные сообщения в выбранный чат."""
    if not getattr(message, "is_private", False):
        return
    if getattr(message, "out", False):  # свои исходящие не пересылаем
        return

    raw_text = message_text(message)
    if not raw_text and getattr(message, "media", None) is None:
        return
    if not _matches_keywords(rule.filters.keywords, raw_text):
        return

    header = await _sender_header(message)
    text = header + transform_text(raw_text, rule.filters)
    await send_copy(client, rule.target_id, message, text)
    await record_ok(rule, message)


async def _checks(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Ловит чеки и подарочные ссылки, складывает их в одно место."""
    raw_text = message_text(message)
    links = [match.group(0) for match in GIFT_LINK_RE.finditer(raw_text)]
    keyword_hit = bool(rule.filters.keywords) and _matches_keywords(
        rule.filters.keywords, raw_text
    )

    if not links and not keyword_hit:
        return

    chat_id = int(getattr(message, "chat_id", 0) or 0)
    message_id = int(getattr(message, "id", 0) or 0)
    if links:
        payloads = [
            {"chat_id": chat_id, "message_id": message_id, "link": link, "text": raw_text[:500]}
            for link in links
        ]
    else:
        payloads = [
            {"chat_id": chat_id, "message_id": message_id, "link": None, "text": raw_text[:500]}
        ]

    if rule.target_id:
        await send_copy(client, rule.target_id, message, transform_text(raw_text, rule.filters))
    await _store(rule, "checks", payloads)
    await record_ok(rule, message)


async def _autosubscribe(client: Any, message: Any, rule: RuleSnapshot) -> None:
    """Находит ссылки на каналы в сообщениях источника и вступает в них."""
    targets = _invite_targets(message_text(message))
    if not targets:
        return

    joined = await _join_all(client, targets)
    if joined:
        await record_ok(rule, message, count=joined)
        logger.info("Автоподписка #{}: вступили в {} чат(ов)", rule.id, joined)


_HANDLERS: dict[str, Callable[[Any, Any, RuleSnapshot], Awaitable[None]]] = {
    "broadcast": _broadcast,
    "baiting": _baiting,
    "mute": _mute,
    "dialogs": _dialogs,
    "checks": _checks,
    "autosubscribe": _autosubscribe,
}


# ──────────────────────────────── Разовые задачи ──────────────────────────────


async def run_oneshot(client: Any, rule: RuleSnapshot) -> dict[str, Any]:
    """Запускает разовую задачу и возвращает сводку для API."""
    _normalize_filters(rule)

    if rule.kind == "parser":
        return await run_parser(client, rule)
    if rule.kind == "autosubscribe":
        return await run_autosubscribe(client, rule)
    return {
        "ok": False,
        "error": f"Задача типа «{KIND_LABELS.get(rule.kind, rule.kind)}» "
        "работает по сообщениям, запускать вручную её не нужно",
    }


async def _known_user_ids(rule: RuleSnapshot) -> set[int]:
    """Люди, уже собранные этой задачей: повторный запуск не должен дублировать их."""
    async with SessionLocal() as session:
        items = await repo.list_collected_items(session, rule.id, limit=MAX_PARSER_LIMIT)

    known: set[int] = set()
    for item in items:
        try:
            value = int((item.payload or {}).get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        if value:
            known.add(value)
    return known


async def run_parser(client: Any, rule: RuleSnapshot) -> dict[str, Any]:
    """Собирает участников чата-источника в ``collected_items``."""
    limit = int(rule.filters.limit or 0)
    limit = max(1, min(limit if limit > 0 else 200, MAX_PARSER_LIMIT))
    known = await _known_user_ids(rule)

    payloads: list[dict] = []
    skipped = 0
    try:
        async for user in client.iter_participants(rule.source_id, limit=limit):
            if getattr(user, "deleted", False) or getattr(user, "bot", False):
                continue
            user_id = int(getattr(user, "id", 0) or 0)
            if not user_id or user_id in known:
                skipped += 1
                continue
            known.add(user_id)
            name = " ".join(
                part
                for part in (getattr(user, "first_name", None), getattr(user, "last_name", None))
                if part
            )
            payloads.append(
                {
                    "user_id": user_id,
                    "username": getattr(user, "username", None),
                    "name": name.strip(),
                    "phone": getattr(user, "phone", None),
                }
            )
    except FloodWaitError as exc:
        return {
            "ok": False,
            "error": f"Telegram просит подождать {int(getattr(exc, 'seconds', 60))} сек",
            "collected": 0,
        }
    except RPCError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "collected": 0}

    added = await _store(rule, "parser", payloads)
    return {"ok": True, "collected": added, "skipped": skipped, "limit": limit}


async def run_autosubscribe(client: Any, rule: RuleSnapshot) -> dict[str, Any]:
    """Вступает в чаты из настроек задачи и в ссылки, найденные в источнике."""
    targets = [
        str(item).strip() for item in (rule.filters.subscribe_to or []) if str(item).strip()
    ]

    # Если задан источник — сначала читаем из него последние посты на предмет ссылок
    if rule.source_id:
        try:
            async for message in client.iter_messages(rule.source_id, limit=20):
                targets.extend(_invite_targets(message_text(message)))
        except RPCError as exc:
            logger.warning("Автоподписка #{}: источник не прочитан: {}", rule.id, exc)

    # порядок сохраняем, но дубли убираем
    unique: list[str] = []
    for target in targets:
        if target not in unique:
            unique.append(target)

    if not unique:
        return {"ok": False, "error": "Не указано ни одного канала для подписки", "joined": 0}

    try:
        joined = await _join_all(client, unique)
    except FloodWaitError as exc:
        return {
            "ok": False,
            "error": f"Telegram просит подождать {int(getattr(exc, 'seconds', 60))} сек",
            "joined": 0,
        }
    return {"ok": True, "joined": joined, "total": len(unique)}


def _invite_targets(text: str) -> list[str]:
    """Достаёт из текста ссылки на чаты, отбрасывая служебные t.me-пути."""
    found: list[str] = []
    for match in CHAT_LINK_RE.finditer(text or ""):
        value = match.group(1).strip("/")
        if not value:
            continue
        head = value.split("/", 1)[0].lower()
        if head in NOT_A_CHAT_LINK:
            continue
        if value not in found:
            found.append(value)
    return found


async def _join_all(client: Any, targets: list[str]) -> int:
    """Вступает в перечисленные чаты. FloodWait пробрасывает наверх."""
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import ImportChatInviteRequest

    joined = 0
    for target in targets:
        try:
            if target.startswith("+") or target.lower().startswith("joinchat/"):
                invite_hash = target[1:] if target.startswith("+") else target.split("/", 1)[1]
                await client(ImportChatInviteRequest(invite_hash))
            else:
                await client(JoinChannelRequest(target))
            joined += 1
        except FloodWaitError:
            raise
        except RPCError as exc:
            # «заявка отправлена», «уже участник», «нет прав» — не ошибка задачи
            logger.info("Автоподписка: не вступили в {}: {}", target, type(exc).__name__)
        # пауза между вступлениями, иначе Telegram быстро присылает FloodWait
        await asyncio.sleep(2)
    return joined


# ─────────────────────────────────── Журнал ──────────────────────────────────


async def _sender_header(message: Any) -> str:
    """Строит шапку «кто написал» для уведомления из диалога."""
    sender_id = getattr(message, "sender_id", None)
    name = ""
    try:
        sender = await message.get_sender()
    except Exception:  # noqa: BLE001 — шапка не обязательна
        sender = None
    if sender is not None:
        name = " ".join(
            part
            for part in (
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            )
            if part
        ).strip() or (getattr(sender, "username", None) or "")
    if not name:
        name = str(sender_id or "неизвестно")
    return f"💬 {name} (id {sender_id}):\n"


async def _store(rule: RuleSnapshot, kind: str, payloads: list[dict]) -> int:
    """Кладёт собранные результаты в БД."""
    if not payloads:
        return 0
    async with SessionLocal() as session:
        added = await repo.add_collected_items(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            kind=kind,
            payloads=payloads,
        )
        await session.commit()
    return added


async def record_ok(rule: RuleSnapshot, message: Any, count: int = 1) -> None:
    """Отмечает успешное срабатывание задачи."""
    async with SessionLocal() as session:
        if count > 0:
            await repo.bump_forwarded(session, rule.id, count)
        await repo.log_forward(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            source_msg_id=int(getattr(message, "id", 0) or 0),
            target_msg_id=int(rule.target_id or 0) or None,
            status="ok",
        )
        await session.commit()
    logger.debug("Задача #{} ({}) сработала", rule.id, rule.kind)


async def record_error(rule: RuleSnapshot, message: Any, error: str) -> None:
    """Пишет ошибку задачи в журнал — чтобы её было видно в статистике."""
    logger.error("Задача #{} ({}): {}", rule.id, rule.kind, error)
    async with SessionLocal() as session:
        await repo.log_forward(
            session,
            rule_id=rule.id,
            user_id=rule.user_id,
            source_msg_id=int(getattr(message, "id", 0) or 0),
            target_msg_id=None,
            status="error",
            error=error[:1000],
        )
        await session.commit()
