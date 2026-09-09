import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image
from telethon.errors import FloodWaitError

from app import account_profile
from app.errors import AppError, ValidationError
from tests.helpers import TEST_USER_ID


@pytest.mark.parametrize('body', [{}, [], {'first_name': ''}, {'about': 'a' * 71},
    {'birthday': {'day': 30, 'month': 2}}, {'birthday': {'day': True, 'month': 1}},
    {'birthday': {'day': 1, 'month': 1, 'year': 2100}}, {'photo': 'invalid'}, {'unknown': 'x'}])
def test_invalid_profile(body):
    with pytest.raises(ValidationError):
        account_profile.validate_changes(body)


def test_optional_year_and_clear():
    assert account_profile.validate_changes({'birthday': {'day': 29, 'month': 2}})
    assert account_profile.validate_changes({'birthday': None, 'about': ''}) == {'birthday': None, 'about': ''}


def test_photo_validated_and_reencoded():
    raw = io.BytesIO()
    Image.new('RGB', (200, 200), 'red').save(raw, 'PNG')
    result = account_profile.validate_changes({'photo': base64.b64encode(raw.getvalue()).decode()})
    assert Image.open(result['photo']).format == 'JPEG'
    with pytest.raises(ValidationError):
        account_profile.validate_changes({'photo': base64.b64encode(b'not an image').decode()})


async def test_profile_partial_failure():
    client = AsyncMock(side_effect=[None, FloodWaitError(None, capture=123)])
    with pytest.raises(AppError) as raised:
        await account_profile.update_profile(client, {'first_name': 'Anna', 'birthday': None})
    assert raised.value.status == 429
    assert raised.value.details['applied'] == ['first_name']
    assert raised.value.details['retry_at']
    assert client.await_args_list[0].kwargs == {'flood_sleep_threshold': 0}


async def test_profile_reads_without_modifying():
    client = AsyncMock(return_value=SimpleNamespace(
        users=[SimpleNamespace(first_name='Anna', last_name=None, photo=None)],
        full_user=SimpleNamespace(about='', birthday=None)))
    result = await account_profile.read_profile(client)
    assert result['first_name'] == 'Anna'
    assert result['birthday'] is None
    assert type(client.await_args.args[0]).__name__ == 'GetFullUserRequest'


async def test_manual_profile_editor_removed(client, auth_headers, create_account):
    await client.get('/api/me', headers=auth_headers)
    account = await create_account(TEST_USER_ID)
    assert (await client.get(f'/api/accounts/{account}/profile', headers=auth_headers)).status == 404
    assert (await client.patch(f'/api/accounts/{account}/profile', headers=auth_headers, json={'about':'x'})).status == 404
