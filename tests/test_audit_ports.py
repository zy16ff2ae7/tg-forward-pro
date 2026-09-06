"""Регрессии аудит-фиксов, портированных на новую кодовую базу.

Каждый тест — один пункт аудита: если фикс случайно откатят, тест покраснеет
с понятным названием, а не тихим возвратом дыры.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import quote

from app.config import settings
from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.db.models import Payment, Subscription
from app.payments import crypto, yookassa
from app.plans import stars_amount
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID, sign_init_data
from tests.test_mailing import FakeClient, clean_manager  # noqa: F401 — фикстура соседнего файла
from tests.test_many_chats import login_open  # noqa: F401 — фикстура соседнего файла
from tests.test_many_chats import make_poster
from tests.test_oneshot_journal import OneShotClient, make_oneshot, run_now


# ───────────────────────────── M1: цена USDT ────────────────────────────────


def test_unique_amount_wraps_each_thousand():
    """Метка не уводит цену в бесконечность: счётчик идёт по модулю 1000."""
    assert crypto.unique_amount(12.0, 17) == 12.017
    assert crypto.unique_amount(12.0, 1017) == 12.017
    assert crypto.unique_amount(12.0, 12500) == 12.5


async def test_check_pending_fetches_trongrid_once(create_user, monkeypatch):
    """Пачка висящих счетов — один запрос к TronGrid, а не по запросу на счёт."""
    monkeypatch.setattr(crypto, "is_configured", lambda: True)
    monkeypatch.setattr(settings, "usdt_wallet", "TOurWallet")
    calls: list[int] = []

    async def counting_fetch(session, since_ms=0):
        calls.append(since_ms)
        return []

    monkeypatch.setattr(crypto, "_fetch_transactions", counting_fetch)

    user_id = await create_user()
    async with session_scope() as session:
        for memo in ("10.001", "10.002", "10.003"):
            await repo.create_payment(
                session, user_id=user_id, provider="usdt", amount=10.0,
                currency="USDT", months=1, memo=memo,
            )

    class SilentBot:
        async def send_message(self, *args, **kwargs):
            pass

    assert await crypto.check_pending(SilentBot()) == 0
    assert len(calls) == 1, f"запросов к TronGrid: {len(calls)}, ждали 1"


# ─────────────────────── M5/M6: входные данные API ──────────────────────────


async def test_query_init_data_is_rejected(client):
    """initData в query — 401: подпись из адресной строки утекает в логи."""
    signed = sign_init_data()
    response = await client.get(f"/api/me?initData={quote(signed)}")
    assert response.status == 401


async def test_garbage_task_id_is_404_not_500(client, auth_headers):
    """Мусор вместо id — 404 от роутера, а не 500 из int()."""
    assert (await client.post("/api/tasks/abc/toggle", headers=auth_headers)).status == 404
    assert (await client.get("/api/tasks/abc/results", headers=auth_headers)).status == 404
    assert (await client.delete("/api/tasks/12x", headers=auth_headers)).status == 404


async def test_source_equals_target_is_400(
    client, auth_headers, create_account, login_open, monkeypatch
):
    """Пересылка чата в него же — 400, а не задача-пустышка."""
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)

    async def same_chat(account_id: int, queries):
        return {ref: (-9001, "Один чат") for ref in queries}

    monkeypatch.setattr(manager, "resolve_many", same_chat)
    response = await client.post(
        "/api/tasks",
        json={
            "command": "copy_channel",
            "account_id": account_id,
            "source": "@same",
            "target": "@same",
        },
        headers=auth_headers,
    )
    assert response.status == 400
    assert "совпадают" in (await response.json())["error"]


# ─────────────────── H1/M3: чужие деньги и повторы ──────────────────────────


async def test_foreign_yookassa_check_is_rejected(create_user, monkeypatch):
    """Проверка чужого счёта: платёж не трогаем, к ЮKassa даже не ходим."""
    from app.bot.handlers.subscription import check_yookassa

    owner_id = await create_user()
    stranger_id = await create_user()
    async with session_scope() as session:
        payment = await repo.create_payment(
            session, user_id=owner_id, provider="yookassa", amount=990.0,
            currency="RUB", months=1, external_id="yk-foreign",
        )
        payment_id = payment.id

    probed: list[str] = []

    async def fake_is_paid(external_id: str) -> bool:
        probed.append(external_id)
        return True

    monkeypatch.setattr(yookassa, "is_paid", fake_is_paid)

    callback = SimpleNamespace(
        data=f"pay:check:{payment_id}",
        from_user=SimpleNamespace(id=stranger_id),
        message=None,
        answer=lambda *args, **kwargs: asyncio.sleep(0),
    )
    await check_yookassa(callback)

    assert probed == [], "чужой счёт нельзя даже проверять у провайдера"
    async with SessionLocal() as session:
        row = await session.get(Payment, payment_id)
        assert row is not None and row.status == "pending"


async def test_stars_replay_activates_once(create_user):
    """Повторный апдейт successful_payment не продлевает подписку дважды."""
    from app.bot.handlers.subscription import on_stars_paid

    user_id = await create_user()
    sent: list[str] = []

    def make_message():
        return SimpleNamespace(
            from_user=SimpleNamespace(id=user_id),
            successful_payment=SimpleNamespace(
                invoice_payload=f"sub:{user_id}:1",
                currency="XTR",
                total_amount=stars_amount(1),
                telegram_payment_charge_id="charge-once",
            ),
            answer=lambda text, **kwargs: sent.append(text) or asyncio.sleep(0),
        )

    await on_stars_paid(make_message())
    async with SessionLocal() as session:
        first = await repo.subscription_until(session, user_id)
    await on_stars_paid(make_message())
    async with SessionLocal() as session:
        second = await repo.subscription_until(session, user_id)

    assert first is not None and second == first


async def test_stars_wrong_amount_does_not_activate(create_user):
    """Сумма не сошлась с тарифом — подписку не включаем, зовём админа."""
    from app.bot.handlers.subscription import on_stars_paid

    user_id = await create_user()
    sent: list[str] = []

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        successful_payment=SimpleNamespace(
            invoice_payload=f"sub:{user_id}:1",
            currency="XTR",
            total_amount=stars_amount(1) + 1,
            telegram_payment_charge_id="charge-wrong",
        ),
        answer=lambda text, **kwargs: sent.append(text) or asyncio.sleep(0),
    )
    await on_stars_paid(message)

    async with SessionLocal() as session:
        assert await repo.subscription_until(session, user_id) is None
    assert sent and "администратору" in sent[0]


# ─────────────── H2: постер без абонемента + мьютекс запусков ────────────────


async def test_poster_skips_without_subscription(create_user, create_account):
    """Кончился абонемент — постер молчит, а не шлёт бесплатно."""
    rule_id, user_id, account_id = await make_poster(
        create_user, create_account, chats=[-9001], messages=["афиша"]
    )
    async with session_scope() as session:
        row = await session.get(Subscription, user_id)
        assert row is not None
        row.active_until = repo.utcnow() - timedelta(days=1)
    await manager.refresh_rules()

    client = FakeClient()
    manager._clients[account_id] = client
    await manager._poster_tick()

    assert client.sent == [], f"без подписки отправок быть не должно: {client.sent}"


async def test_oneshot_second_run_is_busy(create_user, create_account):
    """Второй запуск поверх идущего — вежливый отказ, а не двойная нагрузка."""
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="parser", source_id=-1001
    )
    manager._clients[account_id] = OneShotClient()

    lock = manager._oneshot_locks.setdefault(user_id, asyncio.Lock())
    await lock.acquire()
    try:
        result = await run_now(rule_id, user_id)
    finally:
        lock.release()
        manager._oneshot_locks.pop(user_id, None)

    assert result["ok"] is False
    assert "ещё идёт" in result["error"]


# ───────────────── M1-хвост: подтверждения USDT ─────────────────────────


def test_confirmed_only_follows_setting(monkeypatch):
    """USDT_MIN_CONFIRMATIONS — не пустышка: 0 разрешает свежие переводы."""
    monkeypatch.setattr(settings, "usdt_min_confirmations", 1)
    assert crypto._confirmed_only() is True
    monkeypatch.setattr(settings, "usdt_min_confirmations", 5)
    assert crypto._confirmed_only() is True
    monkeypatch.setattr(settings, "usdt_min_confirmations", 0)
    assert crypto._confirmed_only() is False


async def test_fetch_sends_only_confirmed_flag(monkeypatch):
    """Флаг из настройки реально уходит в запрос к TronGrid."""
    monkeypatch.setattr(settings, "usdt_wallet", "TOurWallet")
    seen: dict = {}

    class FakeResponse:
        status = 200

        async def json(self, content_type=None):
            return {"data": []}

    class FakeGet:
        def __init__(self, params):
            seen.update(params)

        async def __aenter__(self):
            return FakeResponse()

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def get(self, url, params=None, headers=None, timeout=None):
            return FakeGet(params or {})

    monkeypatch.setattr(settings, "usdt_min_confirmations", 1)
    assert await crypto._fetch_transactions(FakeSession()) == []
    assert seen.get("only_confirmed") == "true"

    monkeypatch.setattr(settings, "usdt_min_confirmations", 0)
    assert await crypto._fetch_transactions(FakeSession()) == []
    assert seen.get("only_confirmed") == "false"


# ─────────────── M10: дамп с api_hash не остаётся в /tmp ─────────────────


def _load_script(name: str):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dump_cleanup_removes_file(tmp_path, monkeypatch):
    """Успешный прогон get_api_credentials затирает дамп с api_hash."""
    script = _load_script("get_api_credentials.py")
    dump = tmp_path / "apps.html"
    dump.write_text("<html>hash</html>")
    monkeypatch.setattr(script, "DUMP", dump)
    script.cleanup_dump()
    assert not dump.exists()


# ─────────── L6/L9: деплой везёт нужное и не везёт лишнее ─────────────────


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


def test_requirements_cover_postgres_driver():
    """.env.example обещает Postgres одной строкой — драйвер должен ставиться."""
    requirements = (_repo_root() / "requirements.txt").read_text()
    assert "asyncpg" in requirements


def test_deploy_keeps_dev_tools_off_the_server():
    """gen_initdata (подпись чужого кабинета) и tests/ на сервер не едут."""
    deploy = (_repo_root() / "deploy" / "deploy.sh").read_text()
    assert "scripts/gen_initdata.py" in deploy
    assert "--exclude 'tests/'" in deploy
    for line in deploy.splitlines():
        assert line.strip() not in (
            "--exclude 'scripts' \\",
            "--exclude 'scripts/' \\",
            "--exclude 'scripts/*' \\",
        ), "рабочие скрипты (import/export_session) нужны на сервере"


# ─────────────── L8: гигиена ответов nginx ─────────────────────────


def test_nginx_serves_security_headers():
    """Заголовки гигиены на месте, а X-Frame-Options нет (мини-апп во фрейме)."""
    nginx = (_repo_root() / "deploy" / "nginx.conf").read_text()
    assert "X-Content-Type-Options" in nginx
    assert "Referrer-Policy" in nginx
    assert "Permissions-Policy" in nginx
    assert "add_header X-Frame-Options" not in nginx


# ─────────────── L11: постеры тихо переживают рестарт ─────────────────


def test_posters_calm_after_restart():
    """Рестарт не устраивает залп: постеры ждут свой интервал."""
    import time
    from types import SimpleNamespace

    saved_rules = manager._poster_rules
    saved_state = manager._poster_state
    manager._poster_rules = [SimpleNamespace(id=11), SimpleNamespace(id=22)]
    manager._poster_state = {}
    try:
        manager._calm_posters_after_restart()
        now = time.time()
        assert set(manager._poster_state) == {11, 22}
        for state in manager._poster_state.values():
            assert now - state["last"] < 5
    finally:
        manager._poster_rules = saved_rules
        manager._poster_state = saved_state
