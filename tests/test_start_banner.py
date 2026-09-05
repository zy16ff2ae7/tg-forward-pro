"""Вход в бота: /start доходит, даже когда приветствие длиннее подписи к фото.

Подпись к картинке Telegram обрезать не станет: длиннее 1024 символов — и он
отказывает во всём сообщении, «message caption is too long». Приветствие росло
вместе со списком умений и переросло предел; /start отправлял баннер с подписью
одним куском — и человек в ответ не получал ничего. Ни картинки, ни меню, ни
объяснения: первое, что он видел от бота, было молчание. В журнале боевого
сервиса такой отказ случался по несколько раз в день.

Здесь проверяем, что вход держится в любом случае:

* длинное приветствие приходит целиком — картинкой и текстом раздельно;
* меню лежит на том сообщении, которое потом правит smart_edit, — на тексте;
* короткое приветствие остаётся подписью под баннером, как и было;
* нет картинки — остаётся текст, а не пустой ответ;
* /start не отправляет подписи длиннее предела ни при какой настройке.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.bot import texts
from app.bot.handlers.menu import cmd_start
from app.bot.media import WELCOME_PHOTO
from app.bot.utils import CAPTION_LIMIT, answer_with_banner
from app.config import settings

SHORT = "Привет! 👋 Я — ДОЧА."
LONG = "Привет! 👋 " + "Я умею много всякого. " * 60  # заведомо длиннее предела


class FakeMessage:
    """Сообщение в объёме /start: автор, текст и два способа ответить."""

    def __init__(self, text: str = "/start", user_id: int = 555_123_456) -> None:
        self.text = text
        self.from_user = SimpleNamespace(
            id=user_id, username="doch", full_name="Иван Петров", is_bot=False
        )
        # ensure_user отличает сообщение от нажатия кнопки по isinstance и у
        # второго берёт .message — подделке проще подставить себя же.
        self.message = self
        self.sent: list[dict] = []

    async def answer(self, text, reply_markup=None, **kwargs):
        self.sent.append({"kind": "text", "text": text, "markup": reply_markup})
        return self

    async def answer_photo(self, photo, caption=None, reply_markup=None, **kwargs):
        self.sent.append(
            {"kind": "photo", "photo": photo, "caption": caption, "markup": reply_markup}
        )
        return self


# ───────────────────────── баннер и текст по отдельности ──────────────────────


async def test_a_long_greeting_arrives_in_two_messages():
    """Длинный текст не влезает в подпись — уходит следом, целиком."""
    message = FakeMessage()

    await answer_with_banner(message, WELCOME_PHOTO, LONG, reply_markup="меню")

    kinds = [item["kind"] for item in message.sent]
    assert kinds == ["photo", "text"], "сначала картинка, потом текст"
    assert message.sent[0]["caption"] is None, "подписи нет — она и не прошла бы"
    assert message.sent[1]["text"] == LONG, "текст дошёл целиком, без обрезки"


async def test_the_menu_lies_on_the_message_that_gets_edited():
    """Кнопки — на тексте: именно его правит smart_edit при переходах по меню."""
    message = FakeMessage()

    await answer_with_banner(message, WELCOME_PHOTO, LONG, reply_markup="меню")

    assert message.sent[0]["markup"] is None
    assert message.sent[1]["markup"] == "меню"


async def test_a_short_greeting_stays_a_caption():
    """Пока текст влезает — всё как было: одна картинка с подписью и кнопками."""
    message = FakeMessage()

    await answer_with_banner(message, WELCOME_PHOTO, SHORT, reply_markup="меню")

    assert len(message.sent) == 1
    assert message.sent[0]["caption"] == SHORT
    assert message.sent[0]["markup"] == "меню"


async def test_the_limit_itself_is_still_a_caption():
    """Ровно предел — подпись: лишнего сообщения на границе не появляется."""
    message = FakeMessage()

    await answer_with_banner(message, WELCOME_PHOTO, "я" * CAPTION_LIMIT)

    assert [item["kind"] for item in message.sent] == ["photo"]


async def test_no_picture_still_means_an_answer(tmp_path: Path):
    """Картинки на диске нет — человек получает текст, а не пустоту."""
    message = FakeMessage()

    await answer_with_banner(message, tmp_path / "нет-такого.png", LONG, reply_markup="меню")

    assert [item["kind"] for item in message.sent] == ["text"]
    assert message.sent[0]["text"] == LONG


# ────────────────────────────── сам вход в бота ───────────────────────────────


@pytest.fixture
def bonus_on(monkeypatch):
    """Боевая настройка: подарок за подписку включён — приписка удлиняет текст."""
    monkeypatch.setattr(settings, "bonus_channel", "@papin4_do4a")
    monkeypatch.setattr(settings, "bonus_days", 3)


@pytest.mark.parametrize("with_bonus", [True, False])
async def test_start_sends_the_whole_greeting(monkeypatch, with_bonus):
    """/start доносит приветствие целиком и не шлёт подписи длиннее предела."""
    if with_bonus:
        monkeypatch.setattr(settings, "bonus_channel", "@papin4_do4a")
        monkeypatch.setattr(settings, "bonus_days", 3)
    else:
        monkeypatch.setattr(settings, "bonus_channel", None)
    message = FakeMessage()

    await cmd_start(message, state=None)

    assert message.sent, "на /start человек должен получить ответ"
    captions = [item["caption"] or "" for item in message.sent if item["kind"] == "photo"]
    assert all(len(text) <= CAPTION_LIMIT for text in captions), (
        "подпись длиннее предела Telegram не отправит вовсе"
    )
    whole = "\n".join(
        item.get("text") or item.get("caption") or "" for item in message.sent
    )
    assert texts.welcome("Иван Петров") in whole


async def test_start_survives_the_greeting_it_has_now(bonus_on):
    """Приветствие сегодняшнего дня действительно длиннее подписи.

    Проверка сторожит саму причину поломки: пока текст такой, /start обязан
    уходить двумя сообщениями. Если приветствие однажды укоротят до предела,
    этот тест упадёт — и станет ясно, что ветку с подписью проверяет уже он.
    """
    greeting = texts.welcome("Иван Петров")
    assert len(greeting) > CAPTION_LIMIT, f"длина {len(greeting)}"
    message = FakeMessage()

    await cmd_start(message, state=None)

    assert [item["kind"] for item in message.sent] == ["photo", "text"]
    assert message.sent[1]["text"] == greeting
