"""Фильтры и преобразования текста — то, что решает судьбу каждого сообщения."""
from __future__ import annotations

from types import SimpleNamespace

from app.telegram_client.filters import (
    FilterConfig,
    media_kind,
    parse_words,
    should_forward,
    transform_text,
)


def msg(text: str = "", media: object | None = None, forward: object | None = None):
    return SimpleNamespace(message=text, text=text, media=media, forward=forward)


def cfg(**kwargs) -> FilterConfig:
    return FilterConfig.from_dict(kwargs)


# ───────────────────────────────── фильтры ────────────────────────────────────


def test_empty_filters_pass_everything():
    assert should_forward(msg("любой текст"), cfg()) is True


def test_blacklist_drops_message_case_insensitive():
    config = cfg(blacklist=["РЕКЛАМА"])
    assert should_forward(msg("здесь есть реклама"), config) is False
    assert should_forward(msg("здесь её нет"), config) is True


def test_whitelist_requires_at_least_one_word():
    config = cfg(whitelist=["срочно", "важно"])
    assert should_forward(msg("важно: собрание"), config) is True
    assert should_forward(msg("обычный пост"), config) is False


def test_empty_whitelist_entry_is_ignored():
    """Пустая строка в списке — частая опечатка; она не должна резать всё подряд."""
    assert should_forward(msg("что угодно"), cfg(whitelist=["", "  "])) is True


def test_media_types_filter_by_kind():
    assert media_kind(msg("текст")) == "text"
    assert media_kind(msg("", media=SimpleNamespace(photo=object()))) == "photo"

    only_text = cfg(media_types=["text"])
    assert should_forward(msg("привет"), only_text) is True
    assert should_forward(msg("", media=SimpleNamespace(photo=object())), only_text) is False


def test_min_length():
    config = cfg(min_length=10)
    assert should_forward(msg("коротко"), config) is False
    assert should_forward(msg("вполне длинный текст"), config) is True


def test_skip_forwards():
    post = msg("пересланное", forward=SimpleNamespace())
    assert should_forward(post, cfg(skip_forwards=True)) is False
    assert should_forward(post, cfg(skip_forwards=False)) is True


# ───────────────────────────── преобразования ─────────────────────────────────


def test_replace_pairs_applied_in_order():
    config = cfg(replace=[{"from": "котик", "to": "кот"}, {"from": "кот", "to": "котяра"}])
    assert transform_text("котик пришёл", config) == "котяра пришёл"


def test_replace_ignores_empty_source():
    """Пустой from дал бы бесконечную замену — такой ключ пропускаем."""
    config = cfg(replace=[{"from": "", "to": "что-то"}])
    assert transform_text("текст", config) == "текст"


def test_remove_links_and_mentions():
    config = cfg(remove_links=True, remove_mentions=True)
    assert transform_text("заходи https://t.me/chan и пиши @someuser", config) == "заходи  и пиши"


def test_append_text_goes_to_new_line():
    assert transform_text("пост", cfg(append_text="подпись")) == "пост\nподпись"


def test_extra_line_breaks_collapsed():
    config = cfg(remove_links=True)
    assert "\n\n\n" not in transform_text("a\n\n\nhttps://t.me/x\n\n\nb", config)


def test_parse_words_splits_on_commas_newlines_and_semicolons():
    assert parse_words("один, два\nтри;  , ") == ["один", "два", "три"]


def test_filter_config_ignores_unknown_keys():
    """Старые правила хранят ключи, которых уже нет в модели — они не должны ломать загрузку."""
    config = FilterConfig.from_dict({"whitelist": ["a"], "устаревший_ключ": 1})
    assert config.whitelist == ["a"]
