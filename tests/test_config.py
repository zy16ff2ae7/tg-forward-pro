"""Конфигурация: опечатки в .env не должны ронять сервис, а дыры в безопасности — ронять."""
from __future__ import annotations

from dataclasses import replace

import pytest
from cryptography.fernet import Fernet

from app.config import LOG_LEVELS, Settings, _get_choice, _get_float, load_settings

VALID_KEY = Fernet.generate_key().decode()
VALID_TRC20 = "TQn9Y2khDD95J42FQtQTdwVVR93o1n1gLz"  # 34 символа, начинается с T

# Полностью настроенная оплата: все провайдеры на месте и известен публичный
# адрес. Отсюда через dataclasses.replace получаются варианты контуров.
FULL_PAY_SETTINGS = Settings(
    secret_key=VALID_KEY,
    webapp_url="https://pay.example.com",
    yookassa_shop_id="1",
    yookassa_secret_key="2",
    usdt_wallet=VALID_TRC20,
)


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


def test_warnings_silent_when_all_set(clean_base_dir):
    problems = Settings(
        api_id=12345,
        api_hash="a" * 32,
        admin_ids=[1],
        webapp_url="https://example.com",
        yookassa_shop_id="1",
        yookassa_secret_key="2",
    ).warnings()
    assert problems == []


# ────────────────────── права на .env, базу и логи ────────────────────────────


@pytest.fixture
def clean_base_dir(monkeypatch, tmp_path):
    """Проверки прав смотрят на реальную папку проекта — подменяем её на пустую.

    Иначе тест зависел бы от того, какие права оказались у .env на машине
    разработчика.
    """
    import app.config as config_module

    monkeypatch.setattr(config_module, "BASE_DIR", tmp_path)
    return tmp_path


def test_warnings_report_world_readable_env(clean_base_dir):
    env = clean_base_dir / ".env"
    env.write_text("BOT_TOKEN=123:abc\n", encoding="utf-8")
    env.chmod(0o644)

    problems = Settings().warnings()
    assert any(".env" in text and "chmod 600" in text for text in problems)


def test_warnings_silent_about_private_env(clean_base_dir):
    env = clean_base_dir / ".env"
    env.write_text("BOT_TOKEN=123:abc\n", encoding="utf-8")
    env.chmod(0o600)

    assert not any(".env" in text for text in Settings().warnings())


def test_warnings_report_open_data_dir(clean_base_dir):
    data = clean_base_dir / "data"
    data.mkdir()
    data.chmod(0o755)
    db = data / "app.db"
    db.write_bytes(b"")
    db.chmod(0o644)

    problems = Settings().warnings()
    assert any("data/" in text and "chmod 700" in text for text in problems)
    assert any("app.db" in text and "chmod 600" in text for text in problems)


def test_harden_runtime_files_fixes_modes(tmp_path):
    """Точка входа чинит права сама — деплой не обязан помнить про chmod."""
    from app.fsperms import group_or_world_accessible, harden_runtime_files

    (tmp_path / "data").mkdir()
    (tmp_path / "logs").mkdir()
    env = tmp_path / ".env"
    env.write_text("BOT_TOKEN=1\n", encoding="utf-8")
    db = tmp_path / "data" / "app.db"
    db.write_bytes(b"")
    log = tmp_path / "logs" / "app.log"
    log.write_text("hello\n", encoding="utf-8")
    for path in (env, db, log):
        path.chmod(0o644)
    (tmp_path / "data").chmod(0o755)
    (tmp_path / "logs").chmod(0o755)

    fixed = harden_runtime_files(tmp_path)

    assert set(fixed) == {".env", "data/", "logs/", "data/app.db", "logs/app.log"}
    for path in (env, db, log, tmp_path / "data", tmp_path / "logs"):
        assert not group_or_world_accessible(path)
    # Повторный вызов уже ничего не меняет — в лог не сыплется каждый старт.
    assert harden_runtime_files(tmp_path) == []


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


# ──────────────────────── готовность способов оплаты ──────────────────────────


def test_yookassa_needs_both_keys():
    assert Settings(yookassa_shop_id="1").yookassa_ready is False
    assert Settings(yookassa_secret_key="2").yookassa_ready is False
    assert Settings(yookassa_shop_id="1", yookassa_secret_key="2").yookassa_ready is True


def test_usdt_needs_real_trc20_address():
    assert Settings().usdt_ready is False
    assert Settings(usdt_wallet="мой-кошелёк").usdt_ready is False
    assert Settings(usdt_wallet="TQn9Y").usdt_ready is False  # слишком короткий
    assert Settings(usdt_wallet="A" * 34).usdt_ready is False  # не начинается с T
    assert Settings(usdt_wallet=VALID_TRC20).usdt_ready is True


def test_wallets_bogus_usdt_reported():
    problems = Settings(usdt_wallet="мой-кошелёк").warnings()
    assert any("USDT" in text for text in problems)


def test_payment_methods_only_lists_working_ones():
    """Меню оплаты не должно обещать не подключённые способы."""
    assert Settings().payment_methods() == ["stars", "manual"]

    full = Settings(
        yookassa_shop_id="1",
        yookassa_secret_key="2",
        usdt_wallet=VALID_TRC20,
    ).payment_methods()
    assert full == ["stars", "yookassa", "usdt", "manual"]


def test_payment_menu_hides_unconfigured_methods(monkeypatch):
    """Кнопки карты и USDT не рисуются, пока провайдеры не настроены."""
    from app import paylink
    from app.bot import keyboards as kb

    def labels_for(cfg: Settings, user_id: int | None = None) -> list[str]:
        # Ссылку на страницу оплаты собирает paylink со своим импортом настроек —
        # без второго патча кнопка сайта считала бы настройки боевыми.
        monkeypatch.setattr(kb, "settings", cfg)
        monkeypatch.setattr(paylink, "settings", cfg)
        menu = kb.payment_menu(user_id)
        return [btn.text for row in menu.inline_keyboard for btn in row]

    empty = labels_for(Settings())
    assert "⭐ Telegram Stars" in empty
    assert "👤 Через администратора" in empty
    assert "💳 Карта / СБП" not in empty
    assert "🪙 USDT (TRC-20)" not in empty
    # Ненастроенные способы помечены, чтобы не выглядело как пропажа
    assert "💳 Карта / СБП — скоро" in empty
    assert "🪙 USDT (TRC-20) — скоро" in empty

    # Всё настроено, контур по умолчанию: внутри Telegram — звёзды и админ,
    # карта с криптой уходят одной кнопкой-ссылкой на страницу сервиса.
    external = labels_for(FULL_PAY_SETTINGS, user_id=777)
    assert "⭐ Telegram Stars" in external
    assert "💳 Карта / СБП" not in external
    assert "🪙 USDT (TRC-20)" not in external
    assert "🌐 Карта / СБП / USDT (TRC-20) — на сайте" in external
    assert not any("скоро" in text for text in external)

    # PAY_MODE=inline — старое поведение: всё внутри бота, ссылки нет.
    inline = labels_for(replace(FULL_PAY_SETTINGS, pay_mode="inline"), user_id=777)
    assert "💳 Карта / СБП" in inline
    assert "🪙 USDT (TRC-20)" in inline
    assert not any("на сайте" in text for text in inline)
    assert not any("скоро" in text for text in inline)


def test_payment_menu_marks_soon_when_page_url_unknown(monkeypatch):
    """Способ без адреса страницы помечен «скоро», а не исчезает молча."""
    from app import paylink
    from app.bot import keyboards as kb

    cfg = replace(FULL_PAY_SETTINGS, webapp_url=None, webhook_url=None)
    monkeypatch.setattr(kb, "settings", cfg)
    monkeypatch.setattr(paylink, "settings", cfg)
    labels = [btn.text for row in kb.payment_menu(777).inline_keyboard for btn in row]

    assert "💳 Карта / СБП — скоро" in labels
    assert "🪙 USDT (TRC-20) — скоро" in labels
    assert not any("на сайте" in text for text in labels)


# ───────────────────────────── контуры оплаты ─────────────────────────────────


def test_external_mode_keeps_stars_inside_telegram():
    """По умолчанию внутри Telegram остаются только звёзды и заявка админу."""
    assert FULL_PAY_SETTINGS.inline_payment_methods() == ["stars", "manual"]
    assert FULL_PAY_SETTINGS.external_payment_methods() == ["yookassa", "usdt"]
    assert FULL_PAY_SETTINGS.external_payments_ready is True
    assert all(FULL_PAY_SETTINGS.method_available(m) for m in ("stars", "yookassa", "usdt"))


def test_stars_mode_disables_card_and_crypto_everywhere():
    cfg = replace(FULL_PAY_SETTINGS, pay_mode="stars")
    assert cfg.inline_payment_methods() == ["stars", "manual"]
    assert cfg.external_payment_methods() == []
    assert cfg.method_available("yookassa") is False
    assert cfg.method_available("usdt") is False


def test_inline_mode_returns_everything_into_the_bot():
    cfg = replace(FULL_PAY_SETTINGS, pay_mode="inline")
    assert cfg.inline_payment_methods() == ["stars", "yookassa", "usdt", "manual"]
    assert cfg.external_payment_methods() == []
    assert cfg.method_available("yookassa") is True


def test_pay_page_url_built_from_public_address():
    assert FULL_PAY_SETTINGS.pay_page_url == "https://pay.example.com/pay"
    # /app в конце — это адрес мини-аппа, а не корень сервиса
    assert (
        replace(FULL_PAY_SETTINGS, webapp_url="https://pay.example.com/app").pay_page_url
        == "https://pay.example.com/pay"
    )
    # Отдельный домен для оплаты перебивает адрес сервиса
    assert (
        replace(
            FULL_PAY_SETTINGS, external_payments_url="https://money.example.com/checkout/"
        ).pay_page_url
        == "https://money.example.com/checkout"
    )
    assert replace(FULL_PAY_SETTINGS, webapp_url=None, webhook_url=None).pay_page_url is None


def test_pay_mode_falls_back_to_external_on_garbage(monkeypatch):
    monkeypatch.setenv("PAY_MODE", "как-нибудь")
    assert load_settings().pay_mode == "external"
    monkeypatch.setenv("PAY_MODE", " Inline ")
    assert load_settings().pay_mode == "inline"


def test_warnings_report_unreachable_pay_page():
    problems = replace(FULL_PAY_SETTINGS, webapp_url=None, webhook_url=None).warnings()
    assert any("PAY_MODE=external" in text for text in problems)


def test_warnings_report_tos_risk_of_inline_mode():
    problems = replace(FULL_PAY_SETTINGS, pay_mode="inline").warnings()
    assert any("6.2" in text for text in problems)
    # А в контуре по умолчанию про риск не напоминаем
    assert not any("6.2" in text for text in FULL_PAY_SETTINGS.warnings())


def test_warnings_report_plain_http_pay_page():
    problems = replace(
        FULL_PAY_SETTINGS, external_payments_url="http://money.example.com"
    ).warnings()
    assert any("https" in text for text in problems)


def test_payment_price_line_skips_unconfigured(monkeypatch):
    """Цена не печатается для способов, которыми нельзя заплатить."""
    from app.bot import texts as bot_texts

    monkeypatch.setattr(bot_texts, "settings", Settings(price_stars=100))
    assert bot_texts.price_line() == "Стоимость: 100 ⭐"

    monkeypatch.setattr(
        bot_texts,
        "settings",
        Settings(price_stars=100, price_rub=990, yookassa_shop_id="1", yookassa_secret_key="2"),
    )
    assert bot_texts.price_line() == "Стоимость: 100 ⭐  ·  990 ₽"
