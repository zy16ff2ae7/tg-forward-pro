"""Конфигурация: опечатки в .env не должны ронять сервис, а дыры в безопасности — ронять."""
from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import LOG_LEVELS, Settings, _get_choice, _get_float

VALID_KEY = Fernet.generate_key().decode()


# ───────────────────────────── разбор значений ────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("12", 12.0),
        ("12.5", 12.5),
        ("12,5", 12.5),  # десятичная запятая — частая опечатка
        (" 7 ", 7.0),
    ],
)
def test_get_float_accepts_dot_and_comma(monkeypatch, raw, expected):
    monkeypatch.setenv("PRICE_USDT", raw)
    assert _get_float("PRICE_USDT", 99.0) == expected


def test_get_float_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("PRICE_USDT", "двенадцать")
    assert _get_float("PRICE_USDT", 12.0) == 12.0


def test_get_choice_only_returns_known_levels(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert _get_choice("LOG_LEVEL", LOG_LEVELS, "INFO") == "DEBUG"

    monkeypatch.setenv("LOG_LEVEL", "ОТЛАДКА")
    assert _get_choice("LOG_LEVEL", LOG_LEVELS, "INFO") == "INFO"


# ─────────────────────────── обязательные параметры ───────────────────────────


def test_require_passes_with_valid_minimum():
    Settings(bot_token="123:abc", secret_key=VALID_KEY).require()


def test_require_rejects_missing_token():
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        Settings(bot_token="", secret_key=VALID_KEY).require()


def test_require_rejects_missing_secret():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Settings(bot_token="123:abc", secret_key="").require()


def test_require_rejects_non_fernet_secret():
    """Иначе сервис поднимется, но ни одну сохранённую сессию не расшифровать."""
    with pytest.raises(RuntimeError, match="Fernet"):
        Settings(bot_token="123:abc", secret_key="просто-строка").require()


def test_require_rejects_webhook_without_secret():
    with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
        Settings(
            bot_token="123:abc",
            secret_key=VALID_KEY,
            webhook_url="https://example.com",
        ).require()


# ───────────────────────────── мягкие проверки ────────────────────────────────


def test_warnings_reports_half_configured_yookassa():
    problems = Settings(
        yookassa_shop_id="123", yookassa_secret_key=None
    ).warnings()
    assert any("ЮKassa" in text for text in problems)


def test_warnings_reports_empty_admins_and_missing_mtproto():
    problems = Settings().warnings()
    assert any("ADMIN_IDS" in text for text in problems)
    assert any("API_ID" in text for text in problems)


def test_warnings_silent_when_all_set():
    problems = Settings(
        api_id=12345,
        api_hash="a" * 32,
        admin_ids=[1],
        webapp_url="https://example.com",
        yookassa_shop_id="1",
        yookassa_secret_key="2",
    ).warnings()
    assert problems == []


# ──────────────────────────────── свойства ────────────────────────────────────


def test_mtproto_ready_rejects_placeholders():
    assert Settings(api_id=1, api_hash="0123456789abcdef0123456789abcdef").mtproto_ready is False
    assert Settings(api_id=0, api_hash="a" * 32).mtproto_ready is False
    assert Settings(api_id=1, api_hash="a" * 32).mtproto_ready is True


def test_mini_app_url_falls_back_to_webhook_url():
    settings = Settings(webhook_url="https://example.com/")
    assert settings.mini_app_url == "https://example.com/app/"
    assert settings.public_url == "https://example.com/webhook"


def test_mini_app_url_absent_without_any_public_url():
    assert Settings().mini_app_url is None
