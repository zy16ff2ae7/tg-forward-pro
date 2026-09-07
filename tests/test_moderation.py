"""Модерация 2.0: слова и ссылки — всем, рецидив — мутом, админы — мимо."""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.database import session_scope
from app.db.models import ForwardLog, Rule
from app.telegram_client import jobs
from app.telegram_client.filters import FilterConfig
from app.telegram_client.types import RuleSnapshot
from tests.helpers import TEST_USER_ID
from tests.test_many_chats import (  # noqa: F401
    login_open,
    many_chats_resolved,
    one_shot_stubbed,
)


class ModClient:
    """Чат с админом: удаляет, мутит и помнит всё."""

    def __init__(self, *, admins=()) -> None:
        self.admins = set(admins)
        self.deleted: list[tuple[int, int]] = []
        self.restricted: list[dict] = []

    def iter_participants(self, chat_id, **kwargs):
        admins = self.admins

        async def walk():
            for uid in admins:
                yield SimpleNamespace(id=uid)

        return walk()

    async def delete_messages(self, chat_id, ids):
        self.deleted.extend((chat_id, mid) for mid in ids)

    async def edit_permissions(self, chat_id, user_id, **kwargs):
        self.restricted.append({"chat": chat_id, "user": user_id, **kwargs})


@pytest.fixture(autouse=True)
def clean_admin_cache():
    jobs._mod_admins.clear()
    yield
    jobs._mod_admins.clear()


async def _mute_snapshot(create_user, create_account, conf, *, chat=-500):
    user_id = await create_user()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        from app.db import repo

        await repo.add_subscription_days(session, user_id, 30)
        rule = Rule(
            user_id=user_id, account_id=account_id, source_id=chat,
            target_id=chat, kind="mute", enabled=True, filters=conf.to_dict(),
        )
        session.add(rule)
        await session.flush()
        return RuleSnapshot(
            id=rule.id, user_id=user_id, target_id=chat, mode="copy",
            delay_seconds=0, account_id=account_id, kind="mute",
            source_id=chat, filters=conf,
        )


def _msg(mid, sender, text, chat=-500):
    return SimpleNamespace(id=mid, sender_id=sender, chat_id=chat, message=text)


async def test_target_messages_still_deleted(create_user, create_account):
    conf = FilterConfig(target_user_id=11, keywords=["спам"])
    snapshot = await _mute_snapshot(create_user, create_account, conf)
    client = ModClient()
    await jobs.run_job(client, _msg(1, 11, "это спам"), snapshot)
    await jobs.run_job(client, _msg(2, 11, "мирный пост"), snapshot)
    await jobs.run_job(client, _msg(3, 22, "это спам"), snapshot)
    assert client.deleted == [(-500, 1)]


async def test_banned_word_hits_anyone_but_admins(create_user, create_account):
    conf = FilterConfig(banned_words=["казино"])
    snapshot = await _mute_snapshot(create_user, create_account, conf)
    client = ModClient(admins={99})
    await jobs.run_job(client, _msg(1, 11, "заходи в КАЗИНО"), snapshot)
    await jobs.run_job(client, _msg(2, 99, "заходи в казино"), snapshot)
    await jobs.run_job(client, _msg(3, 11, "просто новости"), snapshot)
    assert client.deleted == [(-500, 1)]


async def test_links_blocked_for_mortals(create_user, create_account):
    conf = FilterConfig(block_links=True)
    snapshot = await _mute_snapshot(create_user, create_account, conf)
    client = ModClient(admins={99})
    await jobs.run_job(client, _msg(1, 11, "глянь https://x.io"), snapshot)
    await jobs.run_job(client, _msg(2, 99, "наш сайт https://x.io"), snapshot)
    assert client.deleted == [(-500, 1)]


async def test_explicit_target_beats_admin_immunity(create_user, create_account):
    conf = FilterConfig(target_user_id=99)
    snapshot = await _mute_snapshot(create_user, create_account, conf)
    client = ModClient(admins={99})
    await jobs.run_job(client, _msg(1, 99, "я админ, мне можно"), snapshot)
    assert client.deleted == [(-500, 1)]


async def test_third_warn_restricts_and_resets(create_user, create_account):
    conf = FilterConfig(banned_words=["казино"], max_warns=3, mute_hours=48)
    snapshot = await _mute_snapshot(create_user, create_account, conf)
    client = ModClient()
    await jobs.run_job(client, _msg(1, 11, "казино раз"), snapshot)
    await jobs.run_job(client, _msg(2, 11, "казино два"), snapshot)
    assert client.restricted == []
    await jobs.run_job(client, _msg(3, 11, "казино три"), snapshot)
    assert len(client.restricted) == 1
    assert client.restricted[0]["user"] == 11
    assert client.restricted[0]["send_messages"] is False
    assert client.deleted == [(-500, 1), (-500, 2), (-500, 3)]
    async with session_scope() as session:
        rule = await session.get(Rule, snapshot.id)
        assert rule.filters["mod_strikes"] == {}
        notes = (
            await session.execute(
                select(ForwardLog.error).where(
                    ForwardLog.rule_id == snapshot.id,
                    ForwardLog.error.like("🔇%"),
                )
            )
        ).scalars().all()
    assert len(notes) == 1 and "48" in notes[0]


async def test_zero_warns_only_deletes(create_user, create_account):
    conf = FilterConfig(banned_words=["казино"], max_warns=0)
    snapshot = await _mute_snapshot(create_user, create_account, conf)
    client = ModClient()
    for mid in range(1, 5):
        await jobs.run_job(client, _msg(mid, 11, "казино"), snapshot)
    assert len(client.deleted) == 4
    assert client.restricted == []


async def test_api_mute_roundtrip(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    payload = {
        "command": "mute", "account_id": account_id,
        "source": "@ch-1", "target_user": "@ch-2",
        "banned_words": ["казино", "ставки"],
        "block_links": True, "max_warns": 2, "mute_hours": 12,
    }
    resp = await client.post("/api/tasks", json=payload, headers=auth_headers)
    assert resp.status == 201, await resp.text()
    task = (await resp.json())["task"]
    assert task["banned_count"] == 2
    assert task["edit"]["banned_words"] == "казино, ставки"
    assert task["edit"]["block_links"] is True
    assert task["edit"]["max_warns"] == 2
    assert task["edit"]["mute_hours"] == 12
