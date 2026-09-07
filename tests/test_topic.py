"""Приёмник-топик: копия уходит в тему форума, а не в корень чата.

Тему умеют только отправки «как своё» (send_message/send_file через
comment_to): у форварда тем нет на уровне Telegram API, поэтому режим
«форвард» с топиком шлёт в корень — а форма такой задачи и вовсе не даёт
сохранить (см. тесты API).
"""
from __future__ import annotations

from types import SimpleNamespace

from app.telegram_client import forwarder, jobs
from app.telegram_client.filters import FilterConfig
from app.telegram_client.types import SENT, RuleSnapshot
from tests.test_pin import _forward_rule


class TopicClient:
    """Клиент, записывающий, в какую тему что ушло."""

    def __init__(self) -> None:
        self.texts: list[dict] = []
        self.files: list[dict] = []
        self.forwards: list[int] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.texts.append({"chat": chat_id, "comment_to": kwargs.get("comment_to")})
        return SimpleNamespace(id=10)

    async def send_file(self, chat_id: int, file, **kwargs):
        self.files.append({"chat": chat_id, "comment_to": kwargs.get("comment_to")})
        return SimpleNamespace(id=11)

    async def download_media(self, message, file=None, **kwargs):
        with open(file, "wb") as fh:
            fh.write(b"x")

    async def forward_messages(self, chat_id: int, message, **kwargs):
        self.forwards.append(chat_id)
        return SimpleNamespace(id=12)


def _snapshot(topic_id: int, mode: str = "copy") -> RuleSnapshot:
    return RuleSnapshot(
        id=1, user_id=100, target_id=-200, mode=mode, delay_seconds=0,
        account_id=1, kind="forward",
        filters=FilterConfig(topic_id=topic_id),
    )


async def test_copy_text_goes_to_topic():
    """Текстовая копия уходит в тему, а не в корень."""
    client = TopicClient()
    message = SimpleNamespace(id=1, message="привет", media=None)

    await forwarder.send_copy(client, -200, message, "привет", topic_id=5)

    assert client.texts == [{"chat": -200, "comment_to": 5}]


async def test_no_topic_means_root():
    """Без топика — как раньше: comment_to пустой."""
    client = TopicClient()
    message = SimpleNamespace(id=1, message="привет", media=None)

    await forwarder.send_copy(client, -200, message, "привет")

    assert client.texts == [{"chat": -200, "comment_to": None}]


async def test_copy_media_goes_to_topic():
    """Медиа-копия уходит в тему тем же путём."""
    client = TopicClient()
    message = SimpleNamespace(
        id=2, message="", media=SimpleNamespace(size=100, document=None)
    )

    await forwarder.send_copy(client, -200, message, "", topic_id=7)

    assert client.files == [{"chat": -200, "comment_to": 7}]


async def test_forward_mode_ignores_topic_but_delivers(create_user, create_account, monkeypatch):
    """Форвард тем не умеет: пост уходит в корень, а не теряется."""
    async def subscribed(uid: int) -> bool:
        return True

    monkeypatch.setattr(forwarder, "subscription_active", subscribed)
    user_id = await create_user()
    rule = await _forward_rule(
        user_id, await create_account(user_id), {"topic_id": 5}
    )
    rule.mode = "forward"
    client = TopicClient()

    result = await forwarder.deliver(
        client, SimpleNamespace(id=3, message="пост", media=None), rule
    )

    assert result is SENT
    assert client.forwards == [-200]


async def test_mailing_text_goes_to_topic():
    """Рассылка текстом — в тему приёмника."""
    client = TopicClient()
    rule = RuleSnapshot(
        id=1, user_id=100, target_id=-200, mode="copy", delay_seconds=0,
        account_id=1, kind="mailing", filters=FilterConfig(topic_id=9),
    )

    sent_id = await jobs.mailing_send(client, rule, jobs.own_text_item("привет"), -200)

    assert sent_id == 10
    assert client.texts == [{"chat": -200, "comment_to": 9}]


async def test_forward_delivers_copy_to_topic(create_user, create_account, monkeypatch):
    """Пересылка-копия с топиком: доставили в тему и засчитали."""
    async def subscribed(uid: int) -> bool:
        return True

    monkeypatch.setattr(forwarder, "subscription_active", subscribed)
    user_id = await create_user()
    rule = await _forward_rule(
        user_id, await create_account(user_id), {"topic_id": 5}
    )
    client = TopicClient()

    result = await forwarder.deliver(
        client, SimpleNamespace(id=4, message="пост", media=None), rule
    )

    assert result is SENT
    assert client.texts == [{"chat": -200, "comment_to": 5}]
