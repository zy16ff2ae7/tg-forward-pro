"""Статистика с ошибками: график честно показывает и больные дни."""
from datetime import timedelta

from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog
from tests.helpers import TEST_USER_ID
from tests.test_many_chats import (  # noqa: F401
    login_open,
    many_chats_resolved,
    one_shot_stubbed,
)


async def _seed(user_id, rule_id, days_ago, status, count):
    moment = repo.utcnow() - timedelta(days=days_ago)
    async with session_scope() as session:
        for _ in range(count):
            session.add(
                ForwardLog(
                    rule_id=rule_id,
                    user_id=user_id,
                    source_msg_id=1,
                    status=status,
                    created_at=moment,
                )
            )
        await session.commit()


async def test_stats_splits_ok_and_errors(create_user, create_account):
    user_id = await create_user()
    await create_account(user_id)
    await _seed(user_id, 1, 0, "ok", 5)
    await _seed(user_id, 1, 0, "error", 2)
    await _seed(user_id, 1, 3, "ok", 4)
    async with session_scope() as session:
        agg = await repo.forward_stats(session, user_id, 14)
    today = repo.utcnow().date().isoformat()
    old = (repo.utcnow() - timedelta(days=3)).date().isoformat()
    assert agg["per_day"][today] == 5
    assert agg["errors_day"][today] == 2
    assert agg["per_day"][old] == 4
    assert old not in agg["errors_day"]
    assert (agg["total"], agg["errors"]) == (9, 2)


async def test_api_stats_carries_errors_per_day(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    await client.get("/api/me", headers=auth_headers)
    await create_account(TEST_USER_ID)
    me = await (await client.get("/api/me", headers=auth_headers)).json()
    user_id = me["id"]
    await _seed(user_id, 1, 0, "ok", 3)
    await _seed(user_id, 1, 0, "error", 1)
    resp = await client.get("/api/stats?days=14", headers=auth_headers)
    assert resp.status == 200
    body = await resp.json()
    assert body["totals"]["errors_days"] == 1
    today = [day for day in body["per_day"] if day["count"] == 3]
    assert len(today) == 1
    assert today[0]["errors"] == 1
    assert len(body["per_day"]) == 14
