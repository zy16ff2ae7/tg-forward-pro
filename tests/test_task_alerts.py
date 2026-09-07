"""Письма о больных задачах: третья подряд — в личку, дальше тишина."""
from types import SimpleNamespace

import pytest

from app.config import settings
from app.db import repo
from app.db.database import session_scope
from app.db.models import ForwardLog, Rule
from app.task_alerts import maybe_alert_problem, set_alert_bot
from app.telegram_client import jobs
from app.telegram_client.filters import FilterConfig
from app.telegram_client.forwarder import log_delivery_error
from app.telegram_client.types import RuleSnapshot
from tests.helpers import TEST_USER_ID
from tests.test_many_chats import (  # noqa: F401
    login_open,
    many_chats_resolved,
    one_shot_stubbed,
)


class FakeBot:
    def __init__(self, *, fail=False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id: int, text: str, **kwargs):
        if self.fail:
            raise RuntimeError("бота заблокировали")
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=len(self.sent))


@pytest.fixture(autouse=True)
def clean_alert_bot():
    yield
    set_alert_bot(None)


async def _snapshot(create_user, create_account, *, alerts=True):
    user_id = await create_user()
    account_id = await create_account(user_id)
    async with session_scope() as session:
        rule = Rule(
            user_id=user_id, account_id=account_id, source_id=-100, target_id=-200,
            kind="forward", mode="copy", enabled=True,
            source_title="src", target_title="dst",
            filters={"alerts": alerts},
        )
        session.add(rule)
        await session.flush()
        return RuleSnapshot(
            id=rule.id, user_id=user_id, target_id=-200, mode="copy",
            delay_seconds=0, kind="forward",
            filters=FilterConfig(alerts=alerts),
        )


def _message(mid=1):
    return SimpleNamespace(id=mid, message="пост", media=None)


async def test_third_error_alerts_once(create_user, create_account):
    snapshot = await _snapshot(create_user, create_account)
    bot = FakeBot()
    set_alert_bot(bot)
    await jobs.record_error(snapshot, _message(1), "чат недоступен")
    await jobs.record_error(snapshot, _message(2), "чат недоступен")
    assert bot.sent == []
    await jobs.record_error(snapshot, _message(3), "чат недоступен")
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == snapshot.user_id
    assert text.startswith("⚠️")
    assert "третья подряд" in text
    # Четвёртая и дальше — тишина: уже писали.
    await jobs.record_error(snapshot, _message(4), "чат недоступен")
    assert len(bot.sent) == 1


async def test_success_resets_streak(create_user, create_account):
    snapshot = await _snapshot(create_user, create_account)
    bot = FakeBot()
    set_alert_bot(bot)
    await jobs.record_error(snapshot, _message(1), "сбой")
    await jobs.record_error(snapshot, _message(2), "сбой")
    await jobs.record_ok(snapshot, _message(3))
    await jobs.record_error(snapshot, _message(4), "сбой")
    await jobs.record_error(snapshot, _message(5), "сбой")
    assert bot.sent == []
    await jobs.record_error(snapshot, _message(6), "сбой")
    assert len(bot.sent) == 1


async def test_muted_task_stays_silent(create_user, create_account):
    snapshot = await _snapshot(create_user, create_account, alerts=False)
    bot = FakeBot()
    set_alert_bot(bot)
    for mid in range(1, 5):
        await jobs.record_error(snapshot, _message(mid), "сбой")
    assert bot.sent == []


async def test_no_bot_no_crash(create_user, create_account):
    snapshot = await _snapshot(create_user, create_account)
    for mid in range(1, 5):
        await jobs.record_error(snapshot, _message(mid), "сбой")


async def test_blocked_bot_does_not_break_journal(create_user, create_account):
    snapshot = await _snapshot(create_user, create_account)
    set_alert_bot(FakeBot(fail=True))
    for mid in range(1, 5):
        await jobs.record_error(snapshot, _message(mid), "сбой")


async def test_delivery_errors_alert_too(create_user, create_account):
    snapshot = await _snapshot(create_user, create_account)
    bot = FakeBot()
    set_alert_bot(bot)
    for mid in range(1, 4):
        await log_delivery_error(None, _message(mid), snapshot, RuntimeError("упало"))
    assert len(bot.sent) == 1
    assert "RuntimeError" in bot.sent[0][1]


async def test_partial_batch_does_not_alert(create_user, create_account):
    snapshot = await _snapshot(create_user, create_account)
    bot = FakeBot()
    set_alert_bot(bot)
    for _ in range(3):
        await jobs.record_batch(snapshot, sent=5, failed=["чат -1: недоступен"])
    assert bot.sent == []


async def test_total_batch_failures_alert(create_user, create_account):
    snapshot = await _snapshot(create_user, create_account)
    bot = FakeBot()
    set_alert_bot(bot)
    for _ in range(3):
        await jobs.record_batch(snapshot, failed=["чат -1: недоступен"])
    assert len(bot.sent) == 1


async def test_api_alerts_toggle_roundtrip(
    client, auth_headers, create_account, login_open, many_chats_resolved
):
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    payload = {
        "command": "copy_channel", "account_id": account_id,
        "source": "@ch-1", "target": "@ch-2", "alerts": False,
    }
    resp = await client.post("/api/tasks", json=payload, headers=auth_headers)
    assert resp.status == 201, await resp.text()
    task = (await resp.json())["task"]
    assert task["alerts"] is False
    assert task["edit"]["alerts"] is False
    # По умолчанию — включены.
    resp = await client.post(
        "/api/tasks",
        json={k: v for k, v in payload.items() if k != "alerts"},
        headers=auth_headers,
    )
    assert resp.status == 201, await resp.text()
    assert (await resp.json())["task"]["alerts"] is True


async def test_alert_carries_cabinet_button(monkeypatch, create_user, create_account):
    """Письмо о больной задаче несёт кнопку кабинета — чинить в один тап."""
    from tests.helpers import add_rule

    monkeypatch.setattr(
        type(settings), "mini_app_url",
        property(lambda self: "https://cabinet.test/app/"),
    )
    user_id = await create_user()
    account_id = await create_account(user_id)
    await add_rule(user_id, account_id)
    async with session_scope() as session:
        rule = (await repo.list_rules(session, user_id))[0]
        for _ in range(3):
            session.add(
                ForwardLog(
                    rule_id=rule.id, user_id=user_id, source_msg_id=1,
                    status="error", error="чат снесён",
                )
            )
        await session.commit()

    sent = {}

    class ButtonBot:
        async def send_message(self, chat_id, text, **kwargs):
            sent.update(kwargs)

    set_alert_bot(ButtonBot())
    await maybe_alert_problem(rule, "чат снесён")
    markup = sent.get("reply_markup")
    assert markup is not None
    assert markup.inline_keyboard[0][0].text == "🖥 Открыть кабинет"
