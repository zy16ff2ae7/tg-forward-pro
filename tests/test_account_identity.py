"""Развязка аккаунтов бесплатными средствами.

Прокси на аккаунт стоят денег, поэтому их нет — но кластеризацию «одна ферма»
разбивают и бесплатные меры: у каждого номера свой отпечаток устройства,
вход своими ключами API доступен и из бота, а мор аккаунтов виден сторожу.
"""
from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from app import accounts_login
from app.bot import keyboards as kb
from app.bot.handlers import accounts as bot_accounts
from app.bot.states import LoginStates
from app.config import settings
from app.db import repo
from app.db.database import session_scope
from app.db.models import TelegramAccount
from app.telegram_client import antispam
from app.telegram_client.manager import SESSION_REVOKED, manager


# ───────────────────────── отпечаток устройства ─────────────────────────


def test_fingerprint_stable_per_phone():
    """Один номер — один отпечаток навсегда, хранить в базе нечего."""
    first = antispam.device_fingerprint("+79000000001")
    second = antispam.device_fingerprint("+79000000001")
    assert first == second
    assert set(first) == {"device_model", "system_version", "app_version"}


def test_fingerprints_differ_across_phones():
    """Разные номера — разные отпечатки: пул реально тасуется."""
    seen = {
        tuple(sorted(antispam.device_fingerprint(f"+7900000000{pos}").items()))
        for pos in range(6)
    }
    assert len(seen) > 1


def test_new_client_uses_phone_fingerprint(monkeypatch, mtproto_on):
    """Рабочий клиент представляется отпечатком своего номера."""
    monkeypatch.setattr(settings, "api_id", 123)
    monkeypatch.setattr(settings, "api_hash", "0123456789abcdef0123456789abcdef")
    phone = "+79000000001"

    client = manager._new_client("", None, fingerprint_seed=phone)

    expected = antispam.device_fingerprint(phone)
    # Telethon прячет отпечаток в InitConnection — читаем оттуда же.
    init = client._init_request
    assert init.device_model == expected["device_model"]
    assert init.system_version == expected["system_version"]
    assert init.app_version == expected["app_version"]


def test_new_client_without_seed_takes_from_pool(monkeypatch, mtproto_on):
    """Без сида (QR до сканирования) — случайный, но из того же пула."""
    monkeypatch.setattr(settings, "api_id", 123)
    monkeypatch.setattr(settings, "api_hash", "0123456789abcdef0123456789abcdef")

    client = manager._new_client("", None)

    init = client._init_request
    assert {
        "device_model": init.device_model,
        "system_version": init.system_version,
        "app_version": init.app_version,
    } in antispam.DEVICE_FINGERPRINTS


# ─────────────────────────── свои ключи в боте ───────────────────────────


class FakeState:
    def __init__(self) -> None:
        self.data: dict = {}
        self.state = None

    async def get_data(self) -> dict:
        return dict(self.data)

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def set_state(self, state) -> None:
        self.state = state

    async def clear(self) -> None:
        self.data.clear()
        self.state = None


class FakeWaitMsg:
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append(text)


class FakeInMessage:
    def __init__(self, text: str, user_id: int) -> None:
        self.text = text
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[tuple[str, object]] = []
        self.deleted = False
        self.wait = FakeWaitMsg()

    async def answer(self, text: str, reply_markup=None, **kwargs):
        self.answers.append((text, reply_markup))
        return self.wait

    async def delete(self) -> None:
        self.deleted = True


def test_login_choice_offers_own_keys():
    """На выборе способа входа ключи предлагаются, а не спрятаны."""
    labels = [b.text for row in kb.login_choice_kb().inline_keyboard for b in row]
    assert any("ключи" in label.lower() for label in labels)


async def test_keys_accepted_and_passed_to_login(create_user, monkeypatch):
    """Ключи приняли — и номер входит уже через них."""
    user_id = await create_user()
    state = FakeState()
    state.state = LoginStates.keys
    message = FakeInMessage("123456 0123456789abcdef0123456789abcdef", user_id)

    await bot_accounts.process_keys(message, state)

    assert state.data == {
        "api_id": 123456,
        "api_hash": "0123456789abcdef0123456789abcdef",
    }
    assert message.deleted is True, "секрет стёрли из переписки"
    assert "приняты" in message.answers[-1][0]

    calls: dict = {}

    async def fake_start(user_id_arg, phone, **kwargs):
        calls.update(kwargs)
        return accounts_login.LoginStep(stage="code", phone=str(phone))

    monkeypatch.setattr(bot_accounts.login, "start", fake_start)
    await bot_accounts.process_phone(FakeInMessage("+79000000001", user_id), state)

    assert calls.get("api_id") == 123456
    assert calls.get("api_hash") == "0123456789abcdef0123456789abcdef"


async def test_keys_rejected_stays(create_user):
    """Мусор вместо ключей — объяснение и тот же шаг, а не вылет."""
    user_id = await create_user()
    state = FakeState()
    state.state = LoginStates.keys
    message = FakeInMessage("мусор", user_id)

    await bot_accounts.process_keys(message, state)

    assert "Нужно два значения" in message.answers[-1][0]
    assert state.state == LoginStates.keys
    assert state.data == {}


# ───────────────────────────── счётчик смертей ─────────────────────────────


async def _kill(account_id: int, reason: str = SESSION_REVOKED):
    async with session_scope() as session:
        account = await session.get(TelegramAccount, account_id)
        await repo.set_account_error(session, account, reason)


async def test_count_disabled_since_sees_only_fresh_hopeless(create_user, create_account):
    """«Умерли сегодня» — свежие безнадёжные; фон и чужие причины мимо."""
    user_id = await create_user()
    fresh = await create_account(user_id)
    old = await create_account(user_id)
    alive = await create_account(user_id)
    other = await create_account(user_id)
    await _kill(fresh)
    await _kill(old)
    await _kill(other, reason="сеть отвалилась")
    async with session_scope() as session:
        aged = await session.get(TelegramAccount, old)
        aged.disabled_at = repo.utcnow() - timedelta(hours=25)
        await session.commit()

    async with session_scope() as session:
        assert await repo.count_disabled_since(
            session, [SESSION_REVOKED], hours=24
        ) == 1
        assert alive is not None


async def test_relogin_clears_disabled_mark(create_user, create_account):
    """Вернулся в работу — метка смерти снята, в следующий мор не попадёт."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    await _kill(account_id)
    async with session_scope() as session:
        account = await session.get(TelegramAccount, account_id)
        assert account.disabled_at is not None
        await repo.set_account_error(session, account, None)

    async with session_scope() as session:
        assert await repo.count_disabled_since(
            session, [SESSION_REVOKED], hours=24
        ) == 0
