"""Автоперевод чужих постов: код языка, перевод, запасной выход."""
from types import SimpleNamespace

import pytest

from app.db.database import session_scope
from app.db.models import Rule
from app.errors import ValidationError
from app.telegram_client.filters import FilterConfig
from app.telegram_client import forwarder
from app.telegram_client.jobs import _broadcast
from app.telegram_client.types import RuleSnapshot
from app.telegram_client.manager import manager
from app.translate import maybe_translate, normalize_lang, translate_text
from tests.helpers import TEST_USER_ID


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(id=len(self.calls))

    async def forward_messages(self, chat_id: int, message):
        self.calls.append({"chat_id": chat_id, "forwarded": True})
        return SimpleNamespace(id=len(self.calls))


def _message(text: str = "Hello world"):
    return SimpleNamespace(id=10, message=text, media=None)


@pytest.mark.parametrize(
    "raw, expected",
    [("ru", "ru"), (" RU ", "ru"), ("zh-cn", "zh-CN"), ("", ""), (None, "")],
)
def test_lang_normalizes(raw, expected):
    assert normalize_lang(raw) == expected


@pytest.mark.parametrize("raw", ["русский", "r", "eng", "ru-RU-x", "12"])
def test_lang_garbage_rejected(raw):
    with pytest.raises(ValidationError):
        normalize_lang(raw)


async def test_maybe_translate_passthrough():
    assert await maybe_translate("", "ru") == ""
    assert await maybe_translate("Hello", "") == "Hello"


async def test_maybe_translate_falls_back_to_original(monkeypatch):
    async def boom(text, target, **kwargs):
        raise RuntimeError("сеть легла")

    monkeypatch.setattr("app.translate.translate_text", boom)
    assert await maybe_translate("Hello", "ru") == "Hello"


async def test_translate_text_parses_provider(monkeypatch):
    seen: dict = {}

    class FakeResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        async def json(self):
            return [[["Привет, мир", "Hello world", None, None]], None, "en"]

    class FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, params=None, data=None):
            seen.update(params or {})
            seen["q"] = (data or {}).get("q")
            return FakeResponse()

    monkeypatch.setattr("app.translate.aiohttp.ClientSession", FakeSession)
    assert await translate_text("Hello world", "ru") == "Привет, мир"
    assert seen["tl"] == "ru" and seen["q"] == "Hello world"


async def _broadcast_rule(user_id: int, account_id: int, filters: dict) -> RuleSnapshot:
    async with session_scope() as session:
        rule = Rule(
            user_id=user_id, account_id=account_id, source_id=-100, target_id=-200,
            kind="broadcast", mode="copy", enabled=True, filters=filters,
        )
        session.add(rule)
        await session.flush()
        return RuleSnapshot(
            id=rule.id, user_id=user_id, target_id=-200, mode="copy",
            delay_seconds=0, kind="broadcast",
            filters=FilterConfig.from_dict(dict(rule.filters or {})),
        )


async def test_broadcast_translates_before_send(create_user, create_account, monkeypatch):
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule = await _broadcast_rule(user_id, account_id, {"targets": [-201], "translate_to": "ru"})

    async def fake_translate(text, target, **kwargs):
        assert target == "ru"
        return "перевод: " + text

    monkeypatch.setattr("app.telegram_client.jobs.maybe_translate", fake_translate)
    client = FakeClient()
    await _broadcast(client, _message(), rule)
    assert [call["text"] for call in client.calls] == [
        "перевод: Hello world", "перевод: Hello world",
    ]


async def test_broadcast_without_lang_sends_original(create_user, create_account, monkeypatch):
    user_id = await create_user()
    account_id = await create_account(user_id)
    rule = await _broadcast_rule(user_id, account_id, {"targets": []})

    async def must_not_run(text, target, **kwargs):
        raise AssertionError("перевод вызван без языка")

    monkeypatch.setattr("app.telegram_client.jobs.maybe_translate", must_not_run)
    client = FakeClient()
    await _broadcast(client, _message(), rule)
    assert client.calls[0]["text"] == "Hello world"


async def test_copy_translates_and_forward_skips(create_user, create_account, monkeypatch):
    user_id = await create_user()
    account_id = await create_account(user_id)
    calls: list[str] = []

    async def fake_translate(text, target, **kwargs):
        calls.append(target)
        return "перевод: " + text

    monkeypatch.setattr("app.translate.translate_text", fake_translate)
    async def subscribed(uid):
        return True

    monkeypatch.setattr(forwarder, "subscription_active", subscribed)
    async with session_scope() as session:
        copy_rule = Rule(
            user_id=user_id, account_id=account_id, source_id=-100, target_id=-200,
            kind="forward", mode="copy", enabled=True,
            filters={"translate_to": "ru"},
        )
        fwd_rule = Rule(
            user_id=user_id, account_id=account_id, source_id=-100, target_id=-200,
            kind="forward", mode="forward", enabled=True,
            filters={"translate_to": "ru"},
        )
        session.add(copy_rule)
        session.add(fwd_rule)
        await session.flush()
        copy_id, fwd_id = copy_rule.id, fwd_rule.id

    client = FakeClient()
    await forwarder.deliver(
        client, _message(),
        RuleSnapshot(
            id=copy_id, user_id=user_id, target_id=-200, mode="copy",
            delay_seconds=0, filters=FilterConfig(translate_to="ru"),
        ),
    )
    assert client.calls[0]["text"] == "перевод: Hello world"
    assert calls == ["ru"]

    # Форвард несёт исходное сообщение целиком: переводить там нечего и незачем.
    await forwarder.deliver(
        client, _message(),
        RuleSnapshot(
            id=fwd_id, user_id=user_id, target_id=-200, mode="forward",
            delay_seconds=0, filters=FilterConfig(translate_to="ru"),
        ),
    )
    assert client.calls[-1].get("forwarded") is True
    assert calls == ["ru"]


@pytest.fixture
def login_open(monkeypatch):
    """Вход аккаунтов в тесте включён: без него создание задачи — 503."""
    from app import accounts_login

    monkeypatch.setattr(accounts_login, "require_enabled", lambda: None)


@pytest.fixture
def chats_resolved(monkeypatch):
    """Кабинет «находит» любые ссылки: каждой — свой id."""
    async def fake_resolve_many(account_id: int, queries):
        refs = [str(raw or "").strip() for raw in queries]
        return {ref: (-1000 - pos, ref) for pos, ref in enumerate(refs) if ref}

    monkeypatch.setattr(manager, "resolve_many", fake_resolve_many)


async def test_api_lang_roundtrip(
    client, auth_headers, create_account, login_open, chats_resolved
):
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    payload = {
        "command": "copy_channel", "account_id": account_id,
        "source": "@src", "target": "@dst", "translate_to": "ru",
    }
    resp = await client.post("/api/tasks", json=payload, headers=auth_headers)
    assert resp.status == 201, await resp.text()
    task = (await resp.json())["task"]
    assert task["translate_to"] == "ru"
    assert task["edit"]["translate_to"] == "ru"
    bad = dict(payload, translate_to="русский")
    resp = await client.post("/api/tasks", json=bad, headers=auth_headers)
    assert resp.status == 400
    assert "язык" in (await resp.json())["error"].lower()
