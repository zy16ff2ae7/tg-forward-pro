"""Кнопки-ссылки под постами: разбор, прикрепление, тишина форварда."""
from types import SimpleNamespace

import pytest

from app.errors import ValidationError
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID
from app.telegram_client.filters import FilterConfig, normalize_buttons
from app.telegram_client.forwarder import send_copy, _send_once
from app.telegram_client.types import RuleSnapshot


class BtnClient:
    """Клиент, который помнит всё, что ему передали, включая кнопки."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.calls.append({"chat_id": chat_id, "text": text, **kwargs})
        return SimpleNamespace(id=len(self.calls))

    async def send_file(self, chat_id: int, file, **kwargs):
        self.calls.append({"chat_id": chat_id, "file": file, **kwargs})
        return SimpleNamespace(id=len(self.calls))

    async def download_media(self, message, file=None):
        with open(file, "wb") as handle:
            handle.write(b"x")
        return file

    async def forward_messages(self, chat_id: int, message):
        self.calls.append({"chat_id": chat_id, "forwarded": True})
        return SimpleNamespace(id=len(self.calls))


def _text_message(text: str = "пост"):
    return SimpleNamespace(media=None, message=text, text=text)


def test_parse_lines_and_dicts():
    buttons = normalize_buttons([
        "Подписаться | t.me/durov",
        {"text": "Купить", "url": "https://x.io/buy"},
    ])
    assert buttons == [
        {"text": "Подписаться", "url": "https://t.me/durov"},
        {"text": "Купить", "url": "https://x.io/buy"},
    ]
    assert normalize_buttons(None) == []
    assert normalize_buttons([]) == []


@pytest.mark.parametrize(
    "raw",
    [
        ["только текст без черты"],
        [{"text": "", "url": "https://x.io"}],
        [{"text": "x" * 65, "url": "https://x.io"}],
        [{"text": "кнопка", "url": "ftp://x.io"}],
        [{"text": "кнопка", "url": "просто слова"}],
        [{"text": f"к{i}", "url": "https://x.io"} for i in range(7)],
    ],
)
def test_parse_garbage_rejected_with_reason(raw):
    with pytest.raises(ValidationError):
        normalize_buttons(raw)


async def test_copy_text_gets_buttons():
    client = BtnClient()
    await send_copy(
        client, -1, _text_message(),
        "пост", buttons=[{"text": "Купить", "url": "https://x.io/buy"}],
    )
    rows = client.calls[0]["buttons"]
    assert len(rows) == 1 and len(rows[0]) == 1
    assert rows[0][0].text == "Купить"
    assert rows[0][0].url == "https://x.io/buy"


async def test_copy_without_buttons_sends_plain():
    client = BtnClient()
    await send_copy(client, -1, _text_message(), "пост", buttons=[])
    assert client.calls[0]["buttons"] is None


async def test_copy_media_gets_buttons_in_caption():
    client = BtnClient()
    media = SimpleNamespace(size=10, document=None)
    message = SimpleNamespace(media=media)
    await send_copy(
        client, -1, message, "подпись",
        buttons=[{"text": "Купить", "url": "https://x.io"}],
    )
    assert client.calls[0]["buttons"][0][0].url == "https://x.io"


async def test_oversize_media_forwards_without_buttons():
    client = BtnClient()
    media = SimpleNamespace(size=60 * 1024 * 1024, document=None)
    await send_copy(
        client, -1, SimpleNamespace(media=media), "пост",
        buttons=[{"text": "Купить", "url": "https://x.io"}],
    )
    assert client.calls[0].get("forwarded") is True
    assert "buttons" not in client.calls[0]


async def test_rule_buttons_flow_into_copy():
    client = BtnClient()
    rule = RuleSnapshot(
        id=1, user_id=1, target_id=-1, mode="copy", delay_seconds=0,
        filters=FilterConfig(buttons=[{"text": "В канал", "url": "https://t.me/x"}]),
    )
    await _send_once(client, rule, _text_message(), "пост")
    assert client.calls[0]["buttons"][0][0].text == "В канал"


async def test_forward_mode_ignores_buttons():
    client = BtnClient()
    rule = RuleSnapshot(
        id=1, user_id=1, target_id=-1, mode="forward", delay_seconds=0,
        filters=FilterConfig(buttons=[{"text": "В канал", "url": "https://t.me/x"}]),
    )
    await _send_once(client, rule, _text_message(), "пост")
    assert client.calls[0].get("forwarded") is True


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


async def test_api_buttons_roundtrip(
    client, auth_headers, create_account, login_open, chats_resolved
):
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    payload = {
        "command": "copy_channel", "account_id": account_id,
        "source": "@src", "target": "@dst",
        "buttons": ["Подписаться | https://t.me/x"],
    }
    resp = await client.post("/api/tasks", json=payload, headers=auth_headers)
    assert resp.status == 201, await resp.text()
    body = await resp.json()
    task = body["task"]
    assert task["buttons_count"] == 1
    assert task["edit"]["buttons"] == [{"text": "Подписаться", "url": "https://t.me/x"}]
    # Мусор отклоняется с понятной причиной, а не молча.
    bad = dict(payload, buttons=["без ссылки"])
    resp = await client.post("/api/tasks", json=bad, headers=auth_headers)
    assert resp.status == 400
    assert "Кнопка" in (await resp.json())["error"]
