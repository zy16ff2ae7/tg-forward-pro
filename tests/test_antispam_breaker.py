"""Антиспам-рубильник: PeerFlood останавливает весь аккаунт, а не одну задачу.

Раньше ограничение за спам падало в общую кучу «ошибка отправки»: задача ждала
30 секунд и долбила дальше, а соседние слали вообще без паузы — временное
ограничение превращалось в полноценный спамблок. Теперь первый же PeerFlood
встаёт рубильником на весь аккаунт: отправки стоят 12 часов, ретраев нет,
а человек читает причину в карточке задачи и в статусе аккаунта.
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from telethon.errors import (
    AuthKeyDuplicatedError,
    PeerFloodError,
    SlowModeWaitError,
)

from app.db import repo
from app.db.database import session_scope
from app.bot import keyboards as kb
from app.bot import texts
from app.db.models import AccountPause, Rule, TelegramAccount
from app.task_alerts import set_alert_bot
from app.telegram_client import jobs
from app.telegram_client.antispam import (
    ACCOUNT_DAILY_JOIN_CAP,
    PEER_FLOOD_PAUSE_HOURS,
)
from app.telegram_client.filters import FilterConfig
from app.telegram_client.manager import SESSION_REVOKED, manager
from app.telegram_client.queue import DeliveryQueue
from app.telegram_client.types import RuleSnapshot
from tests.helpers import TEST_USER_ID
from tests.test_delivery_fixes import _db_rule
from tests.test_mailing import FakeClient, clean_manager, make_mailing, no_pauses  # noqa: F401
from tests.test_many_chats import login_open, many_chats_resolved  # noqa: F401
from tests.test_oneshot_journal import OneShotClient, make_oneshot, run_now  # noqa: F401
from tests.test_task_health import logged


@pytest.fixture(autouse=True)
def _reset_manager(clean_manager):
    """Менеджер — синглтон: между тестами забываем клиентов, круги и паузы."""
    manager._last_send_at.clear()


def _snapshot(rule_id, user_id, account_id, **kwargs) -> RuleSnapshot:
    params = {
        "id": rule_id,
        "user_id": user_id,
        "target_id": -1,
        "account_id": account_id,
        "mode": "copy",
        "delay_seconds": 0,
    }
    params.update(kwargs)
    return RuleSnapshot(**params)


async def _account_row(account_id: int):
    from app.db.models import TelegramAccount

    async with session_scope() as session:
        return await session.get(TelegramAccount, account_id)


# ─────────────────── PeerFlood в рассылке встаёт рубильником ───────────────────


async def test_peer_flood_stops_the_whole_account(create_user, create_account, no_pauses):
    """Первый PeerFlood — пауза всего аккаунта, а не ретрай через 30 секунд."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001, -1002], texts=["всем привет"]
    )
    client = FakeClient(error=PeerFloodError(request=None))
    manager._clients[account_id] = client

    await manager._mailing_tick()

    assert len(client.sent) == 0
    paused_until = manager.sending_paused_until(account_id)
    assert paused_until is not None
    assert paused_until > time.time() + (PEER_FLOOD_PAUSE_HOURS - 1) * 3600
    # Пауза переживает рестарт — она в БД, а не только в памяти.
    async with session_scope() as session:
        row = await session.get(AccountPause, account_id)
        assert row is not None and row.user_id == user_id
    # Человек видит причину и в журнале задачи, и в статусе аккаунта.
    journal = await logged(rule_id)
    assert len(journal) == 1 and "спам" in journal[0][1]
    account = await _account_row(account_id)
    assert account is not None and "спам" in (account.last_error or "")
    assert account.is_active is True, "пауза временная — аккаунт остаётся в работе"


async def test_paused_account_skips_ticks_quietly(create_user, create_account, no_pauses):
    """Пока рубильник стоит, тики молча пропускаются: ни отправок, ни дублей в журнале."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001], texts=["всем привет"]
    )
    manager._clients[account_id] = FakeClient(error=PeerFloodError(request=None))
    await manager._mailing_tick()
    assert await logged(rule_id) != []

    working = FakeClient()
    manager._clients[account_id] = working
    await manager._mailing_tick()
    await manager._mailing_tick()

    assert working.sent == [], "пауза стоит — слать нельзя, даже живому клиенту"
    assert len(await logged(rule_id)) == 1, "причина одна, а не на каждый тик"


async def test_sending_resumes_after_the_pause_expires(
    create_user, create_account, no_pauses
):
    """Пауза кончилась — рассылка продолжает с того же места, а не с начала."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001, -1002], texts=["всем привет"]
    )
    manager._clients[account_id] = FakeClient(error=PeerFloodError(request=None))
    await manager._mailing_tick()
    assert manager.sending_paused_until(account_id) is not None

    working = FakeClient()
    manager._clients[account_id] = working
    manager._send_pause_until[account_id] = time.time() - 1
    await manager._mailing_tick()

    assert [chat for chat, _ in working.sent] == [-1001]


async def test_manual_run_is_refused_while_paused(create_user, create_account):
    """Кнопкой «Запустить» спамблок не снять — только продлить, поэтому отказ."""
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="parser", source_id=-4242
    )
    manager._send_pause_until[account_id] = time.time() + 3600

    result = await run_now(rule_id, user_id)

    assert result["ok"] is False
    assert "паузе" in result["error"]
    journal = await logged(rule_id)
    assert len(journal) == 1 and "паузе" in journal[0][1]


async def test_pauses_survive_a_restart(create_user, create_account):
    """Загрузка при старте поднимает живые паузы и стирает протухшие."""
    user_id = await create_user()
    account_id = await create_account(user_id)
    other_id = await create_account(user_id)
    async with session_scope() as session:
        await repo.set_account_pause(
            session, account_id, user_id, repo.utcnow() + timedelta(hours=6)
        )
        await repo.set_account_pause(
            session, other_id, user_id, repo.utcnow() - timedelta(hours=1)
        )

    await manager._load_send_pauses()

    assert manager.sending_paused_until(account_id) is not None
    assert manager.sending_paused_until(other_id) is None
    async with session_scope() as session:
        assert await session.get(AccountPause, other_id) is None


# ─────────────────────────── медленный режим и очередь ──────────────────────────


async def test_slowmode_in_mailing_waits_like_floodwait(
    create_user, create_account, no_pauses
):
    """SlowMode — пауза на столько, сколько просят, а не ошибка и не ретрай."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001], texts=["всем привет"]
    )
    manager._clients[account_id] = FakeClient(
        error=SlowModeWaitError(None, capture=45)
    )

    before = time.time()
    await manager._mailing_tick()

    state = manager._mailing_state[rule_id]
    assert state["not_before"] >= before + 46
    assert await logged(rule_id) == [], "пауза — не сбой, в журнале ей нечего делать"


async def test_queue_retries_slowmode_but_never_peerflood():
    """Очередь: SlowMode ждёт и повторяет, PeerFlood сразу сдаётся без повторов."""

    async def flaky(client, message, rule):
        flaky.calls += 1
        if flaky.calls == 1:
            raise SlowModeWaitError(None, capture=0)
        return True

    flaky.calls = 0
    queue = DeliveryQueue(
        flaky, workers=1, maxsize=10, concurrency=1, min_interval=0,
        retry_attempts=2, retry_base=0.01, flood_wait_max=60,
    )
    await queue.start()
    try:
        queue.submit(None, None, _snapshot(1, 100, 1))
        for _ in range(200):
            if queue.stats()["sent"] == 1:
                break
            await asyncio.sleep(0.01)
        assert queue.stats()["sent"] == 1
        assert flaky.calls == 2, "SlowMode подождали и повторили"
    finally:
        await queue.stop(drain=False, timeout=1.0)

    async def spammer(client, message, rule):
        spammer.calls += 1
        raise PeerFloodError(request=None)

    spammer.calls = 0
    failed: list = []

    async def collect(client, message, rule, error):
        failed.append(error)

    queue = DeliveryQueue(
        spammer, on_error=collect, workers=1, maxsize=10, concurrency=1,
        min_interval=0, retry_attempts=2, retry_base=0.01, flood_wait_max=60,
    )
    await queue.start()
    try:
        queue.submit(None, None, _snapshot(2, 100, 1))
        for _ in range(200):
            if queue.stats()["failed"] == 1:
                break
            await asyncio.sleep(0.01)
        assert spammer.calls == 1, "PeerFlood не ретраится"
        assert len(failed) == 1 and isinstance(failed[0], PeerFloodError)
    finally:
        await queue.stop(drain=False, timeout=1.0)


# ─────────────────────────────── веер: темп и пауза ─────────────────────────────


class FanClient:
    """Клиент веера: пишет в чаты и считает отправки."""

    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append(chat_id)
        return SimpleNamespace(id=len(self.sent))


async def test_broadcast_waits_between_chats(create_user, create_account, monkeypatch):
    """Веер — не пулемёт: между чатами пауза, а не скорость сети."""
    rule = await _db_rule(create_user, create_account, kind="broadcast", with_trial=True)
    snapshot = _snapshot(
        rule.id, rule.user_id, rule.account_id, kind="broadcast",
        filters=FilterConfig(targets=[-2, -3], gap_jitter=0),
        target_id=-1,
    )
    monkeypatch.setattr(jobs, "BROADCAST_CHAT_GAP", 5.0)
    sleeps: list[float] = []

    async def recorder(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", recorder)
    client = FanClient()

    await jobs._broadcast(client, SimpleNamespace(id=7, message="пост", media=None), snapshot)

    assert client.sent == [-1, -2, -3]
    assert sleeps == [5.0, 5.0], "пауза перед каждым чатом, кроме первого"


async def test_broadcast_skips_while_paused(create_user, create_account):
    """Веер на паузе после спамблока: сообщение пропускается молча."""
    rule = await _db_rule(create_user, create_account, kind="broadcast", with_trial=True)
    snapshot = _snapshot(
        rule.id, rule.user_id, rule.account_id, kind="broadcast",
        filters=FilterConfig(targets=[-2]), target_id=-1,
    )
    manager._send_pause_until[rule.account_id] = time.time() + 3600
    client = FanClient()

    await jobs._broadcast(client, SimpleNamespace(id=7, message="пост", media=None), snapshot)

    assert client.sent == []
    assert await logged(rule.id) == []


# ─────────────────────────── мёртвая сессия гасит аккаунт ────────────────────────


async def test_dead_session_kills_the_account(create_user, create_account, no_pauses):
    """Отозванный ключ — не «ошибка отправки»: аккаунт гасится с причиной."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001], texts=["всем привет"]
    )
    manager._clients[account_id] = FakeClient(
        error=AuthKeyDuplicatedError(request=None)
    )

    await manager._mailing_tick()

    assert account_id not in manager._clients
    account = await _account_row(account_id)
    assert account is not None
    assert account.is_active is False
    assert account.last_error == SESSION_REVOKED
    journal = await logged(rule_id)
    assert len(journal) == 1 and "заново" in journal[0][1]


# ─────────────── автоподписка: темп в форме, потолок, дедуп ───────────────


async def test_autosubscribe_accepts_and_defaults_join_tempo(
    client, auth_headers, create_user, create_account, login_open, many_chats_resolved
):
    """Темп вступлений задаётся в форме; пустое — безопасные умолчания."""
    await create_user(id=TEST_USER_ID)
    account_id = await create_account(TEST_USER_ID)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "autosubscribe",
            "account_id": account_id,
            "targets": ["@ch-1", "@ch-2"],
            "join_gap": 45,
            "join_limit": 3,
            "daily_join_limit": 7,
        },
        headers=auth_headers,
    )
    assert response.status == 201, await response.text()
    edit = (await response.json())["task"]["edit"]
    assert (edit["join_gap"], edit["join_limit"], edit["daily_join_limit"]) == (45, 3, 7)

    response = await client.post(
        "/api/tasks",
        json={
            "command": "autosubscribe",
            "account_id": account_id,
            "targets": ["@ch-3"],
        },
        headers=auth_headers,
    )
    assert response.status == 201, await response.text()
    edit = (await response.json())["task"]["edit"]
    assert (edit["join_gap"], edit["join_limit"], edit["daily_join_limit"]) == (60, 10, 10)


async def test_join_gap_below_the_floor_is_lifted(
    client, auth_headers, create_user, create_account, login_open, many_chats_resolved
):
    """Паузу 5 секунд сервер не пропускает: пол жёсткий."""
    await create_user(id=TEST_USER_ID)
    account_id = await create_account(TEST_USER_ID)
    response = await client.post(
        "/api/tasks",
        json={
            "command": "autosubscribe",
            "account_id": account_id,
            "targets": ["@ch-1"],
            "join_gap": 5,
        },
        headers=auth_headers,
    )
    assert response.status == 201, await response.text()
    assert (await response.json())["task"]["edit"]["join_gap"] == 30


async def test_account_join_cap_cuts_across_tasks(create_user, create_account, monkeypatch):
    """Потолок аккаунта режет суммарный залп: у задачи лимита нет, у номера — есть."""
    monkeypatch.setattr(jobs, "JOIN_MIN_GAP", 0)
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="autosubscribe",
        subscribe_to=["@new-1", "@new-2"], daily_join_limit=0, join_gap=0,
    )
    # Аккаунт уже вступал сегодня — пусть даже этой же задачей, потолку
    # всё равно, чьи вступления считать: он считает вступления номера.
    async with session_scope() as session:
        for _ in range(ACCOUNT_DAILY_JOIN_CAP):
            await repo.log_join(session, rule_id, user_id)


    async with session_scope() as session:
        db_rule = await session.get(Rule, rule_id)
        snapshot = _snapshot(
            db_rule.id, user_id, account_id, kind="autosubscribe",
            filters=FilterConfig.from_dict(db_rule.filters),
        )
    client = OneShotClient()
    result = await jobs.run_autosubscribe(client, snapshot)

    assert result["limited"] is True
    assert any("аккаунта" in problem for problem in result["problems"])
    assert client.tried == [], "потолок исчерпан — ни одного вступления"
    assert result["joined"] == 0


async def test_already_joined_channels_are_not_rejoined(
    create_user, create_account, monkeypatch
):
    """Где уже сидим — туда не вступаем: повторный запуск не долбит JoinChannel."""
    monkeypatch.setattr(jobs, "JOIN_MIN_GAP", 0)
    rule_id, user_id, account_id = await make_oneshot(
        create_user, create_account, kind="autosubscribe",
        subscribe_to=["@a", "@b"], daily_join_limit=0, join_gap=0,
    )

    async def dialogs(account_id: int):
        return [{"id": -100, "username": "a", "title": "A"}]

    monkeypatch.setattr(manager, "list_dialogs", dialogs)


    async with session_scope() as session:
        db_rule = await session.get(Rule, rule_id)
        snapshot = _snapshot(
            db_rule.id, user_id, account_id, kind="autosubscribe",
            filters=FilterConfig.from_dict(db_rule.filters),
        )
    client = OneShotClient()
    result = await jobs.run_autosubscribe(client, snapshot)

    assert (result["joined"], result["already"]) == (1, 1)
    assert client.tried == ["@b"]


# ─────────────────── безопасный режим видно в боте ───────────────────


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))


@pytest.fixture(autouse=True)
def clean_alert_bot():
    yield
    set_alert_bot(None)


async def test_peer_flood_sends_one_safe_mode_letter(
    create_user, create_account, no_pauses
):
    """Рубильник пишет в личку сразу — и только один раз за паузу."""
    rule_id, user_id, account_id = await make_mailing(
        create_user, create_account, targets=[-1001], texts=["всем привет"]
    )
    bot = FakeBot()
    set_alert_bot(bot)
    manager._clients[account_id] = FakeClient(error=PeerFloodError(request=None))

    await manager._mailing_tick()
    await manager._mailing_tick()

    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == user_id
    assert "Безопасный режим" in text and "UTC" in text


def test_rule_card_names_the_pause():
    """Карточка задачи говорит «пауза после спамблока», а не «работает ✅»."""
    rule = Rule(
        id=1, kind="forward", mode="copy", enabled=True, archived=False,
        delay_seconds=0, forwarded_count=0, source_id=-100, target_id=-200,
    )
    card = texts.rule_card(rule, paused_until=time.time() + 3600)

    assert "пауза после спамблока ⏸" in card
    assert "UTC" in card and "вручную" in card


def test_rule_card_without_pause_has_no_pause_line():
    """Без паузы карточка прежняя — новых строк не появляется."""
    rule = Rule(
        id=1, kind="forward", mode="copy", enabled=True, archived=False,
        delay_seconds=0, forwarded_count=0, source_id=-100, target_id=-200,
    )
    card = texts.rule_card(rule)

    assert "спамблок" not in card and "спамблока" not in card


def test_accounts_menu_marks_paused():
    """В списке аккаунтов пауза видна значком, а не только внутри карточки."""
    account = TelegramAccount(
        id=7, user_id=1, phone="+700", session_encrypted="x", is_active=True
    )
    paused = kb.accounts_menu([account], paused={7})
    plain = kb.accounts_menu([account])

    assert paused.inline_keyboard[0][0].text.startswith("⏸")
    assert plain.inline_keyboard[0][0].text.startswith("🟢")
