"""HTTP-слой мини-аппа: авторизация по initData и валидация ввода.

Здесь же проверяется, что любая необработанная ошибка превращается в JSON,
а не в HTML-трассировку — иначе фронтенд ломается на разборе ответа.
"""
from __future__ import annotations

import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.errors import FeatureUnavailable, http_error_middleware
from tests.helpers import TEST_USER_ID, sign_init_data

# ──────────────────────────────── авторизация ─────────────────────────────────


async def test_health_is_open(client):
    response = await client.get("/api/health")
    assert response.status == 200
    assert (await response.json())["ok"] is True


async def test_me_requires_auth(client):
    assert (await client.get("/api/me")).status == 401


async def test_me_creates_user_and_grants_trial(client, auth_headers):
    response = await client.get("/api/me", headers=auth_headers)
    assert response.status == 200

    body = await response.json()
    assert body["id"] == TEST_USER_ID
    assert body["subscription"]["active"] is True
    assert body["subscription"]["days_left"] == settings.trial_days - 1


async def test_tampered_signature_is_rejected(client):
    headers = {"X-Telegram-Init-Data": sign_init_data(corrupt_hash=True)}
    assert (await client.get("/api/me", headers=headers)).status == 401


async def test_signature_from_other_bot_token_is_rejected(client):
    headers = {"X-Telegram-Init-Data": sign_init_data(token="999999:OTHERBOTTOKEN")}
    assert (await client.get("/api/me", headers=headers)).status == 401


async def test_stale_init_data_is_rejected(client):
    """Суточный TTL: украденная неделю назад подпись не должна работать."""
    old = int(time.time()) - 48 * 3600
    headers = {"X-Telegram-Init-Data": sign_init_data(auth_date=old)}
    assert (await client.get("/api/me", headers=headers)).status == 401


@pytest.mark.parametrize(
    "first_name",
    [
        'Ко%22т',      # после лишнего unquote получалась кавычка → битый JSON → 401
        "%41ня",       # и «A» вместо «%41» — имя молча искажалось
        "скидка 50%25",
        "100%",
    ],
)
async def test_percent_in_name_does_not_break_auth(client, first_name):
    """Процент в имени — обычный символ, а не второй слой кодирования.

    parse_qsl уже раскодировал значение; повторный unquote ломал и подпись
    (JSONDecodeError → 401), и сами данные пользователя.
    """
    from app.webapp_api import validate_init_data

    init_data = sign_init_data(first_name=first_name)
    payload = validate_init_data(init_data)

    assert payload is not None
    assert payload["user"]["first_name"] == first_name

    response = await client.get("/api/me", headers={"X-Telegram-Init-Data": init_data})
    assert response.status == 200
    assert (await response.json())["id"] == TEST_USER_ID


# ───────────────────────────────── валидация ──────────────────────────────────


async def test_create_task_without_account_is_400(client, auth_headers):
    response = await client.post("/api/tasks", json={}, headers=auth_headers)
    assert response.status == 400
    assert "аккаунт" in (await response.json())["error"]


async def test_create_task_with_unknown_account_is_404(client, auth_headers):
    # source/target обязательны для пересылки, поэтому до поиска аккаунта доходим
    # только передав их — иначе получим 400 на проверке полей.
    response = await client.post(
        "/api/tasks",
        json={"account_id": 999, "source": "@chan", "target": "@mine"},
        headers=auth_headers,
    )
    assert response.status == 404


async def test_bank_without_subscription_is_409(client, auth_headers):
    response = await client.post("/api/subscription/bank", json={"days": 1}, headers=auth_headers)
    assert response.status == 409


async def test_bank_rejects_garbage_days(client, auth_headers):
    """``days`` приходит с фронтенда строкой — мусор не должен ронять обработчик."""
    response = await client.post(
        "/api/subscription/bank", json={"days": "много"}, headers=auth_headers
    )
    assert response.status in (400, 409)


async def test_distribute_from_empty_piggy_bank_is_409(client, auth_headers):
    response = await client.post(
        "/api/subscription/distribute", json={"days": 1}, headers=auth_headers
    )
    assert response.status == 409


async def test_unknown_task_id_is_404(client, auth_headers):
    response = await client.post("/api/tasks/424242/toggle", headers=auth_headers)
    assert response.status == 404


# ──────────────────────────── единый формат ошибок ────────────────────────────


async def test_unhandled_exception_becomes_json_500():
    app = web.Application(middlewares=[http_error_middleware])

    async def boom(_request: web.Request) -> web.Response:
        raise RuntimeError("всё пропало")

    app.router.add_get("/boom", boom)

    async with TestClient(TestServer(app)) as test_client:
        response = await test_client.get("/boom")
        body = await response.json()

    assert response.status == 500
    assert response.content_type == "application/json"
    assert "error" in body


async def test_feature_unavailable_returns_503_with_feature_marker():
    app = web.Application(middlewares=[http_error_middleware])

    async def gate(_request: web.Request) -> web.Response:
        raise FeatureUnavailable("не подключено", feature="account_login", status="setup_required")

    app.router.add_get("/gate", gate)

    async with TestClient(TestServer(app)) as test_client:
        response = await test_client.get("/gate")
        body = await response.json()

    assert response.status == 503
    assert body["feature"] == "account_login"
    assert body["status"] == "setup_required"


# ─────────────────────── честный статус команд ───────────────────────────


async def test_commands_report_setup_required_without_mtproto(client, auth_headers, monkeypatch):
    """Без MTProto-шлюза ни одна команда не выполнима.

    Историческая ошибка: справочник COMMANDS статичен и у всех команд там
    status="ready", поэтому кабинет показывал «включено» при не поднятом
    шлюзе. Пользователь заполнял форму и ловил отказ на сохранении.
    """
    monkeypatch.setattr(type(settings), "public_login_enabled", property(lambda self: False))

    async with client as test_client:
        response = await test_client.get("/api/commands", headers=auth_headers)
        body = await response.json()

    assert response.status == 200
    assert body["commands"]
    assert {item["status"] for item in body["commands"]} == {"setup_required"}


async def test_commands_report_ready_when_mtproto_is_up(client, auth_headers, monkeypatch):
    monkeypatch.setattr(type(settings), "public_login_enabled", property(lambda self: True))

    async with client as test_client:
        response = await test_client.get("/api/commands", headers=auth_headers)
        body = await response.json()

    assert response.status == 200
    assert {item["status"] for item in body["commands"]} == {"ready"}


async def test_commands_payload_does_not_mutate_catalog(monkeypatch):
    """Справочник общий на всё приложение — подмена статуса не должна его портить."""
    from app.webapp_api import COMMANDS, commands_payload

    monkeypatch.setattr(type(settings), "public_login_enabled", property(lambda self: False))
    commands_payload()
    monkeypatch.setattr(type(settings), "public_login_enabled", property(lambda self: True))

    assert {item["status"] for item in commands_payload()} == {"ready"}
    assert {item["status"] for item in COMMANDS} == {"ready"}


# ──────────────────────────── блоки каталога команд ───────────────────────────


async def test_commands_answer_carries_group_order(client, auth_headers):
    """Порядок и подписи блоков приходят с сервера.

    Иначе они появились бы второй копией в мини-аппе и разошлись бы с
    каталогом при первом же добавлении команды.
    """
    from app.webapp_api import COMMAND_GROUPS

    body = await (await client.get("/api/commands", headers=auth_headers)).json()

    assert body["groups"] == COMMAND_GROUPS
    assert [group["id"] for group in body["groups"]][0] == "publish"
    assert all(group["title"] for group in body["groups"])


async def test_every_command_belongs_to_a_known_group(client, auth_headers):
    """Команда без известной группы уехала бы в «прочее» — это заметно только глазами."""
    body = await (await client.get("/api/commands", headers=auth_headers)).json()
    known = {group["id"] for group in body["groups"]}

    missing = [item["id"] for item in body["commands"] if item.get("group") not in known]
    assert missing == []



# ──────────────────────────────── оплата Stars ────────────────────────────────


class FakeInvoiceBot:
    """Минимальная замена Bot: запоминает вызов create_invoice_link."""

    def __init__(self, link: str = "https://t.me/invoice/abc", error: Exception | None = None):
        self.link = link
        self.error = error
        self.calls: list[dict] = []

    async def create_invoice_link(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.link


async def test_stars_invoice_requires_auth(client):
    assert (await client.post("/api/subscription/invoice")).status == 401


async def test_stars_invoice_unavailable_without_bot(client, auth_headers):
    response = await client.post("/api/subscription/invoice", headers=auth_headers)
    assert response.status == 503

    body = await response.json()
    assert body["feature"] == "stars"
    assert body["status"] == "bot_unavailable"


@pytest.mark.parametrize("months", [0, -1, 13, 10_000, 2, 5])
async def test_stars_invoice_rejects_bad_months(client, auth_headers, months):
    """Срок только из каталога.

    И сверху ограничен (иначе в счёт уедет гигантская сумма в звёздах), и
    «промежуточные» значения вроде 2 месяцев не проходят: цена на кнопке
    должна совпадать с ценой в счёте, а кнопок с таким сроком нет.
    """
    response = await client.post(
        "/api/subscription/invoice", headers=auth_headers, json={"months": months}
    )
    assert response.status == 400


async def test_stars_invoice_error_names_available_periods(client, auth_headers):
    """Ошибка подсказывает, какие сроки есть, — не заставляет угадывать."""
    response = await client.post(
        "/api/subscription/invoice", headers=auth_headers, json={"months": 2}
    )
    body = await response.json()
    assert "1, 3, 6 или 12" in body["error"]


@pytest.mark.parametrize("months", ["много", "1.5", 1.5, True, [], {}])
async def test_stars_invoice_rejects_non_integer_months(bot_client, auth_headers, months):
    """Мусор в ``months`` — отказ, а не молчаливый месяц по умолчанию.

    Раньше нечисловое значение проваливалось в умолчание: человек выбирал год,
    из-за опечатки клиента уезжала строка, и счёт приходил на месяц. Заплатить
    не за то, что выбирал, — худший исход, чем понятная ошибка. ``1.5`` тоже
    мусор: ``int()`` молча делает из него 1.
    """
    bot = FakeInvoiceBot()
    test_client = await bot_client(bot)

    response = await test_client.post(
        "/api/subscription/invoice", headers=auth_headers, json={"months": months}
    )

    assert response.status == 400
    assert bot.calls == []  # до создания счёта дело не дошло


async def test_stars_invoice_rejects_non_object_body(client, auth_headers):
    response = await client.post(
        "/api/subscription/invoice", headers=auth_headers, json=["not", "an", "object"]
    )
    assert response.status == 400


async def test_stars_invoice_prices_are_calculated_server_side(bot_client, auth_headers):
    """Цена и payload считаются на сервере, а не приходят от клиента."""
    bot = FakeInvoiceBot()
    test_client = await bot_client(bot)

    response = await test_client.post(
        "/api/subscription/invoice", headers=auth_headers, json={"months": 3}
    )
    assert response.status == 200

    body = await response.json()
    assert body["url"] == bot.link
    assert body["amount"] == settings.price_stars * 3
    assert body["currency"] == "XTR"

    call = bot.calls[0]
    # Формат читает хендлер successful_payment в боте — менять нельзя.
    assert call["payload"] == f"sub:{TEST_USER_ID}:3"
    assert call["prices"][0].amount == settings.price_stars * 3
    assert call["provider_token"] == ""  # для Stars токен не нужен


async def test_stars_invoice_defaults_to_one_month(bot_client, auth_headers):
    bot = FakeInvoiceBot()
    test_client = await bot_client(bot)

    response = await test_client.post("/api/subscription/invoice", headers=auth_headers)
    assert response.status == 200
    assert bot.calls[0]["payload"] == f"sub:{TEST_USER_ID}:1"


async def test_stars_invoice_bot_failure_becomes_503(bot_client, auth_headers):
    """Сбой Bot API — не 500: пользователю важно «попробуйте позже», а не трассировка."""
    bot = FakeInvoiceBot(error=RuntimeError("Bot API is down"))
    test_client = await bot_client(bot)

    response = await test_client.post("/api/subscription/invoice", headers=auth_headers)
    assert response.status == 503
    assert (await response.json())["status"] == "invoice_failed"
