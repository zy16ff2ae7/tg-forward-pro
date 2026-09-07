"""Упоминание всех: рассылка дёргает участников чата невидимками.

Каждое упоминание — символ-невидимка со ссылкой на пользователя: в чате
чисто, а уведомления уходят. Состав не прочитался — сообщение уходит без
упоминаний, задача не краснеет.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.telegram_client import jobs
from app.telegram_client.filters import FilterConfig
from app.telegram_client.types import RuleSnapshot


class MentionClient:
    """Клиент с составом чата: помнит тексты, сущности и файлы."""

    def __init__(self, users=None, *, participants_error=None) -> None:
        self.users = list(users or [])
        self.participants_error = participants_error
        self.participants_calls: list[int] = []
        self.texts: list[dict] = []
        self.files: list[dict] = []

    async def get_participants(self, chat_id: int, **kwargs):
        self.participants_calls.append(chat_id)
        if self.participants_error is not None:
            raise self.participants_error
        return list(self.users)

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.texts.append(
            {"chat": chat_id, "text": text, "entities": kwargs.get("formatting_entities")}
        )
        return SimpleNamespace(id=20)

    async def send_file(self, chat_id: int, file, **kwargs):
        self.files.append(
            {"chat": chat_id, "entities": kwargs.get("formatting_entities")}
        )
        return SimpleNamespace(id=21)

    async def download_media(self, message, file=None, **kwargs):
        with open(file, "wb") as fh:
            fh.write(b"x")

    async def get_messages(self, chat_id: int, ids=None, **kwargs):
        return SimpleNamespace(id=ids, message="пост из ссылки", media=None)


def _user(uid: int | None, *, bot: bool = False):
    return SimpleNamespace(id=uid, bot=bot)


def _rule(**filters) -> RuleSnapshot:
    return RuleSnapshot(
        id=1, user_id=100, target_id=-200, mode="copy", delay_seconds=0,
        account_id=1, kind="mailing", filters=FilterConfig(**filters),
    )


async def test_mentions_are_invisible_links():
    """Участники уходят невидимками со ссылками — текст чист, сущности на месте."""
    client = MentionClient([_user(11), _user(22)])

    await jobs.mailing_send(client, _rule(mention_all=True), jobs.own_text_item("привет"), -200)

    assert len(client.texts) == 1
    sent = client.texts[0]
    assert sent["text"] == "привет\n" + "\u2060\u2060"
    urls = [entity.url for entity in sent["entities"]]
    assert urls == ["tg://user?id=11", "tg://user?id=22"]
    offsets = [entity.offset for entity in sent["entities"]]
    assert offsets == [len("привет") + 1, len("привет") + 2]
    assert [entity.length for entity in sent["entities"]] == [1, 1]


async def test_bots_and_nameless_are_skipped():
    """Боты и записи без id не упоминаются."""
    client = MentionClient([_user(11), _user(22, bot=True), _user(None)])

    await jobs.mailing_send(client, _rule(mention_all=True), jobs.own_text_item("привет"), -200)

    assert [entity.url for entity in client.texts[0]["entities"]] == ["tg://user?id=11"]


async def test_flag_off_reads_nothing():
    """Галочки нет — состав не читаем, сущности пустые."""
    client = MentionClient([_user(11)])

    await jobs.mailing_send(client, _rule(), jobs.own_text_item("привет"), -200)

    assert client.participants_calls == []
    assert client.texts[0]["text"] == "привет"
    assert client.texts[0]["entities"] is None


async def test_unreadable_roster_sends_plain():
    """Состав не прочитался (нет прав) — сообщение уходит без упоминаний."""
    client = MentionClient(participants_error=RuntimeError("CHAT_ADMIN_REQUIRED"))

    sent_id = await jobs.mailing_send(
        client, _rule(mention_all=True), jobs.own_text_item("привет"), -200
    )

    assert sent_id == 20
    assert client.texts[0]["text"] == "привет"
    assert client.texts[0]["entities"] is None


async def test_linked_message_mentions_too():
    """Пост-ссылка с медиа-путём тоже несёт упоминания."""
    client = MentionClient([_user(11)])
    item = SimpleNamespace(chat_id=-300, message_id=5, text="")

    sent_id = await jobs.mailing_send(client, _rule(mention_all=True), item, -200)

    assert sent_id == 20
    assert [entity.url for entity in client.texts[0]["entities"]] == ["tg://user?id=11"]
