"""«Удалить мои данные»: человек забирает всё, что оставлял в сервисе.

Команда /forget и DELETE /api/me/data — один и тот же сценарий: задачи,
аккаунты с сессиями, журнал, находки, библиотека, платежи и абонемент
исчезают, живые подключения останавливаются, чужие данные не страдают.
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import func, select

from app.bot.handlers import menu
from app.db import repo
from app.db.database import session_scope
from app.db.models import (
    CollectedItem,
    ForwardLog,
    Payment,
    PendingDelivery,
    PendingLogin,
    Rule,
    SavedMessage,
    Subscription,
    TelegramAccount,
    User,
)
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID, FakeCallback, add_rule, button_labels


async def _seed_user(user_id: int, create_account) -> dict[str, int]:
    """Человек со всем: аккаунт, задача, журнал, находки, библиотека, деньги."""
    account_id = await create_account(user_id)
    await add_rule(user_id, account_id)
    async with session_scope() as session:
        rule_id = (
            await session.execute(select(Rule.id).where(Rule.user_id == user_id))
        ).scalar_one()
        session.add(
            ForwardLog(rule_id=rule_id, user_id=user_id, source_msg_id=1, status="ok")
        )
        session.add(
            CollectedItem(
                rule_id=rule_id, user_id=user_id, kind="parser", payload={"name": "x"}
            )
        )
        session.add(SavedMessage(user_id=user_id, title="t", text="hello"))
        session.add(
            PendingDelivery(
                rule_id=rule_id,
                user_id=user_id,
                account_id=account_id,
                source_chat_id=-1001,
                message_id=7,
            )
        )
        session.add(
            PendingLogin(
                user_id=user_id,
                phone="+79990000000",
                session_encrypted="s",
                phone_code_hash="h",
            )
        )
        session.add(
            Payment(
                user_id=user_id,
                provider="stars",
                amount=100.0,
                currency="XTR",
                months=1,
                status="paid",
            )
        )
        await repo.activate_subscription(session, user_id, 1)
    return {"account_id": account_id, "rule_id": rule_id}


async def _count(model, user_id: int) -> int:
    column = model.id if model is User else model.user_id
    async with session_scope() as session:
        result = await session.execute(
            select(func.count()).select_from(model).where(column == user_id)
        )
        return int(result.scalar() or 0)


async def test_delete_user_data_removes_everything(create_user, create_account):
    """Строки человека исчезают из всех таблиц, сосед не страдает."""
    user_id = await create_user()
    neighbour = await create_user()
    await _seed_user(user_id, create_account)
    await _seed_user(neighbour, create_account)

    async with session_scope() as session:
        removed = await repo.delete_user_data(session, user_id)

    assert removed["users"] == 1
    assert removed["rules"] == 1
    assert removed["accounts"] == 1
    for model in (
        User,
        TelegramAccount,
        Rule,
        Subscription,
        Payment,
        ForwardLog,
        CollectedItem,
        SavedMessage,
        PendingDelivery,
        PendingLogin,
    ):
        assert await _count(model, user_id) == 0, model.__tablename__
    for model in (TelegramAccount, Rule, ForwardLog, SavedMessage):
        assert await _count(model, neighbour) >= 1, model.__tablename__


async def test_delete_unknown_user_is_a_noop(create_user):
    """Удаление несуществующего — нули, а не исключение."""
    await create_user()
    async with session_scope() as session:
        removed = await repo.delete_user_data(session, 987_654_321)
    assert removed["users"] == 0


async def test_forget_user_stops_live_clients(create_user, create_account):
    """Юзебот без хозяина не остаётся: подключение закрывается."""
    user_id = await create_user()
    ids = await _seed_user(user_id, create_account)

    disconnected: list[str] = []

    class FakeClient:
        def is_connected(self) -> bool:
            return True

        async def disconnect(self) -> None:
            disconnected.append("bye")

    manager._clients[ids["account_id"]] = FakeClient()
    try:
        removed = await manager.forget_user(user_id)
    finally:
        manager._clients.pop(ids["account_id"], None)

    assert removed["users"] == 1
    assert disconnected == ["bye"]
    assert await _count(User, user_id) == 0


async def test_api_delete_my_data(client, auth_headers, create_user, create_account):
    """Кабинет удаляет данные одним запросом — и только свои."""
    await create_user(id=TEST_USER_ID)
    await _seed_user(TEST_USER_ID, create_account)

    response = await client.delete("/api/me/data", headers=auth_headers)

    assert response.status == 200
    body = await response.json()
    assert body["ok"] is True
    assert body["removed"]["users"] == 1
    assert await _count(User, TEST_USER_ID) == 0


async def test_api_delete_my_data_requires_auth(client):
    """Без подписи чужое удаление не заказать."""
    assert (await client.delete("/api/me/data")).status == 401


class _Incoming:
    """Входящее /forget: хендлеру нужны отправитель и answer."""

    def __init__(self, user_id: int) -> None:
        self.text = "/forget"
        self.from_user = SimpleNamespace(id=user_id)
        self.sent: list[tuple[str, object]] = []

    async def answer(self, text: str, reply_markup=None, **kwargs):
        self.sent.append((text, reply_markup))


async def test_forget_command_warns_first(create_user):
    """Команда не удаляет сразу — показывает предупреждение с кнопками."""
    user_id = await create_user()
    message = _Incoming(user_id)

    await menu.cmd_forget(message)

    text, markup = message.sent[-1]
    assert "необратимое" in text
    labels = button_labels(markup)
    assert any("удалить всё" in label.lower() for label in labels), labels
    async with session_scope() as session:
        assert await session.get(User, user_id) is not None


async def test_forget_no_keeps_everything(create_user):
    """«Оставить» — данные на месте."""
    await create_user(id=TEST_USER_ID)
    callback = FakeCallback("forget:no")

    await menu.forget_cancel(callback)

    async with session_scope() as session:
        assert await session.get(User, TEST_USER_ID) is not None


async def test_forget_yes_wipes_and_says_bye(create_user, create_account):
    """Подтверждение удаляет и прощается по-человечески."""
    await create_user(id=TEST_USER_ID)
    await _seed_user(TEST_USER_ID, create_account)
    callback = FakeCallback("forget:yes")

    await menu.forget_confirm(callback)

    text, _ = callback.message.edits[-1]
    assert "всё удалено" in text
    assert await _count(User, TEST_USER_ID) == 0
