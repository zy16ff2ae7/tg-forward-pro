"""Оплата вне Telegram: подписанная ссылка, страница /pay и зачисление картой.

Внутри Telegram абонемент продаётся только за звёзды — так требуют правила
Telegram (ToS для разработчиков, п. 6.2). Карта и USDT живут на обычной
веб-странице, а на ней нет ``initData``: единственное доказательство «пришёл наш
пользователь» — подпись в ссылке. Отсюда то, что проверяется здесь:

* подделанный, просроченный или подписанный чужим ключом токен счёт не создаёт;
* плательщик берётся из подписи, а не из тела запроса, — иначе можно было бы
  заплатить за себя, а абонемент начислить чужому;
* на странице банка нет кнопки «Я оплатил», поэтому доступ включает фоновая
  проверка — и ровно один раз на счёт;
* брошенный счёт закрывается сам: иначе лимит висящих счетов запирает человека,
  а метка-сумма остаётся занятой навсегда.
"""
from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app import paylink
from app.config import settings
from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.db.models import Payment
from app.payments import crypto, service, yookassa
from app.plans import rub_amount, usdt_amount
from tests.helpers import TEST_USER_ID, RecordingBot

# Настоящий по формату адрес TRC-20: 34 символа base58, начинается с T
VALID_TRC20 = "TQn9Y2khDD95J42FQtQTdwVVR93o1n1gLz"


@pytest.fixture
def external_pay(monkeypatch):
    """Полностью настроенный внешний контур оплаты.

    Настройки — один объект на всё приложение, поэтому правим его поля, а не
    подменяем модуль: и paylink, и webapp_api, и payments смотрят на тот же
    самый ``settings``.
    """
    monkeypatch.setattr(settings, "pay_mode", "external")
    monkeypatch.setattr(settings, "webapp_url", "https://pay.example.test")
    monkeypatch.setattr(settings, "yookassa_shop_id", "shop-1")
    monkeypatch.setattr(settings, "yookassa_secret_key", "secret-1")
    monkeypatch.setattr(settings, "usdt_wallet", VALID_TRC20)
    return settings


async def payments_of(provider: str, status: str = "pending") -> list[Payment]:
    """Счёта провайдера в нужном состоянии — то же, что видит фоновый цикл."""
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Payment).where(Payment.provider == provider, Payment.status == status)
        )
        return list(rows.scalars().all())


# ───────────────────────────── подписанный токен ──────────────────────────────


def test_token_round_trip():
    link = paylink.parse_token(paylink.make_token(TEST_USER_ID, 3))

    assert link is not None
    assert (link.user_id, link.months) == (TEST_USER_ID, 3)
    assert link.expires_at > int(time.time())


def test_tampered_signature_is_rejected():
    token = paylink.make_token(TEST_USER_ID, 1)
    user_id, months, expires, signature = token.split(".")

    assert paylink.parse_token(f"{user_id}.{months}.{expires}.{signature[:-1]}x") is None


def test_swapped_user_id_is_rejected():
    """Главное свойство: чужой абонемент не начислить, подменив id в ссылке."""
    token = paylink.make_token(TEST_USER_ID, 1)
    _, months, expires, signature = token.split(".")

    assert paylink.parse_token(f"999999.{months}.{expires}.{signature}") is None


def test_expired_token_is_rejected():
    assert paylink.parse_token(paylink.make_token(TEST_USER_ID, 1, ttl=-1)) is None


@pytest.mark.parametrize(
    "token",
    ["", "мусор", "1.1.1", "1.1.1.1.1", "a.b.c.d", "0.1.99999999999.x", "1.0.9999999999.x"],
)
def test_garbage_token_is_rejected(token):
    """Мусор в адресной строке — обычное дело: разбор не должен падать."""
    assert paylink.parse_token(token) is None


def test_token_signed_with_other_key_is_rejected(monkeypatch):
    """Утёкший токен со стенда не работает на бою: ключ подписи разный."""
    token = paylink.make_token(TEST_USER_ID, 1)
    monkeypatch.setattr(settings, "secret_key", Fernet.generate_key().decode())

    assert paylink.parse_token(token) is None


@pytest.mark.parametrize("user_id, months", [(0, 1), (-5, 1), (TEST_USER_ID, 0)])
def test_own_signature_does_not_legalize_nonsense(user_id, months):
    """Даже своя подпись не делает «нулевого пользователя» и «нуль месяцев» валидными."""
    assert paylink.parse_token(paylink.make_token(user_id, months)) is None


# ────────────────────────────── адрес страницы ────────────────────────────────

def test_pay_url_is_none_without_external_contour(monkeypatch):
    monkeypatch.setattr(settings, "pay_mode", "stars")
    monkeypatch.setattr(settings, "webapp_url", "https://pay.example.test")

    assert paylink.pay_url(TEST_USER_ID) is None


def test_pay_url_is_none_without_page_address(monkeypatch):
    """Без публичного адреса звать на страницу оплаты некуда."""
    monkeypatch.setattr(settings, "pay_mode", "external")
    monkeypatch.setattr(settings, "webapp_url", None)
    monkeypatch.setattr(settings, "webhook_url", None)
    monkeypatch.setattr(settings, "external_payments_url", None)

    assert paylink.pay_url(TEST_USER_ID) is None


def test_pay_url_carries_signed_token(external_pay):
    url = paylink.pay_url(TEST_USER_ID, 6)

    assert url is not None
    parts = urlparse(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://pay.example.test/pay"

    link = paylink.parse_token(parse_qs(parts.query)["t"][0])
    assert link is not None
    assert (link.user_id, link.months) == (TEST_USER_ID, 6)


# ─────────────────────────────── страница /pay ────────────────────────────────


async def test_pay_page_is_404_stub_when_contour_is_off(client):
    """Контур выключен — вместо страницы понятная заглушка, а не пустой 404."""
    response = await client.get("/pay", params={"t": paylink.make_token(TEST_USER_ID)})

    assert response.status == 404
    assert "отключена" in await response.text()


async def test_pay_page_opens_with_valid_token(client, external_pay):
    response = await client.get("/pay", params={"t": paylink.make_token(TEST_USER_ID)})

    assert response.status == 200
    assert "Оплата абонемента" in await response.text()


async def test_pay_page_is_410_for_expired_token(client, external_pay):
    """410, а не 401: ссылка была настоящей, просто истекла — так видно в логах."""
    response = await client.get("/pay", params={"t": paylink.make_token(TEST_USER_ID, ttl=-1)})

    assert response.status == 410
    assert "устарела" in await response.text()


async def test_pay_page_without_token_is_410(client, external_pay):
    assert (await client.get("/pay")).status == 410


# ────────────────────────────── /api/pay/info ─────────────────────────────────


async def test_pay_info_rejects_bad_token(client, external_pay):
    response = await client.get("/api/pay/info", params={"t": "мусор"})

    assert response.status == 400
    assert "оплату" in (await response.json())["error"]


async def test_pay_info_lists_methods_and_server_side_prices(client, external_pay):
    response = await client.get("/api/pay/info", params={"t": paylink.make_token(TEST_USER_ID, 3)})
    body = await response.json()

    assert response.status == 200
    assert body["months"] == 3
    assert body["methods"] == ["yookassa", "usdt"]
    assert 0 < body["expires_in"] <= paylink.TOKEN_TTL_SECONDS
    assert {item["months"]: item["rub"] for item in body["periods"]}[3] == rub_amount(3)
    assert {item["months"]: item["usdt"] for item in body["periods"]}[3] == usdt_amount(3)


async def test_pay_info_has_no_bot_link_without_bot(client, external_pay):
    """«https://t.me» без имени ведёт на главную Telegram — такой ссылки не даём."""
    response = await client.get("/api/pay/info", params={"t": paylink.make_token(TEST_USER_ID)})

    assert (await response.json())["bot_url"] is None


async def test_pay_info_links_back_to_bot(bot_client, external_pay):
    test_client = await bot_client(RecordingBot())

    response = await test_client.get(
        "/api/pay/info", params={"t": paylink.make_token(TEST_USER_ID)}
    )

    assert (await response.json())["bot_url"] == "https://t.me/my_forward_bot"


async def test_pay_info_is_503_when_contour_is_off(client):
    response = await client.get("/api/pay/info", params={"t": paylink.make_token(TEST_USER_ID)})
    body = await response.json()

    assert response.status == 503
    assert body["feature"] == "external"
    assert body["status"] == "disabled"


# ────────────────────────────── /api/pay/start ────────────────────────────────


@pytest.fixture
async def payer(create_user):
    """Пользователь, на которого выписан токен: платежи ссылаются на users.id."""
    return await create_user(id=TEST_USER_ID)


async def test_pay_start_creates_usdt_invoice(client, external_pay, payer):
    response = await client.post(
        "/api/pay/start",
        json={"t": paylink.make_token(TEST_USER_ID, 3), "method": "usdt"},
    )
    body = await response.json()

    assert response.status == 200
    assert body["method"] == "usdt"
    assert body["wallet"] == VALID_TRC20
    assert body["months"] == 3
    # Метка — та же сумма с уникальным «хвостом» в третьем знаке: платить надо
    # ровно её, иначе перевод не привязать к счёту.
    assert body["amount"] == float(body["memo"])
    assert crypto.to_micro(body["memo"]) == (
        crypto.to_micro(usdt_amount(3)) + body["payment_id"] * crypto.MEMO_STEP_MICRO
    )

    invoices = await payments_of("usdt")
    assert [(item.user_id, item.months, item.memo) for item in invoices] == [
        (TEST_USER_ID, 3, body["memo"])
    ]


async def test_payer_is_taken_from_signature_not_from_body(client, external_pay, payer, create_user):
    """Ключевое свойство контура: платит один — начисляем ровно ему.

    Иначе можно было бы открыть свою ссылку, а в теле запроса назвать чужой
    user_id: деньги свои, абонемент чужому.
    """
    other = await create_user()

    response = await client.post(
        "/api/pay/start",
        json={
            "t": paylink.make_token(TEST_USER_ID, 1),
            "method": "usdt",
            "user_id": other,
            "months": 1,
        },
    )
    assert response.status == 200

    invoices = await payments_of("usdt")
    assert [item.user_id for item in invoices] == [TEST_USER_ID]


@pytest.mark.parametrize("method", ["stars", "manual", "", "monero"])
async def test_pay_start_refuses_methods_outside_the_page(client, external_pay, payer, method):
    """На странице живут только карта и USDT: звёзды продаются в боте."""
    response = await client.post(
        "/api/pay/start", json={"t": paylink.make_token(TEST_USER_ID), "method": method}
    )
    body = await response.json()

    assert response.status == 503
    assert body["status"] == "disabled"


@pytest.mark.parametrize("months", [0, 2, 13, -1, "много", 1.5])
async def test_pay_start_rejects_months_outside_catalog(client, external_pay, payer, months):
    """Срок только из каталога: цену на странице и в счёте считает один и тот же код."""
    response = await client.post(
        "/api/pay/start",
        json={"t": paylink.make_token(TEST_USER_ID), "method": "usdt", "months": months},
    )

    assert response.status == 400
    assert await payments_of("usdt") == []


async def test_pay_start_rejects_non_object_body(client, external_pay, payer):
    response = await client.post("/api/pay/start", json=["usdt"])
    assert response.status == 400


async def test_pay_start_rejects_expired_token(client, external_pay, payer):
    response = await client.post(
        "/api/pay/start",
        json={"t": paylink.make_token(TEST_USER_ID, 1, ttl=-1), "method": "usdt"},
    )

    assert response.status == 400
    assert await payments_of("usdt") == []


async def test_pay_start_stops_after_too_many_pending(client, external_pay, payer):
    """Утёкшая ссылка не должна плодить счёта: свободных метк-сумм конечное число."""
    for _ in range(service.MAX_PENDING_PER_METHOD):
        ok = await client.post(
            "/api/pay/start", json={"t": paylink.make_token(TEST_USER_ID), "method": "usdt"}
        )
        assert ok.status == 200

    response = await client.post(
        "/api/pay/start", json={"t": paylink.make_token(TEST_USER_ID), "method": "usdt"}
    )
    assert response.status == 409
    assert len(await payments_of("usdt")) == service.MAX_PENDING_PER_METHOD


async def test_usdt_memos_never_repeat(client, external_pay, payer):
    """Одинаковая метка у двух висящих счетов — это зачёт перевода не тому."""
    for _ in range(service.MAX_PENDING_PER_METHOD):
        await client.post(
            "/api/pay/start",
            json={"t": paylink.make_token(TEST_USER_ID), "method": "usdt", "months": 1},
        )

    memos = [item.memo for item in await payments_of("usdt")]
    assert len(set(memos)) == len(memos) == service.MAX_PENDING_PER_METHOD


async def test_pay_start_creates_card_invoice(client, external_pay, payer, monkeypatch):
    """Счёт в ЮKassa: сумму назначает сервер, id платежа провайдера сохраняем.

    Без сохранённого external_id фоновая проверка не знает, о чём спрашивать
    провайдера, — оплата так и осталась бы незачтённой.
    """
    calls: list[dict] = []

    async def fake_create_invoice(**kwargs):
        calls.append(kwargs)
        return "https://yookassa.test/checkout/xyz", "yoo-777"

    monkeypatch.setattr(yookassa, "create_invoice", fake_create_invoice)

    response = await client.post(
        "/api/pay/start",
        json={"t": paylink.make_token(TEST_USER_ID, 6), "method": "yookassa"},
    )
    body = await response.json()

    assert response.status == 200
    assert body["url"] == "https://yookassa.test/checkout/xyz"
    assert body["amount"] == rub_amount(6)
    assert body["currency"] == "RUB"
    assert calls[0]["amount_rub"] == float(rub_amount(6))
    assert calls[0]["user_id"] == TEST_USER_ID

    invoices = await payments_of("yookassa")
    assert [(item.user_id, item.months, item.external_id) for item in invoices] == [
        (TEST_USER_ID, 6, "yoo-777")
    ]


async def test_provider_failure_is_503_not_500(client, external_pay, payer, monkeypatch):
    """Сбой банка — «попробуйте позже», а не трассировка на странице оплаты."""

    async def boom(**kwargs):
        raise RuntimeError("ЮKassa недоступна")

    monkeypatch.setattr(yookassa, "create_invoice", boom)

    response = await client.post(
        "/api/pay/start", json={"t": paylink.make_token(TEST_USER_ID), "method": "yookassa"}
    )
    body = await response.json()

    assert response.status == 503
    assert body["status"] == "invoice_failed"


# ─────────────────────────────── /api/pay/link ────────────────────────────────


async def test_pay_link_requires_auth(client, external_pay):
    """Ссылку выдаём только внутри кабинета: она и есть доказательство личности."""
    assert (await client.get("/api/pay/link")).status == 401


async def test_pay_link_is_503_when_contour_is_off(client, auth_headers):
    response = await client.get("/api/pay/link", headers=auth_headers)
    body = await response.json()

    assert response.status == 503
    assert body["feature"] == "external"


async def test_pay_link_returns_fresh_signed_url(client, external_pay, auth_headers):
    response = await client.get("/api/pay/link", headers=auth_headers)
    body = await response.json()

    assert response.status == 200
    assert body["methods"] == ["yookassa", "usdt"]
    assert body["expires_in"] == paylink.TOKEN_TTL_SECONDS

    token = parse_qs(urlparse(body["url"]).query)["t"][0]
    link = paylink.parse_token(token)
    assert link is not None and link.user_id == TEST_USER_ID


# ──────────────────────── контур оплаты в кабинете ────────────────────────────


async def test_cabinet_reports_the_same_contour_everywhere(client, external_pay, auth_headers):
    """``/api/me`` и ``/api/subscription`` не должны расходиться в способах оплаты.

    Кабинет рисует кнопки по одному ответу, а проверяет доступность по другому:
    разойдутся — появится кнопка, которая отвечает «способ недоступен».
    """
    me = await (await client.get("/api/me", headers=auth_headers)).json()
    sub = await (await client.get("/api/subscription", headers=auth_headers)).json()

    assert me["pay"]["mode"] == sub["pay"]["mode"] == "external"
    # Внутри Telegram — только звёзды и заявка админу: карта и USDT ушли на сайт.
    assert me["pay"]["inline"] == sub["pay"]["inline"] == ["stars", "manual"]
    assert me["pay"]["external"] == sub["pay"]["external"] == ["yookassa", "usdt"]
    assert me["pay"]["url"] and sub["pay"]["url"]


# ─────────────────── зачисление без кнопки «я оплатил» ────────────────────────
#
# На странице банка человек может просто закрыть вкладку. Поэтому доступ
# включает фоновая проверка: она опрашивает провайдера, зачисляет ровно один раз
# и закрывает брошенные счёта.


@pytest.fixture
async def card_payment(payer):
    """Висящий счёт на карту с известным id платежа у провайдера."""
    async with session_scope() as session:
        payment = await repo.create_payment(
            session,
            user_id=TEST_USER_ID,
            provider="yookassa",
            amount=float(rub_amount(1)),
            currency="RUB",
            months=1,
            external_id="yoo-555",
        )
        return payment.id


async def age_payments(provider: str, older_than: timedelta) -> None:
    """Сдвигает висящие счёта в прошлое: ждать сутки в тесте нечем."""
    async with session_scope() as session:
        for item in await repo.pending_payments(session, provider):
            item.created_at = repo.utcnow() - older_than - timedelta(minutes=1)


async def test_paid_card_invoice_becomes_subscription(external_pay, card_payment, monkeypatch):
    asked: list[str] = []

    async def fake_is_paid(external_id: str) -> bool:
        asked.append(external_id)
        return True

    monkeypatch.setattr(yookassa, "is_paid", fake_is_paid)
    bot = RecordingBot()

    assert await yookassa.check_pending(bot) == 1
    assert asked == ["yoo-555"]

    async with SessionLocal() as session:
        until = await repo.subscription_until(session, TEST_USER_ID)
        payment = await session.get(Payment, card_payment)

    assert until is not None
    assert (payment.status, payment.tx_id) == ("paid", "yoo-555")
    # Человек ушёл с сайта и ждёт: без сообщения он не узнает, что доступ уже есть.
    assert [chat_id for chat_id, _ in bot.messages] == [TEST_USER_ID]


async def test_card_payment_is_credited_only_once(external_pay, card_payment, monkeypatch):
    """Второй проход цикла не должен продлевать абонемент повторно."""

    async def always_paid(external_id: str) -> bool:
        return True

    monkeypatch.setattr(yookassa, "is_paid", always_paid)
    bot = RecordingBot()

    assert await yookassa.check_pending(bot) == 1
    async with SessionLocal() as session:
        first = await repo.subscription_until(session, TEST_USER_ID)

    assert await yookassa.check_pending(bot) == 0
    async with SessionLocal() as session:
        second = await repo.subscription_until(session, TEST_USER_ID)

    assert first == second
    assert len(bot.messages) == 1


async def test_invoice_without_provider_id_is_not_polled(external_pay, payer, monkeypatch):
    """Ответ провайдера потерялся — спрашивать не о чем, но и падать нельзя."""
    asked: list[str] = []

    async def fake_is_paid(external_id: str) -> bool:
        asked.append(external_id)
        return True

    monkeypatch.setattr(yookassa, "is_paid", fake_is_paid)
    async with session_scope() as session:
        await repo.create_payment(
            session,
            user_id=TEST_USER_ID,
            provider="yookassa",
            amount=float(rub_amount(1)),
            currency="RUB",
            months=1,
        )

    assert await yookassa.check_pending(RecordingBot()) == 0
    assert asked == []


async def test_abandoned_card_invoice_closes_itself(external_pay, card_payment, monkeypatch):
    """По мёртвой ссылке провайдера не спрашиваем: счёт закрываем сами."""
    asked: list[str] = []

    async def fake_is_paid(external_id: str) -> bool:
        asked.append(external_id)
        return True

    monkeypatch.setattr(yookassa, "is_paid", fake_is_paid)
    await age_payments("yookassa", yookassa.PENDING_TTL)

    assert await yookassa.check_pending(RecordingBot()) == 0
    assert asked == []

    async with SessionLocal() as session:
        payment = await session.get(Payment, card_payment)
    assert payment.status == "expired"


async def test_fresh_invoice_survives_the_cleanup(card_payment):
    """Чистка не должна закрывать счёт, который человек как раз оплачивает."""
    async with session_scope() as session:
        dropped = await repo.expire_stale_payments(
            session, "yookassa", older_than=yookassa.PENDING_TTL
        )

    assert dropped == 0
    async with SessionLocal() as session:
        payment = await session.get(Payment, card_payment)
    assert payment.status == "pending"


async def test_expired_usdt_invoices_free_the_limit(client, external_pay, payer, monkeypatch):
    """Иначе лимит висящих счетов запирает человека без нового счёта навсегда."""

    async def no_transfers(session, since_ms=0):
        return []

    monkeypatch.setattr(crypto, "_fetch_transactions", no_transfers)
    for _ in range(service.MAX_PENDING_PER_METHOD):
        await client.post(
            "/api/pay/start", json={"t": paylink.make_token(TEST_USER_ID), "method": "usdt"}
        )
    blocked = await client.post(
        "/api/pay/start", json={"t": paylink.make_token(TEST_USER_ID), "method": "usdt"}
    )
    assert blocked.status == 409

    await age_payments("usdt", crypto.PENDING_TTL)
    assert await crypto.check_pending(RecordingBot()) == 0

    allowed = await client.post(
        "/api/pay/start", json={"t": paylink.make_token(TEST_USER_ID), "method": "usdt"}
    )
    assert allowed.status == 200
