"""HTTP-слой мини-аппа: авторизация по initData и валидация ввода.

Здесь же проверяется, что любая необработанная ошибка превращается в JSON,
а не в HTML-трассировку — иначе фронтенд ломается на разборе ответа.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.config import settings
from app.errors import FeatureUnavailable, http_error_middleware
from app.webapp_api import setup_webapp_routes

TEST_USER_ID = 768_000_001


def sign_init_data(
    user_id: int = TEST_USER_ID,
    *,
    auth_date: int | None = None,
    token: str | None = None,
    corrupt_hash: bool = False,
) -> str:
    """Подписанный initData ровно в том формате, какой шлёт Telegram.

    Важно: сервер сравнивает подпись по РАСКОДИРОВАННЫМ значениям (parse_qsl),
    поэтому подписывать надо исходные строки, а не urlencode-результат.
    """
    data = {
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(
            {"id": user_id, "first_name": "Тест", "username": "tester"}, ensure_ascii=False
        ),
    }
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret = hmac.new(b"WebAppData", (token or settings.bot_token).encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    data["hash"] = "0" * 64 if corrupt_hash else signature
    return urlencode(data)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-Telegram-Init-Data": sign_init_data()}


@pytest.fixture
async def client():
    app = web.Application(middlewares=[http_error_middleware])
    setup_webapp_routes(app, bot=None)
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


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
