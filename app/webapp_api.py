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
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import parse_qsl

from aiogram.types import LabeledPrice
from aiohttp import web
from loguru import logger

from app import accounts_login, bonus, paylink, webapp_build
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
        db_user = await repo.get_user(session, user_id)

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
            # Подарок за подписку на канал сервиса. Выключен настройками —
            # приходит enabled: false, и карточка в кабинете не появляется.
            "bonus": bonus.info(db_user.channel_bonus_at if db_user else None),
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
        # Журнал по всем задачам сразу: запрос на карточку превратил бы один
        # ответ в двадцать походов в базу.
        health = await repo.task_health(session, [rule.id for rule in rules])
        # Тексты рассылок — тем же одним запросом: форма правки показывает
        # текст, а он лежит в библиотеке.
        texts = await _mailing_library(session, user_id, rules)

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
        texts = await _mailing_library(session, rule.user_id, [rule])
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


async def _mailing_texts(payload: dict, filters: dict, *, user_id: int, partial: bool) -> None:
    """Что рассылает задача: текст из поля плюс сохранённые посты из библиотеки.

    Рассылка отправляет записи библиотеки, поэтому набранный текст сначала
    становится её записями, а в задаче остаются ссылки на них.

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
            filters["library_ids"] = [row.id for row in rows]
            return
        posts = [row.id for row in rows if not (row.text or "").strip()]
        texts = [row for row in rows if (row.text or "").strip()]
        if [row.text for row in texts] == msgs:
            filters["library_ids"] = [row.id for row in rows]  # текст не менялся
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
    filters["library_ids"] = fresh + posts


async def _mailing_library(session, user_id: int, rules) -> dict[int, str]:
    """Записи библиотеки, на которые ссылаются рассылки: id → текст.

    Форма правки показывает текст рассылки, а лежит он в библиотеке — значит
    карточке нужны сами тексты, а не только номера записей. Читаем их одним
    запросом на все задачи: чтение на карточку превратило бы один ответ со
    списком задач в двадцать походов в базу.

    Записи без текста (сохранённые посты) остаются в ответе с пустой строкой:
    по ней ``_edit_view`` и отличает их от текста, который можно набрать.
    """
    wanted: set[int] = set()
    for rule in rules:
        if (rule.kind or "forward") != "mailing":
            continue
        wanted.update(_as_ids((rule.filters or {}).get("library_ids")))
    if not wanted:
        return {}
    rows = await repo.saved_messages_by_ids(session, user_id, sorted(wanted))
    return {row.id: row.text or "" for row in rows}


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
        if given("limit"):
            filters["limit"] = max(1, min(_as_int(payload.get("limit"), 200), MAX_PARSER_LIMIT))
    elif kind == "autosubscribe":
        # Ссылки-приглашения храним как есть: вступать по ним будет сама задача.
        if targets is not None:
            filters["subscribe_to"] = targets
    elif kind == "baiting":
        if given("reaction"):
            filters["reaction"] = str(payload.get("reaction") or "").strip() or "👍"
    elif kind in ("checks", "dialogs", "mute"):
        if given("keywords"):
            filters["keywords"] = _as_list(payload.get("keywords"))
    elif kind == "poster":
        if given("message"):
            filters["messages"] = _split_messages(payload.get("message")) or [
                str(payload.get("message") or "").strip()
            ]
        if given("interval"):
            filters["interval_seconds"] = max(1, _as_int(payload.get("interval"), 2)) * 60
        if given("start"):
            filters["window_start"] = str(payload.get("start") or "00:00")[:5]
        if given("end"):
            filters["window_end"] = str(payload.get("end") or "23:59")[:5]
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
        await _mailing_texts(payload, filters, user_id=user_id, partial=partial)


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


@routes.patch("/api/tasks/{task_id}")
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
        # У рассылки текст в поле не нужен, если сообщения взяты из библиотеки.
        if not (kind == "mailing" and _as_ids(payload.get("library_ids"))):
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
    return await _task_json(rule)


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
    return await _task_json(rule)


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


def _message_title(text: str, limit: int = 48) -> str:
    """Короткое имя сообщения для списка библиотеки — его первая строка.

    Сообщение бывает многострочным (прайс, объявление в два абзаца), а в списке
    на него отведена одна строка: без этого в заголовок попадала середина
    второй строки, и записи выглядели одинаково обрезанными.
    """
    head = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
    return head[:limit] + ("…" if len(head) > limit else "")


def _library_view(item) -> dict:
    """Сохранённое сообщение → вид для кабинета."""
    text = (item.text or "").strip()
    return {
        "id": item.id,
        "title": item.title or _message_title(text),
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
            title=_message_title(payload.get("title"), 128),
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
    ``texts`` — тексты записей библиотеки (``_mailing_library``): из них форма
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
        # Считаем только те записи, что ещё живы: сообщение могли удалить из
        # библиотеки, и ссылка на него осталась в задаче. Раньше карточка
        # показывала прежний счёт, а рассылать было нечего. Когда текстов не
        # передали (texts=None), счёт остаётся прежним — гадать не о чём.
        ids = [int(value) for value in (conf.library_ids or [])]
        alive = ids if texts is None else [item for item in ids if item in texts]
        view["mailing"] = {
            "recipients": recipients,
            "messages_count": len(alive),
            # Пустой список записей означает «вся библиотека» — так его читает
            # планировщик. Карточка обязана сказать это словами: без пометки она
            # молчала о том, что уйдёт, а счёт сообщений показывал ноль.
            "whole_library": not ids,
            # Сколько ссылок повисло: карточка скажет, что сообщения удалены, —
            # иначе задача бодро «работает», а в чаты ничего не уходит.
            "messages_gone": len(ids) - len(alive),
            "gap_seconds": conf.gap_seconds,
            "cycle_seconds": conf.cycle_seconds,
            "repeats": conf.repeats,
            "typing": bool(conf.typing),
            "random_pick": bool(conf.random_pick),
        }
        if conf.repeats > 0 and recipients:
            total = recipients * int(conf.repeats)
    view["progress"] = {"done": done, "total": total}
    # Названия чатов задачи: в колонках правила есть имя только первого чата,
    # остальные — числа, поэтому имена запоминаются в настройках при создании и
    # правке. Старые задачи их не знают — там честно останется id.
    names = dict((rule.filters or {}).get("chat_titles") or {})
    if rule.source_id and rule.source_title:
        names.setdefault(str(rule.source_id), rule.source_title)
    if rule.target_id and rule.target_title:
        names.setdefault(str(rule.target_id), rule.target_title)
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


# id чата в тексте сбоя: «-1001234567890» человеку ничего не говорит, а название
# у задачи уже запомнено. Пять цифр и больше — чтобы не трогать номера ошибок.
_CHAT_ID_RE = re.compile(r"-?\d{5,}")
# Причина сбоя на карточке: длиннее в узкий экран не влезает, а полный текст
# остаётся в журнале.
ERROR_TEXT_LIMIT = 160


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
    «нет сбоев» это или «сервер не прислал».
    """
    health = health or {}
    error = str(health.get("error") or "")
    if error:
        # Названия чатов вместо их id: они уже запомнены в настройках задачи.
        error = _CHAT_ID_RE.sub(lambda m: names.get(m.group(0)) or m.group(0), error)
        if len(error) > ERROR_TEXT_LIMIT:
            error = f"{error[:ERROR_TEXT_LIMIT].rstrip()}…"
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

    ``texts`` — тексты записей библиотеки (``_mailing_library``), нужны рассылке.
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
    if kind == "forward":
        edit["mode"] = rule.mode
    elif kind == "parser":
        edit["limit"] = int(conf.limit or 200)
    elif kind == "baiting":
        edit["reaction"] = conf.reaction
    elif kind in ("checks", "dialogs", "mute"):
        edit["keywords"] = ", ".join(conf.keywords or [])
    elif kind == "poster":
        # Обратная сборка текста: сообщения делит пустая строка — тем же
        # правилом, каким их разбирал _split_messages.
        edit["message"] = "\n\n".join(conf.messages or [])
        edit["interval"] = max(1, conf.interval_seconds // 60)
        edit["start"] = conf.window_start
        edit["end"] = conf.window_end
    elif kind == "mailing":
        # Текст рассылки лежит в библиотеке, но правят его здесь: поле показывает
        # то, что уйдёт, — как у постинга, и сообщения так же делит пустая
        # строка. Раньше поле стояло пустым, а набранный в нём текст пропадал.
        # Чипсами рядом остаются только записи без текста — сохранённые посты:
        # их руками не набрать, поэтому они идут списком id.
        ids = [int(value) for value in (conf.library_ids or [])]
        edit["message"] = "\n\n".join(
            texts[item_id] for item_id in ids if (texts.get(item_id) or "").strip()
        )
        edit["library_ids"] = [
            item_id
            for item_id in ids
            if item_id in texts and not (texts[item_id] or "").strip()
        ]
        edit["gap"] = conf.gap_seconds
        edit["cycle"] = conf.cycle_seconds
        edit["repeats"] = conf.repeats
        edit["typing"] = bool(conf.typing)
        edit["random_pick"] = bool(conf.random_pick)
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
        "id": "copy_channel",
        "group": "publish",
        "kind": "forward",
        "emoji": "🔁",
        "title": "Копирование канала",
        "description": "Один канал — в один ваш: новый пост появился в источнике и сразу выходит у вас, с заменами текста.",
        "status": "ready",
        "needs": ["account", "source", "target"],
        "optional": ["mode"],
        "tags": ["чужие посты", "один канал → один"],
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
        "optional": [],
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
        "optional": ["limit"],
        "hint": "Чат-источник отмечайте кнопкой «выбрать» у поля или заранее во вкладке «Чаты». Запускается сразу, результат — кнопкой «Результаты».",
        "tags": ["список участников", "запуск вручную"],
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
        "optional": ["keywords"],
        "tags": ["чеки и подарки", "в один чат"],
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
        "optional": ["reaction"],
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
        "optional": ["keywords"],
        "tags": ["один человек", "нужны права админа"],
    },
    {
        "id": "poster",
        "group": "own",
        "kind": "poster",
        "emoji": "📤",
        # «Постинг по расписанию», а не «авто-постинг»: рядом стоит рассылка,
        # и оба названия читались как «шлёт мои сообщения». Отличие вынесено в
        # само название — здесь главное расписание, у рассылки очередь.
        "title": "Постинг по расписанию",
        "description": "Ваше объявление висит в чатах постоянно: сам шлёт его во все выбранные каждые N минут, пока открыто окно времени.",
        "status": "ready",
        "needs": ["account", "targets", "message"],
        "optional": ["interval", "start", "end"],
        "hint": "Чаты отмечайте кнопкой «выбрать» — сколько нужно, хоть все сразу; круг идёт по очереди, с паузой между чатами. Переносы строк внутри сообщения сохраняются как есть — прайс уйдёт целиком. Нужно второе сообщение — отделите его пустой строкой: за круг уходит одно, следующий круг возьмёт следующее. Интервал в минутах, окно — ЧЧ:ММ.",
        "tags": ["ваш текст", "каждые N минут", "окно времени"],
    },
    {
        "id": "mailing",
        "group": "own",
        "kind": "mailing",
        "emoji": "📨",
        # «По очереди» — единственное, чем она отличается от постинга: там залп
        # по расписанию, здесь один чат за раз с паузой и кругами.
        "title": "Рассылка по очереди",
        "description": "Обход чатов по одному: чат — пауза — следующий, и так круг за кругом. Текст берётся здесь или из библиотеки.",
        "status": "ready",
        "needs": ["account", "targets", "message"],
        "optional": ["gap", "cycle", "repeats", "typing", "random_pick"],
        "hint": "Получателей отмечайте кнопкой «выбрать» — или заранее во вкладке «Чаты». Текст наберите здесь либо возьмите из библиотеки: переносы строк сохраняются, а пустая строка делит текст на два сообщения — уходят по очереди, первое всем, затем второе. Пауза между чатами в секундах, «кругов 0» — крутить без конца.",
        "tags": ["ваш текст", "по одному чату", "пауза и круги"],
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
    {"id": "publish", "title": "чужие посты"},
    {"id": "own", "title": "свои сообщения"},
    {"id": "audience", "title": "аудитория"},
    {"id": "inbox", "title": "входящее"},
    {"id": "moderation", "title": "модерация"},
]

COMMANDS_BY_ID: dict[str, dict] = {item["id"]: item for item in COMMANDS}
# Команда по типу задачи: у сохранённого правила есть kind, а форму правки надо
# собрать по той же команде, из которой задачу создали.
COMMANDS_BY_KIND: dict[str, dict] = {item["kind"]: item for item in COMMANDS}
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
