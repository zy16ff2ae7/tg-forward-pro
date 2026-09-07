"""Статистика и новые действия задач: /api/stats, /api/activity, duplicate, test.

Плюс проверка rate-limit (седьмой быстрый вызов — 429) и security-заголовков.
"""
from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog, Rule
from app.errors import http_error_middleware, security_headers_middleware
from app.webapp_api import setup_webapp_routes
from tests.helpers import sign_init_data
from tests.test_many_chats import login_open  # noqa: F401 — фикстура соседнего файла

OV_USER = 768_100_001
POOR_USER = 768_100_002


async def _rule(user_id: int, account_id: int, **kwargs) -> int:
    async with session_scope() as session:
        rule = Rule(
            user_id=user_id,
            account_id=account_id,
            source_id=kwargs.pop("source_id", -100100),
            source_title=kwargs.pop("source_title", "Источник"),
            target_id=kwargs.pop("target_id", -100200),
            target_title=kwargs.pop("target_title", "Приёмник"),
            **kwargs,
        )
        session.add(rule)
        await session.flush()
        return rule.id


async def _log(rule_id: int, user_id: int, status: str = "ok", error: str | None = None) -> None:
    async with session_scope() as session:
        session.add(
            ForwardLog(
                rule_id=rule_id,
                user_id=user_id,
                source_msg_id=1,
                status=status,
                error=error,
            )
        )


def _headers(user_id: int) -> dict[str, str]:
    return {"X-Telegram-Init-Data": sign_init_data(user_id)}


# ───────────────────────────────── статистика ─────────────────────────────────


async def test_stats_shape(client, create_user, create_account):
    await create_user(id=OV_USER)
    account_id = await create_account(OV_USER)
    await _rule(OV_USER, account_id, forwarded_count=42)

    response = await client.get("/api/stats?days=7", headers=_headers(OV_USER))
    assert response.status == 200
    body = await response.json()

    assert body["totals"]["rules"] == 1
    assert body["totals"]["accounts"] == 1
    assert body["totals"]["forwarded"] == 42
    assert len(body["per_day"]) == 7
    assert set(body["per_day"][0]) == {"date", "count", "errors"}
    assert body["top_rules"][0]["forwarded"] == 42
    assert body["top_rules"][0]["title"]


async def test_stats_counts_only_ok_logs(client, create_user, create_account):
    await create_user(id=OV_USER)
    account_id = await create_account(OV_USER)
    rule_id = await _rule(OV_USER, account_id)
    await _log(rule_id, OV_USER, "ok")
    await _log(rule_id, OV_USER, "ok")
    await _log(rule_id, OV_USER, "ok")
    await _log(rule_id, OV_USER, "error", error="FloodWait")

    response = await client.get("/api/stats?days=7", headers=_headers(OV_USER))
    assert (await response.json())["totals"]["forwarded_days"] == 3


async def test_stats_clamps_period(client, create_user):
    await create_user(id=OV_USER)
    response = await client.get("/api/stats?days=500", headers=_headers(OV_USER))
    assert len((await response.json())["per_day"]) == 90


async def test_activity_feed_newest_first(client, create_user, create_account):
    await create_user(id=OV_USER)
    account_id = await create_account(OV_USER)
    rule_id = await _rule(OV_USER, account_id)
    await _log(rule_id, OV_USER, "ok")
    await _log(rule_id, OV_USER, "error", error="FloodWait: подождать 37 сек.")

    response = await client.get("/api/activity?limit=10", headers=_headers(OV_USER))
    assert response.status == 200
    items = (await response.json())["items"]
    assert len(items) == 2
    assert items[0]["status"] == "error"
    assert "37 сек" in items[0]["error"]
    assert items[0]["rule_id"] == rule_id
    assert items[0]["rule_title"]
    assert items[1]["error"] is None


# ───────────────────────────── дублирование и тест ────────────────────────────


async def test_duplicate_creates_paused_copy(client, create_user, create_account):
    await create_user(id=OV_USER)
    account_id = await create_account(OV_USER)
    rule_id = await _rule(OV_USER, account_id, forwarded_count=10, enabled=True)

    response = await client.post(f"/api/tasks/{rule_id}/duplicate", headers=_headers(OV_USER))
    assert response.status == 201
    clone = (await response.json())["task"]
    assert clone["id"] != rule_id
    assert clone["enabled"] is False
    assert clone["archived"] is False
    assert clone["source"] == "Источник"
    assert clone["target"] == "Приёмник"


async def test_duplicate_missing_is_404(client, create_user):
    await create_user(id=OV_USER)
    response = await client.post("/api/tasks/999999/duplicate", headers=_headers(OV_USER))
    assert response.status == 404


async def test_test_post_offline_is_409(client, create_user, create_account, login_open):
    """Абонемент оплачен, но аккаунт не в сети — 409."""
    await create_user(id=OV_USER)
    async with session_scope() as session:
        await repo.add_subscription_days(session, OV_USER, 30)

    account_id = await create_account(OV_USER)
    rule_id = await _rule(OV_USER, account_id)
    response = await client.post(f"/api/tasks/{rule_id}/test", headers=_headers(OV_USER))
    assert response.status == 409
    assert "не в сети" in (await response.json())["error"]


async def test_test_post_requires_subscription(client, create_user, create_account):
    """Пользователь создан напрямую (без триала) — тестовый пост закрыт, 402."""
    await create_user(id=POOR_USER)
    account_id = await create_account(POOR_USER)
    rule_id = await _rule(POOR_USER, account_id)
    response = await client.post(f"/api/tasks/{rule_id}/test", headers=_headers(POOR_USER))
    assert response.status == 402


# ─────────────────────────── rate-limit и заголовки ──────────────────────────


async def test_rate_limit_returns_429(client, create_user):
    await create_user(id=OV_USER)
    statuses = []
    for _ in range(7):
        response = await client.post("/api/tasks/999999/test", headers=_headers(OV_USER))
        statuses.append(response.status)
    assert statuses == [404] * 6 + [429]
    assert "Слишком часто" in (await response.json())["error"]


async def test_security_headers_present():
    """Ответы несут базовую гигиену — и при этом остаются во фрейме."""
    app = web.Application(middlewares=[http_error_middleware, security_headers_middleware])
    setup_webapp_routes(app, bot=None)
    async with TestClient(TestServer(app)) as test_client:
        response = await test_client.get("/api/health")
        assert response.status == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "X-Frame-Options" not in response.headers
