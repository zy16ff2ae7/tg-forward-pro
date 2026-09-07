"""Мёртвые получатели уходят из задач сами — после трёх безнадёжных сбоев."""
from types import SimpleNamespace

from sqlalchemy import select
from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    SlowModeWaitError,
)

from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog
from app.telegram_client import jobs
from app.telegram_client.filters import FilterConfig
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID
from tests.test_delivery_fixes import _db_rule, _snapshot
from tests.test_many_chats import (  # noqa: F401
    login_open,
    many_chats_resolved,
    one_shot_stubbed,
)


class DeadClient:
    """Один чат мёртв (писать запрещено), остальные живы."""

    def __init__(self, dead) -> None:
        self.dead = dead
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if chat_id == self.dead:
            raise ChatWriteForbiddenError(request=None)
        self.sent.append((chat_id, text))
        return SimpleNamespace(id=len(self.sent))


def test_classifier_tells_hopeless_from_transient():
    assert jobs.is_hopeless_chat_error(ChatWriteForbiddenError(request=None)) is True
    assert jobs.is_hopeless_chat_error(FloodWaitError(None, capture=5)) is False
    assert jobs.is_hopeless_chat_error(SlowModeWaitError(request=None)) is False
    assert jobs.is_hopeless_chat_error(RuntimeError("сеть")) is False


async def test_strikes_prune_from_targets_not_from_main(create_user, create_account):
    rule = await _db_rule(create_user, create_account, kind="broadcast")
    async with session_scope() as session:
        db_rule = await session.get(type(rule), rule.id)
        db_rule.filters = {"targets": [-2, -3]}
        await session.commit()
    for _ in range(2):
        async with session_scope() as session:
            pruned, strikes = await repo.register_chat_strikes(
                session, rule.id, failed={-2: "ChatWriteForbiddenError"}
            )
            await session.commit()
        assert pruned == []
    assert strikes == {"-2": {"fails": 2, "error": "ChatWriteForbiddenError"}}
    async with session_scope() as session:
        pruned, strikes = await repo.register_chat_strikes(
            session, rule.id, failed={-2: "ChatWriteForbiddenError"}
        )
        await session.commit()
    assert pruned == [-2]
    assert strikes == {}
    async with session_scope() as session:
        db_rule = await session.get(type(rule), rule.id)
        assert db_rule.filters["targets"] == [-3]
        assert db_rule.filters["chats_pruned"] == 1


async def test_main_target_never_pruned(create_user, create_account):
    rule = await _db_rule(create_user, create_account, kind="broadcast")
    main = -100200  # target_id из _db_rule
    async with session_scope() as session:
        for _ in range(4):
            pruned, strikes = await repo.register_chat_strikes(
                session, rule.id, failed={main: "ChatWriteForbiddenError"}
            )
        await session.commit()
    assert pruned == []
    assert strikes[str(main)]["fails"] == 4


async def test_success_resets_strikes(create_user, create_account):
    rule = await _db_rule(create_user, create_account, kind="broadcast")
    async with session_scope() as session:
        await repo.register_chat_strikes(
            session, rule.id, failed={-2: "ChatWriteForbiddenError"}
        )
        pruned, strikes = await repo.register_chat_strikes(
            session, rule.id, succeeded=[-2]
        )
        await session.commit()
    assert pruned == [] and strikes == {}


async def test_broadcast_prunes_dead_chat_after_three(create_user, create_account):
    rule = await _db_rule(create_user, create_account, kind="broadcast", with_trial=True)
    snapshot = _snapshot(
        rule, filters=FilterConfig(targets=[-2, -3]), target_id=-1
    )
    async with session_scope() as session:
        db_rule = await session.get(type(rule), rule.id)
        db_rule.filters = {"targets": [-2, -3]}
        await session.commit()
    client = DeadClient(dead=-2)
    message = SimpleNamespace(id=7, message="пост", media=None)
    for _ in range(3):
        await jobs._broadcast(client, message, snapshot)
    assert client.sent and all(chat != -2 for chat, _ in client.sent[-2:])
    # Снимок менеджера тоже обновлён: следующий пост мёртвого не трогает.
    assert snapshot.filters.targets == [-3]
    async with session_scope() as session:
        db_rule = await session.get(type(rule), rule.id)
        assert db_rule.filters["targets"] == [-3]
        notes = (
            await session.execute(
                select(ForwardLog.error).where(
                    ForwardLog.rule_id == rule.id,
                    ForwardLog.error.like("🧹%"),
                )
            )
        ).scalars().all()
    assert len(notes) == 1 and "-2" in notes[0]


async def test_mailing_failure_prunes_and_unsticks(create_user, create_account):
    rule = await _db_rule(create_user, create_account, kind="mailing")
    snapshot = _snapshot(rule, filters=FilterConfig(targets=[-2, -3]))
    async with session_scope() as session:
        db_rule = await session.get(type(rule), rule.id)
        db_rule.filters = {"targets": [-2, -3]}
        await session.commit()
    state: dict = {}
    for _ in range(3):
        await manager._mailing_failed(
            snapshot, state, -2, ChatWriteForbiddenError(request=None)
        )
    assert snapshot.filters.targets == [-3]
    async with session_scope() as session:
        db_rule = await session.get(type(rule), rule.id)
        assert db_rule.filters["targets"] == [-3]


async def test_mailing_transient_failure_keeps_chat(create_user, create_account):
    rule = await _db_rule(create_user, create_account, kind="mailing")
    snapshot = _snapshot(rule, filters=FilterConfig(targets=[-2]))
    state: dict = {}
    for _ in range(5):
        await manager._mailing_failed(
            snapshot, state, -2, SlowModeWaitError(request=None)
        )
    assert snapshot.filters.targets == [-2]
    assert snapshot.filters.chat_strikes == {}


async def test_api_view_carries_pruned_counter(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    resp = await client.post(
        "/api/tasks",
        json={
            "command": "broadcast", "account_id": account_id,
            "source": "@ch-1", "targets": ["@ch-2", "@ch-3"],
        },
        headers=auth_headers,
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["task"]["chats_pruned"] == 0
