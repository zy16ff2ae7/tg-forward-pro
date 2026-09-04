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
from urllib.parse import parse_qsl

from aiogram.types import LabeledPrice
from aiohttp import web
from loguru import logger

from app import accounts_login, paylink, webapp_build
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.errors import FeatureUnavailable, ValidationError, _dumps
from app.payments import service
from app.plans import (
    DEFAULT_MONTHS,
    PERIODS,
    STARS_DESCRIPTION,
    is_valid_period,
    periods_text,
    rub_amount,
    stars_amount,
    usdt_amount,
)
from app.telegram_client.jobs import MAX_PARSER_LIMIT, ONE_SHOT_KINDS
from app.telegram_client.manager import manager

# initData считаем свежим в течение суток
INIT_DATA_TTL = 24 * 60 * 60

# Один и тот же отказ отдаётся везде, где нужен живой MTProto-вход. Текст живёт
# в app/accounts_login.py — там же, где сам вход, чтобы кабинет и бот объясняли
# отключённую функцию одними словами.
LOGIN_UNAVAILABLE_TEXT = accounts_login.LOGIN_UNAVAILABLE_TEXT

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
        # parse_qsl уже раскодировал percent-encoding. Второй unquote ломал бы
        # имена, где сам процент — часть текста («скидка 50%25» → «50%»), а из
        # «Ко%22т» делал бы кавычку и невалидный JSON, то есть отказ 401.
        try:
            user = json.loads(raw_user)
        except json.JSONDecodeError:
            logger.debug("initData: поле user не разобралось как JSON")
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
    accounts_login.require_enabled()


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


def _as_bool(value: Any) -> bool:
    """Галочка из формы: чекбокс приходит и булем, и строкой — принимаем оба."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on", "да")


def _as_ids(value: Any) -> list[int]:
    """Список числовых id из формы. Всё нечисловое молча отбрасываем."""
    result: list[int] = []
    for item in _as_list(value):
        if str(item).lstrip("-").isdigit():
            result.append(int(item))
    return result


def _missed_text(missed: list[str]) -> list[str]:
    """Ненайденные чаты одной короткой строкой.

    Чатов в задаче может быть сколько угодно, поэтому перечислять их все нельзя:
    сообщение с двумя сотнями ссылок в кабинете не читается и не помещается.
    Показываем первые три и число остальных — этого хватает, чтобы понять, что
    именно не нашлось (обычно опечатка в одной ссылке).
    """
    if not missed:
        return []
    head = ", ".join(missed[:3])
    if len(missed) <= 3:
        return [f"Не нашёл {'чат' if len(missed) == 1 else 'чаты'}: {head}"]
    return [f"Не нашёл чаты ({len(missed)}): {head} и ещё {len(missed) - 3}"]


def _split_chats(pairs: list[tuple[int, str]], source_id: int) -> list[tuple[int, str]]:
    """Список чатов задачи без повторов и без источника.

    Геометрия «первый чат в обязательной колонке target_id, остальные — в
    настройках» одна у пересылки в несколько чатов, авто-постинга и рассылки,
    поэтому и подготовка списка одна: повтор означал бы два сообщения в один
    чат за круг, а источник в получателях — пересылку самому себе.
    """
    seen: list[tuple[int, str]] = []
    taken: set[int] = set()
    for chat_id, title in pairs:
        if chat_id and chat_id != source_id and chat_id not in taken:
            taken.add(chat_id)
            seen.append((chat_id, title))
    return seen


# Задачи, которые ходят в любое число чатов, и их ответ на «а чаты-то где?».
# Держим одним словарём: список чатов у них собирается одним и тем же кодом, и
# отличается только словами в ошибке.
MULTI_CHAT_EMPTY: dict[str, str] = {
    "broadcast": "Укажите, в какие чаты пересылать",
    "poster": "Укажите, в какие чаты постить",
    "mailing": "Укажите получателей рассылки",
}
MULTI_CHAT_KINDS: frozenset[str] = frozenset(MULTI_CHAT_EMPTY)

# Как назвать незаполненный список в ответе «Укажите: …». Поле одно (targets), а
# смысл разный: у пересылки и постинга это чаты, у автоподписки — каналы, у
# рассылки — получатели. Одно слово «получателей» на всех сбивало с толку, ведь в
# автоподписке никаких получателей нет.
TARGETS_LABEL: dict[str, str] = {
    "broadcast": "чаты",
    "poster": "чаты",
    "autosubscribe": "каналы",
    "mailing": "получателей",
}


def _months_or_fail(raw: Any, default: int) -> int:
    """Срок абонемента из запроса. Поля нет — умолчание, мусор — отказ.

    Отсутствие поля — нормальный случай «оплатить месяц», у него есть умолчание.
    А вот молча превращать «много» в один месяц нельзя: человек заплатит не за
    то, что выбирал, и в претензии будет прав.

    Разбираем через строку, чтобы «1.5» тоже считалось мусором: ``int(1.5)``
    молча даёт 1, то есть счёт на другой срок, чем просили.
    """
    if raw is None or raw == "":
        months = default
    else:
        try:
            months = int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValidationError(f"Срок — {periods_text()}") from None
    if not is_valid_period(months):
        raise ValidationError(f"Срок — {periods_text()}")
    return months


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
    """Проверка живости. Без авторизации — для мониторинга.

    Плюс счётчики очереди: по ним видно, не копятся ли отправки и не сыплются
    ли ошибки, — иначе узнаём о проблеме только от пользователей. Пропуски идут
    с разбивкой по причинам: «отфильтровано» — норма, «нет подписки» — деньги,
    «фильтр упал» — правило надо править. В общем счётчике всё это неразличимо.
    """
    delivery = dict(manager.delivery_stats())
    try:
        async with SessionLocal() as session:
            delivery["persisted"] = await repo.count_pending_deliveries(session)
    except Exception as exc:  # noqa: BLE001 — health не должен падать из-за БД
        logger.debug("health: не прочитали журнал ожидания: {}", exc)
        delivery["persisted"] = None
    return _json(
        {
            "ok": True,
            "service": "tg-forward",
            # Метка сборки мини-аппа: по ней кабинет понимает, что держит в
            # руках старый бандл, и перезагружается сам (см. webapp/app.js).
            "build": webapp_build.build_stamp(settings.webapp_dir),
            "delivery": delivery,
        }
    )


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
                # Только реально подключённые способы оплаты: мини-апп
                # не должен предлагать карту или USDT, если они не работают.
                "payment_methods": settings.payment_methods(),
            },
            # Где чем платят: внутри Telegram — звёзды, карта и крипта — по
            # ссылке во внешнем браузере (см. app/paylink.py).
            "pay": {
                "mode": settings.pay_mode,
                "inline": settings.inline_payment_methods(),
                "external": settings.external_payment_methods(),
                "url": paylink.pay_url(user_id),
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
    """Каталог команд. status: ready | setup_required.

    ``groups`` — порядок и подписи блоков каталога: кабинет раскладывает
    команды по ним, чтобы список не выглядел свалкой из девяти карточек.
    """
    return _json({"commands": commands_payload(), "groups": COMMAND_GROUPS})


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

        # Парсер — единственная задача с обозримым концом: собрано из лимита.
        # Счётчик берём из базы, пока сессия открыта, а не выдумываем на клиенте.
        collected = {
            rule.id: await repo.count_collected_items(session, rule.id)
            for rule in rules
            if (rule.kind or "forward") == "parser"
        }

    return _json({"tasks": [_task_view(rule, collected.get(rule.id)) for rule in rules]})


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
    # Задачи «в несколько чатов» просят список получателей, но одиночное поле
    # target тоже принимаем: его присылают старые формы, бот и ссылки на задачу,
    # созданные до того, как постинг научился ходить в любое число чатов. Один
    # чат — это просто список из одного, отдельной ветки проверок не нужно.
    if "targets" in needs and "target" not in needs and target and not targets:
        targets, target = [target], ""

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
        missing.append(TARGETS_LABEL.get(kind, "чаты"))
    if "message" in needs and not str(payload.get("message") or "").strip():
        # Рассылке текст в форме не нужен, если сообщения выбраны из библиотеки:
        # оттуда их и берёт планировщик, а копия того же текста в поле только
        # плодила бы дубли записей.
        if not (kind == "mailing" and _as_ids(payload.get("library_ids"))):
            missing.append("сообщение")
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

    # Чаты ищем одним обходом диалогов на все ссылки сразу: чатов в задаче может
    # быть сколько угодно, а поиск каждого по отдельности означал бы столько же
    # обходов Telegram подряд — верный FloodWait на сотне получателей.
    lookups = [source, target, target_user, *(str(raw) for raw in targets)]
    resolved = await manager.resolve_many(account_id, lookups)

    def _resolve_chat(query: str, label: str) -> tuple[int, str] | None:
        if not query:
            return None
        found = resolved.get(query.strip())
        if found is None:
            errors.append(f"Не нашёл {label}: {query}")
            return None
        return found

    found_source = _resolve_chat(source, "источник")
    found_target = _resolve_chat(target, "приёмник")
    found_user = _resolve_chat(target_user, "пользователя")

    # Получателей держим парами (id, название): рассылке первый из них станет
    # приёмником правила, и без названия карточка задачи была бы безымянной.
    missed: list[str] = []
    extra_pairs: list[tuple[int, str]] = []
    for raw in targets:
        ref = str(raw).strip()
        found = resolved.get(ref)
        if found is None:
            missed.append(ref)
            continue
        extra_pairs.append(found)
    extra_targets = [pair[0] for pair in extra_pairs]

    if errors or missed:
        # Рассылке и постингу, где не нашлось ни одного чата, отвечает их
        # собственная ветка — и отвечает 400: это ошибка ввода, а не «чат не
        # найден». Если часть чатов нашлась, про остальные честно сообщаем здесь.
        nowhere_to_send = (
            kind in ("mailing", "poster") and found_target is None and not extra_pairs
        )
        if not nowhere_to_send:
            return _json({"error": "; ".join([*errors, *_missed_text(missed)])}, status=404)

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
    # Все чаты задачи в одном списке: первый чат может прийти и полем «приёмник»
    # (одиночный выбор, старые формы и бот), и первым из получателей.
    chat_pairs = ([found_target] if found_target else []) + extra_pairs

    # Задачи «в любое число чатов» делят список одинаково: первый чат живёт в
    # обязательной колонке target_id, остальные — в настройках. Раньше эти
    # четыре строки стояли в каждой ветке отдельно и разъезжались при правках.
    # Источник выкидываем только у пересылки — там он есть и пересылать пост в
    # его же чат незачем; у постинга и рассылки источника нет вовсе.
    if kind in MULTI_CHAT_KINDS:
        chats = _split_chats(chat_pairs, source_id if kind == "broadcast" else 0)
        if not chats:
            return _json({"error": MULTI_CHAT_EMPTY[kind]}, status=400)
        target_id, target_title = chats[0]
        filters["targets"] = [pair[0] for pair in chats[1:]]

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
    elif kind == "poster":
        # Авто-постер: шлёт собственные сообщения в выбранные чаты по расписанию.
        # Источник не нужен — ставим 0, чтобы не создавать ложного совпадения
        # с входящими сообщениями приёмника.
        source_id, source_title = 0, "авто-постинг"
        msgs = [m.strip() for m in str(payload.get("message") or "").split("\n") if m.strip()]
        if not msgs:
            msgs = [str(payload.get("message") or "").strip()]
        filters["messages"] = msgs
        interval_min = max(1, _as_int(payload.get("interval"), 2))
        filters["interval_seconds"] = interval_min * 60
        filters["window_start"] = str(payload.get("start") or "00:00")[:5]
        filters["window_end"] = str(payload.get("end") or "23:59")[:5]
    elif kind == "mailing":
        # Рассылка по чатам: свои сообщения по списку получателей, по кругу.
        # Источник не нужен — ставим 0, иначе входящее сообщение в первом же
        # чате-получателе «подхватило» бы рассылку как обычную пересылку.
        source_id, source_title = 0, "рассылка по чатам"
        filters["gap_seconds"] = max(0, _as_int(payload.get("gap"), 5))
        filters["gap_jitter"] = max(0, _as_int(payload.get("gap_jitter"), 0))
        filters["cycle_seconds"] = max(0, _as_int(payload.get("cycle"), 10))
        filters["cycle_jitter"] = max(0, _as_int(payload.get("cycle_jitter"), 0))
        filters["repeats"] = max(0, _as_int(payload.get("repeats"), 1))
        filters["typing"] = _as_bool(payload.get("typing"))
        filters["random_pick"] = _as_bool(payload.get("random_pick"))
        filters["link_preview"] = _as_bool(payload.get("link_preview"))

        # Текст из формы кладём в библиотеку: рассылка берёт сообщения оттуда,
        # и потом их можно пополнять, не пересоздавая задачу.
        msgs = [line.strip() for line in str(payload.get("message") or "").split("\n") if line.strip()]
        saved_ids: list[int] = []
        if msgs:
            async with SessionLocal() as session:
                for text in msgs:
                    item = await repo.add_saved_message(
                        session, user_id=user_id, title=text[:48], text=text
                    )
                    saved_ids.append(item.id)
                await session.commit()
        # Явный выбор из библиотеки важнее только что набранного текста.
        filters["library_ids"] = _as_ids(payload.get("library_ids")) or saved_ids

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
        # Авто-постер держит интервал в delay_seconds — его читает планировщик
        if kind == "poster":
            rule.delay_seconds = int(filters.get("interval_seconds", 120))
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
        # Рассылка считает круги по счётчику отправок, поэтому снятая с паузы
        # задача с исчерпанным числом кругов молча бы ничего не делала. Новое
        # включение — это новый заход: начинаем круги заново.
        if not rule.enabled and (rule.kind or "forward") == "mailing" and _mailing_finished(rule):
            rule.forwarded_count = 0
        rule.enabled = not rule.enabled
        await session.commit()

    await manager.refresh_rules()
    return _json({"task": _task_view(rule)})


def _mailing_finished(rule) -> bool:
    """Рассылка сделала все круги, сколько было задано."""
    from app.telegram_client.filters import FilterConfig
    from app.telegram_client.jobs import chat_recipients, mailing_position

    conf = FilterConfig.from_dict(rule.filters or {})
    repeats = max(0, int(conf.repeats or 0))
    if not repeats:
        return False
    recipients = len(chat_recipients(rule))
    _, cycle = mailing_position(rule.forwarded_count, recipients)
    return cycle >= repeats


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


# ─────────────────────── Библиотека сообщений (что рассылать) ─────────────────


def _library_view(item) -> dict:
    """Сохранённое сообщение → вид для кабинета."""
    text = (item.text or "").strip()
    return {
        "id": item.id,
        "title": item.title or (text[:48] + ("…" if len(text) > 48 else "")),
        "text": text,
        "chat_id": int(item.chat_id or 0),
        "message_id": int(item.message_id or 0),
        "created_at": item.created_at.isoformat() if item.created_at else None,
    }


@routes.get("/api/library")
@require_auth
async def list_library(request: web.Request) -> web.Response:
    """Сохранённые сообщения: из них рассылка берёт тексты и посты."""
    user_id = request[USER_ID_KEY]

    async with SessionLocal() as session:
        items = list(await repo.list_saved_messages(session, user_id))

    return _json({"items": [_library_view(item) for item in items]})


@routes.post("/api/library")
@require_auth
async def add_library_item(request: web.Request) -> web.Response:
    """Кладёт сообщение в библиотеку: свой текст или ссылку на готовый пост."""
    user_id = request[USER_ID_KEY]
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "Нужен JSON"}, status=400)
    if not isinstance(payload, dict):
        return _json({"error": "Нужен JSON-объект"}, status=400)

    text = str(payload.get("text") or "").strip()
    chat_id = _as_int(payload.get("chat_id"), 0)
    message_id = _as_int(payload.get("message_id"), 0)
    # Пустая запись — это «отправить ничего»: такая в рассылке только мешает.
    if not text and not (chat_id and message_id):
        return _json({"error": "Дайте текст сообщения или ссылку на пост"}, status=400)

    async with SessionLocal() as session:
        item = await repo.add_saved_message(
            session,
            user_id=user_id,
            title=str(payload.get("title") or "").strip()[:128],
            text=text,
            chat_id=chat_id,
            message_id=message_id,
        )
        await session.commit()
        view = _library_view(item)

    return _json({"item": view}, status=201)


@routes.delete("/api/library/{item_id}")
@require_auth
async def delete_library_item(request: web.Request) -> web.Response:
    """Убирает сообщение из библиотеки. Задачи при этом не падают: рассылка
    просто берёт то, что осталось."""
    user_id = request[USER_ID_KEY]
    item_id = int(request.match_info["item_id"])

    async with SessionLocal() as session:
        item = await repo.get_saved_message(session, item_id, user_id)
        if item is None:
            return _json({"error": "Сообщение не найдено"}, status=404)
        await repo.delete_saved_message(session, item)
        await session.commit()

    await manager.refresh_rules()
    return _json({"ok": True})


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
    """Чаты подключённого аккаунта. Параметры: account_id, q (поиск), limit."""
    user_id = request[USER_ID_KEY]
    account_id = int(request.query.get("account_id") or 0)
    query = (request.query.get("q") or "").strip().lower()
    # По умолчанию отдаём все чаты: постинг и рассылка ходят в любое их число,
    # и отрезанный список означал бы, что часть чатов просто не выбрать мышкой.
    # limit оставлен для случаев, когда нужна короткая витрина.
    limit = max(0, _as_int(request.query.get("limit"), 0))

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

    dialogs = list(await manager.list_dialogs(account_id, limit=limit))
    if query:
        # Ищем и по названию, и по нику: в кабинете поле так и подписано
        # («Название, тема или @username»), а раньше ник не искался вовсе.
        needle = query.lstrip("@")
        dialogs = [
            d
            for d in dialogs
            if needle in d["title"].lower() or needle in str(d.get("username") or "").lower()
        ]

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
                # Короткое имя шага — то же, что отдают ручки входа, чтобы
                # кабинету не приходилось знать про «waiting_*» из БД.
                "step": accounts_login._PUBLIC_STAGE.get(pending.stage, "code")
                if pending
                else None,
                "attempts_left": max(
                    accounts_login.MAX_CODE_ATTEMPTS - int(pending.attempts or 0), 0
                )
                if pending
                else None,
            },
            "bot_url": f"https://t.me/{(await _bot_username()) or ''}".rstrip("/"),
            "features": {
                "account_login_enabled": settings.public_login_enabled,
                "account_login_status": settings.account_login_status,
            },
        }
    )


# ─────────────────────────── Подключение аккаунта ─────────────────────────────
#
# Вход целиком проходит в кабинете: раньше кнопка «Подключить аккаунт» умела
# только открыть чат с ботом, и человек уходил из мини-аппа на середине.
# Сами шаги живут в app/accounts_login.py — там же, откуда их берёт бот.


async def _login_body(request: web.Request) -> dict:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise ValidationError("Нужен JSON") from None
    return payload if isinstance(payload, dict) else {}


@routes.post("/api/accounts/login/start")
@require_auth
async def login_start(request: web.Request) -> web.Response:
    """Шаг 1: {"phone": "+79001234567"} → Telegram присылает код."""
    body = await _login_body(request)
    step = await accounts_login.start(request[USER_ID_KEY], body.get("phone"))
    return _json(step.as_dict())


@routes.post("/api/accounts/login/code")
@require_auth
async def login_code(request: web.Request) -> web.Response:
    """Шаг 2: {"code": "12345"} → либо просим 2FA, либо аккаунт подключён."""
    body = await _login_body(request)
    step = await accounts_login.submit_code(request[USER_ID_KEY], body.get("code"))
    return _json(step.as_dict())


@routes.post("/api/accounts/login/password")
@require_auth
async def login_password(request: web.Request) -> web.Response:
    """Шаг 3: {"password": "…"} — облачный пароль 2FA."""
    body = await _login_body(request)
    step = await accounts_login.submit_password(request[USER_ID_KEY], body.get("password"))
    return _json(step.as_dict())


@routes.post("/api/accounts/login/cancel")
@require_auth
async def login_cancel(request: web.Request) -> web.Response:
    """Забыть незавершённый вход (кнопка «Отмена» на любом шаге)."""
    dropped = await accounts_login.cancel(request[USER_ID_KEY])
    return _json({"ok": True, "dropped": dropped})


@routes.delete("/api/accounts/{account_id}")
@require_auth
async def delete_account(request: web.Request) -> web.Response:
    """Отключает аккаунт: гасит клиент и удаляет сохранённую сессию."""
    account_id = _as_int(request.match_info.get("account_id"), 0)
    phone = await accounts_login.disconnect(request[USER_ID_KEY], account_id)
    return _json({"ok": True, "phone": phone})


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
            # Контур оплаты: внутри Telegram — звёзды, карта и крипта — на сайте.
            # Кабинет открывает эту ссылку через WebApp.openLink, то есть во
            # внешнем браузере: платёж не проходит внутри Telegram.
            "pay": {
                "mode": settings.pay_mode,
                "inline": settings.inline_payment_methods(),
                "external": settings.external_payment_methods(),
                "url": paylink.pay_url(user_id),
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
    # Срок только из каталога: «2 месяца» со стороны клиента — не повод молча
    # округлять, иначе цена на кнопке разойдётся с ценой в счёте.
    months = _months_or_fail(body.get("months"), DEFAULT_MONTHS)

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


# ───────────────── Оплата вне Telegram: карта и USDT на странице ───────────────
#
# Внутри Telegram абонемент продаётся только за звёзды — так требуют правила
# Telegram (ToS для разработчиков, п. 6.2). Карта и крипта живут на обычной
# веб-странице ``/pay``, которая открывается во внешнем браузере. Ничего не
# скрыто: в боте прямо написано, что платёж уходит на сайт сервиса.
#
# Авторизация здесь не по initData (его на странице нет), а по подписанному
# токену из ссылки — см. app/paylink.py.


def _pay_link_or_fail(token: str) -> paylink.PayLink:
    link = paylink.parse_token(token or "")
    if link is None:
        raise ValidationError(
            "Ссылка на оплату устарела или неверна. Откройте её заново из бота."
        )
    return link


def _pay_html(title: str, message: str) -> str:
    """Минимальная страница-заглушка: устаревшая ссылка, выключенный контур.

    Палитра — как в кабинете и в ``webapp/pay.html``: человек пришёл по ссылке из
    бота, и даже страница с отказом должна выглядеть нашей, а не чужой.
    """
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        f"<title>ДОЧА · {title}</title>"
        "<style>body{font:16px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;"
        "margin:0;min-height:100vh;display:flex;align-items:center;"
        "justify-content:center;color:#F8EDF7;background:"
        "radial-gradient(120% 60% at 50% -10%,rgba(255,61,154,.22),transparent 62%),"
        "linear-gradient(180deg,#0F0618 0%,#0A0510 52%,#060309 100%)}"
        "div{max-width:28rem;padding:2rem;text-align:center}"
        "b{display:block;margin:0 0 1rem;letter-spacing:.14em;color:#FF3D9A}"
        "h1{font-size:1.25rem;margin:0 0 .75rem}p{margin:0;color:#B9A0C9}</style>"
        f"</head><body><div><b>ДОЧА</b><h1>{title}</h1>"
        f"<p>{message}</p></div></body></html>"
    )


@routes.get("/pay")
async def pay_page(request: web.Request) -> web.Response:
    """Страница оплаты картой и криптой. Открывается вне Telegram."""
    if not settings.external_payment_methods():
        return web.Response(
            text=_pay_html(
                "Оплата на сайте отключена",
                "Абонемент можно оплатить звёздами в боте.",
            ),
            content_type="text/html",
            status=404,
        )
    if paylink.parse_token(request.query.get("t") or "") is None:
        # 410, а не 400: ссылка была рабочей, просто истекла — это нормальный
        # исход, и понятный статус помогает в логах отличить его от подделки.
        return web.Response(
            text=_pay_html(
                "Ссылка устарела",
                "Ссылка на оплату живёт час. Откройте раздел «Подписка» в боте "
                "и нажмите кнопку оплаты снова.",
            ),
            content_type="text/html",
            status=410,
        )
    page = settings.webapp_dir / "pay.html"
    if not page.exists():
        raise web.HTTPNotFound()
    return web.FileResponse(page)


@routes.get("/api/pay/info")
async def pay_info(request: web.Request) -> web.Response:
    """Что показать на странице оплаты: способы, сроки и цены."""
    link = _pay_link_or_fail(request.query.get("t") or "")
    methods = settings.external_payment_methods()
    if not methods:
        raise FeatureUnavailable(
            "Оплата на сайте отключена", feature="external", status="disabled"
        )
    # Без username бота ссылку не собираем: "https://t.me" без имени ведёт
    # на главную Telegram и выглядит как поломка.
    username = await _bot_username()
    return _json(
        {
            "months": link.months,
            "methods": methods,
            "expires_in": max(0, link.expires_at - int(time.time())),
            "periods": [
                {"months": months, "rub": rub_amount(months), "usdt": usdt_amount(months)}
                for months in PERIODS
            ],
            "bot_url": f"https://t.me/{username}" if username else None,
        }
    )


@routes.get("/api/pay/link")
@require_auth
async def pay_link(request: web.Request) -> web.Response:
    """Свежая ссылка на страницу оплаты для кнопки в кабинете.

    Ссылка из ``/api/me`` подписана на момент открытия кабинета, а живёт час:
    если человек вернулся к вкладке позже, она уже не сработает. Поэтому
    кабинет просит новый токен в момент нажатия.
    """
    user_id = request[USER_ID_KEY]
    url = paylink.pay_url(user_id)
    if not url:
        raise FeatureUnavailable(
            "Оплата на сайте отключена", feature="external", status="disabled"
        )
    return _json(
        {
            "url": url,
            "methods": settings.external_payment_methods(),
            "expires_in": paylink.TOKEN_TTL_SECONDS,
        }
    )


@routes.post("/api/pay/start")
async def pay_start(request: web.Request) -> web.Response:
    """Выставляет счёт по способу оплаты со страницы ``/pay``.

    Пользователь берётся из подписи токена, а не из тела запроса: иначе можно
    было бы оплатить абонемент себе, а начислить его чужому аккаунту.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        raise ValidationError("Ожидается JSON-объект")

    link = _pay_link_or_fail(str(body.get("t") or ""))
    method = str(body.get("method") or "").strip()
    if method not in settings.external_payment_methods():
        raise FeatureUnavailable(
            "Этот способ оплаты недоступен", feature=method or "external", status="disabled"
        )

    # Срок можно поменять на странице: цену всё равно считает сервер, а токен
    # подтверждает только личность плательщика.
    months = _months_or_fail(body.get("months"), link.months)

    invoice = await service.start(method, link.user_id, months)
    logger.info(
        "Страница оплаты: счёт #{} на {} мес. для {} ({})",
        invoice.get("payment_id"),
        months,
        link.user_id,
        method,
    )
    return _json(invoice)


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


def _task_view(rule, collected: int | None = None) -> dict:
    """Правило → вид задачи для мини-аппа.

    ``collected`` — сколько записей задача уже собрала (только для парсера,
    считает вызывающий, пока открыта сессия).
    """
    from app.telegram_client.filters import FilterConfig
    from app.telegram_client.jobs import (
        KIND_LABELS,
        MAX_PARSER_LIMIT,
        chat_recipients,
        task_title,
    )

    kind = rule.kind or "forward"
    view = {
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
        # Включённая задача при отключённом аккаунте ничего не делает. Кабинет
        # обязан показать это метко́й «нет связи», а не бодрым «работает».
        "account_online": manager.is_online(rule.account_id),
        # Разовые задачи запускаются кнопкой, а не реагируют на сообщения
        "oneshot": kind in ONE_SHOT_KINDS,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
    }
    # Авто-постер: выносим расписание, чтобы в карточке задачи было видно,
    # как часто и в каком окне он шлёт (delay в секундах неинформативен).
    conf = FilterConfig.from_dict(rule.filters or {})
    # Чаты задачи считает планировщик — тем же счётом, каким и рассылает.
    # Своя арифметика здесь однажды разошлась бы с настоящим числом получателей.
    chats = chat_recipients(rule)
    if kind == "poster":
        view["interval_min"] = max(1, conf.interval_seconds // 60)
        view["window_start"] = conf.window_start
        view["window_end"] = conf.window_end
        view["messages_count"] = len(conf.messages)

    # Полоса выполнения. total заполняем ТОЛЬКО там, где «всего» существует в
    # настройках задачи: у парсера это лимит участников, у автоподписки —
    # список каналов. Остальные задачи работают, пока их не остановят, — у них
    # total равен null, и кабинет рисует бегунок без процентов вместо
    # выдуманной доли.
    done = int(rule.forwarded_count or 0)
    total: int | None = None
    if kind == "parser":
        done = int(collected or 0)
        limit = int(conf.limit or 0)
        total = max(1, min(limit if limit > 0 else 200, MAX_PARSER_LIMIT))
    elif kind == "autosubscribe" and conf.subscribe_to and not rule.source_id:
        # С источником список пополняется ссылками из его постов — тогда
        # «всего» заранее неизвестно.
        total = len(conf.subscribe_to)
    elif kind == "mailing":
        # У рассылки «всего» есть: получатели × число кругов. Без ограничения
        # кругов (repeats=0) конца нет — тогда и total остаётся null, как у
        # остальных бесконечных задач.
        recipients = len(chats)
        view["mailing"] = {
            "recipients": recipients,
            "messages_count": len(conf.library_ids),
            "gap_seconds": conf.gap_seconds,
            "cycle_seconds": conf.cycle_seconds,
            "repeats": conf.repeats,
            "typing": bool(conf.typing),
            "random_pick": bool(conf.random_pick),
        }
        if conf.repeats > 0 and recipients:
            total = recipients * int(conf.repeats)
    view["progress"] = {"done": done, "total": total}
    # Сколько чатов у задачи — одним полем на все задачи «в несколько чатов»:
    # у пересылки счёт раньше шёл по filters и терял первый чат из target_id.
    if kind in MULTI_CHAT_KINDS:
        view["targets_count"] = len(chats)
    return view


# Каталог команд мини-аппа.
# kind — тип задачи в app.telegram_client.jobs; needs/optional — поля формы,
# по ним фронтенд собирает шторку создания и проверяет обязательность.
# group — блок каталога (см. COMMAND_GROUPS ниже): десять команд одним списком
# читались как свалка, поэтому кабинет раскладывает их по смыслу.
COMMANDS: list[dict] = [
    {
        "id": "copy_channel",
        "group": "publish",
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
        "group": "publish",
        "kind": "broadcast",
        "emoji": "📣",
        # «Пересылка», а не «рассылка»: эта задача разносит ЧУЖОЙ пост из
        # источника, а свои сообщения по чатам шлёт mailing. Два одинаковых
        # названия в каталоге читались как одна команда-двойник.
        "title": "Пересылка в несколько чатов",
        "description": "Одно сообщение из источника — в несколько чатов сразу.",
        "status": "ready",
        # Приёмник отдельным полем не просим: чаты — один список, и первый из
        # них всё равно становится главным. Два поля под одно и то же заставляли
        # заполнять «приёмник» руками даже при выборе чатов мышкой.
        "needs": ["account", "source", "targets"],
        "optional": [],
        "hint": "Источник — откуда берём пост, чаты — куда он уйдёт. Отмечайте кнопкой «выбрать» — сколько нужно, хоть все сразу.",
    },
    {
        "id": "parser",
        "group": "audience",
        "kind": "parser",
        "emoji": "🕵️",
        "title": "Парсер аудитории",
        "description": "Собирает участников чужого чата в список по вашей команде.",
        "status": "ready",
        "needs": ["account", "source"],
        "optional": ["limit"],
        "hint": "Чат-источник отмечайте кнопкой «выбрать» у поля или заранее во вкладке «Чаты». Запускается сразу, результат — кнопкой «Результаты».",
    },
    {
        "id": "autosubscribe",
        "group": "audience",
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
        "group": "inbox",
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
        "group": "inbox",
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
        "group": "moderation",
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
        "group": "moderation",
        "kind": "mute",
        "emoji": "🔇",
        "title": "Мут",
        "description": "Удаляет сообщения выбранного человека в чате, где вы администратор.",
        "status": "ready",
        "needs": ["account", "source", "target_user"],
        "optional": ["keywords"],
    },
    {
        "id": "poster",
        "group": "publish",
        "kind": "poster",
        "emoji": "📤",
        "title": "Авто-постинг",
        "description": "Шлёт ваше сообщение в выбранные чаты каждые N минут в заданном окне времени.",
        "status": "ready",
        "needs": ["account", "targets", "message"],
        "optional": ["interval", "start", "end"],
        "hint": "Чаты отмечайте кнопкой «выбрать» — сколько нужно, хоть все сразу; круг идёт по очереди, с паузой между чатами. Сообщений тоже может быть несколько (каждое с новой строки) — за круг уходит одно, следующий круг возьмёт следующее. Интервал в минутах, окно — ЧЧ:ММ.",
    },
    {
        "id": "mailing",
        "group": "publish",
        "kind": "mailing",
        "emoji": "📨",
        "title": "Рассылка по чатам",
        "description": "Шлёт ваши сообщения по списку чатов: по одному в круг, с паузой между чатами.",
        "status": "ready",
        "needs": ["account", "targets", "message"],
        "optional": ["gap", "cycle", "repeats", "typing", "random_pick"],
        "hint": "Получателей отмечайте кнопкой «выбрать» — или заранее во вкладке «Чаты». Сообщения наберите здесь либо возьмите из библиотеки: уходят по очереди, первое — всем, затем второе. Пауза между чатами в секундах, «кругов 0» — крутить без конца.",
    },
]

# Блоки каталога в порядке показа. Подписи и порядок живут здесь, а не в
# мини-аппе: иначе появилась бы вторая копия, которая рано или поздно разойдётся
# с этим списком. Команда без известной группы попадёт в «прочее».
COMMAND_GROUPS: list[dict] = [
    {"id": "publish", "title": "пересылка и публикация"},
    {"id": "audience", "title": "аудитория"},
    {"id": "inbox", "title": "входящее"},
    {"id": "moderation", "title": "модерация"},
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
    """Главная мини-аппа. Telegram открывает ровно этот адрес.

    Отдаём не файл как есть, а с метками сборки в ссылках на css/js — иначе
    WebView Telegram показывает вёрстку из своего кэша и выкат «не виден».
    Сам документ не кэшируем: только через него клиент узнаёт новые адреса.
    """
    index = settings.webapp_dir / "index.html"
    if not index.exists():
        raise web.HTTPNotFound()
    stamp = webapp_build.build_stamp(settings.webapp_dir)
    html = webapp_build.add_version(index.read_text("utf-8"), stamp)
    return web.Response(
        text=html,
        content_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


@routes.get("/app/app.js")
async def _webapp_bundle(request: web.Request) -> web.Response:
    """Бандл кабинета. Без метки сборки в адресе — отдаём не его, а спасателя.

    Свежий каркас всегда просит `app.js?v=<метка>`. Запрос без метки (или с
    чужой) приходит от старой копии `index.html` в кэше клиента: у неё прежняя
    разметка, и новый код в ней сломается. Такой копии отвечаем скриптом,
    который перезагружает кабинет по адресу с меткой — там каркас точно
    свежий, потому что это другой ключ кэша.
    """
    bundle = settings.webapp_dir / "app.js"
    if not bundle.exists():
        raise web.HTTPNotFound()
    stamp = webapp_build.build_stamp(settings.webapp_dir)
    if stamp and request.query.get("v") == stamp:
        return web.FileResponse(
            bundle,
            headers={
                "Content-Type": "application/javascript; charset=utf-8",
                "Cache-Control": webapp_build.cache_control_for(True),
            },
        )
    return web.Response(
        text=webapp_build.stale_shell_loader(stamp),
        content_type="application/javascript",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


@web.middleware
async def _webapp_cache_headers(request: web.Request, handler: Any) -> Any:
    """Проставляет статике мини-аппа `Cache-Control` (aiohttp его не ставит)."""
    response = await handler(request)
    if request.path.startswith("/app/") and "Cache-Control" not in response.headers:
        response.headers["Cache-Control"] = webapp_build.cache_control_for(
            bool(request.query.get("v"))
        )
    return response


def setup_webapp_routes(app: web.Application, bot: Any = None) -> None:
    """Подключает API и раздачу статики мини-аппа."""
    global _bot
    _bot = bot
    app.add_routes(routes)
    if _webapp_cache_headers not in app.middlewares:
        app.middlewares.append(_webapp_cache_headers)

    if settings.webapp_dir.exists():
        app.router.add_static("/app/", path=str(settings.webapp_dir), name="webapp")
        logger.info(
            "Мини-апп раздаётся из {} (метка сборки {})",
            settings.webapp_dir,
            webapp_build.build_stamp(settings.webapp_dir) or "нет файлов",
        )
    else:
        logger.warning("Папка мини-аппа не найдена: {}", settings.webapp_dir)
