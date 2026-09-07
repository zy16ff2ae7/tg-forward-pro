"""Автозакреп: отправленное сообщение сразу встаёт в закреп приёмника.

Закреп — украшение, а не доставка: без прав админа сообщение всё равно
уходит, а неудача видна в логе службы, а не в журнале ошибок задачи.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.db.database import session_scope
from app.db.models import Rule
from app.telegram_client import forwarder, jobs
from app.telegram_client.filters import FilterConfig
from app.telegram_client.manager import manager
from app.telegram_client.types import SENT, RuleSnapshot


class PinClient:
    """Клиент, помнящий отправки и закрепы. Ломать умеет только закреп."""

    def __init__(self, *, pin_error: BaseException | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.pins: list[tuple[int, int]] = []
        self.pin_error = pin_error

    def is_connected(self) -> bool:
        return True

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=100 + len(self.sent))

    async def pin_message(self, chat_id: int, message_id: int, **kwargs):
        if self.pin_error is not None:
            raise self.pin_error
        self.pins.append((chat_id, message_id))


def _message(mid: int = 7):
    return SimpleNamespace(id=mid, message="свежий пост", media=None)


async def _forward_rule(user_id: int, account_id: int, filters: dict) -> RuleSnapshot:
    async with session_scope() as session:
        rule = Rule(
            user_id=user_id, account_id=account_id, source_id=-100, target_id=-200,
            kind="forward", mode="copy", enabled=True, filters=filters,
        )
        session.add(rule)
        await session.flush()
        return RuleSnapshot(
            id=rule.id, user_id=user_id, target_id=-200, mode="copy",
            delay_seconds=0, account_id=account_id, kind="forward", source_id=-100,
            filters=FilterConfig.from_dict(dict(rule.filters or {})),
        )


async def _subscribed(uid: int) -> bool:
    return True


# ─────────────────────────── пересылка закрепляет ─────────────────────────────


async def test_forward_pins_when_enabled(create_user, create_account, monkeypatch):
    """Галочка стоит — ушло и закрепилось тем же номером."""
    monkeypatch.setattr(forwarder, "subscription_active", _subscribed)
    user_id = await create_user()
    rule = await _forward_rule(
        user_id, await create_account(user_id), {"pin_on_send": True}
    )
    client = PinClient()

    result = await forwarder.deliver(client, _message(), rule)

    assert result is SENT
    assert client.sent == [(-200, "свежий пост")]
    assert client.pins == [(-200, 101)]


async def test_forward_skips_pin_when_disabled(create_user, create_account, monkeypatch):
    """Галочки нет — уходит как раньше, закреп не трогаем."""
    monkeypatch.setattr(forwarder, "subscription_active", _subscribed)
    user_id = await create_user()
    rule = await _forward_rule(user_id, await create_account(user_id), {})
    client = PinClient()

    result = await forwarder.deliver(client, _message(), rule)

    assert result is SENT
    assert client.pins == []


async def test_failed_pin_keeps_delivery(create_user, create_account, monkeypatch):
    """Нет прав на закреп — сообщение ушло, отправка засчитана."""
    monkeypatch.setattr(forwarder, "subscription_active", _subscribed)
    user_id = await create_user()
    rule = await _forward_rule(
        user_id, await create_account(user_id), {"pin_on_send": True}
    )
    client = PinClient(pin_error=RuntimeError("CHAT_ADMIN_REQUIRED"))

    result = await forwarder.deliver(client, _message(), rule)

    assert result is SENT
    assert client.sent == [(-200, "свежий пост")]
    assert client.pins == []


# ─────────────────────── рассылка отдаёт id отправки ──────────────────────────


async def test_mailing_send_returns_message_id():
    """Рассылка возвращает id — иначе закрепу нечего закреплять."""
    client = PinClient()
    rule = RuleSnapshot(
        id=1, user_id=100, target_id=-200, mode="copy", delay_seconds=0,
        account_id=1, kind="mailing", filters=FilterConfig(),
    )

    sent_id = await jobs.mailing_send(client, rule, jobs.own_text_item("привет"), -200)

    assert sent_id == 101


async def test_mailing_tick_pins_when_enabled(create_user, create_account):
    """Рассылка с галочкой: отправила первому чату и закрепила."""
    from tests.test_mailing import make_mailing

    _rule_id, _user_id, account_id = await make_mailing(
        create_user, create_account,
        targets=[-1001, -1002], texts=["всем привет"], pin_on_send=True,
    )
    client = PinClient()
    manager._clients[account_id] = client
    try:
        await manager._mailing_tick()
    finally:
        manager._mailing_rules = []
        manager._mailing_state.clear()
        manager._clients.clear()

    assert client.sent == [(-1001, "всем привет")]
    assert client.pins == [(-1001, 101)]



async def test_broadcast_pins_each_chat(create_user, create_account):
    """Веер с галочкой: ушло в два чата — закрепилось в обоих."""
    from tests.test_delivery_fixes import _db_rule, _snapshot

    rule = await _db_rule(create_user, create_account, kind="broadcast")
    snapshot = _snapshot(
        rule,
        filters=FilterConfig(targets=[-300], pin_on_send=True),
    )
    client = PinClient()

    await jobs._broadcast(client, _message(), snapshot)

    assert [chat for chat, _ in client.sent] == [-100200, -300]
    assert client.pins == [(-100200, 101), (-300, 102)]
