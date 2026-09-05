#!/usr/bin/env python3
"""Дымовой прогон кабинета: каждый эндпоинт на живом сокете, без сети.

Зачем отдельно от pytest: тесты собирают приложение из тех же функций, но по
кусочкам. Здесь оно поднимается целиком и так же, как в app/main.py — те же
middleware, те же маршруты, та же статика мини-аппа, — и опрашивается настоящим
HTTP-клиентом. Так видно то, чего не видно в юнит-тестах: не забыт ли маршрут,
на месте ли файлы webapp/, честен ли ответ при выключенном контуре оплаты.

Наружу не ходим: сервер слушает 127.0.0.1 на случайном порту, бот в приложение
не передаётся (часть ответов от этого честно 503), TronGrid и ЮKassa не
дёргаются. База своя, временная — рабочую прогон не открывает.

Запуск: python scripts/smoke_api.py
Код возврата 1 — хотя бы одна проверка не прошла.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator
from urllib.parse import quote

from cryptography.fernet import Fernet

# Настройки и движок БД создаются в момент импорта app.*, поэтому окружение
# правим до первого такого импорта — позже это уже ни на что не влияет.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP_DIR = Path(tempfile.mkdtemp(prefix="tgf-smoke-"))
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TMP_DIR / 'smoke.db'}"
# Токен свой: initData подписывается локально, в Bot API прогон не ходит.
# Рабочий токен из .env для этого не нужен и не должен участвовать.
os.environ["BOT_TOKEN"] = "123456789:AASmokeTokenNotARealBotToken00000"
os.environ["SECRET_KEY"] = Fernet.generate_key().decode()
# Тариф фиксируем: проверки копилки считают дни, а не «сколько получится».
os.environ["TRIAL_DAYS"] = "3"
os.environ["LOG_LEVEL"] = "WARNING"

import aiohttp  # noqa: E402
from aiohttp import web  # noqa: E402
from loguru import logger  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from telethon.errors import PhoneCodeInvalidError, SessionPasswordNeededError  # noqa: E402

from app import accounts_login, paylink  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import repo  # noqa: E402
from app.db.database import dispose_db, init_db, session_scope  # noqa: E402
from app.db.models import CollectedItem, ForwardLog, PendingDelivery  # noqa: E402
from app.errors import http_error_middleware  # noqa: E402
from app.payments import crypto, service, yookassa  # noqa: E402
from app.plans import PERIODS, rub_amount, usdt_amount  # noqa: E402
from app.telegram_client.manager import manager  # noqa: E402
from app.webapp_api import COMMAND_GROUPS, COMMANDS, setup_webapp_routes  # noqa: E402
from app.webapp_build import build_stamp  # noqa: E402
from tests.helpers import sign_init_data  # noqa: E402

# Логи приложения в отчёте только мешают: INFO о раздаче статики и обновлении
# правил не имеет отношения к проверкам. Ошибки и предупреждения оставляем.
logger.remove()
logger.add(sys.stderr, level="WARNING", format="  ! {message}")

SMOKE_USER_ID = 999_000_222
# Настоящий по формату адрес TRC-20: settings.usdt_ready проверяет длину и
# алфавит, иначе контур USDT считается ненастроенным. Переводов не делаем.
USDT_WALLET = "TQn9Y2khDD95J42FQtQTdwVVR93o1n1gLz"

# Номер, на который «подключается» аккаунт в разделе входа. Он же удаляется в
# конце раздела, чтобы остальные проверки видели ту же одну учётку из seed().
LOGIN_PHONE = "+79001234567"
# Ключи MTProto для раздела входа: сама готовность шлюза считается по ним, а в
# .env разработчика их может не быть. Плейсхолдеры из .env.example не подходят —
# settings.mtproto_ready считает их ненастроенными.
SMOKE_API_ID = 1_234_567
SMOKE_API_HASH = "1234abcd" * 4


@contextmanager
def stubbed_gateway(**overrides: Any) -> Iterator[None]:
    """Подменяет вызовы MTProto-шлюза на время раздела.

    Вход по номеру — единственная часть кабинета, которая обязана говорить с
    Telegram. Прогон offline, поэтому шлюз заменяется заглушками: проверяем
    своё — шаги, коды ответов и состояние в БД, — а не работу Telegram.
    """
    for name, value in overrides.items():
        setattr(manager, name, value)
    try:
        yield
    finally:
        for name in overrides:
            delattr(manager, name)



class SubscriberBot:
    """Бот в объёме проверки подписки: один ``getChatMember``.

    Настоящий Bot API прогону недоступен (и не нужен), а без бота подарок
    отвечает только «не смогли проверить» — путь начисления остался бы
    непроверенным целиком.
    """

    def __init__(self, status: str = "member") -> None:
        self.status = status
        self.calls: list[tuple[str, int]] = []

    async def get_chat_member(self, chat_id: Any, user_id: int) -> Any:
        self.calls.append((chat_id, user_id))
        return SimpleNamespace(status=self.status, is_member=True)


@contextmanager
def stubbed_bot(bot: Any) -> Iterator[Any]:
    """Подставляет бота в уже поднятое приложение и убирает после раздела.

    setup_webapp_routes держит бота в переменной модуля — там же его и меняем,
    чтобы не поднимать второй сервер ради одного эндпоинта.
    """
    import app.webapp_api as webapp_api

    before = webapp_api._bot
    webapp_api._bot = bot
    try:
        yield bot
    finally:
        webapp_api._bot = before


@contextmanager
def configured(**values: Any) -> Iterator[None]:
    """Временно переключает настройки — как monkeypatch в тестах.

    Нужно, чтобы один прогон увидел и включённый внешний контур оплаты, и
    выключенный: в .env одновременно оба состояния не задать.
    """
    before = {name: getattr(settings, name) for name in values}
    for name, value in values.items():
        setattr(settings, name, value)
    try:
        yield
    finally:
        for name, value in before.items():
            setattr(settings, name, value)


class Report:
    """Печатает результат каждой проверки сразу и помнит провалы для итога."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def section(self, title: str) -> None:
        print(f"\n── {title} " + "─" * max(0, 60 - len(title)))

    def check(self, what: str, ok: bool, detail: str = "") -> bool:
        self.rows.append((what, bool(ok), detail))
        print(f"  {'✓' if ok else '✗'} {what}" + (f" — {detail}" if detail else ""))
        return bool(ok)

    def note(self, text: str) -> None:
        """Пояснение, а не проверка: чего прогон сознательно не делает."""
        print(f"  · {text}")

    @property
    def failures(self) -> list[tuple[str, bool, str]]:
        return [row for row in self.rows if not row[1]]


class Cabinet:
    """HTTP-клиент кабинета: подпись Telegram подставляется сама."""

    def __init__(self, session: aiohttp.ClientSession, base: str) -> None:
        self._session = session
        self._base = base
        self.headers = {"X-Telegram-Init-Data": sign_init_data(SMOKE_USER_ID)}

    async def request(
        self, method: str, path: str, *, auth: bool = True, **kwargs: Any
    ) -> tuple[int, Any]:
        """Возвращает (статус, тело). Тело — разобранный JSON или текст.

        Редиректы не проходим: корень и /app отвечают 302, и проверять надо
        именно их, а не то, куда они ведут.
        """
        headers: dict[str, str] = dict(self.headers) if auth else {}
        headers.update(kwargs.pop("headers", None) or {})
        async with self._session.request(
            method, self._base + path, headers=headers, allow_redirects=False, **kwargs
        ) as response:
            if response.content_type == "application/json":
                return response.status, await response.json()
            return response.status, await response.text()

    async def get(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        return await self.request("GET", path, **kwargs)

    async def get_headers(self, path: str, **kwargs: Any) -> tuple[int, dict[str, str]]:
        """Только статус и заголовки — нужно проверкам кэша мини-аппа."""
        auth = kwargs.pop("auth", True)
        headers: dict[str, str] = dict(self.headers) if auth else {}
        headers.update(kwargs.pop("headers", None) or {})
        async with self._session.get(
            self._base + path, headers=headers, allow_redirects=False, **kwargs
        ) as response:
            return response.status, dict(response.headers)

    async def post(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        return await self.request("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> tuple[int, Any]:
        return await self.request("DELETE", path, **kwargs)


async def seed() -> tuple[int, int]:
    """Пользователь, аккаунт и правило: без них половине эндпоинтов нечего отдать.

    Правило создаём напрямую в БД, а не через API: создание задачи требует
    живого MTProto-входа (найти чат по имени), которого в offline-прогоне нет.
    Сам отказ этого эндпоинта проверяется отдельно.
    """
    async with session_scope() as session:
        await repo.get_or_create_user(
            session, SMOKE_USER_ID, username="smoke", full_name="Дымовой прогон"
        )
        account = await repo.add_account(
            session,
            user_id=SMOKE_USER_ID,
            phone="+79000000000",
            # Шифровать нечего: Telethon-клиент прогон не поднимает.
            session_encrypted="smoke-session",
        )
        rule = await repo.add_rule(
            session,
            user_id=SMOKE_USER_ID,
            account_id=account.id,
            source_id=-1001234567890,
            source_title="Источник",
            target_id=-1009876543210,
            target_title="Приёмник",
        )
        return account.id, rule.id


async def start_server() -> tuple[web.AppRunner, str]:
    """Поднимает то же приложение, что и app/main.py, на свободном порту."""
    app = web.Application(middlewares=[http_error_middleware])
    # bot=None — как при работе одной веб-части сервиса: всё, что требует Bot
    # API (счёт в звёздах, имя бота в ссылке), обязано ответить честным отказом.
    setup_webapp_routes(app, bot=None)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    host, port = runner.addresses[0][:2]
    return runner, f"http://{host}:{port}"


# ─────────────────────────────── Разделы прогона ──────────────────────────────


async def check_static(cab: Cabinet, rep: Report) -> None:
    """Мини-апп раздаётся сервисом: без файлов кабинет открывается пустым окном."""
    rep.section("Мини-апп и статика")

    status, body = await cab.get("/", auth=False)
    rep.check("GET / — редирект на кабинет", status == 302, f"статус {status}")

    status, _ = await cab.get("/app", auth=False)
    rep.check("GET /app — редирект на /app/", status == 302, f"статус {status}")

    status, body = await cab.get("/app/", auth=False)
    rep.check(
        "GET /app/ — страница кабинета",
        status == 200 and "app.js" in str(body),
        f"статус {status}",
    )

    for asset in ("app.js", "styles.css", "pay.html"):
        status, _ = await cab.get(f"/app/{asset}", auth=False)
        rep.check(f"GET /app/{asset}", status == 200, f"статус {status}")

    status, _ = await cab.get("/app/no-such-file.js", auth=False)
    rep.check("Несуществующий файл — 404", status == 404, f"статус {status}")

    # Кэш WebView: без метки в адресе Telegram показывает вёрстку из своего
    # кэша, и выкат «не виден». Метка приходит вместе с HTML, поэтому сам HTML
    # кэшировать нельзя, а помеченные файлы, наоборот, можно навсегда.
    stamp = build_stamp(settings.webapp_dir)
    rep.check("Метка сборки мини-аппа посчитана", bool(stamp), "метка пустая")
    rep.check(
        "GET /app/ — ссылки на css/js с меткой сборки",
        f"styles.css?v={stamp}" in str(body) and f"app.js?v={stamp}" in str(body),
        "в HTML нет помеченных адресов",
    )

    status, headers = await cab.get_headers("/app/", auth=False)
    rep.check(
        "GET /app/ — Cache-Control: no-store",
        headers.get("Cache-Control") == "no-store",
        f"заголовок {headers.get('Cache-Control')!r}",
    )

    status, headers = await cab.get_headers("/app/styles.css", auth=False)
    rep.check(
        "Статика без метки — Cache-Control: no-cache",
        headers.get("Cache-Control") == "no-cache",
        f"заголовок {headers.get('Cache-Control')!r}",
    )

    status, headers = await cab.get_headers(f"/app/styles.css?v={stamp}", auth=False)
    rep.check(
        "Статика с меткой — кэшируется навсегда",
        "immutable" in (headers.get("Cache-Control") or ""),
        f"заголовок {headers.get('Cache-Control')!r}",
    )

    # Бандл без метки просит только старая копия index.html из кэша клиента:
    # ей отдаём не новый код (он не подойдёт к её разметке), а перезагрузку на
    # адрес с меткой.
    status, body = await cab.get("/app/app.js", auth=False)
    loader = str(body)
    rep.check(
        "app.js без метки — скрипт-спасатель, а не бандл",
        status == 200 and "location.replace" in loader and "async function boot()" not in loader,
        f"статус {status}",
    )
    rep.check(
        "Спасатель знает текущую метку",
        stamp in loader,
        "метки в скрипте нет",
    )
    status, headers = await cab.get_headers("/app/app.js", auth=False)
    rep.check(
        "Спасатель не кэшируется",
        headers.get("Cache-Control") == "no-store",
        f"заголовок {headers.get('Cache-Control')!r}",
    )

    status, body = await cab.get(f"/app/app.js?v={stamp}", auth=False)
    rep.check(
        "app.js с меткой — настоящий бандл",
        status == 200 and "async function boot()" in str(body),
        f"статус {status}",
    )


async def check_health(cab: Cabinet, rep: Report) -> None:
    """Мониторинг: /api/health открыт без подписи и показывает счётчики очереди."""
    rep.section("Здоровье сервиса")

    status, body = await cab.get("/api/health", auth=False)
    rep.check("GET /api/health без подписи — 200", status == 200, f"статус {status}")
    if status != 200 or not isinstance(body, dict):
        return
    rep.check("ok: true", body.get("ok") is True)
    rep.check(
        "метка сборки мини-аппа в ответе",
        body.get("build") == build_stamp(settings.webapp_dir),
        f"build={body.get('build')!r}",
    )
    delivery = body.get("delivery") or {}
    expected = {"submitted", "dropped", "sent", "failed", "skipped", "restored", "queued"}
    missing = sorted(expected - set(delivery))
    rep.check("счётчики очереди на месте", not missing, f"нет: {missing}" if missing else "")
    rep.check(
        "пропуски с разбивкой по причинам",
        isinstance(delivery.get("skips"), dict),
        f"skips={delivery.get('skips')!r}",
    )
    rep.check(
        "журнал ожидания читается",
        delivery.get("persisted") == 0,
        f"persisted={delivery.get('persisted')!r}",
    )


async def check_auth(cab: Cabinet, rep: Report) -> None:
    """Вход только по подписи Telegram: подделать её без токена бота нельзя."""
    rep.section("Авторизация по initData")

    status, _ = await cab.get("/api/me", auth=False)
    rep.check("без подписи — 401", status == 401, f"статус {status}")

    cases = {
        "битая подпись": sign_init_data(SMOKE_USER_ID, corrupt_hash=True),
        "чужой токен бота": sign_init_data(
            SMOKE_USER_ID, token="987654321:AAAnotherBotEntirely000000000000"
        ),
        "просроченный initData": sign_init_data(
            SMOKE_USER_ID, auth_date=int(time.time()) - 2 * 24 * 60 * 60
        ),
    }
    for name, init_data in cases.items():
        status, _ = await cab.get(
            "/api/me", auth=False, headers={"X-Telegram-Init-Data": init_data}
        )
        rep.check(f"{name} — 401", status == 401, f"статус {status}")

    status, _ = await cab.get("/api/me")
    rep.check("своя подпись — 200", status == 200, f"статус {status}")

    # Тот же initData принимается и параметром запроса: так его передают
    # страницы, куда заголовок не подставить.
    quoted = quote(cab.headers["X-Telegram-Init-Data"], safe="")
    status, _ = await cab.get(f"/api/me?initData={quoted}", auth=False)
    rep.check("подпись в query-параметре — 200", status == 200, f"статус {status}")


async def check_profile(cab: Cabinet, rep: Report) -> dict:
    """Профиль: подписка, тарифы и признаки включённых возможностей."""
    rep.section("Профиль и тарифы")

    status, body = await cab.get("/api/me")
    if not rep.check("GET /api/me — 200", status == 200, f"статус {status}"):
        return {}

    rep.check("id совпадает с подписью", body.get("id") == SMOKE_USER_ID)
    subscription = body.get("subscription") or {}
    rep.check(
        "пробный период выдан автоматически",
        subscription.get("active") is True and subscription.get("days_left", 0) >= 2,
        f"days_left={subscription.get('days_left')}",
    )
    stats = body.get("stats") or {}
    rep.check(
        "статистика видит правило и аккаунт",
        stats.get("rules") == 1 and stats.get("accounts") == 1,
        f"{stats}",
    )
    tariffs = body.get("tariffs") or {}
    rep.check(
        "тарифы отдаются из настроек",
        tariffs.get("rub") == settings.price_rub
        and tariffs.get("stars") == settings.price_stars,
        f"{tariffs}",
    )
    features = body.get("features") or {}
    rep.check(
        "способы оплаты — только настроенные",
        features.get("payment_methods") == settings.payment_methods(),
        f"{features.get('payment_methods')}",
    )
    rep.check(
        "состояние входа аккаунтов честное",
        features.get("account_login_enabled") == settings.public_login_enabled
        and features.get("account_login_status") == settings.account_login_status,
        f"{features.get('account_login_status')}",
    )
    return body


async def check_commands(cab: Cabinet, rep: Report) -> None:
    """Каталог команд: статус считается по реальному состоянию, а не из справочника."""
    rep.section("Каталог команд")

    status, body = await cab.get("/api/commands")
    if not rep.check("GET /api/commands — 200", status == 200, f"статус {status}"):
        return
    items = body.get("commands") or []
    rep.check(
        "все команды на месте",
        len(items) == len(COMMANDS),
        f"{len(items)} из {len(COMMANDS)}",
    )
    rep.check(
        "у каждой команды есть тип и обязательные поля",
        all(item.get("kind") and isinstance(item.get("needs"), list) for item in items),
    )
    expected = "ready" if settings.public_login_enabled else "setup_required"
    rep.check(
        f"статус команд считается по состоянию шлюза — {expected}",
        all(item.get("status") == expected for item in items),
    )

    # Блоки каталога: порядок и подписи держит сервер, иначе в мини-аппе
    # появилась бы вторая копия списка и разошлась бы с этим.
    groups = body.get("groups") or []
    rep.check(
        "блоки каталога приходят с сервера",
        groups == COMMAND_GROUPS and all(g.get("title") for g in groups),
        f"{[g.get('id') for g in groups]}",
    )
    known = {g.get("id") for g in groups}
    unknown = sorted({item.get("group") for item in items} - known)
    rep.check(
        "каждая команда в известном блоке",
        not unknown,
        f"без блока: {unknown}" if unknown else "",
    )

    # Четыре задачи «в чаты» внешне похожи, и в каталоге их путали. Различает их
    # только текст карточки: название, описание и метки в подвале.
    twins = [
        item
        for item in items
        if item.get("id") in ("copy_channel", "broadcast", "poster", "mailing")
    ]
    rep.check(
        "четыре задачи «в чаты» читаются как разные",
        len(twins) == 4
        and len({item.get("title") for item in twins}) == 4
        and len({item.get("description") for item in twins}) == 4
        and len({tuple(item.get("tags") or ()) for item in twins}) == 4,
        f"{[item.get('title') for item in twins]}",
    )
    rep.check(
        "у каждой команды есть метки отличий (не больше трёх)",
        all(item.get("tags") and len(item["tags"]) <= 3 for item in items),
        f"{[item.get('id') for item in items if not item.get('tags')]}",
    )


async def check_tasks(cab: Cabinet, rep: Report, account_id: int, rule_id: int) -> None:
    """Задачи: список, создание, пауза, режим, архив, результаты, удаление."""
    rep.section("Задачи")

    status, body = await cab.get("/api/tasks")
    tasks = (body or {}).get("tasks") or []
    rep.check(
        "GET /api/tasks — активная задача видна",
        status == 200 and [t["id"] for t in tasks] == [rule_id],
        f"статус {status}, задач {len(tasks)}",
    )
    status, body = await cab.get("/api/tasks?status=done")
    rep.check(
        "архив пока пуст",
        status == 200 and not (body or {}).get("tasks"),
        f"статус {status}",
    )

    status, body = await cab.post("/api/tasks", json={})
    rep.check(
        "создание без полей — 400 со списком нужного",
        status == 400 and "Укажите" in str((body or {}).get("error")),
        f"статус {status}",
    )
    status, body = await cab.post("/api/tasks", data="не json")
    rep.check("тело не JSON — 400", status == 400, f"статус {status}")

    status, body = await cab.post(
        "/api/tasks", json={"account_id": 10**9, "source": "@src", "target": "@dst"}
    )
    rep.check(
        "чужой аккаунт — 404",
        status == 404 and "Аккаунт не найден" in str((body or {}).get("error")),
        f"статус {status}",
    )

    status, body = await cab.post(
        "/api/tasks",
        json={"command": "copy_channel", "account_id": account_id, "source": "@src", "target": "@dst"},
    )
    if settings.public_login_enabled:
        rep.check(
            "создание задачи — чат не найден (клиент не поднят)",
            status == 404,
            f"статус {status}",
        )
    else:
        rep.check(
            "создание задачи — 503 с указанием причины",
            status == 503 and (body or {}).get("feature") == "account_login",
            f"статус {status}, feature={(body or {}).get('feature')}",
        )


async def check_task_actions(cab: Cabinet, rep: Report, rule_id: int) -> None:
    """Кнопки карточки задачи: пауза, режим, архив, запуск, результаты, удаление."""
    rep.section("Управление задачей")

    status, body = await cab.post(f"/api/tasks/{rule_id}/toggle")
    rep.check(
        "пауза выключает задачу",
        status == 200 and (body or {}).get("task", {}).get("enabled") is False,
        f"статус {status}",
    )
    status, body = await cab.post(f"/api/tasks/{rule_id}/toggle")
    rep.check(
        "повторное нажатие возвращает в работу",
        status == 200 and (body or {}).get("task", {}).get("enabled") is True,
        f"статус {status}",
    )

    status, body = await cab.post(f"/api/tasks/{rule_id}/mode")
    rep.check(
        "режим переключается на форвард",
        status == 200 and (body or {}).get("task", {}).get("mode") == "forward",
        f"статус {status}",
    )
    status, body = await cab.post(f"/api/tasks/{rule_id}/mode")
    rep.check(
        "и обратно на копию",
        status == 200 and (body or {}).get("task", {}).get("mode") == "copy",
        f"статус {status}",
    )

    status, body = await cab.post(f"/api/tasks/{rule_id}/run")
    rep.check(
        "запуск обычной пересылки — 409, она работает по сообщениям",
        status == 409,
        f"статус {status}",
    )

    status, body = await cab.get(f"/api/tasks/{rule_id}/results")
    rep.check(
        "результаты — пустой список без ошибки",
        status == 200 and (body or {}).get("total") == 0,
        f"статус {status}",
    )

    status, body = await cab.post(f"/api/tasks/{rule_id}/archive")
    rep.check(
        "архив убирает задачу из рабочих",
        status == 200 and (body or {}).get("task", {}).get("archived") is True,
        f"статус {status}",
    )
    status, body = await cab.get("/api/tasks?status=done")
    rep.check(
        "и показывает её в архиве",
        status == 200 and len((body or {}).get("tasks") or []) == 1,
        f"статус {status}",
    )
    status, body = await cab.post(f"/api/tasks/{rule_id}/toggle")
    rep.check(
        "архивную задачу нельзя включить — 409",
        status == 409,
        f"статус {status}",
    )
    status, body = await cab.post(f"/api/tasks/{rule_id}/archive?undo=1")
    rep.check(
        "возврат из архива",
        status == 200 and (body or {}).get("task", {}).get("archived") is False,
        f"статус {status}",
    )

    status, _ = await cab.post("/api/tasks/10000000/toggle")
    rep.check("чужая задача — 404", status == 404, f"статус {status}")

    status, body = await cab.delete(f"/api/tasks/{rule_id}")
    rep.check("удаление задачи", status == 200 and (body or {}).get("ok") is True, f"статус {status}")
    status, _ = await cab.delete(f"/api/tasks/{rule_id}")
    rep.check("повторное удаление — 404", status == 404, f"статус {status}")


async def check_mailing_and_library(cab: Cabinet, rep: Report, account_id: int) -> None:
    """Рассылка по очереди и библиотека сообщений: задача вместе с содержимым.

    Рассылка — единственная задача, которая заводится из кабинета не пустой:
    тексты из формы ложатся в библиотеку, оттуда их берёт планировщик. Поиск
    чатов подменён заглушкой (в offline-прогоне искать нечем) — проверяем своё:
    коды ответов, геометрию получателей, счёт работы и саму библиотеку. Здесь же
    проходят постинг и пересылка в чаты: список чатов у всех трёх задач один и
    тот же код, и ломаться он будет сразу у всех.
    """
    rep.section("Рассылка и библиотека")

    status, body = await cab.get("/api/library")
    rep.check(
        "GET /api/library — список без ошибки",
        status == 200 and isinstance((body or {}).get("items"), list),
        f"статус {status}",
    )

    status, _ = await cab.post("/api/library", json={"text": "   "})
    rep.check("запись без текста и без поста — 400", status == 400, f"статус {status}")

    status, body = await cab.post("/api/library", json={"text": "смоук: одно сообщение"})
    item_id = int(((body or {}).get("item") or {}).get("id") or 0)
    rep.check("запись добавлена — 201 с id", status == 201 and bool(item_id), f"статус {status}")

    status, body = await cab.get("/api/library")
    rep.check(
        "и видна в списке",
        any(item.get("id") == item_id for item in (body or {}).get("items") or []),
    )

    status, body = await cab.delete(f"/api/library/{item_id}")
    rep.check(
        "удаление записи",
        status == 200 and (body or {}).get("ok") is True,
        f"статус {status}",
    )
    status, _ = await cab.delete(f"/api/library/{item_id}")
    rep.check("повторное удаление — 404", status == 404, f"статус {status}")

    # Чаты «находятся» без Telegram: имя запроса и есть чат. Кабинет ищет их
    # пачкой — одним вызовом на все ссылки задачи, его и подменяем.
    chats = {"@smoke-one": -1001, "@smoke-two": -1002}
    # Массовка для проверки «в бесконечное число чатов»: столько получателей
    # мышкой не набирают, зато видно, что ни счёт, ни ответ не упираются в предел.
    many = {f"@smoke-many-{n}": -2000 - n for n in range(250)}
    sweeps = 0

    async def resolve_many(_account_id: int, queries) -> dict[str, tuple[int, str]]:
        nonlocal sweeps
        sweeps += 1
        found: dict[str, tuple[int, str]] = {}
        for raw in queries:
            key = str(raw or "").strip()
            chat_id = chats.get(key) or many.get(key)
            if chat_id:
                found[key] = (chat_id, key)
        return found

    task: dict = {}
    with configured(api_id=SMOKE_API_ID, api_hash=SMOKE_API_HASH), stubbed_gateway(
        resolve_many=resolve_many
    ):
        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "mailing",
                "account_id": account_id,
                "targets": list(chats),
                # Пустая строка делит сообщения, одиночный перенос — нет.
                "message": "первое\n\nвторое",
                "gap": 7,
                "repeats": 2,
            },
        )
        task = (body or {}).get("task") or {}
        info = task.get("mailing") or {}
        rep.check(
            "рассылка создана — 201",
            status == 201 and task.get("kind") == "mailing",
            f"статус {status} {(body or {}).get('error') or ''}",
        )
        rep.check(
            "получатели, тексты и пауза — из формы",
            info.get("recipients") == 2
            and info.get("messages_count") == 2
            and info.get("gap_seconds") == 7,
            f"{info}",
        )
        rep.check(
            "работа измерима: получатели × круги",
            task.get("progress") == {"done": 0, "total": 4},
            f"{task.get('progress')}",
        )

        status, body = await cab.get("/api/library")
        texts = sorted(str(item.get("text")) for item in (body or {}).get("items") or [])
        rep.check("тексты из формы легли в библиотеку", texts == ["второе", "первое"], f"{texts}")

        # Многострочный текст — одно сообщение: прайс, объявление в три ряда.
        # Раньше резали по каждому переносу, и прайс уходил построчно.
        price = "Приму 1 код,момент\n4000 - 552\nПриму 1 код,момент\n4500 - 585"
        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "mailing",
                "account_id": account_id,
                "targets": ["@smoke-one"],
                "message": price,
            },
        )
        multiline = (body or {}).get("task") or {}
        rep.check(
            "прайс в четыре строки — одно сообщение, а не четыре",
            status == 201 and (multiline.get("mailing") or {}).get("messages_count") == 1,
            f"статус {status}, {(multiline.get('mailing') or {}).get('messages_count')}",
        )
        status, body = await cab.get("/api/library")
        saved = [item for item in (body or {}).get("items") or [] if item.get("text") == price]
        rep.check(
            "переносы сохранены, а в заголовке — первая строка",
            bool(saved) and saved[0].get("title") == "Приму 1 код,момент",
            f"{[item.get('title') for item in saved]}",
        )
        multiline_id = int(multiline.get("id") or 0)
        if multiline_id:
            await cab.delete(f"/api/tasks/{multiline_id}")
        for item in saved:
            await cab.delete(f"/api/library/{int(item.get('id') or 0)}")

        # Кабинет умеет не перепечатывать сохранённое: отмеченные в библиотеке
        # сообщения уходят в задачу ссылками (library_ids), а поле «Сообщение»
        # остаётся пустым. Проверяем, что такая задача заводится и знает, сколько
        # у неё текстов. Список берём свежий: выше библиотеку успели пополнить и
        # почистить, и ссылка на удалённую запись сбила бы счёт.
        status, body = await cab.get("/api/library")
        lib_ids = [int(item.get("id") or 0) for item in (body or {}).get("items") or []]
        status, from_lib = await cab.post(
            "/api/tasks",
            json={
                "command": "mailing",
                "account_id": account_id,
                "targets": ["@smoke-one"],
                "library_ids": lib_ids,
            },
        )
        lib_task = (from_lib or {}).get("task") or {}
        lib_info = lib_task.get("mailing") or {}
        rep.check(
            "рассылка из библиотеки — без текста в форме",
            status == 201 and lib_info.get("messages_count") == len(lib_ids),
            f"статус {status}, {lib_info}",
        )
        lib_task_id = int(lib_task.get("id") or 0)
        if lib_task_id:
            await cab.delete(f"/api/tasks/{lib_task_id}")

        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "mailing",
                "account_id": account_id,
                "targets": ["@нет-такого-чата"],
                "message": "текст",
            },
        )
        rep.check(
            "рассылать некуда — 400, а не «чат не найден»",
            status == 400 and "получател" in str((body or {}).get("error")),
            f"статус {status}, {(body or {}).get('error')}",
        )

        # ── Много чатов: предела числу получателей нет ──
        before = sweeps
        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "mailing",
                "account_id": account_id,
                "targets": list(many),
                "message": "массовая",
                "repeats": 1,
            },
        )
        big = (body or {}).get("task") or {}
        rep.check(
            f"рассылка на {len(many)} чатов — 201",
            status == 201 and (big.get("mailing") or {}).get("recipients") == len(many),
            f"статус {status}, {(body or {}).get('error') or (big.get('mailing') or {})}",
        )
        rep.check(
            "все чаты попали в задачу: счёт и работа сходятся",
            big.get("targets_count") == len(many)
            and (big.get("progress") or {}).get("total") == len(many),
            f"{big.get('targets_count')} / {big.get('progress')}",
        )
        rep.check(
            "чаты найдены одним обходом, а не по одному",
            sweeps - before == 1,
            f"обходов: {sweeps - before}",
        )
        big_id = int(big.get("id") or 0)
        if big_id:
            await cab.delete(f"/api/tasks/{big_id}")

        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "poster",
                "account_id": account_id,
                "targets": list(many),
                "message": "постим всем",
                "interval": 5,
            },
        )
        poster = (body or {}).get("task") or {}
        rep.check(
            f"постинг по расписанию на {len(many)} чатов — 201",
            status == 201 and poster.get("targets_count") == len(many),
            f"статус {status}, {(body or {}).get('error') or poster.get('targets_count')}",
        )
        rep.check(
            "в заголовке — счёт чатов, а не одно имя",
            "250" in str(poster.get("title")),
            f"{poster.get('title')}",
        )
        poster_id = int(poster.get("id") or 0)
        if poster_id:
            await cab.delete(f"/api/tasks/{poster_id}")

        # Одиночное поле «приёмник» постинг обязан принимать по-прежнему: с ним
        # приходят задачи из бота и старые ссылки на форму.
        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "poster",
                "account_id": account_id,
                "target": "@smoke-one",
                "message": "постим в один",
            },
        )
        one = (body or {}).get("task") or {}
        rep.check(
            "постинг с одним приёмником — по-прежнему 201",
            status == 201 and one.get("targets_count") == 1,
            f"статус {status}, {(body or {}).get('error') or one.get('targets_count')}",
        )
        one_id = int(one.get("id") or 0)
        if one_id:
            await cab.delete(f"/api/tasks/{one_id}")

        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "poster",
                "account_id": account_id,
                "targets": ["@smoke-one", "@нет-1", "@нет-2", "@нет-3", "@нет-4", "@нет-5"],
                "message": "текст",
            },
        )
        rep.check(
            "ненайденные чаты — одной короткой строкой со счётом",
            status == 404
            and "(5)" in str((body or {}).get("error"))
            and "и ещё 2" in str((body or {}).get("error")),
            f"статус {status}, {(body or {}).get('error')}",
        )

        # ── Пересылка в чаты: тот же список, что у постинга и рассылки ──
        # Отдельного поля «приёмник» у неё больше нет: чаты — один список, и
        # первый из них становится главным. Проверяем счёт (он раньше шёл по
        # настройкам и терял первый чат) и то, что источник не попал в получатели.
        before = sweeps
        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "broadcast",
                "account_id": account_id,
                "source": "@smoke-one",
                "targets": list(many),
            },
        )
        cast = (body or {}).get("task") or {}
        rep.check(
            f"пересылка в {len(many)} чатов — 201",
            status == 201 and cast.get("targets_count") == len(many),
            f"статус {status}, {(body or {}).get('error') or cast.get('targets_count')}",
        )
        rep.check(
            "в заголовке пересылки — счёт чатов",
            "250" in str(cast.get("title")),
            f"{cast.get('title')}",
        )
        rep.check(
            "источник и чаты найдены одним обходом",
            sweeps - before == 1,
            f"обходов: {sweeps - before}",
        )
        cast_id = int(cast.get("id") or 0)
        if cast_id:
            await cab.delete(f"/api/tasks/{cast_id}")

        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "broadcast",
                "account_id": account_id,
                "source": "@smoke-one",
                "targets": ["@smoke-one", "@smoke-two"],
            },
        )
        back = (body or {}).get("task") or {}
        rep.check(
            "источник в списке чатов не превращается в пересылку самому себе",
            status == 201 and back.get("targets_count") == 1,
            f"статус {status}, {(body or {}).get('error') or back.get('targets_count')}",
        )
        back_id = int(back.get("id") or 0)
        if back_id:
            await cab.delete(f"/api/tasks/{back_id}")

        # Одиночный «приёмник» пересылка принимает по-прежнему: так её создаёт бот.
        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "broadcast",
                "account_id": account_id,
                "source": "@smoke-one",
                "target": "@smoke-two",
            },
        )
        old = (body or {}).get("task") or {}
        rep.check(
            "пересылка с одним приёмником — по-прежнему 201",
            status == 201 and old.get("targets_count") == 1,
            f"статус {status}, {(body or {}).get('error') or old.get('targets_count')}",
        )
        old_id = int(old.get("id") or 0)
        if old_id:
            await cab.delete(f"/api/tasks/{old_id}")

        status, body = await cab.post(
            "/api/tasks",
            json={"command": "broadcast", "account_id": account_id, "source": "@smoke-one"},
        )
        rep.check(
            "пересылка без чатов — 400 и словом «чаты», а не «получателей»",
            status == 400 and "чаты" in str((body or {}).get("error")),
            f"статус {status}, {(body or {}).get('error')}",
        )

    # Прибираем за собой: следующие разделы видят кабинет без задач и с пустой
    # библиотекой — ровно таким, каким его оставил seed().
    task_id = int(task.get("id") or 0)
    if task_id:
        status, _ = await cab.delete(f"/api/tasks/{task_id}")
        rep.check("рассылка удаляется", status == 200, f"статус {status}")
    status, body = await cab.get("/api/library")
    for item in (body or {}).get("items") or []:
        await cab.delete(f"/api/library/{item.get('id')}")
    status, body = await cab.get("/api/library")
    rep.check("библиотека снова пуста", not ((body or {}).get("items") or []), f"{body}")
    rep.note("поиск чатов подменён заглушкой: Telegram в разделе не участвует")


async def check_task_edit(cab: Cabinet, rep: Report, account_id: int) -> None:
    """Правка готовой задачи: PATCH /api/tasks/{id}.

    Раньше поменять интервал, текст или список чатов можно было только
    пересозданием задачи — вместе с ней терялись счётчики, номер и место в круге
    рассылки. Проверяем на живом сокете то же, что тесты: меняется только
    присланное, прежние чаты второй раз не ищутся, а форма правки возвращается
    серверу без изменений.
    """
    rep.section("Правка задачи")

    chats = {"@edit-one": -3001, "@edit-two": -3002, "@edit-three": -3003}
    sweeps = 0

    async def resolve_many(_account_id: int, queries) -> dict[str, tuple[int, str]]:
        nonlocal sweeps
        sweeps += 1
        found: dict[str, tuple[int, str]] = {}
        for raw in queries:
            key = str(raw or "").strip()
            if key in chats:
                found[key] = (chats[key], key)
        return found

    task: dict = {}
    with configured(api_id=SMOKE_API_ID, api_hash=SMOKE_API_HASH), stubbed_gateway(
        resolve_many=resolve_many
    ):
        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "poster",
                "account_id": account_id,
                "targets": ["@edit-one", "@edit-two"],
                "message": "объявление",
                "interval": 5,
                "start": "09:00",
                "end": "21:00",
            },
        )
        task = (body or {}).get("task") or {}
        task_id = int(task.get("id") or 0)
        rep.check(
            "постинг для правки создан — 201",
            status == 201 and bool(task_id),
            f"статус {status}, {(body or {}).get('error') or ''}",
        )
        if not task_id:
            return

        rep.check(
            "у задачи есть форма правки и имена всех чатов",
            [chat.get("title") for chat in task.get("chats") or []] == ["@edit-one", "@edit-two"]
            and (task.get("edit") or {}).get("targets") == ["-3001", "-3002"]
            and ((task.get("edit") or {}).get("names") or {}).get("-3002") == "@edit-two",
            f"{task.get('chats')} / {(task.get('edit') or {}).get('targets')}",
        )

        status, body = await cab.patch(f"/api/tasks/{task_id}", json={"interval": 15})
        saved = (body or {}).get("task") or {}
        rep.check(
            "интервал поменялся, задача осталась той же",
            status == 200 and saved.get("id") == task_id and saved.get("interval_min") == 15,
            f"статус {status}, {(body or {}).get('error') or saved.get('interval_min')}",
        )
        rep.check(
            "остальные настройки правка не тронула",
            (saved.get("window_start"), saved.get("window_end")) == ("09:00", "21:00")
            and saved.get("messages_count") == 1
            and saved.get("targets_count") == 2,
            f"{saved.get('window_start')}–{saved.get('window_end')}, "
            f"{saved.get('messages_count')} сообщ., {saved.get('targets_count')} чат.",
        )

        # Счётчик отправок — не настройка: пересоздание задачи его обнуляло.
        async with session_scope() as session:
            rule = await repo.get_rule(session, task_id, SMOKE_USER_ID)
            rule.forwarded_count = 17
        status, body = await cab.patch(f"/api/tasks/{task_id}", json={"message": "другое"})
        saved = (body or {}).get("task") or {}
        rep.check(
            "счётчик отправок правку переживает",
            status == 200 and saved.get("forwarded") == 17,
            f"статус {status}, отправлено {saved.get('forwarded')}",
        )

        before = sweeps
        status, body = await cab.patch(
            f"/api/tasks/{task_id}",
            json={"targets": (task.get("edit") or {}).get("targets"), "interval": 7},
        )
        rep.check(
            "прежние чаты второй раз не ищутся",
            status == 200 and sweeps == before,
            f"статус {status}, обходов: {sweeps - before}",
        )

        before = sweeps
        status, body = await cab.patch(
            f"/api/tasks/{task_id}",
            json={"targets": [*((task.get("edit") or {}).get("targets") or []), "@edit-three"]},
        )
        saved = (body or {}).get("task") or {}
        rep.check(
            "новый чат добавляется одним обходом",
            status == 200 and saved.get("targets_count") == 3 and sweeps - before == 1,
            f"статус {status}, {saved.get('targets_count')} чат., обходов: {sweeps - before}",
        )

        status, body = await cab.patch(
            f"/api/tasks/{task_id}", json={"targets": ["-3001", "@нет-такого"]}
        )
        rep.check(
            "опечатка в ссылке — 404, список чатов остаётся прежним",
            status == 404 and "Не нашёл" in str((body or {}).get("error")),
            f"статус {status}, {(body or {}).get('error')}",
        )
        status, body = await cab.get("/api/tasks")
        alive = [t for t in (body or {}).get("tasks") or [] if t.get("id") == task_id]
        rep.check(
            "и задача работает на трёх чатах",
            bool(alive) and alive[0].get("targets_count") == 3,
            f"{alive[0].get('targets_count') if alive else 'задачи нет'}",
        )

        status, body = await cab.patch(f"/api/tasks/{task_id}", json={"message": ""})
        rep.check(
            "пустое обязательное поле — 400, а не «оставить как было»",
            status == 400 and "сообщение" in str((body or {}).get("error")),
            f"статус {status}, {(body or {}).get('error')}",
        )

        # Что кабинет подставил в форму, то сервер принимает обратно без правок.
        status, body = await cab.get("/api/tasks")
        current = next(
            (t for t in (body or {}).get("tasks") or [] if t.get("id") == task_id), {}
        )
        status, body = await cab.patch(f"/api/tasks/{task_id}", json=current.get("edit") or {})
        saved = (body or {}).get("task") or {}
        rep.check(
            "форма правки возвращается серверу без изменений",
            status == 200
            and saved.get("edit") == current.get("edit")
            and saved.get("title") == current.get("title"),
            f"статус {status}, {(body or {}).get('error') or ''}",
        )

    # Текст и интервал правятся даже при закрытом входе в аккаунт: прежние чаты
    # у задачи уже есть вместе с именами, обход диалогов нужен только новым.
    # Раньше «переделать задачу» означало создать её заново — а создание без
    # входа невозможно, и поправить свой же текст было нельзя вообще никак.
    with configured(api_id=0, api_hash=""):
        status, body = await cab.patch(f"/api/tasks/{task_id}", json={"message": "новый текст"})
        rep.check(
            "текст правится при закрытом входе в аккаунт",
            status == 200,
            f"статус {status}, {(body or {}).get('error') or ''}",
        )
        status, body = await cab.patch(f"/api/tasks/{task_id}", json={"targets": ["@edit-new"]})
        rep.check(
            "а новый чат без входа — 503 с причиной, а не пятисотка",
            status == 503 and (body or {}).get("feature") == "account_login",
            f"статус {status}, feature={(body or {}).get('feature')}",
        )

    await cab.post(f"/api/tasks/{task_id}/archive")
    status, body = await cab.patch(f"/api/tasks/{task_id}", json={"interval": 3})
    rep.check(
        "архивную задачу не правим — 409",
        status == 409 and "архив" in str((body or {}).get("error")),
        f"статус {status}, {(body or {}).get('error')}",
    )

    status, _ = await cab.patch("/api/tasks/10000000", json={"interval": 3})
    rep.check("чужая или удалённая задача — 404", status == 404, f"статус {status}")
    status, _ = await cab.patch(f"/api/tasks/{task_id}", data="не json")
    rep.check("тело правки не JSON — 400", status == 400, f"статус {status}")

    # Прибираем за собой: следующие разделы видят кабинет таким, каким его
    # оставил seed().
    status, _ = await cab.delete(f"/api/tasks/{task_id}")
    rep.check("задача прогона удалена", status == 200, f"статус {status}")
    rep.note("поиск чатов подменён заглушкой: Telegram в разделе не участвует")


async def check_task_health(cab: Cabinet, rep: Report, account_id: int) -> None:
    """Здоровье задачи на карточке: когда сработала и на чём сломалась.

    В журнал пересылок писали четыре места, а читать его не умел никто: карточка
    показывала «работает» задаче, которая последние сутки только падала, а
    причину было видно лишь в логе службы на сервере. Здесь весь путь проверяется
    на живом сокете — от записи в журнале до строки, которую увидит человек:
    пометка UTC (без неё «5 минут назад» съезжает на часовой пояс), название чата
    вместо его id, обрезка под узкий экран и чистка старых записей.
    """
    rep.section("Здоровье задачи")

    async def resolve_many(_account_id: int, queries) -> dict[str, tuple[int, str]]:
        names = {
            "@health-one": (-1001234567890, "Афиша"),
            "@health-two": (-1009876543210, "Зеркало афиши"),
        }
        asked = [str(raw or "").strip() for raw in queries]
        return {ref: names[ref] for ref in asked if ref in names}

    async def journal(rule_id: int, *, status: str = "ok", error: str = "", age_days: int = 0):
        """Строка журнала — тем же вызовом, каким её пишет планировщик."""
        async with session_scope() as session:
            await repo.log_forward(
                session,
                rule_id=rule_id,
                user_id=SMOKE_USER_ID,
                source_msg_id=0,
                target_msg_id=None,
                status=status,
                error=error or None,
            )
            if age_days:
                newest = await session.execute(
                    select(ForwardLog)
                    .where(ForwardLog.rule_id == rule_id)
                    .order_by(ForwardLog.id.desc())
                    .limit(1)
                )
                newest.scalar_one().created_at = repo.utcnow() - timedelta(days=age_days)

    async def card(rule_id: int) -> dict:
        """Карточка задачи так, как её видит кабинет — из общего списка."""
        _, body = await cab.get("/api/tasks")
        for task in (body or {}).get("tasks") or []:
            if task.get("id") == rule_id:
                return task
        return {}

    with configured(api_id=SMOKE_API_ID, api_hash=SMOKE_API_HASH), stubbed_gateway(
        resolve_many=resolve_many
    ):
        status, body = await cab.post(
            "/api/tasks",
            json={
                "command": "poster",
                "account_id": account_id,
                "targets": ["@health-one", "@health-two"],
                "message": "объявление",
                "interval": 5,
            },
        )
    task = (body or {}).get("task") or {}
    task_id = int(task.get("id") or 0)
    if not rep.check(
        "постинг для журнала создан — 201",
        status == 201 and bool(task_id),
        f"статус {status}, {(body or {}).get('error') or ''}",
    ):
        return

    rep.check(
        "у новой задачи здоровье есть, но пустое",
        task.get("health") == {"ok_at": None, "error": None, "error_at": None, "failing": False},
        f"{task.get('health')}",
    )

    await journal(task_id)
    health = (await card(task_id)).get("health") or {}
    rep.check(
        "время последней отправки помечено UTC",
        str(health.get("ok_at") or "").endswith("+00:00") and health.get("failing") is False,
        f"{health}",
    )

    await journal(
        task_id, status="error", error="не ушло в -1009876543210: ChatWriteForbiddenError"
    )
    health = (await card(task_id)).get("health") or {}
    rep.check(
        "сбой виден, id чата заменён названием",
        health.get("failing") is True
        and health.get("error") == "не ушло в Зеркало афиши: ChatWriteForbiddenError",
        f"{health}",
    )

    await journal(task_id)
    health = (await card(task_id)).get("health") or {}
    rep.check(
        "задача заработала — предупреждение не кричит, причина осталась",
        health.get("failing") is False and bool(health.get("error")),
        f"{health}",
    )

    await journal(task_id, status="error", error="очень длинная причина " * 40)
    health = (await card(task_id)).get("health") or {}
    reason = str(health.get("error") or "")
    rep.check(
        "длинная причина обрезана под узкий экран",
        reason.endswith("…") and len(reason) <= 161,
        f"{len(reason)} символов",
    )

    status, body = await cab.post(f"/api/tasks/{task_id}/toggle")
    single = ((body or {}).get("task") or {}).get("health") or {}
    rep.check(
        "ответ на одну задачу того же состава, что карточка в списке",
        status == 200 and bool(single.get("error")) and single.get("failing") is True,
        f"статус {status}, {single}",
    )
    # Возвращаем задачу в работу: список задач по умолчанию отдаёт активные, и
    # снятая с работы карточка в него не попадёт.
    await cab.post(f"/api/tasks/{task_id}/toggle")

    # Чистка журнала: он растёт быстрее остальных таблиц — по строке на каждый
    # проход постинга, — а нужен только для ответа «работает ли задача».
    await journal(task_id, age_days=repo.FORWARD_LOG_TTL_DAYS + 5)
    async with session_scope() as session:
        dropped = await repo.trim_forward_logs(session)
    health = (await card(task_id)).get("health") or {}
    rep.check(
        "старая запись убрана, свежие на месте",
        dropped == 1 and bool(health.get("error")),
        f"убрано {dropped}, {health}",
    )

    status, _ = await cab.delete(f"/api/tasks/{task_id}")
    rep.check("задача прогона удалена", status == 200, f"статус {status}")
    rep.note("журнал наполнен вручную: планировщик и Telegram в разделе не участвуют")


async def check_task_cleanup(cab: Cabinet, rep: Report, account_id: int) -> None:
    """Удалённая задача не оставляет следов, а её номер достаётся следующей.

    Три таблицы ссылаются на задачу номером без внешнего ключа: журнал
    пересылок, находки и очередь недосланных сообщений. SQLite выдаёт номера по
    правилу «наибольший плюс один», поэтому номер удалённой задачи получает
    следующая созданная — вместе с чужим сбоем на карточке и чужими находками в
    «Результатах». Проверяем на живом сокете обе половины: удаление чистит за
    собой, а фоновый проход лечит базы, где задачи удаляли раньше.
    """
    rep.section("Следы удалённой задачи")

    async def resolve_many(_account_id: int, queries) -> dict[str, tuple[int, str]]:
        asked = [str(raw or "").strip() for raw in queries]
        return {ref: (-1005550001, "Старый источник") for ref in asked if ref == "@gone"}

    async def rows(rule_id: int) -> dict[str, int]:
        async with session_scope() as session:
            counts = {}
            for model in (ForwardLog, CollectedItem, PendingDelivery):
                counts[model.__tablename__] = int(
                    (
                        await session.execute(
                            select(func.count())
                            .select_from(model)
                            .where(model.rule_id == rule_id)
                        )
                    ).scalar_one()
                )
            return counts

    async def make_parser() -> tuple[int, dict]:
        """Парсер: у него на карточке видны и сбой, и число находок."""
        with configured(api_id=SMOKE_API_ID, api_hash=SMOKE_API_HASH), stubbed_gateway(
            resolve_many=resolve_many
        ):
            status, body = await cab.post(
                "/api/tasks",
                json={
                    "command": "parser",
                    "account_id": account_id,
                    "source": "@gone",
                    "limit": 100,
                },
            )
        task = (body or {}).get("task") or {}
        return status, task

    async def fill(rule_id: int) -> None:
        """Задача поработала: сбой в журнале, находка и недосланное сообщение."""
        async with session_scope() as session:
            await repo.log_forward(
                session,
                rule_id=rule_id,
                user_id=SMOKE_USER_ID,
                source_msg_id=1,
                target_msg_id=None,
                status="error",
                error="чат закрыт",
            )
            session.add(
                CollectedItem(
                    rule_id=rule_id,
                    user_id=SMOKE_USER_ID,
                    kind="parser",
                    payload={"id": 7, "username": "gone"},
                )
            )
            await repo.remember_pending_delivery(
                session,
                rule_id=rule_id,
                user_id=SMOKE_USER_ID,
                account_id=account_id,
                source_chat_id=-1005550001,
                message_id=900,
            )

    status, task = await make_parser()
    doomed = int(task.get("id") or 0)
    if not rep.check(
        "парсер для проверки создан — 201",
        status == 201 and bool(doomed),
        f"статус {status}, {(task or {}).get('error') or ''}",
    ):
        return

    await fill(doomed)
    _, body = await cab.get(f"/api/tasks/{doomed}/results")
    before = (await cab.get("/api/tasks"))[1] or {}
    card = next(
        (item for item in (before.get("tasks") or []) if item.get("id") == doomed), {}
    )
    rep.check(
        "у задачи есть история: сбой и находка",
        (card.get("health") or {}).get("failing") is True
        and (card.get("progress") or {}).get("done") == 1
        and (body or {}).get("total") == 1,
        f"{card.get('health')}, найдено {(card.get('progress') or {}).get('done')}",
    )

    status, _ = await cab.delete(f"/api/tasks/{doomed}")
    left = await rows(doomed)
    rep.check(
        "удаление унесло журнал, находки и очередь",
        status == 200 and set(left.values()) == {0},
        f"статус {status}, осталось {left}",
    )

    status, reborn_task = await make_parser()
    reborn = int(reborn_task.get("id") or 0)
    rep.check(
        "номер удалённой задачи достался новой",
        reborn == doomed,
        f"было #{doomed}, стало #{reborn}",
    )
    rep.check(
        "новая задача с тем же номером — с чистой карточкой",
        reborn_task.get("health")
        == {"ok_at": None, "error": None, "error_at": None, "failing": False}
        and (reborn_task.get("progress") or {}).get("done") == 0,
        f"{reborn_task.get('health')}, найдено "
        f"{(reborn_task.get('progress') or {}).get('done')}",
    )

    # База после старого удаления: строки есть, задачи нет. Так выглядели все
    # базы до этой правки — их лечит фоновый проход, а не ручные запросы.
    await fill(reborn)
    async with session_scope() as session:
        rule = await repo.get_rule(session, reborn, SMOKE_USER_ID)
        await session.delete(rule)
    async with session_scope() as session:
        dropped = await repo.drop_orphan_records(session)
    rep.check(
        "фоновый проход убирает следы задач, которых уже нет",
        dropped == {"forward_logs": 1, "collected_items": 1, "pending_deliveries": 1},
        f"{dropped}",
    )
    async with session_scope() as session:
        rep.check(
            "на чистой базе проход молчит",
            await repo.drop_orphan_records(session) == {},
            "проход нашёл лишнее",
        )


async def check_chats_and_accounts(cab: Cabinet, rep: Report, account_id: int) -> None:
    """Чаты и аккаунты: пустой список тут — честный ответ, а не поломка."""
    rep.section("Чаты и аккаунты")

    status, body = await cab.get(f"/api/chats?account_id={account_id}")
    if settings.public_login_enabled:
        rep.check(
            "чаты аккаунта — список без ошибки",
            status == 200 and isinstance((body or {}).get("chats"), list),
            f"статус {status}",
        )
    else:
        rep.check(
            "чаты — пусто и с объяснением, почему",
            status == 200
            and (body or {}).get("chats") == []
            and (body or {}).get("feature") == "account_login"
            and bool((body or {}).get("note")),
            f"статус {status}",
        )

    status, body = await cab.get("/api/chats?account_id=999999999")
    rep.check(
        "чужой аккаунт — пусто с пометкой",
        status == 200 and "не найден" in str((body or {}).get("note", "")).lower(),
        f"статус {status}",
    )

    # Список чатов не обрезан: задачи ходят в любое их число, и чат, которого нет
    # в списке, нельзя выбрать мышкой. Диалоги подменяем — Telegram тут не участвует.
    dialogs = [
        {"id": -3000 - n, "title": f"смоук-чат {n}", "username": None, "kind": "group"}
        for n in range(300)
    ]

    async def list_dialogs(_account_id: int, limit: int = 0):
        return dialogs[:limit] if limit > 0 else list(dialogs)

    with configured(api_id=SMOKE_API_ID, api_hash=SMOKE_API_HASH), stubbed_gateway(
        list_dialogs=list_dialogs
    ):
        status, body = await cab.get(f"/api/chats?account_id={account_id}")
        rep.check(
            f"чаты отдаются все {len(dialogs)}, без обрезки",
            status == 200 and (body or {}).get("total") == len(dialogs),
            f"статус {status}, всего {(body or {}).get('total')}",
        )
        status, body = await cab.get(f"/api/chats?account_id={account_id}&limit=10")
        rep.check(
            "короткая витрина по limit — по-прежнему работает",
            status == 200 and (body or {}).get("total") == 10,
            f"всего {(body or {}).get('total')}",
        )
        status, body = await cab.get(f"/api/chats?account_id={account_id}&q=чат 299")
        rep.check(
            "поиск достаёт чат из хвоста списка",
            status == 200 and (body or {}).get("total") == 1,
            f"нашлось {(body or {}).get('total')}",
        )

    status, body = await cab.get("/api/accounts")
    if not rep.check("GET /api/accounts — 200", status == 200, f"статус {status}"):
        return
    accounts = (body or {}).get("accounts") or []
    rep.check(
        "аккаунт виден с телефоном и состоянием",
        len(accounts) == 1
        and accounts[0].get("phone") == "+79000000000"
        and accounts[0].get("online") is False,
        f"{accounts}",
    )
    rep.check(
        "незавершённого входа нет",
        (body or {}).get("pending_login", {}).get("exists") is False,
    )
    rep.check(
        "подписка и копилка в одном ответе",
        (body or {}).get("subscription", {}).get("active") is True
        and (body or {}).get("subscription", {}).get("piggy_bank_days") == 0,
        f"{(body or {}).get('subscription')}",
    )


async def walk_login(cab: Cabinet, rep: Report) -> None:
    """Проходит шаги входа по HTTP и сверяет состояние в ответе /api/accounts."""
    status, body = await cab.post("/api/accounts/login/start", json={"phone": "телефон"})
    rep.check(
        "мусор вместо номера — 400 с примером формата",
        status == 400 and "+79001234567" in str((body or {}).get("error")),
        f"статус {status}",
    )

    status, body = await cab.post("/api/accounts/login/start", json={"phone": LOGIN_PHONE})
    rep.check(
        "шаг 1: код запрошен",
        status == 200 and (body or {}).get("stage") == "code",
        f"статус {status}, {body}",
    )
    status, body = await cab.get("/api/accounts")
    pending = (body or {}).get("pending_login") or {}
    rep.check(
        "незавершённый вход виден кабинету",
        pending.get("exists") is True
        and pending.get("step") == "code"
        and pending.get("attempts_left") == accounts_login.MAX_CODE_ATTEMPTS,
        f"{pending}",
    )

    status, body = await cab.post("/api/accounts/login/code", json={"code": "00000"})
    rep.check(
        "неверный код — 400, вход остаётся на том же шаге",
        status == 400 and "Осталось попыток" in str((body or {}).get("error")),
        f"статус {status}, {(body or {}).get('error')}",
    )
    status, body = await cab.get("/api/accounts")
    rep.check(
        "попытка списана, шаг не сброшен",
        (body or {}).get("pending_login", {}).get("attempts_left")
        == accounts_login.MAX_CODE_ATTEMPTS - 1,
        f"{(body or {}).get('pending_login')}",
    )

    status, body = await cab.post("/api/accounts/login/code", json={"code": "11111"})
    rep.check(
        "шаг 2: включён 2FA — просим облачный пароль",
        status == 200 and (body or {}).get("stage") == "password",
        f"статус {status}, {body}",
    )

    status, body = await cab.post("/api/accounts/login/password", json={"password": "секрет"})
    account_id = (body or {}).get("account_id")
    rep.check(
        "шаг 3: аккаунт подключён",
        status == 200 and (body or {}).get("stage") == "done" and bool(account_id),
        f"статус {status}, {body}",
    )
    await check_login_result(cab, rep, account_id)


async def check_login_result(cab: Cabinet, rep: Report, account_id: int | None) -> None:
    """Итог входа: аккаунт в списке, сессия зашифрована, отключение работает."""
    status, body = await cab.get("/api/accounts")
    phones = [item.get("phone") for item in (body or {}).get("accounts") or []]
    rep.check(
        "аккаунт появился в кабинете, незавершённого входа больше нет",
        LOGIN_PHONE in phones and (body or {}).get("pending_login", {}).get("exists") is False,
        f"{phones}",
    )

    async with session_scope() as session:
        rows = [
            item
            for item in await repo.list_accounts(session, SMOKE_USER_ID)
            if item.phone == LOGIN_PHONE
        ]
    stored = rows[0].session_encrypted if rows else ""
    rep.check(
        "сессия в БД только шифром",
        bool(stored) and "smoke-session-after-2fa" not in stored,
        "иначе доступ к аккаунту утекает вместе с дампом базы",
    )

    status, body = await cab.post("/api/accounts/login/cancel")
    rep.check(
        "отмена без незавершённого входа — честное dropped: false",
        status == 200 and (body or {}).get("dropped") is False,
        f"статус {status}, {body}",
    )

    status, body = await cab.delete(f"/api/accounts/{account_id}")
    rep.check(
        "отключение аккаунта убирает его вместе с сессией",
        status == 200 and (body or {}).get("phone") == LOGIN_PHONE,
        f"статус {status}, {body}",
    )
    status, _ = await cab.delete(f"/api/accounts/{account_id}")
    rep.check("повторное отключение — 404", status == 404, f"статус {status}")


async def check_account_login(cab: Cabinet, rep: Report) -> None:
    """Подключение аккаунта целиком: номер → код → пароль 2FA, не выходя из кабинета.

    Раньше кнопка «Подключить аккаунт» умела единственное — открыть чат с ботом,
    и человек уходил из мини-аппа на середине пути. Теперь вход проходит здесь,
    значит и проверять его надо на живом сокете. Telegram при этом не участвует:
    шлюз подменён заглушками (см. stubbed_gateway).
    """
    rep.section("Подключение аккаунта")

    with configured(api_id=0, api_hash=""):
        status, body = await cab.post("/api/accounts/login/start", json={"phone": LOGIN_PHONE})
        rep.check(
            "без MTProto-шлюза — 503 с причиной, а не молчание",
            status == 503
            and (body or {}).get("feature") == "account_login"
            and (body or {}).get("status") == "setup_required",
            f"статус {status}",
        )

    attempts = {"code": 0}

    async def send_code(_phone: str) -> tuple[str, str]:
        return "smoke-temp-session", "smoke-code-hash"

    async def sign_in_code(**_kwargs: Any) -> str:
        attempts["code"] += 1
        if attempts["code"] == 1:
            # Первая попытка — «опечатка в цифре»: вход обязан её пережить.
            raise PhoneCodeInvalidError(request=None)
        raise SessionPasswordNeededError(request=None)

    async def sign_in_password(_password: str, _session: str) -> str:
        return "smoke-session-after-2fa"

    async def check_session(_session: str) -> tuple[bool, str | None, str | None]:
        return True, "Дымовой прогон", None

    async def start_account(_account: Any, _session: str) -> bool:
        return True

    async def stop_account(_account_id: int) -> None:
        return None

    async def refresh_rules() -> None:
        return None

    with configured(api_id=SMOKE_API_ID, api_hash=SMOKE_API_HASH), stubbed_gateway(
        send_code=send_code,
        sign_in_code=sign_in_code,
        sign_in_password=sign_in_password,
        check_session=check_session,
        start_account=start_account,
        stop_account=stop_account,
        refresh_rules=refresh_rules,
    ):
        await walk_login(cab, rep)
    rep.note("Telegram в разделе не участвует: код, вход и проверка сессии — заглушки")


async def check_subscription(cab: Cabinet, rep: Report, me: dict) -> None:
    """Абонемент: состояние, цены, счёт в звёздах и копилка дней."""
    rep.section("Абонемент и оплата звёздами")

    status, body = await cab.get("/api/subscription")
    if not rep.check("GET /api/subscription — 200", status == 200, f"статус {status}"):
        return
    rep.check(
        "срок и остаток дней на месте",
        body.get("active") is True and body.get("until") and body.get("days_left", 0) >= 2,
        f"days_left={body.get('days_left')}",
    )
    # Кабинет читает контур оплаты из двух ответов — они обязаны совпадать,
    # иначе кнопки в разных разделах предлагают разные способы.
    same_pay = body.get("pay") == me.get("pay")
    rep.check(
        "контур оплаты совпадает с /api/me",
        same_pay,
        "" if same_pay else f"{body.get('pay')} ≠ {me.get('pay')}",
    )

    status, body = await cab.post("/api/subscription/invoice")
    rep.check(
        "счёт в звёздах без бота — 503 с причиной",
        status == 503
        and (body or {}).get("feature") == "stars"
        and (body or {}).get("status") == "bot_unavailable",
        f"статус {status}, {body}",
    )
    for months in ("много", 2, 1.5, 0):
        status, _ = await cab.post("/api/subscription/invoice", json={"months": months})
        rep.check(f"срок {months!r} — 400", status == 400, f"статус {status}")
    rep.note("сам счёт Stars не выставляем: это вызов Bot API, а прогон offline")

    rep.section("Копилка дней")
    status, body = await cab.post("/api/subscription/bank", json={"days": 1})
    rep.check(
        "день уходит в копилку",
        status == 200 and (body or {}).get("moved") == 1 and (body or {}).get("banked_days") == 1,
        f"статус {status}, {body}",
    )
    status, body = await cab.post("/api/subscription/bank", json={})
    rep.check(
        "сутки активного периода заморозить не дают — 409",
        status == 409,
        f"статус {status}",
    )
    status, body = await cab.post("/api/subscription/distribute", json={})
    rep.check(
        "дни возвращаются в абонемент",
        status == 200
        and (body or {}).get("moved") == 1
        and (body or {}).get("banked_days") == 0
        and (body or {}).get("active") is True,
        f"статус {status}, {body}",
    )
    status, body = await cab.post("/api/subscription/distribute", json={})
    rep.check("пустая копилка — 409", status == 409, f"статус {status}")


async def check_pay_disabled(cab: Cabinet, rep: Report) -> None:
    """Контур выключен: отказ должен быть внятным, а не пустой страницей."""
    rep.section("Оплата вне Telegram: контур выключен")

    with configured(pay_mode="stars"):
        token = paylink.make_token(SMOKE_USER_ID, 3)

        status, body = await cab.get("/pay", auth=False)
        rep.check(
            "GET /pay — 404 с объяснением",
            status == 404 and "отключена" in str(body),
            f"статус {status}",
        )
        status, body = await cab.get("/api/pay/link")
        rep.check(
            "ссылку на страницу не выдаём — 503",
            status == 503 and (body or {}).get("feature") == "external",
            f"статус {status}",
        )
        status, body = await cab.get(f"/api/pay/info?t={token}", auth=False)
        rep.check(
            "данные страницы — 503",
            status == 503 and (body or {}).get("status") == "disabled",
            f"статус {status}",
        )
        status, body = await cab.post(
            "/api/pay/start", auth=False, json={"t": token, "method": "usdt"}
        )
        rep.check(
            "счёт по выключенному способу не выставляется — 503",
            status == 503 and (body or {}).get("feature") == "usdt",
            f"статус {status}",
        )


async def check_pay_page(cab: Cabinet, rep: Report) -> None:
    """Контур включён: страница, реквизиты и счёт USDT — целиком, без сети."""
    rep.section("Оплата вне Telegram: страница и USDT")

    with configured(
        pay_mode="external",
        webapp_url="https://smoke.example.test",
        usdt_wallet=USDT_WALLET,
        yookassa_shop_id=None,
        yookassa_secret_key=None,
    ):
        status, body = await cab.get("/api/pay/link")
        ok = status == 200 and str((body or {}).get("url", "")).startswith(
            "https://smoke.example.test/pay?t="
        )
        rep.check("кабинет получает свежую ссылку", ok, f"статус {status}")
        rep.check(
            "на странице только карта и крипта",
            (body or {}).get("methods") == ["usdt"],
            f"{(body or {}).get('methods')} (ключей ЮKassa в прогоне нет)",
        )
        rep.check(
            "срок жизни ссылки — час",
            (body or {}).get("expires_in") == paylink.TOKEN_TTL_SECONDS,
            f"{(body or {}).get('expires_in')} сек",
        )
        token = paylink.make_token(SMOKE_USER_ID, 3)

        status, body = await cab.get(f"/pay?t={token}", auth=False)
        rep.check(
            "страница оплаты открывается по подписанной ссылке",
            status == 200 and "Оплата абонемента" in str(body),
            f"статус {status}",
        )
        for name, path in (
            ("без токена", "/pay"),
            ("с подделанным токеном", "/pay?t=1.1.99999999999.xxx"),
            ("с просроченным токеном", f"/pay?t={paylink.make_token(SMOKE_USER_ID, 3, ttl=-10)}"),
        ):
            status, _ = await cab.get(path, auth=False)
            rep.check(f"страница {name} — 410", status == 410, f"статус {status}")

        await check_pay_info(cab, rep, token)
        await check_pay_start(cab, rep, token)


async def check_pay_info(cab: Cabinet, rep: Report, token: str) -> None:
    """Что видит страница оплаты: способы, сроки и цены — считает их сервер."""
    status, body = await cab.get(f"/api/pay/info?t={token}", auth=False)
    if not rep.check("GET /api/pay/info — 200", status == 200, f"статус {status}"):
        return
    rep.check(
        "срок берётся из подписи ссылки",
        body.get("months") == 3,
        f"months={body.get('months')}",
    )
    rep.check(
        "остаток жизни ссылки честный",
        0 < body.get("expires_in", 0) <= paylink.TOKEN_TTL_SECONDS,
        f"{body.get('expires_in')} сек",
    )
    prices = {item["months"]: (item["rub"], item["usdt"]) for item in body.get("periods", [])}
    expected = {months: (rub_amount(months), usdt_amount(months)) for months in PERIODS}
    rep.check("цены всех сроков считает сервер", prices == expected, f"{prices}")
    rep.check(
        "ссылки на бота нет, пока нет имени бота",
        body.get("bot_url") is None,
        "иначе кнопка вела бы на главную Telegram",
    )

    status, _ = await cab.get("/api/pay/info?t=подделка", auth=False)
    rep.check("битый токен — 400", status == 400, f"статус {status}")


async def check_pay_start(cab: Cabinet, rep: Report, token: str) -> None:
    """Счёт USDT: реквизиты, уникальная метка-сумма и защита от перебора."""
    status, body = await cab.post(
        "/api/pay/start", auth=False, json={"t": token, "method": "usdt", "months": 3}
    )
    if not rep.check("POST /api/pay/start (usdt) — 200", status == 200, f"статус {status}"):
        return

    rep.check(
        "реквизиты: кошелёк, сеть, сумма",
        body.get("wallet") == USDT_WALLET
        and "TRC-20" in str(body.get("network"))
        and body.get("currency") == "USDT",
        f"{body.get('network')}",
    )
    expected_micro = crypto.to_micro(usdt_amount(3)) + body["payment_id"] * crypto.MEMO_STEP_MICRO
    rep.check(
        "метка-сумма привязана к номеру счёта",
        crypto.to_micro(body["memo"]) == expected_micro,
        f"memo={body.get('memo')}",
    )

    _, second = await cab.post(
        "/api/pay/start", auth=False, json={"t": token, "method": "usdt", "months": 3}
    )
    rep.check(
        "второй счёт получает свою метку",
        (second or {}).get("memo") != body.get("memo"),
        f"{body.get('memo')} → {(second or {}).get('memo')}",
    )

    status, _ = await cab.post(
        "/api/pay/start", auth=False, json={"t": token, "method": "yookassa"}
    )
    rep.check("ненастроенная карта — 503", status == 503, f"статус {status}")
    status, _ = await cab.post(
        "/api/pay/start", auth=False, json={"t": token, "method": "usdt", "months": 2}
    )
    rep.check("срок не из каталога — 400", status == 400, f"статус {status}")
    expired = paylink.make_token(SMOKE_USER_ID, 3, ttl=-10)
    status, _ = await cab.post(
        "/api/pay/start", auth=False, json={"t": expired, "method": "usdt"}
    )
    rep.check("просроченная ссылка — 400", status == 400, f"статус {status}")
    status, _ = await cab.post("/api/pay/start", auth=False, data="не json")
    rep.check("тело не JSON — 400", status == 400, f"статус {status}")

    # Каждый счёт занимает уникальную метку, поэтому перебор кнопки ограничен.
    # Цикл ограничен сверху: если лимит не сработает, прогон не должен зависнуть.
    for _ in range(service.MAX_PENDING_PER_METHOD + 2):
        status, _ = await cab.post(
            "/api/pay/start", auth=False, json={"t": token, "method": "usdt"}
        )
        if status != 200:
            break
    rep.check(
        f"больше {service.MAX_PENDING_PER_METHOD} неоплаченных счетов — 409",
        status == 409,
        f"статус {status}",
    )


async def check_pay_card(cab: Cabinet, rep: Report) -> None:
    """Счёт картой целиком, кроме самого вызова ЮKassa.

    Провайдера подменяем заглушкой: настоящий вызов — это сеть и живой магазин,
    а проверить нужно своё — строку платежа, сохранённый номер счёта у
    провайдера и ответ странице оплаты.
    """
    rep.section("Оплата вне Telegram: карта")

    async def fake_invoice(**_kwargs: Any) -> tuple[str, str]:
        return "https://yookassa.smoke/checkout/1", "yoo-smoke-1"

    real_invoice = yookassa.create_invoice
    yookassa.create_invoice = fake_invoice  # type: ignore[assignment]
    try:
        with configured(
            pay_mode="external",
            webapp_url="https://smoke.example.test",
            usdt_wallet=USDT_WALLET,
            yookassa_shop_id="smoke-shop",
            yookassa_secret_key="smoke-secret",
        ):
            token = paylink.make_token(SMOKE_USER_ID, 12)
            status, body = await cab.get(f"/api/pay/info?t={token}", auth=False)
            rep.check(
                "с ключами ЮKassa на странице оба способа",
                status == 200 and (body or {}).get("methods") == ["yookassa", "usdt"],
                f"{(body or {}).get('methods')}",
            )

            status, body = await cab.post(
                "/api/pay/start", auth=False, json={"t": token, "method": "yookassa"}
            )
            if not rep.check(
                "POST /api/pay/start (карта) — 200", status == 200, f"статус {status}"
            ):
                return
            rep.check(
                "в ответе ссылка на оплату и сумма от сервера",
                body.get("url") == "https://yookassa.smoke/checkout/1"
                and body.get("amount") == rub_amount(12)
                and body.get("currency") == "RUB",
                f"{body.get('amount')} ₽ за {body.get('months')} мес.",
            )

            async with session_scope() as session:
                rows = {p.id: p for p in await repo.pending_payments(session, "yookassa")}
            saved = rows.get(body.get("payment_id"))
            rep.check(
                "номер счёта у провайдера сохранён в БД",
                saved is not None and saved.external_id == "yoo-smoke-1",
                "без него фоновая проверка не найдёт платёж",
            )
    finally:
        yookassa.create_invoice = real_invoice  # type: ignore[assignment]
    rep.note("вызов ЮKassa заменён заглушкой: прогон не ходит в сеть и не создаёт счёт")


async def check_bonus(cab: Cabinet, rep: Report) -> None:
    """Подарок за подписку на канал: выключенный, без бота и с ботом.

    Проверка подписки — единственный вызов Bot API в этом разделе, поэтому бот
    подменяется заглушкой: наружу прогон не ходит, а проверять надо своё —
    коды отказов, разовость подарка и то, что дни действительно прибавились.
    """
    rep.section("Подарок за подписку на канал")

    with configured(bonus_channel=None):
        status, body = await cab.get("/api/me")
        info = (body or {}).get("bonus") or {}
        rep.check(
            "подарок выключен — /api/me не обещает дней",
            status == 200 and info.get("enabled") is False and info.get("days") == 0,
            f"{info}",
        )
        status, body = await cab.post("/api/subscription/bonus")
        rep.check(
            "выключенный подарок не начисляют — 503",
            status == 503 and (body or {}).get("status") == "disabled",
            f"статус {status}, {body}",
        )

    with configured(bonus_channel="https://t.me/papin4_do4a", bonus_days=3):
        status, body = await cab.get("/api/me")
        info = (body or {}).get("bonus") or {}
        rep.check(
            "включённый подарок описан целиком",
            status == 200
            and info.get("enabled") is True
            and info.get("channel") == "@papin4_do4a"
            and info.get("url") == "https://t.me/papin4_do4a"
            and info.get("days") == 3
            and info.get("claimed") is False,
            f"{info}",
        )

        # Кабинет без бота проверить подписку не может — и не должен врать,
        # будто человек не подписан: это отказ сервиса, а не отказ человеку.
        status, body = await cab.post("/api/subscription/bonus")
        rep.check(
            "без бота проверка подписки — 503, а не «вы не подписаны»",
            status == 503 and (body or {}).get("status") == "unavailable",
            f"статус {status}, {body}",
        )

        with stubbed_bot(SubscriberBot("left")):
            status, body = await cab.post("/api/subscription/bonus")
            rep.check(
                "не подписан — 403 с названием канала",
                status == 403
                and (body or {}).get("status") == "not_member"
                and "@papin4_do4a" in ((body or {}).get("message") or ""),
                f"статус {status}, {body}",
            )

        _, before = await cab.get("/api/subscription")
        with stubbed_bot(SubscriberBot("member")) as bot:
            status, body = await cab.post("/api/subscription/bonus")
            granted = rep.check(
                "подписчику начислены 3 дня",
                status == 200
                and (body or {}).get("granted") is True
                and (body or {}).get("days") == 3
                and (body or {}).get("until"),
                f"статус {status}, {body}",
            )
            rep.check(
                "подписку спросили именно у канала подарка",
                bot.calls == [("@papin4_do4a", SMOKE_USER_ID)],
                f"{bot.calls}",
            )
            # Второе нажатие — отдельный код: кабинету надо отличать «уже
            # получено» от «подпишитесь», иначе он снова откроет канал.
            status, body = await cab.post("/api/subscription/bonus")
            rep.check(
                "второй раз подарок не дают — 409",
                status == 409 and (body or {}).get("status") == "already",
                f"статус {status}, {body}",
            )
            rep.check(
                "повторный отказ не тратит запрос к Telegram",
                len(bot.calls) == 1,
                f"вызовов {len(bot.calls)}",
            )

        _, after = await cab.get("/api/subscription")
        if granted:
            grew = (after or {}).get("days_left", 0) - (before or {}).get("days_left", 0)
            rep.check("остаток дней вырос на подарок", grew == 3, f"+{grew} дн.")
        status, body = await cab.get("/api/me")
        info = (body or {}).get("bonus") or {}
        rep.check(
            "/api/me помнит выданный подарок",
            info.get("claimed") is True and info.get("claimed_at"),
            f"{info}",
        )
        status, _ = await cab.post("/api/subscription/bonus", auth=False)
        rep.check("без подписи Telegram — 401", status == 401, f"статус {status}")

    # Метку в БД снимаем: раздел не должен влиять на остальные проверки.
    async with session_scope() as session:
        user = await repo.get_user(session, SMOKE_USER_ID)
        user.channel_bonus_at = None


async def check_misc(cab: Cabinet, rep: Report) -> None:
    """Мелочи, которые ломаются молча: неизвестный маршрут и чужой метод."""
    rep.section("Прочее")

    status, _ = await cab.get("/api/no-such-endpoint")
    rep.check("неизвестный эндпоинт — 404", status == 404, f"статус {status}")
    status, _ = await cab.request("PUT", "/api/tasks")
    rep.check("метод не поддерживается — 405", status == 405, f"статус {status}")


async def run_all(rep: Report) -> None:
    """Поднимает приложение и проходит по всем разделам."""
    await init_db()
    account_id, rule_id = await seed()
    runner, base = await start_server()
    print(f"Сервер прогона: {base}")

    try:
        async with aiohttp.ClientSession() as session:
            cab = Cabinet(session, base)
            await check_static(cab, rep)
            await check_health(cab, rep)
            await check_auth(cab, rep)
            me = await check_profile(cab, rep)
            await check_commands(cab, rep)
            await check_tasks(cab, rep, account_id, rule_id)
            await check_task_actions(cab, rep, rule_id)
            await check_mailing_and_library(cab, rep, account_id)
            await check_task_edit(cab, rep, account_id)
            await check_task_health(cab, rep, account_id)
            await check_task_cleanup(cab, rep, account_id)
            await check_chats_and_accounts(cab, rep, account_id)
            await check_account_login(cab, rep)
            await check_subscription(cab, rep, me)
            await check_bonus(cab, rep)
            await check_pay_disabled(cab, rep)
            await check_pay_page(cab, rep)
            await check_pay_card(cab, rep)
            await check_misc(cab, rep)
    finally:
        await runner.cleanup()
        await dispose_db()


async def main() -> int:
    print("Дымовой прогон кабинета tg-forward — целиком, на живом сокете")
    print(f"База: {TMP_DIR / 'smoke.db'} (временная, рабочую не открываем)")
    print(f"Контур оплаты в .env: PAY_MODE={settings.pay_mode}, "
          f"способы {settings.payment_methods()}")

    rep = Report()
    try:
        await run_all(rep)
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    failures = rep.failures
    print(f"\nПроверок: {len(rep.rows)}, не прошло: {len(failures)}")
    for what, _, detail in failures:
        print(f"  ✗ {what}" + (f" — {detail}" if detail else ""))
    if failures:
        return 1
    print("Все проверки пройдены ✅")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
