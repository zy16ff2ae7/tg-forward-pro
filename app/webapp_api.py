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
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl

from aiogram.types import BufferedInputFile, LabeledPrice
from aiohttp import web
from loguru import logger

from app import accounts_login, bonus, exports, paylink, promocode, referral, webapp_build
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.errors import FeatureUnavailable, ValidationError, _dumps
from app.payments import service
from app.plans import (
    DEFAULT_MONTHS,
    PERIODS,
    STARS_DESCRIPTION,
    STARS_SUBSCRIPTION_PERIOD,
    apply_discount,
    is_valid_period,
    periods_text,
    rub_amount,
    stars_amount,
    usdt_amount,
)
from app.task_health import chat_names, error_text
from app.telegram_client.jobs import (
    MAX_PARSER_LIMIT,
    ONE_SHOT_KINDS,
    merge_scheduled_state,
    normalize_scheduled_posts,
    scheduled_pending,
    task_title,
    window_tz_minutes,
)
from app.telegram_client.manager import HOPELESS_ERRORS, manager
from app.telegram_client.filters import normalize_buttons
from app.translate import normalize_lang

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
    """initData принимаем только из заголовка X-Telegram-Init-Data.

    Query-вариант (?initData=...) оседал бы в access-логах nginx, истории
    браузера и Referer, а подпись валидна сутки — это готовый угон сессии.
    """
    return request.headers.get("X-Telegram-Init-Data") or ""


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


def _split_messages(value: Any) -> list[str]:
    """Текст из формы → список сообщений. Граница между ними — пустая строка.

    Раньше резали по каждому переносу строки, и любое многострочное сообщение —
    прайс, объявление в два абзаца — разлеталось на десяток отдельных отправок.
    Перенос строки внутри одного сообщения человек ставит гораздо чаще, чем
    хочет отправить второе сообщение, поэтому переносы остаются в тексте, а
    делит сообщения именно пустая строка. Внутренние переносы сохраняем как
    есть, обрезаем только края блока.
    """
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return [block.strip() for block in re.split(r"\n[ \t]*\n+", text) if block.strip()]


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

# Задачи, которые шлют СВОИ сообщения, а не чужие посты. Свои тексты у обеих
# лежат в одном месте — в библиотеке (`/api/library`), а в задаче остаются
# ссылки на записи. Раньше постинг держал копии текстов в своих настройках:
# одна и та же опечатка правилась дважды, правка записи в библиотеке до постинга
# не доходила, а библиотека не могла сказать, что запись кто-то отправляет.
OWN_TEXT_KINDS: frozenset[str] = frozenset({"poster", "mailing"})

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


# Простой in-memory rate limit для дорогих мутирующих эндпоинтов
# (защита от спама созданием/запуском задач поверх лимитов nginx).
# Декоратор обязан стоять ПОД @require_auth — user_id уже лежит в запросе.
_RATE_BUCKETS: dict[tuple[int, str], list[float]] = {}


def rate_limit(max_calls: int, period_seconds: int) -> Callable:
    """Не чаще max_calls вызовов за period_seconds на пользователя. Лишнее — 429."""

    def decorator(handler: Callable) -> Callable:
        name = getattr(handler, "__name__", "handler")

        async def wrapper(request: web.Request) -> web.StreamResponse:
            user_id = request[USER_ID_KEY]
            now = time.monotonic()
            key = (int(user_id or 0), f"{name}:{max_calls}:{period_seconds}")
            calls = _RATE_BUCKETS.setdefault(key, [])
            while calls and calls[0] <= now - period_seconds:
                calls.pop(0)
            if len(calls) >= max_calls:
                retry = int(calls[0] + period_seconds - now) + 1
                return _json(
                    {"error": f"Слишком часто. Повторите через {retry} сек."},
                    status=429,
                )
            calls.append(now)
            # Не даём словарю расти бесконечно (счётчики за прошлые периоды бесполезны)
            if len(_RATE_BUCKETS) > 10000:
                _RATE_BUCKETS.clear()
            return await handler(request)

        return wrapper

    return decorator


# ───────────────────────────────── Эндпоинты ──────────────────────────────────

STARTED_AT = time.time()


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
    db_size: int | None = None
    url = settings.database_url
    if url.startswith("sqlite"):
        path = url.rsplit("///", 1)[-1].split("?", 1)[0]
        try:
            db_size = Path(path).stat().st_size
        except OSError:
            db_size = None
    return _json(
        {
            "ok": True,
            "service": "tg-forward",
            # Метка сборки мини-аппа: по ней кабинет понимает, что держит в
            # руках старый бандл, и перезагружается сам (см. webapp/app.js).
            "build": webapp_build.build_stamp(settings.webapp_dir),
            "uptime_seconds": int(time.time() - STARTED_AT),
            "accounts_online": sum(1 for _ in manager.online_ids()),
            "db_size_bytes": db_size,
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
        autorenew = await repo.stars_autorenew(session, user_id)
        rules_count = await repo.count_rules(session, user_id, include_archived=False)
        accounts = await repo.list_accounts(session, user_id)
        forwarded = sum(
            rule.forwarded_count
            for rule in await repo.list_rules(session, user_id, include_archived=False)
        )
        db_user = await repo.get_user(session, user_id)
        referral_stats = await referral.info(session, user_id)

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
                "autorenew": autorenew,
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
            # Подарок за подписку на канал сервиса. Выключен настройками —
            # приходит enabled: false, и карточка в кабинете не появляется.
            "bonus": bonus.info(db_user.channel_bonus_at if db_user else None),
            # Реферальная программа: ссылка-приглашение и счёт.
            "referral": referral_stats,
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


# Шаблоны задач: задание с предзаполненной формой. Аккаунт, источник и чаты
# человек выбирает сам — в шаблоне только настройки, которые одинаковы у всех.
# Формат fill — ровно поля формы (см. needs/optional команды): кабинет открывает
# шторку создания с этими значениями, дальше человек правит и подтверждает.
TASK_TEMPLATES: list[dict] = [
    {
        "id": "mirror",
        "command": "clone",
        "emoji": "📋",
        "title": "Зеркало канала",
        "description": "Чужой канал — вашим: история и новые посты.",
        "fill": {"history": 50, "uniquify": True},
    },
    {
        "id": "deals",
        "command": "listener",
        "emoji": "🏷️",
        "title": "Охотник за скидками",
        "description": "Посты про выгоду — вам в чат.",
        "fill": {"keywords": "скидка, акция, промокод, розыгрыш, распродажа"},
    },
    {
        "id": "clean_copy",
        "command": "copy_channel",
        "emoji": "🔁",
        "title": "Чистая копия",
        "description": "Копия без метки «переслано», текст под себя.",
        "fill": {"mode": "copy", "uniquify": True},
    },
    {
        "id": "fanout",
        "command": "broadcast",
        "emoji": "📣",
        "title": "Веер новостей",
        "description": "Один источник — сразу во все ваши чаты.",
        "fill": {},
    },
    {
        "id": "commenters",
        "command": "parser",
        "emoji": "💬",
        "title": "Сбор комментаторов",
        "description": "Самые вовлечённые читатели — списком.",
        "fill": {"parser_mode": "comments", "scan": 500, "limit": 200},
    },
]


@routes.get("/api/templates")
@require_auth
async def task_templates(request: web.Request) -> web.Response:
    """Шаблоны задач для каталога: команда + предзаполненные поля формы."""
    return _json({"templates": TASK_TEMPLATES})


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
        # Журнал по всем задачам сразу: запрос на карточку превратил бы один
        # ответ в двадцать походов в базу.
        health = await repo.task_health(session, [rule.id for rule in rules])
        # Тексты своих сообщений (рассылка, постинг) — тем же одним запросом:
        # форма правки показывает текст, а он лежит в библиотеке.
        texts = await _library_texts(session, user_id, rules)

    return _json(
        {
            "tasks": [
                _task_view(rule, collected.get(rule.id), health.get(rule.id), texts)
                for rule in rules
            ]
        }
    )


async def _task_json(
    rule, *, collected: int | None = None, extra: dict | None = None, status: int = 200
) -> web.Response:
    """Ответ с одной задачей — тем же составом полей, что и в списке.

    Здоровье задачи читается здесь, а не в каждом обработчике: пять копий
    одного словаря разъезжались бы при любом новом поле.
    """
    async with SessionLocal() as session:
        health = await repo.task_health(session, [rule.id])
        texts = await _library_texts(session, rule.user_id, [rule])
    body: dict[str, Any] = {"task": _task_view(rule, collected, health.get(rule.id), texts)}
    if extra:
        body.update(extra)
    return _json(body, status=status)


# Задачи, у которых источника нет по устройству: 0 в source_id тут по делу —
# иначе входящее сообщение в чате-получателе подхватило бы рассылку как обычную
# пересылку. Название рядом видит человек в карточке, поэтому оно осмысленное.
NO_SOURCE_TITLE: dict[str, str] = {
    "poster": "постинг по расписанию",
    "mailing": "рассылка по очереди",
}
# Задачи, которые работают и без источника: подпись вместо пустого места.
EMPTY_SOURCE_TITLE: dict[str, str] = {
    "autosubscribe": "все чаты аккаунта",
    "dialogs": "личные диалоги",
}


def _remember_names(filters: dict, pairs: list[tuple[int, str] | None]) -> None:
    """Запоминает названия чатов задачи рядом с их id.

    В колонках правила есть названия только источника и первого чата, а
    остальные чаты — просто числа. Из-за этого задача на двадцать чатов не могла
    показать ни одного имени, а правка списка предлагала выбирать
    «-1001234567890». Названия уже получены обходом диалогов — второй раз
    спрашивать Telegram незачем, храним их в настройках задачи.
    """
    names = dict(filters.get("chat_titles") or {})
    for pair in pairs:
        if not pair:
            continue
        chat_id, title = pair
        if chat_id and title and title != str(chat_id):
            names[str(chat_id)] = title
    filters["chat_titles"] = names


def _stored_chats(rule) -> list[tuple[int, str]]:
    """Чаты задачи парами (id, название) — так, как их знает сама задача.

    Счёт чатов берём у планировщика (``chat_recipients``), названия — из
    запомненных: тогда правка списка не теряет первый чат из ``target_id`` и не
    требует нового обхода диалогов.
    """
    from app.telegram_client.jobs import chat_recipients

    names = dict((rule.filters or {}).get("chat_titles") or {})
    if rule.target_id and rule.target_title:
        names.setdefault(str(rule.target_id), rule.target_title)
    return [(chat_id, names.get(str(chat_id)) or str(chat_id)) for chat_id in chat_recipients(rule)]


async def _own_texts(payload: dict, filters: dict, *, user_id: int, partial: bool) -> None:
    """Что отправляет задача: текст из поля плюс сохранённые посты из библиотеки.

    Общее для рассылки и постинга (``OWN_TEXT_KINDS``): свои сообщения у обеих
    живут в библиотеке, поэтому набранный текст сначала становится её записями, а
    в задаче остаются ссылки на них. Один текст — одна запись: правка записи
    доходит сразу до всех задач, где она выбрана.

    Главное здесь — текст в поле. Раньше выбор из библиотеки был важнее, и в
    форме правки поле «Сообщение» стояло пустым: человек вписывал новый текст,
    видел «Сохранено», а рассылка продолжала слать старый — набранное молча
    уходило в библиотеку никому не нужной записью. Теперь поле показывает то,
    что уйдёт, и правка поля меняет рассылку.

    Записи без текста — это сохранённые посты, руками их не набрать: они
    приходят отдельным списком id (чипсы рядом с полем) и остаются при задаче,
    даже когда текст поменяли.

    Тот же текст копий не плодит: сначала ищем запись с ровно таким текстом и
    только потом добавляем новую — иначе библиотека после пяти правок интервала
    выглядела бы как пять одинаковых сообщений.
    """
    if partial and "message" not in payload and "library_ids" not in payload:
        return  # правка не про сообщения — оставляем как было

    def keep(ids: list[int]) -> None:
        """Ссылки на записи — в задачу, старые копии текстов — вон.

        ``filters["messages"]`` остался от постинга, который держал тексты у
        себя: не убрать его — и планировщик с карточкой продолжали бы читать
        старую копию, а правка библиотеки до чатов не доходила бы.
        """
        filters["library_ids"] = ids
        filters.pop("messages", None)

    # Пришедший список важнее прежнего: это и есть новый выбор. Не пришёл —
    # смотрим, что у задачи уже привязано.
    base = (
        _as_ids(payload.get("library_ids"))
        if "library_ids" in payload
        else _as_ids(filters.get("library_ids"))
    )
    msgs = _split_messages(payload.get("message"))
    async with SessionLocal() as session:
        # Удалённые из библиотеки записи отбрасываются сами: их здесь уже нет.
        rows = await repo.saved_messages_by_ids(session, user_id, base)
        if not msgs:
            # Текста нет — уйдут выбранные записи. Пустой список означает «вся
            # библиотека»: так его читает планировщик.
            keep([row.id for row in rows])
            return
        posts = [row.id for row in rows if not (row.text or "").strip()]
        texts = [row for row in rows if (row.text or "").strip()]
        if [row.text for row in texts] == msgs:
            keep([row.id for row in rows])  # текст не менялся
            return
        fresh: list[int] = []
        for text in msgs:
            item = await repo.find_saved_message_by_text(session, user_id, text)
            if item is None:
                item = await repo.add_saved_message(
                    session, user_id=user_id, title=_message_title(text), text=text
                )
            fresh.append(item.id)
        await session.commit()
    keep(fresh + posts)


async def _library_texts(session, user_id: int, rules) -> dict[int, str]:
    """Записи библиотеки, на которые ссылаются задачи: id → текст.

    Форма правки показывает текст рассылки и постинга, а лежит он в библиотеке —
    значит карточке нужны сами тексты, а не только номера записей. Читаем их
    одним запросом на все задачи: чтение на карточку превратило бы один ответ со
    списком задач в двадцать походов в базу.

    Записи без текста (сохранённые посты) остаются в ответе с пустой строкой:
    по ней ``_edit_view`` и отличает их от текста, который можно набрать.
    """
    wanted: set[int] = set()
    for rule in rules:
        if (rule.kind or "forward") not in OWN_TEXT_KINDS:
            continue
        wanted.update(_as_ids((rule.filters or {}).get("library_ids")))
    if not wanted:
        return {}
    rows = await repo.saved_messages_by_ids(session, user_id, sorted(wanted))
    return {row.id: row.text or "" for row in rows}


def _own_texts_state(conf, texts: dict[int, str] | None) -> dict:
    """Что уйдёт из библиотеки: сколько записей живо, что пропало, вся ли она.

    Одинаково для рассылки и постинга. Считаем только живые записи: сообщение
    могли удалить из библиотеки, а ссылка на него в задаче осталась — раньше
    карточка показывала прежний счёт, а отправлять было нечего. Когда текстов не
    передали (``texts=None``), счёт остаётся прежним — гадать не о чём.

    Старые задачи постинга держат тексты в своих настройках (``messages``): в
    библиотеку они переедут при первой правке, а пока считаем по ним — иначе
    карточка задачи, созданной до переезда, показывала бы ноль сообщений.
    """
    if conf.messages and not conf.library_ids:
        return {
            "messages_count": len(conf.messages),
            "whole_library": False,
            "messages_gone": 0,
        }
    ids = [int(value) for value in (conf.library_ids or [])]
    alive = ids if texts is None else [item_id for item_id in ids if item_id in texts]
    return {
        "messages_count": len(alive),
        # Пустой список записей означает «вся библиотека» — так его читает
        # планировщик. Карточка обязана сказать это словами: без пометки она
        # молчала о том, что уйдёт, а счёт сообщений показывал ноль.
        "whole_library": not ids,
        # Сколько ссылок повисло: карточка скажет, что сообщения удалены, —
        # иначе задача бодро «работает», а в чаты ничего не уходит.
        "messages_gone": len(ids) - len(alive),
    }


def _own_texts_edit(conf, texts: dict[int, str]) -> dict:
    """Свои сообщения для формы правки: {"message": текст, "library_ids": посты}.

    Обратная сборка текста: сообщения делит пустая строка — тем же правилом,
    каким их разбирал ``_split_messages``. Чипсами рядом остаются только записи
    без текста, сохранённые посты: их руками не набрать, поэтому они идут
    списком id. Старая задача постинга показывает текст из своих настроек, пока
    он не переехал в библиотеку, — иначе поле правки было бы пустым и первое же
    «Сохранить» стёрло бы то, что задача постит.
    """
    if conf.messages and not conf.library_ids:
        return {"message": "\n\n".join(conf.messages), "library_ids": []}
    ids = [int(value) for value in (conf.library_ids or [])]
    return {
        "message": "\n\n".join(
            texts[item_id] for item_id in ids if (texts.get(item_id) or "").strip()
        ),
        "library_ids": [
            item_id
            for item_id in ids
            if item_id in texts and not (texts[item_id] or "").strip()
        ],
    }


async def _apply_task_settings(
    kind: str,
    payload: dict,
    filters: dict,
    *,
    user_id: int,
    targets: list[str] | None = None,
    partial: bool = False,
) -> None:
    """Настройки задачи из тела запроса — в filters правила.

    Одна функция и на создание, и на правку. При создании отсутствующее поле
    получает значение по умолчанию, при правке (``partial=True``) остаётся
    прежним: человек, который поменял интервал, не должен потерять окно времени.
    Вторая копия этих же правил в обработчике правки разошлась бы с первой на
    первой же новой настройке — а настроек тут на десять типов задач.
    """

    def given(key: str) -> bool:
        """Поле пришло — или это создание, где у каждой настройки есть умолчание."""
        return key in payload or not partial

    if kind == "parser":
        # limit — сколько сохранить, scan — сколько перебрать: фильтры
        # отсеивают, и смотреть приходится больше, чем забираешь.
        if given("limit"):
            filters["limit"] = max(1, min(_as_int(payload.get("limit"), 200), MAX_PARSER_LIMIT))
        if given("scan"):
            filters["scan_limit"] = max(
                1, min(_as_int(payload.get("scan"), 1000), MAX_PARSER_LIMIT)
            )
        if given("parser_mode"):
            mode = str(payload.get("parser_mode") or "").strip().lower()
            filters["parser_mode"] = (
                mode if mode in ("participants", "history", "comments") else "participants"
            )
        for field, default in (
            ("require_username", True),
            ("exclude_admins", True),
            ("only_premium", False),
            ("only_with_photo", False),
            ("active_only", False),
        ):
            if given(field):
                value = payload.get(field)
                filters[field] = _as_bool(value) if field in payload else default
        if given("online_within_hours"):
            filters["online_within_hours"] = max(
                0, min(_as_int(payload.get("online_within_hours"), 0), 720)
            )
        if given("api_delay"):
            filters["api_delay"] = max(0, min(_as_int(payload.get("api_delay"), 0), 60))
        if given("invite_to"):
            filters["invite_to"] = str(payload.get("invite_to") or "").strip()
    elif kind == "autosubscribe":
        # Ссылки-приглашения храним как есть: вступать по ним будет сама задача.
        if targets is not None:
            filters["subscribe_to"] = targets
    elif kind == "baiting":
        if given("reaction"):
            filters["reaction"] = str(payload.get("reaction") or "").strip() or "👍"
    elif kind == "checks":
        if given("keywords"):
            filters["keywords"] = _as_list(payload.get("keywords"))
    elif kind == "mute":
        if given("keywords"):
            filters["keywords"] = _as_list(payload.get("keywords"))
        if given("banned_words"):
            filters["banned_words"] = _as_list(payload.get("banned_words"))
        if given("block_links"):
            filters["block_links"] = _as_bool(payload.get("block_links"))
        if given("max_warns"):
            filters["max_warns"] = max(0, min(_as_int(payload.get("max_warns"), 3), 10))
        if given("mute_hours"):
            filters["mute_hours"] = max(1, min(_as_int(payload.get("mute_hours"), 24), 720))
    elif kind == "listener":
        if given("keywords"):
            filters["keywords"] = _as_list(payload.get("keywords"))
        # Слова — смысл задачи: слушатель без слов — это пересылка, а молча
        # созданный «слушатель всего» завалил бы чат каждым постом источника.
        # Проверяем создание и явную очистку; чужие правки не трогаем.
        if (not partial or "keywords" in payload) and not [
            word for word in (filters.get("keywords") or []) if str(word).strip()
        ]:
            raise ValidationError("Слушателю нужны ключевые слова — без них это пересылка")
    elif kind == "dialogs":
        if given("keywords"):
            filters["keywords"] = _as_list(payload.get("keywords"))
        # Галочки включены из коробки: боты, архив и мут редко нужны в
        # уведомлениях. При создании отсутствующее поле — True, при правке —
        # «оставь как было».
        for field in ("ignore_bots", "ignore_archived", "ignore_muted"):
            if given(field):
                filters[field] = _as_bool(payload.get(field)) if field in payload else True
    elif kind == "poster":
        # Свои тексты постинга живут там же, где у рассылки, — в библиотеке
        # (см. _own_texts и OWN_TEXT_KINDS). Раньше постинг держал копии текстов
        # в своих настройках: та же опечатка правилась дважды, а правка записи в
        # библиотеке до чатов постинга не доходила вообще.
        await _own_texts(payload, filters, user_id=user_id, partial=partial)
        if given("interval"):
            filters["interval_seconds"] = max(1, _as_int(payload.get("interval"), 2)) * 60
        if given("start"):
            filters["window_start"] = str(payload.get("start") or "00:00")[:5]
        if given("end"):
            filters["window_end"] = str(payload.get("end") or "23:59")[:5]
        # Чьи часы у окна: смещение кабинета от UTC (его знает браузер человека).
        # Без него окно считалось по часам сервера — а он стоит в UTC, и
        # московское «окно 10:00–20:00» работало 13:00–23:00 по Москве.
        if given("tz"):
            filters["window_tz"] = window_tz_minutes(payload.get("tz"))
        # Расписание по датам вместо кругов: слоты проверяет normalize,
        # мусор отклоняется понятной ошибкой, а не чинится молча.
        if given("schedule_only"):
            value = payload.get("schedule_only")
            filters["schedule_only"] = _as_bool(value) if "schedule_only" in payload else False
        if "scheduled_posts" in payload or not partial:
            # Уже ушедшие даты правка присылает вместе с новыми — их состояние
            # переносим, иначе они воскресают и уходят по второму кругу.
            fresh = normalize_scheduled_posts(payload.get("scheduled_posts"))
            filters["scheduled_posts"] = merge_scheduled_state(
                filters.get("scheduled_posts"), fresh
            )
    elif kind == "mailing":
        for field, name, default in (
            ("gap", "gap_seconds", 5),
            ("gap_jitter", "gap_jitter", 0),
            ("cycle", "cycle_seconds", 10),
            ("cycle_jitter", "cycle_jitter", 0),
            ("repeats", "repeats", 1),
        ):
            if given(field):
                filters[name] = max(0, _as_int(payload.get(field), default))
        for field in ("typing", "random_pick", "link_preview"):
            if given(field):
                filters[field] = _as_bool(payload.get(field))
        await _own_texts(payload, filters, user_id=user_id, partial=partial)

    # Письма о проблемах — у всех фоновых задач разом: разовым человек и так
    # смотрит в лицо, а фоновые ломаются тихо. Выключается галочкой.
    if kind in ("forward", "broadcast", "poster", "mailing", "clone",
                "listener", "checks", "dialogs", "baiting", "mute"):
        if "alerts" in payload or not partial:
            filters["alerts"] = _as_bool(payload.get("alerts")) if "alerts" in payload else True
    # Кнопки-ссылки под постом — у всех, кто публикует копии: пересылка,
    # веерная отправка, постинг и рассылка. Мусор отклоняется с номером кнопки.
    if kind in ("forward", "broadcast", "poster", "mailing", "clone"):
        if "buttons" in payload or not partial:
            filters["buttons"] = normalize_buttons(payload.get("buttons"))

    # Перевод чужих постов — у пересылки и веера: свои тексты человек пишет
    # сразу на своём языке, переводить их не надо.
    if kind in ("forward", "broadcast", "clone"):
        if "translate_to" in payload or not partial:
            filters["translate_to"] = normalize_lang(payload.get("translate_to"))

    # Уникализация — там же, где перевод: чужой текст под своё авторство.
    if kind in ("forward", "broadcast", "clone"):
        if "uniquify" in payload or not partial:
            filters["uniquify"] = _as_bool(payload.get("uniquify"))
    # Клон: сколько постов истории забрать (0 — только новые, без прошлого).
    if kind == "clone":
        if "history" in payload or not partial:
            filters["clone_history"] = max(0, min(500, _as_int(payload.get("history"), 50)))


@routes.post("/api/tasks")
@require_auth
@rate_limit(20, 60)
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
    if command is None and command_id in ("poster", "mailing"):
        # Старые клиенты помнят две команды вместо единого слота: их id
        # понимаем — это тот же слот с уже выбранным режимом.
        legacy_mode = "queue" if command_id == "mailing" else "schedule"
        command_id = "sender"
        payload = {**payload, "send_mode": legacy_mode}
        command = COMMANDS_BY_ID["sender"]
    if command is not None:
        kind = command["kind"]
        if command_id == "sender":
            # Единый слот: механика — из переключателя формы. Очередь — только
            # явным выбором, всё остальное (и старые клиенты без поля) — постинг.
            send_mode = str(payload.get("send_mode") or "").strip().lower()
            if send_mode in ("queue", "mailing"):
                kind = "mailing"
    elif kind in VALID_KINDS:
        command = COMMANDS_BY_KIND.get(kind)
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
        # Рассылке и постингу текст в форме не нужен, если сообщения выбраны из
        # библиотеки: оттуда их и берёт планировщик, а копия того же текста в
        # поле только плодила бы дубли записей. Постеру по расписанию текст не
        # нужен вовсе: каждый слот несёт свой.
        has_schedule = kind == "poster" and bool(payload.get("scheduled_posts"))
        if not has_schedule and not (
            kind in OWN_TEXT_KINDS and _as_ids(payload.get("library_ids"))
        ):
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
        # Автоподписке ненайденный канал — норма, а не ошибка: в него как раз и
        # предстоит вступить, поэтому в диалогах аккаунта его нет и быть не
        # может. Ссылку-приглашение (t.me/+…) до вступления не разрешает вообще
        # никто. Такие ссылки уходят задаче как есть — их разбирает уже
        # `run_autosubscribe`, когда вступает. Ошибку по источнику при этом
        # по-прежнему возвращаем: из него читают посты, а значит доступ нужен.
        if kind == "autosubscribe" and not errors:
            missed = []
        elif not nowhere_to_send:
            return _json({"error": "; ".join([*errors, *_missed_text(missed)])}, status=404)

    source_id, source_title = found_source or (0, "")
    target_id, target_title = found_target or (0, "")

    if kind == "forward" and source_id and source_id == target_id:
        return _json({"error": "Источник и приёмник совпадают — пересылать некуда"}, status=400)

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

    if kind in NO_SOURCE_TITLE:
        source_id, source_title = 0, NO_SOURCE_TITLE[kind]
    elif not source_id and kind in EMPTY_SOURCE_TITLE:
        source_title = EMPTY_SOURCE_TITLE[kind]
    if kind == "parser":
        # приёмник парсеру не нужен, но колонка обязательна — пишем туда источник
        target_id, target_title = source_id, source_title

    # Названия всех чатов задачи — рядом с их id: карточка покажет имена, а
    # правка задачи не будет заново обходить диалоги ради того же списка.
    _remember_names(filters, [found_source, found_target, found_user, *chat_pairs])
    await _apply_task_settings(kind, payload, filters, user_id=user_id, targets=targets)

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

    return await _task_json(saved, extra={"run": run_result}, status=201)


@routes.patch(r"/api/tasks/{task_id:\d+}")
@require_auth
async def update_task(request: web.Request) -> web.Response:
    """Меняет настройки готовой задачи.

    Тело — те же поля, что и при создании (их список приходит в /api/commands:
    needs/optional), но применяются только пришедшие: остальные настройки,
    счётчики и место в круге рассылки остаются как были. Тип задачи и аккаунт не
    меняются — это была бы уже другая задача.

    Раньше правки не было вовсе: чтобы поменять интервал, текст или список
    чатов, задачу приходилось удалять и создавать заново — вместе с ней
    терялась вся статистика, а у рассылки ещё и место в круге.
    """
    user_id = request[USER_ID_KEY]
    task_id = _as_int(request.match_info.get("task_id"), 0)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "Нужен JSON"}, status=400)

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        kind = rule.kind or "forward"
        account_id = rule.account_id
        filters = dict(rule.filters or {})
        source_id, source_title = int(rule.source_id or 0), rule.source_title or ""
        target_id, target_title = int(rule.target_id or 0), rule.target_title or ""
        mode = rule.mode or "copy"
        archived = bool(rule.archived)
        stored_chats = _stored_chats(rule)

    if archived:
        # Архивная задача не работает, и менять её настройки — обещать человеку
        # то, чего не произойдёт. Сначала из архива, потом настройки.
        return _json(
            {"error": "Задача в архиве: верните её из архива, чтобы менять настройки"},
            status=409,
        )

    command = COMMANDS_BY_KIND.get(kind) or COMMANDS_BY_ID["copy_channel"]
    needs = set(command["needs"])

    source = str(payload.get("source") or "").strip()
    target = str(payload.get("target") or "").strip()
    target_user = str(payload.get("target_user") or "").strip()
    targets = _as_list(payload.get("targets")) if "targets" in payload else None
    # Одиночный «приёмник» у задач со списком чатов означает список из одного:
    # так присылают бот и формы, сделанные до списка чатов.
    if "targets" in needs and "target" not in needs and target and targets is None:
        targets, target = [target], ""

    # Пустое обязательное поле — это не «оставить как было», а попытка стереть
    # то, без чего задача не работает: отвечаем той же ошибкой, что при создании.
    missing: list[str] = []
    if "source" in payload and "source" in needs and not source:
        missing.append("источник")
    if "target" in payload and "target" in needs and not target:
        missing.append("приёмник")
    if "target_user" in payload and "target_user" in needs and not target_user:
        missing.append("человека, за которым следим")
    if targets is not None and "targets" in needs and not targets:
        missing.append(TARGETS_LABEL.get(kind, "чаты"))
    if "message" in payload and "message" in needs and not _split_messages(payload.get("message")):
        # Текст в поле не нужен, если сообщения взяты из библиотеки (рассылка,
        # постинг): пустое поле при выбранных записях — это «шлём выбранное».
        # Постеру по расписанию текст не нужен, если правка несёт даты.
        has_schedule = kind == "poster" and bool(payload.get("scheduled_posts"))
        if not has_schedule and not (
            kind in OWN_TEXT_KINDS and _as_ids(payload.get("library_ids"))
        ):
            missing.append("сообщение")
    if missing:
        return _json({"error": "Укажите: " + ", ".join(missing)}, status=400)

    # Ссылки, которые в задаче уже стоят, второй раз не ищем: у неё есть и id, и
    # название. Поэтому текст, интервал и порядок чатов правятся даже при
    # отключённом аккаунте — обход диалогов нужен только для НОВЫХ чатов.
    known: dict[str, tuple[int, str]] = {
        str(chat_id): (chat_id, title) for chat_id, title in stored_chats
    }
    names = dict(filters.get("chat_titles") or {})
    if source_id:
        known[str(source_id)] = (source_id, source_title or str(source_id))
    watched = int(filters.get("target_user_id") or 0)
    if watched:
        known[str(watched)] = (watched, names.get(str(watched)) or str(watched))

    wanted = [ref for ref in [source, target, target_user, *(targets or [])] if ref]
    unknown = [ref for ref in wanted if ref not in known]
    resolved: dict[str, tuple[int, str]] = {}
    if unknown:
        _require_account_login()
        resolved = await manager.resolve_many(account_id, unknown)

    errors: list[str] = []

    def _pick(ref: str, label: str) -> tuple[int, str] | None:
        if not ref:
            return None
        found = known.get(ref) or resolved.get(ref)
        if found is None:
            errors.append(f"Не нашёл {label}: {ref}")
        return found

    found_source = _pick(source, "источник")
    found_target = _pick(target, "приёмник")
    found_user = _pick(target_user, "пользователя")

    missed: list[str] = []
    chat_pairs: list[tuple[int, str]] = []
    if targets is None:
        chat_pairs = list(stored_chats)  # список чатов не правили — берём прежний
    else:
        for raw in targets:
            ref = str(raw).strip()
            found = known.get(ref) or resolved.get(ref)
            if found is None:
                missed.append(ref)
            else:
                chat_pairs.append(found)

    if errors or missed:
        nowhere_to_send = kind in ("mailing", "poster") and not chat_pairs
        if kind == "autosubscribe" and not errors:
            missed = []  # в эти каналы задача ещё только вступит — это норма
        elif not nowhere_to_send:
            return _json({"error": "; ".join([*errors, *_missed_text(missed)])}, status=404)

    if "source" in payload:
        source_id, source_title = found_source or (0, "")
    if "target" in payload and "target" in needs:
        target_id, target_title = found_target or (0, "")
    if found_user is not None:
        filters["target_user_id"] = found_user[0]
    if kind == "forward" and payload.get("mode") in ("copy", "forward"):
        mode = payload["mode"]

    # Дальше — та же геометрия чатов и те же колонки, что и при создании задачи.
    if kind in MULTI_CHAT_KINDS:
        chats = _split_chats(chat_pairs, source_id if kind == "broadcast" else 0)
        if not chats:
            return _json({"error": MULTI_CHAT_EMPTY[kind]}, status=400)
        target_id, target_title = chats[0]
        filters["targets"] = [pair[0] for pair in chats[1:]]
    elif kind == "autosubscribe" and targets is not None:
        filters["targets"] = [pair[0] for pair in chat_pairs]

    if kind in NO_SOURCE_TITLE:
        source_id, source_title = 0, NO_SOURCE_TITLE[kind]
    elif not source_id and kind in EMPTY_SOURCE_TITLE:
        source_title = EMPTY_SOURCE_TITLE[kind]
    if kind == "parser":
        target_id, target_title = source_id, source_title

    if kind in ("poster", "mailing"):
        # Единый слот: переключатель режима в форме правки меняет механику.
        # Это единственный разрешённый сдвиг типа — остальные задачи типа
        # не меняют (см. докстринг). Разбираем до настроек: ветка парсинга
        # зависит от kind.
        send_mode = str(payload.get("send_mode") or "").strip().lower()
        if send_mode in ("queue", "mailing"):
            kind = "mailing"
        elif send_mode in ("schedule", "poster"):
            kind = "poster"

    _remember_names(filters, [found_source, found_target, found_user, *chat_pairs])
    await _apply_task_settings(
        kind, payload, filters, user_id=user_id, targets=targets, partial=True
    )

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        rule.source_id, rule.source_title = source_id, source_title
        rule.target_id, rule.target_title = target_id, target_title
        rule.mode = mode
        rule.kind = kind
        rule.filters = filters
        if kind == "poster":
            # интервал постинга планировщик читает из delay_seconds
            rule.delay_seconds = int(filters.get("interval_seconds", 120))
        await session.commit()

    # Обработчики и планировщик держат снимок правил в памяти: без обновления
    # задача работала бы по старым настройкам до перезапуска службы.
    await manager.refresh_rules()

    async with SessionLocal() as session:
        saved = await repo.get_rule(session, task_id, user_id)
        collected = (
            await repo.count_collected_items(session, task_id) if kind == "parser" else None
        )
    return await _task_json(saved, collected=collected)


@routes.post(r"/api/tasks/{task_id:\d+}/toggle")
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
    return await _task_json(rule)


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


@routes.post(r"/api/tasks/{task_id:\d+}/mode")
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
    return await _task_json(rule)


@routes.post(r"/api/tasks/{task_id:\d+}/archive")
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
    return await _task_json(rule)


@routes.post("/api/tasks/bulk")
@require_auth
async def bulk_tasks(request: web.Request) -> web.Response:
    """Массовые действия: всё на паузу, всё запустить, всё в архив.

    Тело ``{"action": "pause_all" | "resume_all" | "archive_all"}``. Область
    действия решает сервер, а не клиент: пауза бьёт только по активным,
    запуск — только по стоящим на паузе, архив — только по неархивным
    неактивным. Активные задачи в архив пачкой не уезжают: это уже не
    уборка, а способ одним неверным тапом остановить весь сервис.

    Возвращает, сколько задач задело. Ноль — не ошибка: список мог быть пуст.
    """
    user_id = request[USER_ID_KEY]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    action = (body or {}).get("action") if isinstance(body, dict) else None
    if action not in ("pause_all", "resume_all", "archive_all"):
        raise ValidationError("Действие — pause_all, resume_all или archive_all")

    affected = 0
    async with SessionLocal() as session:
        rules = await repo.list_rules(session, user_id, include_archived=True)
        for rule in rules:
            kind = rule.kind or "forward"
            if action == "pause_all":
                if not rule.enabled or rule.archived:
                    continue
                rule.enabled = False
            elif action == "resume_all":
                if rule.enabled or rule.archived:
                    continue
                # Рассылка с исчерпанными кругами после включения молчала бы —
                # как в одиночном toggle, начинаем круги заново.
                if kind == "mailing" and _mailing_finished(rule):
                    rule.forwarded_count = 0
                rule.enabled = True
            else:
                if rule.archived or rule.enabled:
                    continue
                await repo.set_rule_archived(session, rule, True)
            affected += 1
        await session.commit()

    if affected:
        await manager.refresh_rules()
    logger.info("Массовое действие {} от {}: задето {}", action, user_id, affected)
    return _json({"action": action, "affected": affected})


@routes.post(r"/api/tasks/{task_id:\d+}/invite")
@require_auth
@rate_limit(10, 60)
async def invite_task(request: web.Request) -> web.Response:
    """Зовёт собранных парсером людей в чат из настроек — одну пачку.

    Пачками, а не всех разом: инвайт упирается в лимиты Telegram. Сколько
    ушло, скольких позвать нельзя и сколько осталось — в ответе.
    """
    user_id = request[USER_ID_KEY]
    task_id = int(request.match_info["task_id"])

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        if (rule.kind or "forward") != "parser":
            return _json({"error": "Приглашать умеет только парсер аудитории"}, status=409)
        if not rule.enabled or rule.archived:
            return _json({"error": "Задача не активна"}, status=409)

    _require_account_login()

    result = await manager.invite_task_now(rule)
    return _json({"invite": result})


@routes.post(r"/api/tasks/{task_id:\d+}/run")
@require_auth
@rate_limit(10, 60)
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


@routes.get(r"/api/tasks/{task_id:\d+}/results")
@require_auth
async def task_results(request: web.Request) -> web.Response:
    """Что насобирала задача: участники парсера или пойманные чеки.

    Отдаём страницами: ``offset`` — сколько уже показано. Парсер собирает до
    10 000 участников, а в шторку влезает сотня, и без сдвига остальное нельзя
    было даже досмотреть — кабинет всегда просил одну и ту же первую страницу.
    """
    user_id = request[USER_ID_KEY]
    task_id = int(request.match_info["task_id"])
    limit = max(1, min(_as_int(request.query.get("limit"), 100), 1000))
    offset = max(0, _as_int(request.query.get("offset"), 0))

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        items = list(
            await repo.list_collected_items(session, task_id, limit=limit, offset=offset)
        )
        total = await repo.count_collected_items(session, task_id)

    return _json(
        {
            "kind": rule.kind,
            "total": total,
            "offset": offset,
            "has_more": offset + len(items) < total,
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


@routes.post(r"/api/tasks/{task_id:\d+}/export")
@require_auth
async def export_task_results(request: web.Request) -> web.Response:
    """Присылает собранное файлом в чат с ботом.

    Не отдаём файл ответом на запрос: внутри Telegram кабинет живёт в WebView, а
    он скачанное не сохраняет — «Выгрузить» молча ничего не делало бы. Документ
    от бота попадает в переписку, откуда его достаёт любой клиент.

    Берём всё сразу, до потолка одного прогона парсера: смысл выгрузки как раз в
    том, чего не видно в шторке. Упёрлись в потолок — говорим об этом в подписи.
    """
    user_id = request[USER_ID_KEY]
    task_id = int(request.match_info["task_id"])
    tz = window_tz_minutes(request.query.get("tz"))

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        kind = rule.kind or "forward"
        title = task_title(rule)
        items = list(
            await repo.list_collected_items(session, task_id, limit=MAX_PARSER_LIMIT)
        )
        total = await repo.count_collected_items(session, task_id)

    if not items:
        return _json({"error": "Выгружать пока нечего — задача ничего не собрала"}, status=409)
    if _bot is None:
        raise FeatureUnavailable(
            "Файл присылает бот, а он сейчас недоступен. Попробуйте позже.",
            feature="export",
            status="bot_unavailable",
        )

    filename = exports.export_filename(kind, task_id)
    document = BufferedInputFile(
        exports.collected_csv(kind, items, tz_minutes=tz), filename=filename
    )
    try:
        await _bot.send_document(
            user_id,
            document,
            caption=exports.export_caption(kind, rule_title=title, sent=len(items), total=total),
        )
    except Exception as exc:  # noqa: BLE001
        # Подробности Bot API — в лог, человеку — куда смотреть: чаще всего файл
        # не уходит потому, что бота заблокировали или чат с ним удалили.
        logger.warning("Не удалось отправить выгрузку задачи {} для {}: {}", task_id, user_id, exc)
        return _json(
            {"error": "Телеграм не принял файл. Откройте чат с ботом и попробуйте снова."},
            status=502,
        )

    return _json({"ok": True, "sent": len(items), "total": total, "filename": filename})


# ─────────────────────── Библиотека сообщений (что рассылать) ─────────────────


def _message_title(text: str, limit: int = 48) -> str:
    """Короткое имя сообщения для списка библиотеки — его первая строка.

    Сообщение бывает многострочным (прайс, объявление в два абзаца), а в списке
    на него отведена одна строка: без этого в заголовок попадала середина
    второй строки, и записи выглядели одинаково обрезанными.
    """
    head = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
    return head[:limit] + ("…" if len(head) > limit else "")


def _library_view(item, used_by: Sequence[str] = ()) -> dict:
    """Сохранённое сообщение → вид для кабинета.

    ``used_by`` — названия задач, которые эту запись рассылают. Библиотека одна
    на все задачи, поэтому «убрать текст» здесь — это правка работающей
    рассылки: пока список этого не показывал, удаление выглядело безобидной
    уборкой, а задача оставалась без сообщений.
    """
    text = (item.text or "").strip()
    return {
        "id": item.id,
        "title": item.title or _message_title(text),
        "text": text,
        "chat_id": int(item.chat_id or 0),
        "message_id": int(item.message_id or 0),
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "used_by": list(used_by),
    }


async def _library_usage(session, user_id: int) -> tuple[dict[int, list[str]], list[str]]:
    """Кто отправляет записи библиотеки: (id записи → названия задач, «вся библиотека»).

    Считаем рассылку и постинг (``OWN_TEXT_KINDS``): свои сообщения у обеих лежат
    здесь, значит и предупредить при удалении надо про обе.

    Архивные задачи не считаем: они не работают, и пугать ими при удалении
    незачем. Задача без выбранных записей берёт всю библиотеку — такие идут
    вторым списком: они держат каждую запись, в том числе ту, которую добавят
    завтра.
    """
    from app.telegram_client.jobs import task_title

    used: dict[int, list[str]] = {}
    whole: list[str] = []
    for rule in await repo.list_rules(session, user_id, include_archived=False):
        if (rule.kind or "forward") not in OWN_TEXT_KINDS:
            continue
        filters = rule.filters or {}
        ids = _as_ids(filters.get("library_ids"))
        if not ids:
            # Старый постинг с текстами в своих настройках библиотеку не читает:
            # записать его во «всю библиотеку» значило бы пугать удалением
            # записи, которую он не отправляет.
            if filters.get("messages"):
                continue
            whole.append(task_title(rule))
            continue
        for item_id in ids:
            used.setdefault(item_id, []).append(task_title(rule))
    return used, whole


@routes.get("/api/library")
@require_auth
async def list_library(request: web.Request) -> web.Response:
    """Сохранённые сообщения: из них рассылка берёт тексты и посты."""
    user_id = request[USER_ID_KEY]

    async with SessionLocal() as session:
        items = list(await repo.list_saved_messages(session, user_id))
        used, whole = await _library_usage(session, user_id)

    return _json(
        {"items": [_library_view(item, [*used.get(item.id, []), *whole]) for item in items]}
    )



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
            title=_message_title(payload.get("title"), 128),
            text=text,
            chat_id=chat_id,
            message_id=message_id,
        )
        await session.commit()
        # Рассылка без выбранных записей берёт всю библиотеку: новая запись уже
        # стоит в её очереди, и человек должен видеть это сразу, а не по факту
        # отправки.
        _, whole = await _library_usage(session, user_id)
        view = _library_view(item, whole)

    return _json({"item": view}, status=201)


@routes.patch(r"/api/library/{item_id:\d+}")
@require_auth
async def update_library_item(request: web.Request) -> web.Response:
    """Правит сохранённое сообщение на месте: текст и название.

    Опечатку в тексте раньше можно было исправить только «удалить и добавить
    заново». Новая запись — это новый id, а рассылки помнят старый: задача молча
    оставалась без сообщения. Правка на месте id сохраняет, поэтому исправленный
    текст сразу уходит из всех задач, где эта запись выбрана, — очередь
    планировщик читает из библиотеки на каждом проходе.
    """
    user_id = request[USER_ID_KEY]
    item_id = _as_int(request.match_info.get("item_id"), 0)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "Нужен JSON"}, status=400)
    if not isinstance(payload, dict):
        return _json({"error": "Нужен JSON-объект"}, status=400)

    async with SessionLocal() as session:
        item = await repo.get_saved_message(session, item_id, user_id)
        if item is None:
            return _json({"error": "Сообщение не найдено"}, status=404)

        post = bool(int(item.chat_id or 0) and int(item.message_id or 0))
        if "text" in payload:
            text = str(payload.get("text") or "").strip()
            if not text:
                # Пустой текст — это удаление записи, а не правка: так и говорим.
                # Молча стереть его нельзя, рассылке было бы нечего отправлять.
                return _json(
                    {"error": "Текст пустой: чтобы убрать сообщение, удалите запись"},
                    status=400,
                )
            if post:
                # У готового поста своего текста нет — уходит сам пост из канала.
                # Подменив его текстом, мы бы тихо превратили запись в другую.
                return _json(
                    {"error": "Это готовый пост: его правят в канале, где он лежит"},
                    status=400,
                )
            # Название, собранное из прежнего текста, идёт за текстом: иначе в
            # списке осталась бы старая первая строка при новом тексте. Своё имя,
            # которое человек задал руками, не трогаем.
            follows_text = (item.title or "") == _message_title(item.text or "")
            item.text = text
            if follows_text and "title" not in payload:
                # Тем же вызовом, каким имя собирали при создании записи: с другой
                # длиной оно перестало бы совпадать с текстом и замерло навсегда.
                item.title = _message_title(text)
        if "title" in payload:
            item.title = _message_title(payload.get("title"), 128)
        await session.commit()
        used, whole = await _library_usage(session, user_id)
        view = _library_view(item, [*used.get(item.id, []), *whole])

    return _json({"item": view})


@routes.delete(r"/api/library/{item_id:\d+}")
@require_auth
async def delete_library_item(request: web.Request) -> web.Response:
    """Убирает сообщение из библиотеки. Задачи при этом не падают: рассылка
    просто берёт то, что осталось, а если не осталось ничего — пишет об этом на
    карточке («рассылать нечего») вместо бодрого «работает»."""
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


@routes.delete("/api/me/data")
@require_auth
@rate_limit(3, 3600)
async def delete_my_data(request: web.Request) -> web.Response:
    """«Удалить мои данные»: человек забирает всё, что оставлял в сервисе.

    Лимит жёсткий (3 раза в час): кнопка необратимая, а дёргать её скриптом
    незачем.
    """
    user_id = request[USER_ID_KEY]
    removed = await manager.forget_user(user_id)
    return _json({"ok": True, "removed": removed})


@routes.delete(r"/api/tasks/{task_id:\d+}")
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


@routes.get("/api/stats")
@require_auth
async def stats(request: web.Request) -> web.Response:
    """Сводка для экрана статистики: итоги, пересылки по дням, топ задач."""
    user_id = request[USER_ID_KEY]
    try:
        days = int(request.query.get("days") or 14)
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, 90))

    async with SessionLocal() as session:
        agg = await repo.forward_stats(session, user_id, days)
        rules = list(await repo.list_rules(session, user_id, include_archived=False))
        accounts = list(await repo.list_accounts(session, user_id))
        until = await repo.subscription_until(session, user_id)

    today = repo.utcnow().date()
    per_day = [
        {
            "date": (today - timedelta(days=offset)).isoformat(),
            "count": agg["per_day"].get((today - timedelta(days=offset)).isoformat(), 0),
            "errors": agg["errors_day"].get((today - timedelta(days=offset)).isoformat(), 0),
        }
        for offset in range(days - 1, -1, -1)
    ]
    top = sorted(rules, key=lambda r: r.forwarded_count or 0, reverse=True)[:5]
    return _json(
        {
            "totals": {
                "rules": len(rules),
                "accounts": len(accounts),
                "forwarded": sum(r.forwarded_count or 0 for r in rules),
                "forwarded_days": agg["total"],
                "errors_days": agg["errors"],
            },
            "subscription": {
                "active": until is not None,
                "until": until.isoformat() if until else None,
            },
            "per_day": per_day,
            "top_rules": [
                {
                    "id": r.id,
                    "title": _task_view(r)["title"],
                    "forwarded": r.forwarded_count or 0,
                }
                for r in top
            ],
        }
    )


@routes.get("/api/activity/hours")
@require_auth
async def activity_hours(request: web.Request) -> web.Response:
    """Активность ленты по часам: когда жить, тогда и постить.

    Параметры: ``days`` (1–90, по умолчанию 14) и ``tz`` — сдвиг в минутах
    от UTC, в котором считать часы (по умолчанию 0). Часы кабинета шлёт
    свои: иначе «постите в 9» прилетело бы не в те девять.
    """
    user_id = request[USER_ID_KEY]
    try:
        days = int(request.query.get("days") or 14)
    except (TypeError, ValueError):
        days = 14
    try:
        tz_offset = int(request.query.get("tz") or 0)
    except (TypeError, ValueError):
        tz_offset = 0

    async with SessionLocal() as session:
        result = await repo.activity_hours(session, user_id, days, tz_offset)
    return _json({**result, "days": max(1, min(days, 90)), "tz": tz_offset})


@routes.get("/api/activity")
@require_auth
async def activity(request: web.Request) -> web.Response:
    """Лента последних срабатываний: какая задача, где и чем закончилось."""
    user_id = request[USER_ID_KEY]
    limit = _as_int(request.query.get("limit"), 30)

    async with SessionLocal() as session:
        logs = list(await repo.recent_logs(session, user_id, limit))
        titles = {r.id: _task_view(r)["title"] for r in await repo.list_rules(session, user_id)}

    return _json(
        {
            "items": [
                {
                    "rule_id": row.rule_id,
                    "rule_title": titles.get(row.rule_id, f"Задача #{row.rule_id}"),
                    "status": row.status,
                    "error": (row.error or "")[:160] if row.status != "ok" else None,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in logs
            ]
        }
    )


@routes.post(r"/api/tasks/{task_id:\d+}/duplicate")
@require_auth
@rate_limit(20, 60)
async def duplicate_task(request: web.Request) -> web.Response:
    """Копия задачи: те же источник/приёмник/настройки, но на паузе."""
    user_id = request[USER_ID_KEY]
    task_id = int(request.match_info["task_id"])

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        rules_count = await repo.count_rules(session, user_id, include_archived=False)
        if not await repo.has_active_subscription(session, user_id):
            if rules_count >= settings.max_rules_free:
                return _json(
                    {
                        "error": f"Без абонемента доступно только {settings.max_rules_free} правила",
                        "need_subscription": True,
                    },
                    status=402,
                )
        clone = await repo.duplicate_rule(session, rule)
        await session.commit()
        clone_id = clone.id

    await manager.refresh_rules()
    async with SessionLocal() as session:
        saved = await repo.get_rule(session, clone_id, user_id)
    return _json({"task": _task_view(saved)}, status=201)


@routes.post(r"/api/tasks/{task_id:\d+}/test")
@require_auth
@rate_limit(6, 60)
async def test_task(request: web.Request) -> web.Response:
    """Тестовый пост в приёмник задачи — проверка, что аккаунт может писать."""
    user_id = request[USER_ID_KEY]
    task_id = int(request.match_info["task_id"])

    async with SessionLocal() as session:
        rule = await repo.get_rule(session, task_id, user_id)
        if rule is None:
            return _json({"error": "Задача не найдена"}, status=404)
        if not isinstance(rule.target_id, int) or not rule.target_id:
            return _json({"error": "У задачи нет приёмника"}, status=409)
        if not await repo.has_active_subscription(session, user_id):
            return _json(
                {
                    "error": "Тестовый пост доступен с абонементом",
                    "need_subscription": True,
                },
                status=402,
            )

    _require_account_login()
    result = await manager.send_test_post(rule.account_id, rule.target_id)
    if not result["ok"]:
        return _json({"error": result["error"]}, status=409)
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
                # Мёртвую сессию повтором не оживить — кабинету надо предлагать
                # не «попробовать снова», а вход по номеру заново.
                "needs_login": account.last_error in HOPELESS_ERRORS,
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
                # Автопродление за Stars: рекуррентное списание живо.
                "autorenew": bool(banked is not None and banked.stars_autorenew),
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
    """Шаг 1: {"phone": "+79001234567"} → Telegram присылает код.

    {"phone": ..., "resend": true} — «код не пришёл»: повтор тем же способом,
    каким Telegram шлёт дальше (приложение → SMS → звонок). Код из прошлого
    сообщения после повтора мёртв.
    """
    body = await _login_body(request)
    step = await accounts_login.start(
        request[USER_ID_KEY], body.get("phone"), resend=bool(body.get("resend"))
    )
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


@routes.post(r"/api/accounts/{account_id:\d+}/retry")
@require_auth
async def retry_account(request: web.Request) -> web.Response:
    """Ещё одна попытка поднять аккаунт: кнопка «Попробовать снова» в кабинете."""
    account_id = _as_int(request.match_info.get("account_id"), 0)
    result = await accounts_login.retry(request[USER_ID_KEY], account_id)
    return _json({"ok": True, **result})


@routes.delete(r"/api/accounts/{account_id:\d+}")
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
@rate_limit(6, 60)
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
    # Автопродление — только помесячно: период подписки в звёздах всегда 30 дней.
    autorenew = body.get("autorenew") is True
    if autorenew and months != 1:
        raise ValidationError("Автопродление работает только на сроке 1 месяц")

    # Подарок: id или @username друга. Друг должен быть в базе (первый /start),
    # себе дарить нельзя, автопродление в подарок не заворачивается.
    gift_to = None
    raw_gift = body.get("gift_to")
    if raw_gift:
        if autorenew:
            raise ValidationError(
                "Подарок — только разовым счётом: дарить автопродление нельзя"
            )
        token = str(raw_gift).strip()
        async with SessionLocal() as session:
            if token.isdigit():
                gift_to = await repo.get_user(session, int(token))
            else:
                gift_to = await repo.get_user_by_username(session, token)
        if gift_to is None:
            raise ValidationError(
                "Не нашли такого друга: пусть сначала запустит бота (/start)"
            )
        if gift_to.id == user_id:
            raise ValidationError("Себе дарить не надо — оформите абонемент как обычно")

    if _bot is None:
        raise FeatureUnavailable(
            "Оплата звёздами временно недоступна",
            feature="stars",
            status="bot_unavailable",
        )

    if gift_to is not None:
        title = f"Подарок: абонемент на {months} мес."
    else:
        title = "Абонемент на 1 месяц" if months == 1 else f"Абонемент на {months} мес."
    amount = stars_amount(months)
    discount = 0
    description = STARS_DESCRIPTION
    if not autorenew:
        # Разовый счёт дешевле, если ждёт скидка. Автопродлению скидок нет:
        # Telegram списывает по первому счёту каждый месяц — разовая скидка
        # стала бы вечной.
        async with SessionLocal() as session:
            pending = await repo.pending_discount(session, user_id)
        if pending is not None:
            discount = int(pending.percent or 0)
            amount = int(apply_discount(amount, discount))
            description = f"{STARS_DESCRIPTION} Скидка {discount}% по промокоду."
    try:
        link = await _bot.create_invoice_link(
            title=title,
            description=description,
            # Формат читает хендлер successful_payment в боте — менять нельзя.
            payload=(
                f"gift:{user_id}:{gift_to.id}:{months}"
                if gift_to is not None
                else f"sub:{user_id}:{months}"
            ),
            provider_token="",  # для Stars платёжный токен не нужен
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=amount)],
            # Подписка отличается от разового счёта одним параметром: дальше
            # Telegram списывает сам, а продлевает тот же хендлер оплаты.
            **({"subscription_period": STARS_SUBSCRIPTION_PERIOD} if autorenew else {}),
        )
    except Exception as exc:  # noqa: BLE001
        # Пользователю подробности Bot API ни к чему, а в лог они попасть должны.
        logger.warning("Не удалось создать инвойс Stars для {}: {}", user_id, exc)
        raise FeatureUnavailable(
            "Не удалось создать счёт. Попробуйте позже.",
            feature="stars",
            status="invoice_failed",
        ) from exc

    return _json(
        {
            "url": link,
            "months": months,
            "amount": amount,
            "currency": "XTR",
            "autorenew": autorenew,
            "discount_percent": discount,
            "gift_to": gift_to.id if gift_to is not None else None,
        }
    )


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


@routes.post("/api/subscription/bonus")
@require_auth
async def claim_bonus(request: web.Request) -> web.Response:
    """Подарок за подписку на канал сервиса: проверить и начислить.

    Тела у запроса нет — что дарим и за какой канал, решает сервер: цифру дней
    с клиента принимать нельзя. Отказы разведены по кодам (см.
    ``bonus.HTTP_STATUS``): 403 — «подпишитесь», 409 — «уже получено»,
    503 — «проверить не смогли». Кабинету это нужно, чтобы не звать человека
    подписываться на канал, в котором он уже стоит.
    """
    user_id = request[USER_ID_KEY]
    tg_user = request[TG_USER_KEY]

    async with SessionLocal() as session:
        # Кабинет мог открыться сразу на «Аккаунтах», минуя /api/me, — тогда
        # строки пользователя ещё нет, и начислять было бы некому.
        await repo.get_or_create_user(
            session,
            user_id=user_id,
            username=tg_user.get("username"),
            full_name=" ".join(
                part
                for part in (tg_user.get("first_name"), tg_user.get("last_name"))
                if part
            )
            or tg_user.get("username"),
        )
        await session.commit()

        result = await bonus.claim(session, _bot, user_id)
        if result.granted:
            await session.commit()
        else:
            await session.rollback()

    payload: dict[str, Any] = {
        "status": result.status,
        "granted": result.granted,
        "days": result.days,
        "until": result.until.isoformat() if result.until else None,
        "channel": bonus.channel(),
        "url": settings.bonus_url or "",
        "message": bonus.message(result),
    }
    if not result.granted:
        # Тот же текст ещё и в error: кабинет и старые клиенты показывают
        # именно его, не разбирая status.
        payload["error"] = payload["message"]
    logger.info("Подарок за подписку {}: {}", user_id, result.status)
    return _json(payload, status=bonus.HTTP_STATUS.get(result.status, 503))


@routes.post("/api/subscription/promo")
@require_auth
async def redeem_promo(request: web.Request) -> web.Response:
    """Промокод из кабинета: тело ``{"code": "ЛЕТО"}``.

    Начисление — то же, что в боте (``app/promocode.py``): дни и счётчик кода
    живут в одном месте, а не в двух копиях. Отказы разведены по кодам (см.
    ``promocode.HTTP_STATUS``): 404 — «нет такого», 409 — «уже ваш / разобран»,
    410 — «срок вышел».
    """
    user_id = request[USER_ID_KEY]
    tg_user = request[TG_USER_KEY]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json({"error": "Нужен JSON"}, status=400)
    code = str((body or {}).get("code") or "").strip()
    if not code:
        return _json({"error": "Пришлите код", "status": "unknown"}, status=400)

    async with SessionLocal() as session:
        await repo.get_or_create_user(
            session,
            user_id=user_id,
            username=tg_user.get("username"),
            full_name=" ".join(
                part
                for part in (tg_user.get("first_name"), tg_user.get("last_name"))
                if part
            )
            or tg_user.get("username"),
        )
        await session.commit()

        result = await promocode.redeem(session, user_id, code)
        if result.granted:
            await session.commit()
        else:
            await session.rollback()

    payload: dict[str, Any] = {
        "status": result.status,
        "granted": result.granted,
        "days": result.days,
        "until": result.until.isoformat() if result.until else None,
        "percent": result.percent,
        "message": promocode.message(result),
    }
    if not result.granted:
        payload["error"] = payload["message"]
    logger.info("Промокод {} от {}: {}", code, user_id, result.status)
    return _json(payload, status=promocode.HTTP_STATUS.get(result.status, 503))


_bot: Any = None

# Username бота почти не меняется — не дёргаем Bot API на каждый /api/accounts.
_bot_username_cache: str | None = None


async def _bot_username() -> str | None:
    """Username бота — нужен для кнопки «Открыть в боте»."""
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    if _bot is None:
        return None
    try:
        me = await _bot.get_me()
    except Exception:  # noqa: BLE001
        return None
    if me.username:
        _bot_username_cache = me.username
    return me.username


def _task_view(
    rule,
    collected: int | None = None,
    health: dict | None = None,
    texts: dict[int, str] | None = None,
) -> dict:
    """Правило → вид задачи для мини-аппа.

    ``collected`` — сколько записей задача уже собрала (только для парсера,
    считает вызывающий, пока открыта сессия).
    ``health`` — чем закончились последние срабатывания (``repo.task_health``):
    без него карточка бодро показывала «работает» задаче, которая последние
    сутки только падает, а причину было видно лишь в логе службы на сервере.
    ``texts`` — тексты записей библиотеки (``_library_texts``): из них форма
    правки собирает поле «Сообщение» рассылки.
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
    view["buttons_count"] = len(
        [item for item in (conf.buttons or []) if isinstance(item, dict)]
    )
    view["translate_to"] = conf.translate_to or ""
    view["uniquify"] = bool(conf.uniquify)
    if kind == "listener":
        view["keywords_count"] = len(
            [word for word in (conf.keywords or []) if str(word).strip()]
        )
    if kind in ("forward", "broadcast", "poster", "mailing", "clone",
                "listener", "checks", "dialogs", "baiting", "mute"):
        view["alerts"] = bool(getattr(conf, "alerts", True))
    if kind == "mute":
        view["banned_count"] = len(
            [word for word in (conf.banned_words or []) if str(word).strip()]
        )
    if kind in ("broadcast", "poster", "mailing"):
        # Сколько мёртвых чатов задача уже убрала сама: авто-уборка обязана
        # быть видна, иначе пропавшие получатели выглядят как баг.
        view["chats_pruned"] = int(getattr(conf, "chats_pruned", 0) or 0)
    if kind == "clone":
        view["clone_done"] = bool(conf.clone_done)
        view["clone_left"] = len(conf.clone_ids or [])
        view["clone_history"] = int(conf.clone_history or 0)
    if kind == "poster":
        view["interval_min"] = max(1, conf.interval_seconds // 60)
        view["window_start"] = conf.window_start
        view["window_end"] = conf.window_end
        # Чьи часы у окна: смещение хозяина от UTC в минутах. null — часы
        # сервера (он стоит в UTC), и карточка обязана сказать это словами:
        # иначе «окно 10:00–20:00» у московского хозяина читается как его
        # собственное время, а работает на три часа позже.
        view["window_tz"] = window_tz_minutes(conf.window_tz)
        # Тексты постинга лежат в библиотеке — как у рассылки, тем же счётом.
        view.update(_own_texts_state(conf, texts))
        # Расписание по датам: сколько дат всего, сколько ждут, ближайшая.
        slots = [s for s in (conf.scheduled_posts or []) if isinstance(s, dict)]
        pending = scheduled_pending(slots)
        view["schedule_only"] = bool(conf.schedule_only)
        view["scheduled_total"] = len(slots)
        view["scheduled_pending"] = len(pending)
        view["scheduled_next"] = pending[0].get("at") if pending else None

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
    elif kind == "poster" and conf.schedule_only and conf.scheduled_posts:
        # У расписания «всего» — число дат: полоса показывает, сколько ушло.
        slots = [s for s in conf.scheduled_posts if isinstance(s, dict)]
        total = len(slots)
        done = sum(1 for s in slots if s.get("sent"))
    elif kind == "mailing":
        # У рассылки «всего» есть: получатели × число кругов. Без ограничения
        # кругов (repeats=0) конца нет — тогда и total остаётся null, как у
        # остальных бесконечных задач.
        recipients = len(chats)
        view["mailing"] = {
            "recipients": recipients,
            # Что уйдёт (счёт живых записей, повисшие ссылки, «вся библиотека») —
            # общим счётом с постингом: свои сообщения обеих задач в библиотеке.
            **_own_texts_state(conf, texts),
            "gap_seconds": conf.gap_seconds,
            "cycle_seconds": conf.cycle_seconds,
            "repeats": conf.repeats,
            "typing": bool(conf.typing),
            "random_pick": bool(conf.random_pick),
        }
        if conf.repeats > 0 and recipients:
            total = recipients * int(conf.repeats)
    view["progress"] = {"done": done, "total": total}
    # Названия чатов задачи — из общего ``app.task_health``: карточка в боте
    # называет чаты в причине сбоя теми же словами, что и кабинет.
    names = chat_names(rule)
    # Сколько чатов у задачи — одним полем на все задачи «в несколько чатов»:
    # у пересылки счёт раньше шёл по filters и терял первый чат из target_id.
    if kind in MULTI_CHAT_KINDS:
        view["targets_count"] = len(chats)
        view["chats"] = [
            {"id": chat_id, "title": names.get(str(chat_id)) or str(chat_id)}
            for chat_id in chats
        ]
    view["health"] = _health_view(health, names)
    view["edit"] = _edit_view(rule, kind, conf, chats, names, texts or {})
    return view


def _utc_iso(moment: datetime | None) -> str | None:
    """Время из БД → строка с явной пометкой UTC.

    В базе даты лежат без tzinfo, а браузер строку без пометки читает как
    местное время: «5 минут назад» превращалось бы в «3 часа назад» ровно на
    разницу часовых поясов.
    """
    if moment is None:
        return None
    return moment.replace(tzinfo=timezone.utc).isoformat()


def _health_view(health: dict | None, names: dict[str, str]) -> dict:
    """Здоровье задачи для карточки: когда сработала и на чём сломалась.

    Ключ есть всегда, даже когда журнал пуст: кабинету не приходится угадывать,
    «нет сбоев» это или «сервер не прислал». Причину сбоя причёсывает общий
    ``app.task_health``: теми же словами её показывает карточка задачи в боте.
    """
    health = health or {}
    error = error_text(health.get("error"), names)
    return {
        "ok_at": _utc_iso(health.get("ok_at")),
        "error": error or None,
        "error_at": _utc_iso(health.get("error_at")),
        # Сломана ли задача сейчас: после сбоя не было ни одного успеха.
        "failing": bool(health.get("failing")),
    }


def _edit_view(
    rule, kind: str, conf, chats: list[int], names: dict[str, str], texts: dict[int, str]
) -> dict:
    """Значения задачи для формы правки — ровно те, что принимает /api/tasks.

    Форма правки в кабинете — это форма создания с подставленными значениями,
    поэтому и поля здесь называются так же, как в теле запроса: второй набор
    имён означал бы второй разбор на сервере и вечные расхождения между ними.

    ``texts`` — тексты записей библиотеки (``_library_texts``): их показывают в
    форме правки рассылка и постинг — свои сообщения обеих лежат в библиотеке.
    """
    edit: dict[str, Any] = {"account_id": rule.account_id, "names": names}
    if kind in MULTI_CHAT_KINDS:
        edit["targets"] = [str(chat_id) for chat_id in chats]
    elif kind == "autosubscribe":
        # У автоподписки список — это ссылки, по которым она вступает: id у
        # ненайденного канала ещё нет, и подставлять в форму нечего кроме них.
        edit["targets"] = [str(ref) for ref in (conf.subscribe_to or [])]
    if rule.source_id:
        edit["source"] = str(rule.source_id)
    if rule.target_id and kind not in MULTI_CHAT_KINDS and kind != "parser":
        edit["target"] = str(rule.target_id)
    if conf.target_user_id:
        edit["target_user"] = str(conf.target_user_id)
    if kind in ("forward", "broadcast", "poster", "mailing", "clone",
                "listener", "checks", "dialogs", "baiting", "mute"):
        edit["alerts"] = bool(getattr(conf, "alerts", True))
    if kind in ("forward", "broadcast", "poster", "mailing", "clone"):
        # Кнопки под постом — как есть: форма показывает их строками.
        edit["buttons"] = [
            {"text": item.get("text"), "url": item.get("url")}
            for item in (conf.buttons or [])
            if isinstance(item, dict)
        ]
    if kind in ("forward", "broadcast", "clone"):
        edit["translate_to"] = conf.translate_to or ""
    if kind in ("forward", "broadcast", "clone"):
        edit["uniquify"] = bool(conf.uniquify)
    if kind == "clone":
        edit["history"] = int(conf.clone_history or 0)
    if kind == "forward":
        edit["mode"] = rule.mode
    elif kind == "parser":
        edit["parser_mode"] = conf.parser_mode or "participants"
        edit["invite_to"] = conf.invite_to or ""
        edit["scan"] = int(conf.scan_limit or 1000)
        edit["limit"] = int(conf.limit or 200)
        edit["require_username"] = bool(conf.require_username)
        edit["exclude_admins"] = bool(conf.exclude_admins)
        edit["only_premium"] = bool(conf.only_premium)
        edit["only_with_photo"] = bool(conf.only_with_photo)
        edit["active_only"] = bool(conf.active_only)
        edit["online_within_hours"] = int(conf.online_within_hours or 0)
        edit["api_delay"] = int(conf.api_delay or 0)
    elif kind == "baiting":
        edit["reaction"] = conf.reaction
    elif kind == "checks":
        edit["keywords"] = ", ".join(conf.keywords or [])
    elif kind == "mute":
        edit["keywords"] = ", ".join(conf.keywords or [])
        edit["banned_words"] = ", ".join(conf.banned_words or [])
        edit["block_links"] = bool(conf.block_links)
        edit["max_warns"] = int(conf.max_warns or 0)
        edit["mute_hours"] = int(conf.mute_hours or 24)
    elif kind == "listener":
        edit["keywords"] = ", ".join(conf.keywords or [])
    elif kind == "dialogs":
        edit["keywords"] = ", ".join(conf.keywords or [])
        edit["ignore_bots"] = bool(conf.ignore_bots)
        edit["ignore_archived"] = bool(conf.ignore_archived)
        edit["ignore_muted"] = bool(conf.ignore_muted)
    elif kind == "poster":
        # Текст постинга лежит в библиотеке — тем же полем и тем же правилом
        # «пустая строка делит сообщения», что у рассылки.
        edit.update(_own_texts_edit(conf, texts))
        edit["interval"] = max(1, conf.interval_seconds // 60)
        edit["start"] = conf.window_start
        edit["end"] = conf.window_end
        # Смещение окна от UTC — тем же именем, каким его принимает /api/tasks.
        # Кабинет присылает своё при каждом сохранении, но форме оно нужно и
        # прежним: по нему видно, чьи часы у задачи сейчас.
        edit["tz"] = window_tz_minutes(conf.window_tz)
        edit["send_mode"] = "schedule"
        # Единый слот: форма правки одна на обе механики, поэтому отдаём и
        # поля очереди — с текущими значениями (у постинга это умолчания).
        # Иначе переключение режима в правке показывало бы пустоту.
        edit["gap"] = conf.gap_seconds
        edit["cycle"] = conf.cycle_seconds
        edit["repeats"] = conf.repeats
        edit["typing"] = bool(conf.typing)
        edit["random_pick"] = bool(conf.random_pick)
        edit["link_preview"] = bool(conf.link_preview)
        # Расписание — как есть, для редактора дат (уже ушедшие — с меткой,
        # чтобы форма не предлагала править прошлое).
        edit["schedule_only"] = bool(conf.schedule_only)
        edit["scheduled_posts"] = [
            {
                "id": s.get("id"),
                "at": s.get("at"),
                "text": s.get("text") or "",
                "library_id": s.get("library_id"),
                "sent": bool(s.get("sent")),
            }
            for s in (conf.scheduled_posts or [])
            if isinstance(s, dict)
        ]
    elif kind == "mailing":
        # Текст рассылки лежит в библиотеке, но правят его здесь: поле показывает
        # то, что уйдёт, — как у постинга. Раньше поле стояло пустым, а набранный
        # в нём текст пропадал.
        edit.update(_own_texts_edit(conf, texts))
        edit["gap"] = conf.gap_seconds
        edit["cycle"] = conf.cycle_seconds
        edit["repeats"] = conf.repeats
        edit["typing"] = bool(conf.typing)
        edit["random_pick"] = bool(conf.random_pick)
        edit["link_preview"] = bool(conf.link_preview)
        edit["send_mode"] = "queue"
        # И наоборот: поля расписания с умолчаниями — для переключения режима
        # (редактор дат тех же слотов, что у постинга: отправляет общий воркер).
        edit["interval"] = max(1, conf.interval_seconds // 60)
        edit["start"] = conf.window_start
        edit["end"] = conf.window_end
        edit["tz"] = window_tz_minutes(conf.window_tz)
        edit["schedule_only"] = bool(conf.schedule_only)
        edit["scheduled_posts"] = [
            {
                "id": s.get("id"),
                "at": s.get("at"),
                "text": s.get("text") or "",
                "library_id": s.get("library_id"),
                "sent": bool(s.get("sent")),
            }
            for s in (conf.scheduled_posts or [])
            if isinstance(s, dict)
        ]
    return edit


# Каталог команд мини-аппа.
# kind — тип задачи в app.telegram_client.jobs; needs/optional — поля формы,
# по ним фронтенд собирает шторку создания и проверяет обязательность.
# group — блок каталога (см. COMMAND_GROUPS ниже): десять команд одним списком
# читались как свалка, поэтому кабинет раскладывает их по смыслу.
# tags — две-три метки в подвале карточки. Четыре команды «в несколько чатов»
# по описанию читались как одна и та же задача («кажется это всё одно и то
# же»), поэтому у каждой в метках стоит ровно то, чем она отличается от
# соседней: ЧЕЙ текст уходит и КАК он расходится по чатам.
COMMANDS: list[dict] = [
    {
        "id": "sender",
        "group": "own",
        # Один слот на две механики: расписание и очередь — это «как слать»,
        # а не разные задачи. Точный kind выбирает send_mode из формы
        # (см. create_task); обе механики шлют свои тексты из общей библиотеки.
        # Слот первый в каталоге: своими сообщениями пользуются чаще всего.
        "kind": "poster",
        "kinds": ["poster", "mailing"],
        "emoji": "📤",
        "title": "Постинг и рассылка",
        "description": "Ваши сообщения по чатам: по расписанию — каждые N минут в окне времени, по очереди — чат, пауза, следующий. Текст здесь или из библиотеки.",
        "status": "ready",
        "needs": ["account", "targets", "message"],
        "optional": ["send_mode", "schedule_only", "scheduled_posts", "buttons", "interval", "start", "end", "gap", "cycle", "repeats", "typing", "random_pick", "link_preview", "alerts"],
        "hint": "Чаты отмечайте кнопкой «выбрать» — хоть все сразу. Текст наберите здесь либо возьмите из библиотеки: переносы строк сохраняются, пустая строка делит текст на сообщения — уходят по очереди. Расписание: интервал в минутах, окно — ЧЧ:ММ по вашим часам. Очередь: паузы в секундах, «кругов 0» — крутить без конца.",
        "tags": ["ваш текст", "расписание или очередь"],
    },
    {
        "id": "copy_channel",
        "group": "publish",
        "kind": "forward",
        "emoji": "🔁",
        "title": "Копирование канала",
        "description": "Один канал — в один ваш: новый пост появился в источнике и сразу выходит у вас, с заменами текста.",
        "status": "ready",
        "needs": ["account", "source", "target"],
        "optional": ["mode", "buttons", "translate_to", "uniquify", "alerts"],
        "tags": ["чужие посты", "один канал → один"],
    },
    {
        "id": "clone",
        "group": "publish",
        "kind": "clone",
        "emoji": "📋",
        "title": "Клон канала",
        "description": "Ваш канал как зеркало чужого: сначала забирается история, дальше новые посты выходят сами.",
        "status": "ready",
        "needs": ["account", "source", "target"],
        "optional": ["history", "buttons", "translate_to", "uniquify", "alerts"],
        "hint": "История забирается не залпом, а порциями — большой канал догрузится за несколько минут. Новые посты из источника выходят у вас сразу, не дожидаясь конца догрузки.",
        "tags": ["чужие посты", "с историей", "один канал → один"],
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
        "description": "Тот же канал — сразу в десятки чатов: пост из источника уходит во все выбранные одним залпом, как только вышел.",
        "status": "ready",
        # Приёмник отдельным полем не просим: чаты — один список, и первый из
        # них всё равно становится главным. Два поля под одно и то же заставляли
        # заполнять «приёмник» руками даже при выборе чатов мышкой.
        "needs": ["account", "source", "targets"],
        "optional": ["buttons", "translate_to", "uniquify", "alerts"],
        "hint": "Источник — откуда берём пост, чаты — куда он уйдёт. Отмечайте кнопкой «выбрать» — сколько нужно, хоть все сразу. Свой текст здесь не нужен: уходит то, что вышло в источнике.",
        "tags": ["чужие посты", "все чаты разом", "по факту поста"],
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
        "optional": [
            "parser_mode",
            "scan",
            "limit",
            "require_username",
            "exclude_admins",
            "only_premium",
            "only_with_photo",
            "active_only",
            "online_within_hours",
            "api_delay",
            "invite_to",
        ],
        "hint": "Чат-источник отмечайте кнопкой «выбрать» у поля или заранее во вкладке «Чаты». Режим «участники» листает состав чата, «авторы» — писавших, «комментарии» — обсуждавших посты: самые вовлечённые. «Просмотреть» — сколько перебрать, «собрать» — сколько сохранить: фильтры отсеивают, и смотреть приходится больше. Запускается сразу, результат — кнопкой «Результаты». Собранных можно позвать в свой чат кнопкой «Пригласить» — пачками по 20.",
        "tags": ["список участников", "фильтры и режимы", "запуск вручную"],
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
        "tags": ["вступает сама", "ссылки из источника"],
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
        "optional": ["keywords", "alerts"],
        "tags": ["чеки и подарки", "в один чат"],
    },
    {
        "id": "listener",
        "group": "inbox",
        "kind": "listener",
        "emoji": "👂",
        "title": "Слушатель слов",
        "description": "Следит за чатом и присылает посты с вашими словами.",
        "status": "ready",
        "needs": ["account", "source", "target"],
        "optional": ["keywords", "alerts"],
        "hint": "Слова — через запятую: «скидка, акция, розыгрыш». Совпадение ищется без учёта регистра, пост приходит с названием чата.",
        "tags": ["свои слова", "в один чат"],
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
        "optional": ["keywords", "ignore_bots", "ignore_archived", "ignore_muted", "alerts"],
        "hint": "Источник не нужен: задача слушает все личные диалоги аккаунта. Ботов, архивные и заглушённые чаты пропускает — галочки снимаются.",
        "tags": ["личные сообщения", "источник не нужен"],
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
        "optional": ["reaction", "alerts"],
        "tags": ["один человек", "реакция"],
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
        "optional": ["keywords", "banned_words", "block_links", "max_warns", "mute_hours", "alerts"],
        "hint": "Цель — человек под надзором, слова — для всех. Каждое удаление — варн автору: набрал максимум — получает мут на часы. Админы от слов и ссылок освобождены.",
        "tags": ["слова и ссылки", "варны и мут", "нужны права админа"],
    },
]

# Блоки каталога в порядке показа. Подписи и порядок живут здесь, а не в
# мини-аппе: иначе появилась бы вторая копия, которая рано или поздно разойдётся
# с этим списком. Команда без известной группы попадёт в «прочее».
# «Чужие посты» и «свои сообщения» разведены по разным блокам намеренно: пока
# все четыре лежали в одной «публикации», разница между пересылкой чужого поста
# и рассылкой своего текста в списке не читалась вообще. Подписи короткие — они
# же стоят в чипсах над списком, а там на 390 px длинная фраза уезжает за край.
COMMAND_GROUPS: list[dict] = [
    {"id": "own", "title": "свои сообщения"},
    {"id": "publish", "title": "чужие посты"},
    {"id": "audience", "title": "аудитория"},
    {"id": "inbox", "title": "входящее"},
    {"id": "moderation", "title": "модерация"},
]

COMMANDS_BY_ID: dict[str, dict] = {item["id"]: item for item in COMMANDS}
# Команда по типу задачи: у сохранённого правила есть kind, а форму правки надо
# собрать по той же команде, из которой задачу создали.
COMMANDS_BY_KIND: dict[str, dict] = {item["kind"]: item for item in COMMANDS}
# Обе механики единого слота открывают одну и ту же форму: правка задачи
# находит команду по kind правила, а kind у постинга и рассылки разный.
COMMANDS_BY_KIND["mailing"] = COMMANDS_BY_ID["sender"]
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
    # Тот же бот пишет письма о больных задачах (см. app.task_alerts).
    from app.task_alerts import set_alert_bot

    set_alert_bot(bot)
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
