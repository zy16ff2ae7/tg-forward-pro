import asyncio
from types import SimpleNamespace

import pytest

from app import join_queue
from app.db.database import session_scope
from app.db.models import Rule


@pytest.fixture(autouse=True)
def isolated_pauses(monkeypatch):
    from app.telegram_client.manager import manager
    monkeypatch.setattr(manager, '_send_pause_until', {})


async def make_rule(create_user, create_account):
    user = await create_user()
    account = await create_account(user)
    async with session_scope() as session:
        rule = Rule(user_id=user, account_id=account, kind='autosubscribe', source_id=0, target_id=0,
                    filters={'subscribe_to':['@one','@two']})
        session.add(rule)
        await session.flush()
        return rule


async def stored(rule):
    async with session_scope() as session:
        return await session.get(Rule, rule.id)


async def wait_done(rule):
    for _ in range(200):
        if not join_queue.running(rule.id):
            return
        await asyncio.sleep(.01)
    raise AssertionError('queue did not finish')


async def test_background_progress_cancel_and_resume(create_user, create_account):
    rule = await make_rule(create_user, create_account)
    entered = asyncio.Event()
    async def run(_):
        await join_queue.progress('@one', 'joined')
        entered.set()
        await asyncio.Event().wait()
    manager = SimpleNamespace(run_task_now=run)
    assert (await join_queue.start(manager, rule))['queued']
    await asyncio.wait_for(entered.wait(), 2)
    assert (await stored(rule)).filters['join_queue']['items']['@one']['status'] == 'joined'
    assert (await join_queue.start(manager, rule))['queued']
    await join_queue.cancel_inactive(set())
    saved = await stored(rule)
    assert join_queue.view(saved)['state'] == 'stopped'
    async def resume(_):
        assert join_queue.completed_targets() == {'@one'}
        await join_queue.progress('@two', 'requested')
        return {'ok':True}
    await join_queue.start(SimpleNamespace(run_task_now=resume), saved)
    await wait_done(rule)
    assert join_queue.view(await stored(rule))['state'] == 'done'
    assert len((await stored(rule)).filters['join_queue']['items']) == 2


async def test_flood_wait_survives_restart(create_user, create_account):
    rule = await make_rule(create_user, create_account)
    async def run(_):
        await join_queue.progress('@one', 'waiting', code='FloodWaitError')
        await join_queue.flood_wait(300)
        return {'ok':False, 'error':'Wait'}
    manager = SimpleNamespace(run_task_now=run)
    await join_queue.start(manager, rule)
    await wait_done(rule)
    saved = await stored(rule)
    assert saved.filters['join_queue']['retry_at']
    assert not (await join_queue.start(manager, saved))['ok']


def test_stale_running_is_interrupted():
    rule = SimpleNamespace(id=999999, filters={'join_queue':{'state':'running'}})
    assert join_queue.view(rule)['state'] == 'stopped'


async def test_stop_endpoint_cannot_stop_another_users_queue(client, auth_headers, create_user, create_account):
    rule = await make_rule(create_user, create_account)
    entered = asyncio.Event()
    async def run(_):
        entered.set()
        await asyncio.Event().wait()
    try:
        await join_queue.start(SimpleNamespace(run_task_now=run), rule)
        await asyncio.wait_for(entered.wait(), 2)
        response = await client.post(f'/api/tasks/{rule.id}/stop', headers=auth_headers)
        assert response.status == 404
        assert join_queue.running(rule.id)
    finally:
        await join_queue.stop(rule.id)


async def test_same_account_rejects_second_queue(create_user, create_account):
    rule = await make_rule(create_user, create_account)
    other = SimpleNamespace(id=rule.id + 1000, account_id=rule.account_id)
    entered = asyncio.Event()
    async def run(_):
        entered.set()
        await asyncio.Event().wait()
    try:
        manager = SimpleNamespace(run_task_now=run)
        await join_queue.start(manager, rule)
        await asyncio.wait_for(entered.wait(), 2)
        assert not (await join_queue.start(manager, other))['ok']
    finally:
        await join_queue.stop(rule.id)


async def test_per_run_limit_can_resume_without_rejoining(create_user, create_account, monkeypatch):
    from app.telegram_client import jobs
    from app.telegram_client.manager import _snapshot
    from unittest.mock import AsyncMock
    rule = await make_rule(create_user, create_account)
    async with session_scope() as session:
        current = await session.get(Rule, rule.id)
        current.filters = {**current.filters, 'join_limit':1, 'join_gap':0}
    monkeypatch.setattr(jobs, 'JOIN_MIN_GAP', 0)
    monkeypatch.setattr(jobs, '_join_account_allowance', AsyncMock(return_value=10))
    monkeypatch.setattr(jobs, '_log_join', AsyncMock())
    async def split(_, targets):
        return targets, 0
    monkeypatch.setattr(jobs, '_split_already_member', split)
    telegram = AsyncMock()
    async def run(current):
        return await jobs.run_autosubscribe(telegram, _snapshot(current))
    manager = SimpleNamespace(run_task_now=run)
    await join_queue.start(manager, rule)
    await wait_done(rule)
    report = join_queue.view(await stored(rule))
    assert report['state'] == 'stopped'
    assert report['remaining'] == 1
    await join_queue.start(manager, rule)  # stale caller must load fresh saved progress
    await wait_done(rule)
    assert join_queue.view(await stored(rule))['state'] == 'done'
    assert telegram.await_count == 2
    assert [call.args[0].channel for call in telegram.await_args_list] == ['@one','@two']


async def test_start_reads_settings_after_editor_commits(create_user, create_account):
    rule = await make_rule(create_user, create_account)
    started = asyncio.Event()
    async def run(current):
        assert current.filters['subscribe_to'] == ['@edited']
        started.set()
        return {'ok':True}
    async with join_queue.rule_lock(rule.id):
        pending = asyncio.create_task(join_queue.start(SimpleNamespace(run_task_now=run), rule))
        await asyncio.sleep(0)
        async with session_scope() as session:
            current = await session.get(Rule, rule.id)
            current.filters = {'subscribe_to':['@edited']}
        assert not started.is_set()
    assert (await pending)['queued']
    await wait_done(rule)
    assert started.is_set()
