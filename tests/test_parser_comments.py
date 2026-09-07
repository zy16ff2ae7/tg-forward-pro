"""Парсер комментариев и инвайт: вовлечённые вместо мёртвых душ."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError, UserPrivacyRestrictedError
from telethon.tl.functions.channels import (
    GetFullChannelRequest,
    InviteToChannelRequest,
)

from app import exports
from app.db import repo
from app.db.database import session_scope
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
from tests.test_oneshot_journal import person
from tests.test_parser import HistoryClient, message


class CommentsClient:
    """Канал с обсуждением: посты, ответы и люди за ними."""

    def __init__(
        self,
        *,
        posts,
        replies,
        users,
        linked_chat_id=-777,
        channel=True,
        fail_users=(),
        flood_on_call=0,
    ):
        self._posts = list(posts)
        self._replies = dict(replies)
        self._users = dict(users)
        self._linked = linked_chat_id
        self._channel = channel
        self._fail_users = set(fail_users)
        self._flood_on_call = flood_on_call
        self.full_calls = 0
        self.invites: list[tuple] = []

    def iter_participants(self, chat_id, limit: int = 0, **kwargs):
        async def nobody():
            if False:
                yield None

        return nobody()

    def iter_messages(self, chat_id, limit: int = 0, reply_to=None):
        if reply_to is None:
            pool = self._posts[: limit or None]
        else:
            pool = self._replies.get(reply_to, [])[: limit or None]

        async def walk():
            for item in pool:
                yield item

        return walk()

    async def get_entity(self, ref):
        if isinstance(ref, int) and ref in self._users:
            return self._users[ref]
        return SimpleNamespace(id=ref, broadcast=self._channel)

    async def __call__(self, request):
        if isinstance(request, GetFullChannelRequest):
            self.full_calls += 1
            return SimpleNamespace(
                full_chat=SimpleNamespace(linked_chat_id=self._linked)
            )
        if isinstance(request, InviteToChannelRequest):
            self.invites.append((request.channel, list(request.users)))
            if self._flood_on_call and len(self.invites) == self._flood_on_call:
                raise FloodWaitError(None, capture=30)
            for user_id in request.users:
                if int(user_id) in self._fail_users:
                    raise UserPrivacyRestrictedError(request=None)
            return SimpleNamespace()
        raise AssertionError(f"нежданный запрос: {request!r}")


def post(post_id, with_replies=True):
    return SimpleNamespace(
        id=post_id, replies=SimpleNamespace() if with_replies else None
    )


def reply(sender_id):
    return SimpleNamespace(sender_id=sender_id)


def _commenters_conf(**extra):
    params = {"parser_mode": "comments", "limit": 10, "scan_limit": 50}
    params.update(extra)
    return FilterConfig(**params)


async def test_comments_collects_most_talkative_first(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    users = {uid: person(uid) for uid in (11, 12, 13)}
    client = CommentsClient(
        posts=[post(1), post(2), post(3, with_replies=False)],
        replies={
            1: [reply(11), reply(11), reply(12)],
            2: [reply(11), reply(13)],
        },
        users=users,
    )
    result = await jobs.run_parser(client, _snapshot(rule, filters=_commenters_conf()))
    assert result["ok"] is True
    assert result["mode"] == "comments"
    assert result["collected"] == 3
    async with session_scope() as session:
        items = await repo.list_collected_items(session, rule.id, limit=10)
    counts = {
        item.payload["user_id"]: item.payload["comments"] for item in items
    }
    assert counts == {11: 3, 12: 1, 13: 1}


async def test_comments_limit_cuts_quiet_not_active(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    users = {uid: person(uid) for uid in (11, 12, 13)}
    client = CommentsClient(
        posts=[post(1)],
        replies={1: [reply(11)] * 5 + [reply(12)] * 2 + [reply(13)]},
        users=users,
    )
    conf = _commenters_conf(limit=2)
    result = await jobs.run_parser(client, _snapshot(rule, filters=conf))
    assert result["collected"] == 2
    async with session_scope() as session:
        items = await repo.list_collected_items(session, rule.id, limit=10)
    assert {item.payload["user_id"] for item in items} == {11, 12}


async def test_comments_group_source_needs_no_discussion(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    client = CommentsClient(
        posts=[post(1)],
        replies={1: [reply(11)]},
        users={11: person(11)},
        channel=False,
    )
    result = await jobs.run_parser(client, _snapshot(rule, filters=_commenters_conf()))
    assert result["ok"] is True
    assert result["collected"] == 1
    assert client.full_calls == 0


async def test_comments_channel_without_discussion_explains(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    client = CommentsClient(posts=[], replies={}, users={}, linked_chat_id=None)
    result = await jobs.run_parser(client, _snapshot(rule, filters=_commenters_conf()))
    assert result["ok"] is False
    assert "обсужден" in result["error"]
    assert result["collected"] == 0


async def test_comments_respects_filters(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    users = {
        11: person(11),
        12: person(12, bot=True),
        13: person(13, username=""),
    }
    client = CommentsClient(
        posts=[post(1)],
        replies={1: [reply(11), reply(12), reply(13)]},
        users=users,
    )
    result = await jobs.run_parser(client, _snapshot(rule, filters=_commenters_conf()))
    assert result["collected"] == 1
    assert result["filtered"] == 2


async def test_comments_second_run_skips_known(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    client = CommentsClient(
        posts=[post(1)],
        replies={1: [reply(11)]},
        users={11: person(11)},
    )
    conf = _commenters_conf()
    first = await jobs.run_parser(client, _snapshot(rule, filters=conf))
    second = await jobs.run_parser(client, _snapshot(rule, filters=conf))
    assert (first["collected"], second["collected"]) == (1, 0)
    assert second["skipped"] == 1


async def test_history_mode_survived_screen_refactor(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    users = {11: person(11), 12: person(12, bot=True)}
    client = HistoryClient(
        [message(11), message(12), message(11)], users
    )
    conf = FilterConfig(parser_mode="history", limit=10, scan_limit=50)
    result = await jobs.run_parser(client, _snapshot(rule, filters=conf))
    assert result["ok"] is True
    assert result["collected"] == 1
    assert result["filtered"] == 1


def _item(payload):
    return SimpleNamespace(
        payload=payload, created_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    )


def test_csv_gains_comments_column_only_when_needed():
    plain = exports.collected_csv(
        "parser", [_item({"user_id": 11, "username": "u11", "name": "A", "phone": None})]
    ).decode("utf-8-sig")
    assert plain.splitlines()[0] == "id;ник;имя;телефон;когда (UTC)"
    rich = exports.collected_csv(
        "parser",
        [
            _item({"user_id": 11, "username": "u11", "name": "A", "phone": None}),
            _item(
                {
                    "user_id": 12,
                    "username": "u12",
                    "name": "B",
                    "phone": None,
                    "comments": 7,
                }
            ),
        ],
    ).decode("utf-8-sig")
    lines = rich.splitlines()
    assert lines[0] == "id;ник;имя;телефон;комментариев;когда (UTC)"
    assert lines[2].split(";")[4] == "7"


async def _seed_collected(rule, user_ids):
    snapshot = _snapshot(rule)
    payloads = [
        {"user_id": uid, "username": f"user{uid}", "name": "Кто-то", "phone": None}
        for uid in user_ids
    ]
    added, _ = await jobs._store(snapshot, "parser", payloads)
    assert added == len(user_ids)
    return snapshot


async def test_invite_marks_invited_and_takes_next_batch(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    snapshot = await _seed_collected(rule, [11, 12, 13])
    snapshot.filters = FilterConfig(invite_to="@my")
    client = CommentsClient(posts=[], replies={}, users={})
    first = await jobs.invite_collected(client, snapshot)
    assert first == {"ok": True, "invited": 3, "failed": 0, "pending": 0}
    assert len(client.invites) == 3
    async with session_scope() as session:
        items = await repo.list_collected_items(session, rule.id, limit=10)
    assert all(item.payload.get("invited") is True for item in items)
    second = await jobs.invite_collected(client, snapshot)
    assert second["invited"] == 0 and second["pending"] == 0
    assert len(client.invites) == 3


async def test_invite_marks_privacy_errors_and_continues(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    snapshot = await _seed_collected(rule, [11, 12])
    snapshot.filters = FilterConfig(invite_to="@my")
    client = CommentsClient(posts=[], replies={}, users={}, fail_users={11})
    result = await jobs.invite_collected(client, snapshot)
    assert result["ok"] is True
    assert (result["invited"], result["failed"], result["pending"]) == (1, 1, 0)
    async with session_scope() as session:
        items = await repo.list_collected_items(session, rule.id, limit=10)
    by_user = {item.payload["user_id"]: item.payload for item in items}
    assert "invite_error" in by_user[11]
    assert by_user[12].get("invited") is True
    # Повторный вызов закрытых больше не трогает.
    again = await jobs.invite_collected(client, snapshot)
    assert again["invited"] == 0 and again["pending"] == 0


async def test_invite_floodwait_stops_batch_partially(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    snapshot = await _seed_collected(rule, [11, 12, 13])
    snapshot.filters = FilterConfig(invite_to="@my")
    client = CommentsClient(posts=[], replies={}, users={}, flood_on_call=2)
    result = await jobs.invite_collected(client, snapshot)
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["invited"] == 1
    assert result["pending"] == 2
    assert "подождать 30" in result["error"]


async def test_invite_without_target_explains(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    snapshot = await _seed_collected(rule, [11])
    snapshot.filters = FilterConfig()
    client = CommentsClient(posts=[], replies={}, users={})
    result = await jobs.invite_collected(client, snapshot)
    assert result["ok"] is False
    assert "настройках" in result["error"]
    assert client.invites == []


async def test_repo_payload_patch_merges(create_user, create_account):
    rule = await _db_rule(create_user, create_account, with_trial=True)
    await _seed_collected(rule, [11])
    async with session_scope() as session:
        items = await repo.list_collected_items(session, rule.id, limit=10)
        await repo.update_collected_payload(session, items[0].id, {"invited": True})
        await session.commit()
    async with session_scope() as session:
        items = await repo.list_collected_items(session, rule.id, limit=10)
    assert items[0].payload["user_id"] == 11
    assert items[0].payload["invited"] is True


async def test_api_parser_comments_and_invite_roundtrip(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    payload = {
        "command": "parser",
        "account_id": account_id,
        "source": "@ch-1",
        "parser_mode": "comments",
        "invite_to": "@my",
    }
    resp = await client.post("/api/tasks", json=payload, headers=auth_headers)
    assert resp.status == 201, await resp.text()
    task = (await resp.json())["task"]
    assert task["edit"]["parser_mode"] == "comments"
    assert task["edit"]["invite_to"] == "@my"
    # Без живого клиента — честная ошибка, а не падение.
    resp = await client.post(
        f"/api/tasks/{task['id']}/invite", headers=auth_headers
    )
    assert resp.status == 200
    assert (await resp.json())["invite"]["ok"] is False
    # Инвайт — только у парсера.
    copy_payload = {
        "command": "copy_channel",
        "account_id": account_id,
        "source": "@ch-1",
        "target": "@ch-2",
    }
    resp = await client.post("/api/tasks", json=copy_payload, headers=auth_headers)
    assert resp.status == 201, await resp.text()
    copy_id = (await resp.json())["task"]["id"]
    resp = await client.post(f"/api/tasks/{copy_id}/invite", headers=auth_headers)
    assert resp.status == 409
