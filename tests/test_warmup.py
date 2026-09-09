import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func
from telethon.errors import FloodWaitError
from telethon.tl import types, functions

from app import warmup, warmup_plan
from app.db.database import session_scope
from app.db.models import Rule
from app.errors import AppError, ConflictError, ValidationError
from tests.helpers import TEST_USER_ID
from tests import test_many_chats

login_open = test_many_chats.login_open


def config(**values):
    return warmup_plan.normalize({'account_ids':[1], 'request_key':'warmup_test_1', **values}, now=time.time())


@pytest.mark.parametrize('values', [dict(account_ids=[True]), dict(days=31), dict(gap_minutes=1),
    dict(paid_gift=True), dict(gift_budget=10), dict(paid_gift='yes'), dict(story_privacy='friends'),
    dict(targets=['https://example.com']), dict(birthday={'day':30,'month':2}),
    dict(avatar=False,bio=False,stories=False), dict(days=1,daily_joins=1,targets=['@alpha','@beta'])])
def test_reject_invalid_plans(values):
    with pytest.raises(ValidationError):
        config(**values)


def test_default_plan_has_no_invented_birthday_or_paid_actions():
    plan = warmup_plan.build(config(), 1)
    assert [s['kind'] for s in plan] == ['avatar','bio'] + ['story'] * 7
    assert all(s['privacy'] == 'contacts' for s in plan if s['kind'] == 'story')
    assert len({s['random_id'] for s in plan if s['kind'] == 'story'}) == 7
    assert 'random_id' not in str(warmup_plan.public_steps(plan))


def test_no_catchup_burst():
    now = time.time()
    raw = {'warmup':{'gap_minutes':60}, 'warmup_last_join':now, 'warmup_last_story':now}
    assert warmup.due_at(raw, {'kind':'join','due_at':now-99999}) == now + 3600
    assert warmup.due_at(raw, {'kind':'story','due_at':now-99999}) == now + 86400


async def total_rules():
    async with session_scope() as session:
        return await session.scalar(select(func.count()).select_from(Rule))


async def test_bulk_preview_ownership_create_replay_and_paused_duplicate(
    client, auth_headers, create_user, create_account, login_open, monkeypatch
):
    from app.telegram_client.manager import manager
    monkeypatch.setattr(manager, 'refresh_rules', AsyncMock())
    await client.get('/api/me', headers=auth_headers)
    first = await create_account(TEST_USER_ID)
    second = await create_account(TEST_USER_ID)
    other = await create_account(await create_user())
    body = {'account_ids':[first,second], 'request_key':'bulk_warmup_1'}
    response = await client.post('/api/warmup?preview=1', json=body, headers=auth_headers)
    assert response.status == 200, await response.text()
    assert len((await response.json())['preview']['accounts']) == 2
    assert await total_rules() == 0
    denied = await client.post('/api/warmup', json={**body,'account_ids':[first,other]}, headers=auth_headers)
    assert denied.status == 404
    assert await total_rules() == 0
    created = await client.post('/api/warmup', json=body, headers=auth_headers)
    assert created.status == 201, await created.text()
    tasks = (await created.json())['tasks']
    assert len(tasks) == 2
    assert tasks[0]['warmup']['total'] == 9
    replay = await client.post('/api/warmup', json=body, headers=auth_headers)
    assert replay.status == 200
    assert (await replay.json())['replayed']
    assert await total_rules() == 2
    changed = await client.post('/api/warmup', json={**body,'days':8}, headers=auth_headers)
    assert changed.status == 409
    async with session_scope() as session:
        for rule in (await session.scalars(select(Rule))).all():
            rule.enabled = False
    duplicate = await client.post('/api/warmup', json={**body,'request_key':'bulk_warmup_2'}, headers=auth_headers)
    assert duplicate.status == 409
    assert (await client.post('/api/warmup', json=body)).status == 401


async def make_rule(create_user, create_account, *, kind='bio', **step_values):
    user = await create_user()
    account = await create_account(user)
    cfg = config(account_ids=[account], paid_gift=kind == 'gift', gift_budget=10 if kind == 'gift' else 0)
    step = {'id':1, 'kind':kind, 'status':'pending', 'due_at':time.time()-10,
            'text':'Тест', 'budget':10, 'privacy':'contacts', 'random_id':1234, **step_values}
    async with session_scope() as session:
        rule = Rule(user_id=user, account_id=account, source_id=0, target_id=0, kind='warmup',
                    filters={'warmup':cfg, 'warmup_steps':[step]})
        session.add(rule)
        await session.flush()
        return rule


async def stored(rule):
    async with session_scope() as session:
        return await session.get(Rule, rule.id)


def fake_manager(monkeypatch, telegram=None):
    monkeypatch.setattr(warmup, 'subscription_active', AsyncMock(return_value=True))
    telegram = telegram or AsyncMock()
    return SimpleNamespace(_oneshot_locks={}, is_online=lambda _:True, sending_paused_until=lambda _:None,
        profile_client=lambda _:telegram, update_account_profile=AsyncMock(),
        note_join_wait=AsyncMock(), note_peer_flood=AsyncMock())


async def test_durable_done_and_no_repetition(create_user, create_account, monkeypatch):
    rule = await make_rule(create_user, create_account)
    execute = AsyncMock(return_value={'note':'ok'})
    monkeypatch.setattr(warmup, 'execute', execute)
    manager = fake_manager(monkeypatch)
    await warmup._run_step(manager, rule.id, rule.user_id)
    await warmup._run_step(manager, rule.id, rule.user_id)
    assert (await stored(rule)).filters['warmup_steps'][0]['status'] == 'done'
    execute.assert_awaited_once()


@pytest.mark.parametrize('values', [{'status':'running'}, {'payment_started':True}])
async def test_restart_never_replays_uncertain_action(create_user, create_account, monkeypatch, values):
    rule = await make_rule(create_user, create_account, kind='gift', **values)
    execute = AsyncMock()
    monkeypatch.setattr(warmup, 'execute', execute)
    await warmup._run_step(fake_manager(monkeypatch), rule.id, rule.user_id)
    saved = await stored(rule)
    assert not saved.enabled
    assert saved.filters['warmup_steps'][0]['status'] == 'uncertain'
    execute.assert_not_awaited()
    await warmup.skip_uncertain(saved)
    assert warmup.report(await stored(rule))['state'] == 'done'


@pytest.mark.parametrize('error', [FloodWaitError(None, capture=120), AppError('limit', status=409, details={'code':'PeerFloodError'})])
async def test_telegram_limits_pause_account(create_user, create_account, monkeypatch, error):
    rule = await make_rule(create_user, create_account)
    monkeypatch.setattr(warmup, 'execute', AsyncMock(side_effect=error))
    manager = fake_manager(monkeypatch)
    await warmup._run_step(manager, rule.id, rule.user_id)
    saved = await stored(rule)
    if isinstance(error, FloodWaitError):
        assert saved.filters['warmup_steps'][0]['status'] == 'waiting'
        assert saved.filters['warmup_steps'][0]['due_at'] > time.time() + 110
        manager.note_join_wait.assert_awaited_once()
    else:
        assert not saved.enabled
        manager.note_peer_flood.assert_awaited_once()


async def test_cancel_inflight_requires_review(create_user, create_account, monkeypatch):
    rule = await make_rule(create_user, create_account)
    entered = asyncio.Event()
    async def execute(*_):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(warmup, 'execute', execute)
    await warmup.tick(fake_manager(monkeypatch))
    await asyncio.wait_for(entered.wait(), 2)
    await warmup.cancel_inactive(set())
    assert not warmup.busy_account(rule.account_id)
    assert (await stored(rule)).filters['warmup_steps'][0]['status'] == 'uncertain'


async def test_shared_oneshot_lock_and_future_due_prevent_work(create_user, create_account, monkeypatch):
    rule = await make_rule(create_user, create_account, due_at=time.time()+1000)
    manager = fake_manager(monkeypatch)
    execute = AsyncMock()
    monkeypatch.setattr(warmup, 'execute', execute)
    await warmup._run_step(manager, rule.id, rule.user_id)
    async with session_scope() as session:
        current = await session.get(Rule, rule.id)
        current.filters = {**current.filters, 'warmup_steps':[{**current.filters['warmup_steps'][0], 'due_at':0}]}
    async with manager._oneshot_locks[rule.user_id]:
        pending = asyncio.create_task(warmup._run_step(manager, rule.id, rule.user_id))
        await asyncio.sleep(.01)
        execute.assert_not_awaited()
        assert not pending.done()
    execute.return_value = {'note':'ok'}
    await pending
    execute.assert_awaited_once()


async def test_existing_profile_preserved(create_user, create_account, monkeypatch):
    rule = await make_rule(create_user, create_account)
    monkeypatch.setattr(warmup.account_profile, 'read_profile', AsyncMock(return_value={'about':'Своё'}))
    manager = fake_manager(monkeypatch)
    await warmup._run_step(manager, rule.id, rule.user_id)
    assert (await stored(rule)).filters['warmup_steps'][0]['status'] == 'skipped'
    manager.update_account_profile.assert_not_awaited()


async def test_story_uses_saved_id_and_contacts(create_user, create_account, monkeypatch):
    rule = await make_rule(create_user, create_account, kind='story')
    telegram = AsyncMock(side_effect=[SimpleNamespace(count_remains=1), None])
    telegram.upload_file = AsyncMock(return_value=types.InputFile(1,1,'x',''))
    await warmup.execute(fake_manager(monkeypatch, telegram), rule, rule.filters['warmup_steps'][0])
    sent = telegram.await_args_list[1].args[0]
    assert isinstance(sent, functions.stories.SendStoryRequest)
    assert sent.random_id == 1234
    assert isinstance(sent.privacy_rules[0], types.InputPrivacyValueAllowContacts)
    assert sent.period == 86400


def gift_client(*, cost=10, outcome=None):
    return AsyncMock(side_effect=[SimpleNamespace(gifts=[SimpleNamespace(id=1, stars=10)]),
        types.payments.PaymentFormStarGift(123, types.Invoice('XTR',[types.LabeledPrice('gift',cost)])),
        outcome or types.payments.PaymentResult(None)])


async def test_gift_cap_prevents_charge(create_user, create_account, monkeypatch):
    rule = await make_rule(create_user, create_account, kind='gift')
    telegram = gift_client(cost=11)
    with pytest.raises(ConflictError):
        await warmup.execute(fake_manager(monkeypatch, telegram), rule, rule.filters['warmup_steps'][0])
    assert telegram.await_count == 2
    assert not (await stored(rule)).filters['warmup_steps'][0].get('payment_started')


async def test_gift_payment_intent_stored_before_charge_and_wait_never_retries(create_user, create_account, monkeypatch):
    rule = await make_rule(create_user, create_account, kind='gift')
    telegram = gift_client(outcome=FloodWaitError(None,capture=10))
    manager = fake_manager(monkeypatch, telegram)
    await warmup._run_step(manager, rule.id, rule.user_id)
    saved = await stored(rule)
    step = saved.filters['warmup_steps'][0]
    assert step['payment_started'] and step['status'] == 'uncertain'
    assert not saved.enabled
    manager.note_join_wait.assert_awaited_once()
    async with session_scope() as session:
        (await session.get(Rule, rule.id)).enabled = True
    await warmup._run_step(manager, rule.id, rule.user_id)
    assert telegram.await_count == 3


async def test_future_rule_does_not_starve_second_account(create_user, create_account, monkeypatch):
    first = await make_rule(create_user, create_account, due_at=time.time()+86400)
    second_account = await create_account(first.user_id)
    async with session_scope() as session:
        second = Rule(user_id=first.user_id, account_id=second_account, source_id=0, target_id=0, kind='warmup',
            filters={**first.filters, 'warmup_steps':[{**first.filters['warmup_steps'][0], 'due_at':0}]})
        session.add(second)
        await session.flush()
    execute = AsyncMock(return_value={'note':'ok'})
    monkeypatch.setattr(warmup, 'execute', execute)
    await warmup.tick(fake_manager(monkeypatch))
    workers = [t for t, _ in warmup._workers.values()]
    await asyncio.wait_for(asyncio.gather(*workers), 2)
    execute.assert_awaited_once()
    assert execute.await_args.args[1].account_id == second_account
    assert (await stored(first)).filters['warmup_steps'][0]['status'] == 'pending'
    assert (await stored(second)).filters['warmup_steps'][0]['status'] == 'done'


@pytest.mark.parametrize('deleted', [True, False])
async def test_cancelled_plan_never_charges(create_user, create_account, monkeypatch, deleted):
    rule = await make_rule(create_user, create_account, kind='gift')
    async with session_scope() as session:
        current = await session.get(Rule, rule.id)
        if deleted:
            await session.delete(current)
        else:
            current.enabled = False
    telegram = gift_client()
    with pytest.raises(ConflictError):
        await warmup.execute(fake_manager(monkeypatch, telegram), rule, rule.filters['warmup_steps'][0])
    assert telegram.await_count == 2
