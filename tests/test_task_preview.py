"""Launch review validates real settings without persisting or starting work."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.db.database import session_scope
from app.db.models import Rule, SavedMessage
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID
from tests import test_many_chats

login_open = test_many_chats.login_open
many_chats_resolved = test_many_chats.many_chats_resolved


async def counts():
    async with session_scope() as session:
        return tuple([
            await session.scalar(select(func.count()).select_from(model))
            for model in (Rule, SavedMessage)
        ])


@pytest.mark.parametrize('command', ['mailing', 'poster', 'autosubscribe'])
async def test_preview_does_not_write_or_run(
    client, auth_headers, create_account, login_open, many_chats_resolved, monkeypatch, command
):
    await client.get('/api/me', headers=auth_headers)
    account = await create_account(TEST_USER_ID)
    run = AsyncMock()
    refresh = AsyncMock()
    monkeypatch.setattr(manager, 'run_task_now', run)
    monkeypatch.setattr(manager, 'refresh_rules', refresh)
    body = {'command': command, 'account_id': account, 'targets': ['@ch-1', '@ch-2'],
            'message': 'Первая строка\nВторая строка\n\nДругое сообщение', 'gap': 1}
    response = await client.post('/api/tasks?preview=1', json=body, headers=auth_headers)
    assert response.status == 200, await response.text()
    preview = (await response.json())['preview']
    if command != 'autosubscribe':
        assert preview['messages_count'] == 2
        assert preview['messages'][0] == 'Первая строка\nВторая строка'
    if command == 'mailing':
        assert preview['gap_seconds'] >= 30
    assert await counts() == (0, 0)
    run.assert_not_awaited()
    refresh.assert_not_awaited()


async def test_preview_and_creation_share_settings(
    client, auth_headers, create_account, login_open, many_chats_resolved, monkeypatch
):
    await client.get('/api/me', headers=auth_headers)
    account = await create_account(TEST_USER_ID)
    monkeypatch.setattr(manager, 'refresh_rules', AsyncMock())
    body = {'command': 'mailing', 'account_id': account,
            'targets': ['@ch-1', '@ch-1', '@ch-2'], 'message': 'Объявление',
            'gap': 1, 'cycle': 1, 'start': '10:00', 'end': '20:00', 'tz': 180}
    preview_response = await client.post('/api/tasks?preview=1', json=body, headers=auth_headers)
    assert preview_response.status == 200
    preview = (await preview_response.json())['preview']
    created = await client.post('/api/tasks', json=body, headers=auth_headers)
    assert created.status == 201, await created.text()
    task = (await created.json())['task']
    assert len(preview['chats']) == task['targets_count'] == 2
    assert preview['gap_seconds'] == task['mailing']['gap_seconds']
    assert preview['cycle_seconds'] == task['mailing']['cycle_seconds']
    assert preview['window_start'] == task['window_start']
    assert preview['window_tz'] == task['window_tz']
    assert await counts() == (1, 1)


async def test_preview_requires_auth_and_account_ownership(
    client, auth_headers, create_user, create_account
):
    response = await client.post('/api/tasks?preview=1', json={})
    assert response.status == 401
    other = await create_user()
    account = await create_account(other)
    response = await client.post('/api/tasks?preview=1', headers=auth_headers,
                                 json={'command': 'mailing', 'account_id': account,
                                       'targets': ['@ch-1'], 'message': 'Текст'})
    assert response.status == 404
    assert await counts() == (0, 0)


async def test_task_exposes_spam_pause_and_window(
    client, auth_headers, create_account, login_open, many_chats_resolved, monkeypatch
):
    import time
    from datetime import datetime
    await client.get('/api/me', headers=auth_headers)
    account = await create_account(TEST_USER_ID)
    monkeypatch.setattr(manager, 'refresh_rules', AsyncMock())
    monkeypatch.setattr(manager, 'sending_paused_until', lambda _: time.time() + 3600)
    from app.telegram_client import jobs
    monkeypatch.setattr(jobs, 'quiet_wait_seconds', lambda *args, **kwargs: 120)
    response = await client.post('/api/tasks', headers=auth_headers,
                                 json={'command': 'mailing', 'account_id': account,
                                       'targets': ['@ch-1'], 'message': 'Текст'})
    assert response.status == 201
    task = (await response.json())['task']
    assert datetime.fromisoformat(task['paused_until']).tzinfo is not None
    assert datetime.fromisoformat(task['window_opens_at']).timestamp() > time.time()
    tasks = await client.get('/api/tasks', headers=auth_headers)
    assert (await tasks.json())['tasks'][0]['paused_until']


async def test_old_mailing_reports_effective_intervals_without_rewriting_settings(
    client, auth_headers, create_account, login_open, many_chats_resolved, monkeypatch
):
    from app.db import repo
    await client.get('/api/me', headers=auth_headers)
    account = await create_account(TEST_USER_ID)
    monkeypatch.setattr(manager, 'refresh_rules', AsyncMock())
    response = await client.post('/api/tasks', headers=auth_headers,
                                 json={'command': 'mailing', 'account_id': account,
                                       'targets': ['@ch-1'], 'message': 'Текст'})
    assert response.status == 201
    rule_id = (await response.json())['task']['id']
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, TEST_USER_ID)
        rule.filters = {**rule.filters, 'gap_seconds': 1, 'cycle_seconds': 10}
    response = await client.get('/api/tasks', headers=auth_headers)
    task = (await response.json())['tasks'][0]
    assert task['mailing']['gap_seconds'] == 30
    assert task['mailing']['cycle_seconds'] == 60
    async with session_scope() as session:
        rule = await repo.get_rule(session, rule_id, TEST_USER_ID)
        assert rule.filters['gap_seconds'] == 1
        assert rule.filters['cycle_seconds'] == 10
