"""API для Telegram Mini App.

Авторизация — по подписи `initData`, которую фронтенд берёт из
`window.Telegram.WebApp.initData`. Ни паролей, ни токенов: проверяем HMAC,
достаём `user.id` и работаем с БД от его имени.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote

from aiogram.types import LabeledPrice
from aiohttp import web
from loguru import logger

from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.errors import FeatureUnavailable, ValidationError, _dumps
from app.plans import STARS_DESCRIPTION, is_valid_period, periods_text, stars_amount
from app.telegram_client.jobs import MAX_PARSER_LIMIT, ONE_SHOT_KINDS
from app.telegram_client.manager import manager

# initData считаем свежим в течение суток
INIT_DATA_TTL = 24 * 60 * 60

# Один и тот же отказ отдаётся везде, где нужен живой MTProto-вход
LOGIN_UNAVAILABLE_TEXT = (
    "Подключение аккаунтов временно на настройке. Кабинет уже работает, "
    "пересылка включится после подключения MTProto-шлюза сервиса."
)

routes = web.RouteTableDef()

# aiohttp просит типизированные ключи вместо строковых: со строковыми ключами
# он предупреждает, а в следующих версиях перестанет работать.
USER_ID_KEY = web.RequestKey("user_id", int)
TG_USER_KEY = web.RequestKey("tg_user", dict)


# ─────────────────────────────── Авторизация ──────────────────────────────────


def validate_init_data(init_data: str) -> dict[str, Any] | None:
    """Проверяет подпись initData. Возвращает payload или None, если подпись битая."""
    if not init_data or not settings.bot_token:
        return None

    parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(parsed.items()))
    secret_key = hmac.new(
        b"WebAppData", settings.bot_token.encode(), hashlib.sha256
    ).digest()
    calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        logger.debug("initData не прошёл проверку подписи")
        return None

    auth_date = int(parsed.get("auth_date") or 0)
    if not auth_date or (time.time() - auth_date) > INIT_DATA_TTL:
        logger.debug("initData устарел")
        return None

    user: dict | None = None
    raw_user = parsed.get("user")
    if raw_user:
        try:
            user = json.loads(unquote(raw_user))
        except json.JSONDecodeError:
            user = None

    return {"user": user, "auth_date": auth_date, "query_id": parsed.get("query_id")}


def _init_data_from_request(request: web.Request) -> str:
    """initData приходит либо в заголовке, либо в query-параметре."""
    return (
        request.headers.get("X-Telegram-Init-Data")
        or request.query.get("initData")
        or ""
    )


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=_dumps)


def _require_account_login() -> None:
    """Общий отказ там, где нужен живой MTProto-вход (создание задачи, запуск)."""
    if not settings.public_login_enabled:
        raise FeatureUnavailable(
            LOGIN_UNAVAILABLE_TEXT,
            feature="account_login",
            status=settings.account_login_status,
        )


def _as_list(value: Any) -> list[str]:
    """Принимает список или строку «одно, другое» — отдаёт непустые значения."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = [str(item).strip() for item in value]
    else:
        raw = [chunk.strip() for chunk in re.split(r"[,\n;]+", str(value))]
    return [item for item in raw if item]


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def require_auth(handler: Callable) -> Callable:
    """Пропускает только запросы с валидным initData."""

    async def wrapper(request: web.Request) -> web.StreamResponse:
        payload = validate_init_data(_init_data_from_request(request))
        user = (payload or {}).get("user") or {}
        user_id = user.get("id")
        if not user_id:
            return _json({"error": "unauthorized"}, status=401)
        request[TG_USER_KEY] = user
        request[USER_ID_KEY] = int(user_id)
        return await handler(request)

    return wrapper


# ───────────────────────────────── Эндпоинты ──────────────────────────────────


@routes.get("/api/health")
async def health(_request: web.Request) -> web.Response:
    """Проверка живости. Без авторизации — для мониторинга."""
    return _json({"ok": True, "service": "tg-forward"})


@routes.get("/api/me")
@require_auth
async def me(request: web.Request) -> web.Response:
    """Профиль, подписка и короткая статистика."""
    user_id = request[USER_ID_KEY]
    tg_user = request[TG_USER_KEY]

    async with SessionLocal() as session:
        await repo.get_or_create_user(
            session,
            user_id=user_id,
            username=tg_user.get("username"),
            full_name=" ".join(
                part for part in (tg_user.get("first_name"), tg_user.get("last_name")) if part
            )
            or tg_user.get("username"),
        )
        await repo.grant_trial(session, user_id)
        await session.commit()

        until = await repo.subscription_until(session, user_id)
        rules_count = await repo.count_rules(session, user_id, include_archived=False)
        accounts = await repo.list_accounts(session, user_id)
        forwarded = sum(
            rule.forwarded_count
            for rule in await repo.list_rules(session, user_id, include_archived=False)
        )

    days_left = 0
    if until:
        days_left = max((until - repo.utcnow()).days, 0)

    return _json(
        {
            "id": user_id,
            "username": tg_user.get("username"),
            "name": tg_user.get("first_name") or tg_user.get("username") or str(user_id),
            "photo_url": tg_user.get("photo_url"),
            "is_admin": user_id in settings.admin_ids,
            "subscription": {
                "active": until is not None,
                "until": until.isoformat() if until else None,
                "days_left": days_left,
            },
            "stats": {
                "rules": rules_count,
                "accounts": len(accounts),
                "forwarded": forwarded,
            },
            "tariffs": {
                "rub": settings.price_rub,
                "stars": settings.price_stars,
                "usdt": settings.price_usdt,
                "trial_days": settings.trial_days,
                "max_rules_free": settings.max_rules_free,
            },
            "features": {
                "account_login_enabled": settings.public_login_enabled,
                "account_login_status": settings.account_login_status,
            },
        }
    )


def commands_payload() -> list[dict]:
    """Каталог команд со статусом, посчитанным по реальному состоянию.

    COMMANDS — статический справочник, и у каждой команды там "ready".
    Но каждая из них требует подключённый личный аккаунт, а значит без
    MTProto-шлюза она физически не выполнима. Отдавать "ready" в этом
    случае — обман: пользователь видит «включено», заполняет форму и
    получает отказ на сохранении. Поэтому статус считаем на лету.
    """
    if settings.public_login_enabled:
        return COMMANDS
    return [{**item, "status": "setup_required"} for item in COMMANDS]


@routes.get("/api/commands")
@require_auth
async def commands(_request: web.Request) -> web.Response:
    """Каталог команд. status: ready | setup_required."""
    return _json({"commands": commands_payload()})


@routes.get("/api/tasks")
@require_auth
async def list_tasks(request: web.Request) -> web.Response:
    """Задачи пользователя. status: active | paused | done (архив)."""
    user_id = request[USER_ID_KEY]
    status = request.query.get("status", "active")

    async with SessionLocal() as session:
        if status == "done":
            # Архив — задачи, убранные из работы, но не удалённые
            rules = [r for r in await repo.list_rules(session, user_id) if r.archived]
        else:
            rules = list(await repo.list_rules(session, user_id, include_archived=False))
            rules = [r for r in rules if r.enabled == (status == "active")]

    return _json({"tasks": [_task_view(rule) for rule in rules]})


@routes.post("/api/tasks")
@require_auth
async def create_task(request: web.Request) -> web.Response:
    """Создаёт задачу любого типа.

    Тело: {"command": "copy_channel", "account_id": int, ...} — либо старый
    вариант без command: тогда создаётся обычная пересылка. Остальные поля
    зависят от команды, их список приходит в /api/commands (needs/optional).
    """
    user_id = request[USER_ID_KEY]
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "Нужен JSON"}, status=400)

    command_id = str(payload.get("command") or "").strip()
    kind = str(payload.get("kind") or "").strip()
    command = COMMANDS_BY_ID.get(command_id)
    if command is not None:
        kind = command["kind"]
    elif kind in VALID_KINDS:
        command = next((item for item in COMMANDS if item["kind"] == kind), None)
    else:
        # старый клиент прислал только аккаунт/источник/приёмник — это пересылка
        command, kind = COMMANDS_BY_ID["copy_channel"], "forward"

    account_id = _as_int(payload.get("account_id"), 0)
    source = str(payload.get("source") or "").strip()
    target = str(payload.get("target") or "").strip()
    target_user = str(payload.get("target_user") or "").strip()
    targets = _as_list(payload.get("targets"))

    needs = set(command["needs"])
    missing = []
    if "account" in needs and not account_id:
        missing.append("аккаунт")
    if "source" in needs and not source:
        missing.append("источник")
    if "target" in needs and not target:
        missing.append("приёмник")
    if "target_user" in needs and not target_user:
        missing.append("человека, за которым следим")
    if "targets" in needs and not targets:
        missing.append("получателей")
    if missing:
        return _json({"error": "Укажите: " + ", ".join(missing)}, status=400)

    if not account_id:
        return _json({"error": "Укажите аккаунт"}, status=400)

    async with SessionLocal() as session:
        account = await repo.get_account(session, account_id, user_id)
        if account is None:
            return _json({"error": "Аккаунт не найден"}, status=404)

        # Архивные задачи уже не работают, поэтому лимит не занимают
        rules_count = await repo.count_rules(session, user_id, include_archived=False)
        subscribed = await repo.has_active_subscription(session, user_id)
        if not subscribed and rules_count >= settings.max_rules_free:
            return _json(
                {
                    "error": f"Без абонемента доступно только {settings.max_rules_free} правила",
                    "need_subscription": True,
                },
                status=402,
            )

    _require_account_login()

    from app.telegram_client.filters import default_filters

    filters = default_filters()
    errors: list[str] = []

    async def _resolve_chat(query: str, label: str) -> tuple[int, str] | None:
        if not query:
            return None
        found = await manager.resolve_chat(account_id, query)
        if found is None:
            errors.append(f"Не нашёл {label}: {query}")
            return None
        return found

    found_source = await _resolve_chat(source, "источник")
    found_target = await _resolve_chat(target, "приёмник")
    found_user = await _resolve_chat(target_user, "пользователя")

    extra_targets: list[int] = []
    for raw in targets:
        found = await _resolve_chat(str(raw), "получателя")
        if found is not None:
            extra_targets.append(found[0])

    if errors:
        return _json({"error": "; ".join(errors)}, status=404)

    source_id, source_title = found_source or (0, "")
    target_id, target_title = found_target or (0, "")

    mode = payload.get("mode") or "copy"
    if mode not in ("copy", "forward"):
        mode = "copy"
    if kind != "forward":
        # режим «копия/форвард» относится только к обычной пересылке
        mode = "copy"

    if found_user is not None:
        filters["target_user_id"] = found_user[0]
    filters["targets"] = extra_targets

    if kind == "parser":
        filters["limit"] = max(1, min(_as_int(payload.get("limit"), 200), MAX_PARSER_LIMIT))
        # приёмник парсеру не нужен, но колонка обязательна — пишем туда источник
        target_id, target_title = source_id, source_title
    elif kind == "autosubscribe":
        filters["subscribe_to"] = targets
        if not source_id:
            source_title = "все чаты аккаунта"
    elif kind == "baiting":
        filters["reaction"] = str(payload.get("reaction") or "").strip() or "👍"
    elif kind in ("checks", "dialogs", "mute"):
        filters["keywords"] = _as_list(payload.get("keywords"))
        if kind == "dialogs" and not source_id:
            source_title = "личные диалоги"

    async with SessionLocal() as session:
        rule = await repo.add_rule(
            session,
            user_id=user_id,
            account_id=account_id,
            source_id=source_id,
            source_title=source_title,
            target_id=target_id,
            target_title=target_title,
        )
        rule.kind = kind
        rule.mode = mode
        rule.filters = filters
        await session.commit()
        rule_id = rule.id

    await manager.refresh_rules()

    # Парсер и автоподписка работают по запросу — запускаем их сразу
    run_result: dict | None = None
    if kind in ONE_SHOT_KINDS:
        run_result = await manager.run_task_now(rule)

    async with SessionLocal() as session:
        saved = await repo.get_rule(session, rule_id, user_id)

    return _json({"task": _task_view(saved), "run": run_result}, status=201)


@routes.post("/api/tasks/{task_id}/toggle")
@require_auth
async def toggle_task(request: web.Request) -> web.Response:
    """Ставит задачу на паузу или снимает с паузы."""
    user_id = request[USER_ID_KEY]
    task_id = int(request.match_info["task_id"])

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        if rule.archived:
            return _json(
                {"error": "Задача в архиве — верните её из архива, чтобы продолжить"},
                status=409,
            )
        rule.enabled = not rule.enabled
        await session.commit()

    await manager.refresh_rules()
    return _json({"task": _task_view(rule)})


@routes.post("/api/tasks/{task_id}/mode")
@require_auth
async def switch_mode(request: web.Request) -> web.Response:
    """Переключает режим: copy (без метки) / forward (обычный форвард).

    Относится только к обычной пересылке — у остальных задач поведение
    определяется типом, а не этим переключателем.
    """
    user_id = request[USER_ID_KEY]
    task_id = int(request.match_info["task_id"])

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        if (rule.kind or "forward") != "forward":
            return _json(
                {"error": "Режим переключается только у обычной пересылки"}, status=409
            )
        rule.mode = "forward" if rule.mode == "copy" else "copy"
        await session.commit()

    await manager.refresh_rules()
    return _json({"task": _task_view(rule)})


@routes.post("/api/tasks/{task_id}/archive")
@require_auth
async def archive_task(request: web.Request) -> web.Response:
    """Убирает задачу в архив (?undo=1 — вернуть обратно)."""
    user_id = request[USER_ID_KEY]
    task_id = int(request.match_info["task_id"])
    archived = request.query.get("undo") != "1"

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        await repo.set_rule_archived(session, rule, archived)
        await session.commit()

    await manager.refresh_rules()
    return _json({"task": _task_view(rule)})


@routes.post("/api/tasks/{task_id}/run")
@require_auth
async def run_task(request: web.Request) -> web.Response:
    """Запускает разовую задачу: парсер аудитории или автоподписку."""
    user_id = request[USER_ID_KEY]
    task_id = int(request.match_info["task_id"])

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        if (rule.kind or "forward") not in ONE_SHOT_KINDS:
            return _json(
                {"error": "Эта задача работает по сообщениям — запуск вручную не нужен"},
                status=409,
            )
        if not rule.enabled or rule.archived:
            return _json({"error": "Задача не активна"}, status=409)

    _require_account_login()

    result = await manager.run_task_now(rule)
    return _json({"run": result})


@routes.get("/api/tasks/{task_id}/results")
@require_auth
async def task_results(request: web.Request) -> web.Response:
    """Что насобирала задача: участники парсера или пойманные чеки."""
    user_id = request[USER_ID_KEY]
    task_id = int(request.match_info["task_id"])
    limit = _as_int(request.query.get("limit"), 100)

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        items = list(await repo.list_collected_items(session, task_id, limit=limit))
        total = await repo.count_collected_items(session, task_id)

    return _json(
        {
            "kind": rule.kind,
            "total": total,
            "items": [
                {
                    "id": item.id,
                    "payload": item.payload,
                    "created_at": item.created_at.isoformat() if item.created_at else None,
                }
                for item in items
            ],
        }
    )


@routes.delete("/api/tasks/{task_id}")
@require_auth
async def delete_task(request: web.Request) -> web.Response:
    user_id = request[USER_ID_KEY]
    task_id = int(request.match_info["task_id"])

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        await repo.delete_rule(session, rule)
        await session.commit()

    await manager.refresh_rules()
    return _json({"ok": True})


@routes.get("/api/chats")
@require_auth
async def list_chats(request: web.Request) -> web.Response:
    """Чаты подключённого аккаунта. Параметры: account_id, q (поиск)."""
    user_id = request[USER_ID_KEY]
    account_id = int(request.query.get("account_id") or 0)
    query = (request.query.get("q") or "").strip().lower()

    async with SessionLocal() as session:
        account = await repo.get_account(session, account_id, user_id)
        if account is None:
            return _json({"chats": [], "total": 0, "note": "Аккаунт не найден"})

    if not settings.public_login_enabled:
        # Здесь пустой список — не ошибка, а честный ответ: чатов просто неоткуда взять
        return _json(
            {
                "chats": [],
                "total": 0,
                "online": False,
                "note": LOGIN_UNAVAILABLE_TEXT,
                "feature": "account_login",
                "status": settings.account_login_status,
            }
        )

    dialogs = list(await manager.list_dialogs(account_id, limit=200))
    if query:
        dialogs = [d for d in dialogs if query in d["title"].lower()]

    return _json({"chats": dialogs, "total": len(dialogs), "online": manager.is_online(account_id)})


@routes.get("/api/accounts")
@require_auth
async def list_accounts(request: web.Request) -> web.Response:
    """Аккаунты пользователя + состояние подписки."""
    user_id = request[USER_ID_KEY]

    async with SessionLocal() as session:
        accounts = list(await repo.list_accounts(session, user_id))
        until = await repo.subscription_until(session, user_id)
        pending = await repo.get_pending_login(session, user_id)
        banked = await repo.get_subscription(session, user_id)

    items = []
    for account in accounts:
        items.append(
            {
                "id": account.id,
                "phone": account.phone,
                "is_active": account.is_active,
                "online": manager.is_online(account.id),
                "last_error": account.last_error,
                "created_at": account.created_at.isoformat() if account.created_at else None,
            }
        )

    return _json(
        {
            "accounts": items,
            "subscription": {
                "active": until is not None,
                "until": until.isoformat() if until else None,
                "days_left": max((until - repo.utcnow()).days, 0) if until else 0,
                # Копилка: дни, снятые с активного периода и не привязанные к дате
                "piggy_bank_days": int(banked.banked_days or 0) if banked is not None else 0,
            },
            "pending_login": {
                "exists": pending is not None,
                "phone": pending.phone if pending else None,
                "stage": pending.stage if pending else None,
            },
            "bot_url": f"https://t.me/{(await _bot_username()) or ''}".rstrip("/"),
            "features": {
                "account_login_enabled": settings.public_login_enabled,
                "account_login_status": settings.account_login_status,
            },
        }
    )


@routes.get("/api/subscription")
@require_auth
async def subscription(request: web.Request) -> web.Response:
    """Состояние абонемента и тарифы."""
    user_id = request[USER_ID_KEY]
    async with SessionLocal() as session:
        until = await repo.subscription_until(session, user_id)
        sub = await repo.get_subscription(session, user_id)

    return _json(
        {
            "active": until is not None,
            "until": until.isoformat() if until else None,
            "days_left": max((until - repo.utcnow()).days, 0) if until else 0,
            "banked_days": int(sub.banked_days or 0) if sub is not None else 0,
            "tariffs": {
                "rub": settings.price_rub,
                "stars": settings.price_stars,
                "usdt": settings.price_usdt,
                "trial_days": settings.trial_days,
            },
        }
    )


# ──────────────────────────────── Оплата Stars ───────────────────────────

@routes.post("/api/subscription/invoice")
@require_auth
async def create_stars_invoice(request: web.Request) -> web.Response:
    """Ссылка на счёт в Telegram Stars для оплаты абонемента.

    Инвойс создаёт бот через Bot API: сумма и payload формируются на сервере,
    мини-апп получает только ссылку и открывает её через WebApp.openInvoice.
    Пользователь не покидает кабинет, а зачисление по-прежнему приходит
    в хендлер successful_payment бота — вторую реализацию оплаты не плодим.
    """
    user_id = request[USER_ID_KEY]

    # Пустое тело — нормальный случай: «оплатить месяц». А мусор вместо
    # JSON-объекта молча принимать нельзя: months уедет в значение по умолчанию
    # и пользователь заплатит не за то, что выбирал.
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ValidationError("Ожидается JSON-объект с полем months")
    months = _as_int(body.get("months"), 1)

    # Срок только из каталога: «2 месяца» со стороны клиента — не повод
    # молча округлять, иначе цена на кнопке разойдётся с ценой в счёте.
    if not is_valid_period(months):
        raise ValidationError(f"Срок — {periods_text()}")

    if _bot is None:
        raise FeatureUnavailable(
            "Оплата звёздами временно недоступна",
            feature="stars",
            status="bot_unavailable",
        )

    title = "Абонемент на 1 месяц" if months == 1 else f"Абонемент на {months} мес."
    amount = stars_amount(months)
    try:
        link = await _bot.create_invoice_link(
            title=title,
            description=STARS_DESCRIPTION,
            # Формат читает хендлер successful_payment в боте — менять нельзя.
            payload=f"sub:{user_id}:{months}",
            provider_token="",  # для Stars платёжный токен не нужен
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=amount)],
        )
    except Exception as exc:  # noqa: BLE001
        # Пользователю подробности Bot API ни к чему, а в лог они попасть должны.
        logger.warning("Не удалось создать инвойс Stars для {}: {}", user_id, exc)
        raise FeatureUnavailable(
            "Не удалось создать счёт. Попробуйте позже.",
            feature="stars",
            status="invoice_failed",
        ) from exc

    return _json({"url": link, "months": months, "amount": amount, "currency": "XTR"})


@routes.post("/api/subscription/bank")
@require_auth
async def bank_subscription(request: web.Request) -> web.Response:
    """Замораживает дни активной подписки в копилку.

    Всегда остаётся минимум сутки активного периода. Если days не передали —
    замораживается всё, что можно.
    """
    user_id = request[USER_ID_KEY]
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    days = _as_int((payload or {}).get("days"), 0)

    async with SessionLocal() as session:
        until = await repo.subscription_until(session, user_id)
        if until is None:
            return _json({"error": "Активной подписки нет — замораживать нечего"}, status=409)
        if days <= 0:
            days = max((until - repo.utcnow()).days - 1, 0)
        moved = await repo.bank_days(session, user_id, days)
        if not moved:
            return _json(
                {"error": "Столько дней заморозить нельзя: нужно оставить хотя бы сутки активными"},
                status=409,
            )
        sub = await repo.get_subscription(session, user_id)
        await session.commit()

    return _json(
        {
            "moved": moved,
            "banked_days": int(sub.banked_days or 0) if sub is not None else 0,
        }
    )


@routes.post("/api/subscription/distribute")
@require_auth
async def distribute_subscription(request: web.Request) -> web.Response:
    """Распределяет дни из копилки обратно в активную подписку.

    days <= 0 или отсутствует — возвращаются все дни.
    """
    user_id = request[USER_ID_KEY]
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        payload = {}
    days = _as_int((payload or {}).get("days"), 0)

    async with SessionLocal() as session:
        moved = await repo.unbank_days(session, user_id, days)
        if not moved:
            return _json({"error": "В копилке нет дней"}, status=409)
        until = await repo.subscription_until(session, user_id)
        sub = await repo.get_subscription(session, user_id)
        await session.commit()

    return _json(
        {
            "moved": moved,
            "banked_days": int(sub.banked_days or 0) if sub is not None else 0,
            "active": until is not None,
            "until": until.isoformat() if until else None,
            "days_left": max((until - repo.utcnow()).days, 0) if until else 0,
        }
    )


_bot: Any = None


async def _bot_username() -> str | None:
    """Username бота — нужен для кнопки «Открыть в боте»."""
    if _bot is None:
        return None
    try:
        me = await _bot.get_me()
        return me.username
    except Exception:  # noqa: BLE001
        return None


def _task_view(rule) -> dict:
    """Правило → вид задачи для мини-аппа."""
    from app.telegram_client.jobs import KIND_LABELS, task_title

    kind = rule.kind or "forward"
    return {
        "id": rule.id,
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "title": task_title(rule),
        "source": rule.source_title or str(rule.source_id or ""),
        "target": rule.target_title or str(rule.target_id or ""),
        "enabled": rule.enabled,
        "archived": bool(rule.archived),
        "mode": rule.mode,
        "delay": rule.delay_seconds,
        "forwarded": rule.forwarded_count,
        "account_id": rule.account_id,
        # Разовые задачи запускаются кнопкой, а не реагируют на сообщения
        "oneshot": kind in ONE_SHOT_KINDS,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
    }


# Каталог команд мини-аппа.
# kind — тип задачи в app.telegram_client.jobs; needs/optional — поля формы,
# по ним фронтенд собирает шторку создания и проверяет обязательность.
COMMANDS: list[dict] = [
    {
        "id": "copy_channel",
        "kind": "forward",
        "emoji": "🔁",
        "title": "Копирование канала",
        "description": "Копирует новые публикации между каналами с заменами текста.",
        "status": "ready",
        "needs": ["account", "source", "target"],
        "optional": ["mode"],
    },
    {
        "id": "broadcast",
        "kind": "broadcast",
        "emoji": "📣",
        "title": "Рассылка по чатам",
        "description": "Одно сообщение из источника — в несколько чатов сразу.",
        "status": "ready",
        "needs": ["account", "source", "target", "targets"],
        "optional": [],
        "hint": "Дополнительные получатели — через запятую: @chan1, @chan2.",
    },
    {
        "id": "parser",
        "kind": "parser",
        "emoji": "🕵️",
        "title": "Парсер аудитории",
        "description": "Собирает участников чужого чата в список по вашей команде.",
        "status": "ready",
        "needs": ["account", "source"],
        "optional": ["limit"],
        "hint": "Запускается сразу после создания. Результат смотрите кнопкой «Результаты».",
    },
    {
        "id": "autosubscribe",
        "kind": "autosubscribe",
        "emoji": "🤝",
        "title": "Автоподписка",
        "description": "Вступает в каналы из списка и подхватывает ссылки из источника.",
        "status": "ready",
        "needs": ["account", "targets"],
        "optional": ["source"],
        "hint": "Каналы — через запятую: @chan1, t.me/+invite.",
    },
    {
        "id": "checks",
        "kind": "checks",
        "emoji": "🧾",
        "title": "Ловец чеков",
        "description": "Ловит чеки и подарочные ссылки в чатах и складывает в одно место.",
        "status": "ready",
        "needs": ["account", "source", "target"],
        "optional": ["keywords"],
    },
    {
        "id": "dialogs",
        "kind": "dialogs",
        "emoji": "💬",
        "title": "Уведомления из диалогов",
        "description": "Присылает входящие личные сообщения в выбранный чат.",
        "status": "ready",
        "needs": ["account", "target"],
        "optional": ["keywords"],
        "hint": "Источник не нужен: задача слушает все личные диалоги аккаунта.",
    },
    {
        "id": "baiting",
        "kind": "baiting",
        "emoji": "🎣",
        "title": "Байтинг",
        "description": "Ставит реакцию на сообщения выбранного человека в общем чате.",
        "status": "ready",
        "needs": ["account", "source", "target_user"],
        "optional": ["reaction"],
    },
    {
        "id": "mute",
        "kind": "mute",
        "emoji": "🔇",
        "title": "Мут",
        "description": "Удаляет сообщения выбранного человека в чате, где вы администратор.",
        "status": "ready",
        "needs": ["account", "source", "target_user"],
        "optional": ["keywords"],
    },
]

COMMANDS_BY_ID: dict[str, dict] = {item["id"]: item for item in COMMANDS}
VALID_KINDS: set[str] = {item["kind"] for item in COMMANDS}


# ────────────────────────────── Регистрация роутов ────────────────────────────


@routes.get("/")
async def _index_redirect(_request: web.Request) -> web.Response:
    """Корень отдаём редиректом на мини-апп."""
    raise web.HTTPFound("/app/")


@routes.get("/app")
async def _webapp_no_slash(_request: web.Request) -> web.Response:
    raise web.HTTPFound("/app/")


@routes.get("/app/")
async def _webapp_index(_request: web.Request) -> web.Response:
    """Главная мини-аппа. Telegram открывает ровно этот адрес."""
    index = settings.webapp_dir / "index.html"
    if not index.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(index)


def setup_webapp_routes(app: web.Application, bot: Any = None) -> None:
    """Подключает API и раздачу статики мини-аппа."""
    global _bot
    _bot = bot
    app.add_routes(routes)

    if settings.webapp_dir.exists():
        app.router.add_static("/app/", path=str(settings.webapp_dir), name="webapp")
        logger.info("Мини-апп раздаётся из {}", settings.webapp_dir)
    else:
        logger.warning("Папка мини-аппа не найдена: {}", settings.webapp_dir)
