"""Уникализация: текст меняется для поиска, а не для читателя."""
import pytest

from app.telegram_client.filters import FilterConfig, transform_text, uniquify_text
from app.telegram_client.manager import manager
from tests.helpers import TEST_USER_ID


_BACK = str.maketrans({
        "a": "а", "e": "е", "o": "о", "p": "р", "c": "с", "x": "х",
        "A": "А", "B": "В", "E": "Е", "K": "К", "M": "М", "H": "Н",
        "O": "О", "P": "Р", "C": "С", "T": "Т", "X": "Х",
    })


def _readable(text: str) -> str:
    """Возвращает двойников в кириллицу: что увидит глаз читателя."""
    return text.translate(_BACK)


def test_homoglyphs_keep_reading_but_change_bytes():
    out = uniquify_text("мама мыла раму")
    assert out != "мама мыла раму"
    # На глаз — то же самое: каждая подмена из таблицы двойников.
    assert _readable(out) == "мама мыла раму"


def test_synonyms_replace_case_aware():
    assert _readable(uniquify_text("Очень быстро.")) == "Весьма стремительно."


def test_synonyms_keep_word_forms_they_do_not_know():
    # «купили» — не инфинитив из таблицы: трогаем только буквы, смысл не калечим.
    assert _readable(uniquify_text("купили дом")) == "купили дом"


def test_deterministic():
    text = "Очень большой и красивый дом"
    assert uniquify_text(text) == uniquify_text(text)
    assert uniquify_text("") == ""


def test_transform_applies_uniquify_last():
    # Замены пользователя ложатся до подмены символов — иначе их паттерны,
    # набранные обычными буквами, не совпали бы.
    conf = FilterConfig(
        replace=[{"from": "старое", "to": "новое"}], uniquify=True
    )
    out = transform_text("старое слово", conf)
    assert _readable(out) == _readable(uniquify_text("новое слово"))


def test_transform_off_by_default():
    assert transform_text("мама", FilterConfig()) == "мама"


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


async def test_api_uniquify_roundtrip(
    client, auth_headers, create_account, login_open, chats_resolved
):
    await client.get("/api/me", headers=auth_headers)
    account_id = await create_account(TEST_USER_ID)
    payload = {
        "command": "copy_channel", "account_id": account_id,
        "source": "@src", "target": "@dst", "uniquify": True,
    }
    resp = await client.post("/api/tasks", json=payload, headers=auth_headers)
    assert resp.status == 201, await resp.text()
    task = (await resp.json())["task"]
    assert task["uniquify"] is True
    assert task["edit"]["uniquify"] is True
