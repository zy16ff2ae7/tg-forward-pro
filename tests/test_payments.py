"""Платежи: один перевод — один зачёт, одна метка — один платёж.

Здесь закрываются три способа получить лишний месяц абонемента бесплатно:

* нажать «Я оплатил» дважды и дождаться, пока оба нажатия увидят ``pending``;
* заплатить один раз и дать фоновой проверке зачесть перевод по кругу;
* попасть в соседнюю метку-сумму, где сравнение с допуском 0.001 не различало
  12.017 и 12.018.
"""
from __future__ import annotations

import pytest

from app.db import repo
from app.db.database import SessionLocal, session_scope
from app.db.models import Payment
from app.payments import crypto


@pytest.fixture
async def payment(create_user):
    """Ожидающий платёж на 1 месяц с меткой-суммой."""
    user_id = await create_user()
    async with session_scope() as session:
        created = await repo.create_payment(
            session,
            user_id=user_id,
            provider="usdt",
            amount=10.0,
            currency="USDT",
            months=1,
            memo="10.017",
        )
        return created.id, user_id


async def get_payment(payment_id: int):
    async with SessionLocal() as session:
        return await session.get(Payment, payment_id)


# ──────────────────────────── Атомарный зачёт платежа ─────────────────────────


async def test_claim_payment_succeeds_once(payment):
    payment_id, user_id = payment

    async with session_scope() as session:
        first = await repo.claim_payment(session, await session.get(Payment, payment_id))
    async with session_scope() as session:
        second = await repo.claim_payment(session, await session.get(Payment, payment_id))

    assert (first, second) == (True, False)
    row = await get_payment(payment_id)
    assert row.status == "paid"
    assert row.paid_at is not None


async def test_claim_payment_writes_tx_id(payment):
    payment_id, _ = payment

    async with session_scope() as session:
        assert await repo.claim_payment(
            session, await session.get(Payment, payment_id), tx_id="0xabc"
        )

    row = await get_payment(payment_id)
    assert row.tx_id == "0xabc"
    async with SessionLocal() as session:
        found = await repo.payment_with_tx(session, "0xabc")
        assert found is not None and found.id == payment_id


async def test_second_claim_does_not_grant_second_month(payment):
    """Ровно то, что раньше давало двойное начисление на двойное нажатие."""
    payment_id, user_id = payment

    async with session_scope() as session:
        row = await session.get(Payment, payment_id)
        assert await repo.claim_payment(session, row)
        first_until = await repo.activate_subscription(session, user_id, row.months)

    async with session_scope() as session:
        row = await session.get(Payment, payment_id)
        if await repo.claim_payment(session, row):  # не должно случиться
            await repo.activate_subscription(session, user_id, row.months)

    async with SessionLocal() as session:
        until = await repo.subscription_until(session, user_id)
    assert until == first_until


# ──────────────────────────────── Метки-суммы ─────────────────────────────────


async def test_reserved_memos_counts_only_pending(payment):
    payment_id, _ = payment

    async with SessionLocal() as session:
        assert await repo.reserved_memos(session, "usdt") == {"10.017"}

    async with session_scope() as session:
        assert await repo.claim_payment(session, await session.get(Payment, payment_id))

    async with SessionLocal() as session:
        assert await repo.reserved_memos(session, "usdt") == set()


async def test_reserve_memo_shifts_when_taken(payment, create_user):
    """Цены разных тарифов отличаются на целые единицы — метки могут совпасть.

    Платёж #17 по цене 10 и платёж #7 по цене 10.01 дают одну и ту же метку.
    Свободная метка ищется сдвигом на 0.001, а не «как получилось».
    """
    _, _ = payment  # держит 10.017 занятой

    async with SessionLocal() as session:
        memo = await crypto.reserve_memo(session, 10.0, 17)
    assert memo == "10.018"

    async with SessionLocal() as session:
        free = await crypto.reserve_memo(session, 10.0, 25)
    assert free == "10.025"


def test_to_micro_separates_adjacent_memos():
    """Старое сравнение с допуском путало соседние метки, целые единицы — нет.

    Метки 12.016 и 12.017 отличаются ровно на 0.001, но в double разность выходит
    0.00099999999999944 — меньше допуска. Перевод по метке 12.017 закрывал счёт
    с меткой 12.016: доступ получал не тот, кто платил. Больше половины пар
    соседних меток ведут себя именно так.
    """
    expected, neighbour = 12.016, 12.017
    value = crypto.to_micro(neighbour) / 10**crypto.USDT_DECIMALS
    assert abs(value - expected) < 0.001, "именно так и возникала подмена платежа"

    assert crypto.to_micro("12.016") == 12_016_000
    assert crypto.to_micro(neighbour) != crypto.to_micro(expected)


# ───────────────────────── Проверка перевода в блокчейне ──────────────────────


def test_is_our_usdt_requires_contract_and_wallet(monkeypatch):
    monkeypatch.setattr(crypto.settings, "usdt_wallet", "TOurWallet", raising=False)
    good = {"token_info": {"address": crypto.USDT_CONTRACT, "symbol": "USDT"}, "to": "TOurWallet"}
    assert crypto._is_our_usdt(good)

    # Свой контракт с символом «USDT» выпускает любой — символ ничего не значит
    fake = {"token_info": {"address": "TFakeContract", "symbol": "USDT"}, "to": "TOurWallet"}
    assert not crypto._is_our_usdt(fake)

    # Перевод не на наш кошелёк (only_to — обещание TronGrid, а не проверка)
    other = {"token_info": {"address": crypto.USDT_CONTRACT}, "to": "TSomeoneElse"}
    assert not crypto._is_our_usdt(other)


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        self.messages.append((chat_id, text))


async def test_check_pending_credits_once_per_transfer(payment, monkeypatch):
    """Один перевод не может закрыть два счёта, даже с одинаковой меткой."""
    payment_id, user_id = payment
    transfer = {
        "transaction_id": "0xdeadbeef",
        "value": str(crypto.to_micro("10.017")),
        "token_info": {"address": crypto.USDT_CONTRACT, "symbol": "USDT"},
        "to": "TOurWallet",
    }

    monkeypatch.setattr(crypto, "is_configured", lambda: True)

    async def fake_matches(expected, since=None):
        return [transfer] if crypto.to_micro(expected) == crypto.to_micro("10.017") else []

    monkeypatch.setattr(crypto, "find_incoming_matches", fake_matches)

    bot = FakeBot()
    assert await crypto.check_pending(bot) == 1
    row = await get_payment(payment_id)
    assert row.status == "paid" and row.tx_id == "0xdeadbeef"
    assert len(bot.messages) == 1

    # Второй счёт с той же меткой: перевод уже зачтён — доступ не выдаём
    async with session_scope() as session:
        await repo.create_payment(
            session,
            user_id=user_id,
            provider="usdt",
            amount=10.0,
            currency="USDT",
            months=1,
            memo="10.017",
        )

    assert await crypto.check_pending(bot) == 0
    assert len(bot.messages) == 1


async def test_check_pending_skips_transfer_without_hash(payment, monkeypatch):
    """Без хеша транзакции защиты от повторного зачёта нет — лучше не зачислять."""
    payment_id, _ = payment
    monkeypatch.setattr(crypto, "is_configured", lambda: True)

    async def fake_matches(expected, since=None):
        return [{"transaction_id": "", "value": str(crypto.to_micro("10.017"))}]

    monkeypatch.setattr(crypto, "find_incoming_matches", fake_matches)

    assert await crypto.check_pending(FakeBot()) == 0
    row = await get_payment(payment_id)
    assert row.status == "pending"
