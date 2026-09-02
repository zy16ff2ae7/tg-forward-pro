"""Конфигурация сервиса. Все значения читаются из окружения/.env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# .env подхватываем, если он есть (на VDS его создаёт deploy-скрипт)
load_dotenv(BASE_DIR / ".env")


def _get(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value


def _get_int(name: str, default: int = 0) -> int:
    raw = _get(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    """Число с плавающей точкой из .env. Опечатка не роняет старт — берём умолчание."""
    raw = _get(name)
    if raw is None:
        return default
    try:
        return float(str(raw).strip().replace(",", "."))
    except ValueError:
        return default


def _get_list(name: str, default: list[int] | None = None) -> list[int]:
    raw = _get(name)
    if not raw:
        return default or []
    result: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            result.append(int(chunk))
    return result


@dataclass(slots=True)
class Settings:
    # Бот
    bot_token: str = ""
    admin_ids: list[int] = field(default_factory=list)

    # Telethon
    api_id: int = 0
    api_hash: str = ""
    proxy: str | None = None

    # Сервер
    host: str = "127.0.0.1"
    port: int = 8080
    webhook_url: str | None = None
    webhook_path: str = "/webhook"
    webhook_secret: str | None = None

    # Мини-апп (Telegram Web App)
    webapp_url: str | None = None
    webapp_dir: Path = field(
        default_factory=lambda: BASE_DIR / "webapp"
    )

    # БД
    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    # Шифрование сессий
    secret_key: str = ""

    # Тарифы
    price_rub: int = 990
    price_stars: int = 299
    price_usdt: float = 12.0
    trial_days: int = 3
    max_rules_free: int = 3
    renew_remind_days: int = 3

    # Оплата
    yookassa_shop_id: str | None = None
    yookassa_secret_key: str | None = None
    yookassa_return_url: str | None = None
    usdt_wallet: str | None = None
    trongrid_api_key: str | None = None
    usdt_min_confirmations: int = 1

    # Масштабирование пересылки (безопасные лимиты, не обход антиспама Telegram)
    delivery_workers: int = 4
    delivery_queue_maxsize: int = 2000
    send_global_concurrency: int = 8
    send_min_interval_seconds: float = 1.2
    send_retry_attempts: int = 2
    send_retry_base_seconds: float = 2.0
    flood_wait_max_seconds: int = 900

    log_level: str = "INFO"

    @property
    def use_webhook(self) -> bool:
        return bool(self.webhook_url)

    @property
    def public_url(self) -> str | None:
        """Полный адрес вебхука для Bot API."""
        if not self.webhook_url:
            return None
        return self.webhook_url.rstrip("/") + self.webhook_path

    @property
    def mini_app_url(self) -> str | None:
        """Публичный адрес мини-аппа (для кнопок бота).

        Если WEBAPP_URL не задан, строим из WEBHOOK_URL — на VDS этого достаточно.
        """
        if self.webapp_url:
            return self.webapp_url.rstrip("/")
        if self.webhook_url:
            return self.webhook_url.rstrip("/") + "/app/"
        return None

    @property
    def mtproto_ready(self) -> bool:
        """Готов ли шлюз личных аккаунтов Telegram (Telethon).

        Плейсхолдеры из .env.example не считаем рабочими значениями: это позволяет
        запускать публичную часть сервиса без падения, пока MTProto-шлюз не подключён.
        """
        api_hash = (self.api_hash or "").strip().lower()
        placeholder_hashes = {
            "0123456789abcdef0123456789abcdef",
            "your_api_hash",
            "change-me",
        }
        if self.api_id <= 0 or not api_hash:
            return False
        if api_hash in placeholder_hashes or set(api_hash) == {"x"}:
            return False
        return True

    @property
    def public_login_enabled(self) -> bool:
        """Можно ли пользователям подключать аккаунты по телефону прямо сейчас."""
        return self.mtproto_ready

    @property
    def account_login_status(self) -> str:
        """Короткий публичный статус для кабинета и API."""
        if self.mtproto_ready:
            return "ready"
        return "setup_required"

    def require(self) -> None:
        """Проверка параметров, обязательных для запуска бота и мини-аппа.

        API_ID/API_HASH сознательно не блокируют старт: без них работают меню,
        мини-апп, подписки, платежи и админка. Заблокирован только вход личных
        аккаунтов по номеру телефона — это обрабатывается в accounts/manager.
        """
        missing: list[str] = []
        if not self.bot_token:
            missing.append("BOT_TOKEN")
        if not self.secret_key:
            missing.append("SECRET_KEY")
        if missing:
            raise RuntimeError(
                "Не заполнены обязательные параметры в .env: " + ", ".join(missing)
            )

        # Ключ проверяем сразу: с неправильным SECRET_KEY сервис поднимется,
        # но ни одну сохранённую сессию потом не расшифровать.
        try:
            Fernet(self.secret_key.encode("utf-8"))
        except (ValueError, TypeError) as exc:
            raise RuntimeError(
                "SECRET_KEY не является ключом Fernet. "
                "Сгенерируйте новый: python scripts/gen_secret.py"
            ) from exc

        if self.use_webhook and not self.webhook_secret:
            raise RuntimeError(
                "Задан WEBHOOK_URL, но нет WEBHOOK_SECRET — вебхук без секрета "
                "примет любое поддельное обновление."
            )

    def warnings(self) -> list[str]:
        """Мягкие проблемы: сервис работает, но что-то из настроек неполное."""
        problems: list[str] = []
        if not self.mtproto_ready:
            problems.append(
                "API_ID/API_HASH не заданы — вход аккаунтов по номеру и пересылка отключены"
            )
        if not self.admin_ids:
            problems.append("ADMIN_IDS пуст — админ-команды недоступны")
        if self.mini_app_url is None:
            problems.append(
                "Не задан WEBAPP_URL или WEBHOOK_URL — кнопка мини-аппа в меню не появится"
            )
        if bool(self.yookassa_shop_id) != bool(self.yookassa_secret_key):
            problems.append(
                "ЮKassa подключена наполовину: нужен и YOOKASSA_SHOP_ID, и YOOKASSA_SECRET_KEY"
            )
        if self.delivery_workers < 1:
            problems.append("DELIVERY_WORKERS меньше 1 — очередь доставки не сможет работать")
        if self.delivery_queue_maxsize < 10:
            problems.append("DELIVERY_QUEUE_MAXSIZE слишком мал — при всплеске посты будут отбрасываться")
        if self.send_min_interval_seconds < 0.5:
            problems.append("SEND_MIN_INTERVAL_SECONDS ниже 0.5 — высокий риск FloodWait")
        return problems


def _normalize_database_url(database_url: str) -> str:
    """Делает SQLite-путь стабильным независимо от текущей рабочей папки."""
    if not database_url.startswith("sqlite"):
        return database_url

    if database_url.endswith(":memory:"):
        return database_url

    prefix = "sqlite+aiosqlite:///"
    if database_url.startswith(prefix):
        raw_path = database_url[len(prefix) :]
        db_path = Path(raw_path)
        if not db_path.is_absolute():
            db_path = BASE_DIR / db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return "sqlite+aiosqlite:///" + str(db_path)

    (BASE_DIR / "data").mkdir(parents=True, exist_ok=True)
    return database_url


def load_settings() -> Settings:
    database_url = _normalize_database_url(
        _get("DATABASE_URL", "sqlite+aiosqlite:///./data/app.db") or ""
    )

    return Settings(
        bot_token=_get("BOT_TOKEN", "") or "",
        admin_ids=_get_list("ADMIN_IDS"),
        api_id=_get_int("API_ID"),
        api_hash=_get("API_HASH", "") or "",
        proxy=_get("PROXY"),
        host=_get("HOST", "127.0.0.1") or "127.0.0.1",
        port=_get_int("PORT", 8080),
        webhook_url=_get("WEBHOOK_URL"),
        webhook_path=_get("WEBHOOK_PATH", "/webhook") or "/webhook",
        webhook_secret=_get("WEBHOOK_SECRET"),
        webapp_url=_get("WEBAPP_URL"),
        database_url=database_url,
        secret_key=_get("SECRET_KEY", "") or "",
        price_rub=_get_int("PRICE_RUB", 990),
        price_stars=_get_int("PRICE_STARS", 299),
        price_usdt=_get_float("PRICE_USDT", 12.0),
        trial_days=_get_int("TRIAL_DAYS", 3),
        max_rules_free=_get_int("MAX_RULES_FREE", 3),
        renew_remind_days=_get_int("RENEW_REMIND_DAYS", 3),
        yookassa_shop_id=_get("YOOKASSA_SHOP_ID"),
        yookassa_secret_key=_get("YOOKASSA_SECRET_KEY"),
        yookassa_return_url=_get("YOOKASSA_RETURN_URL"),
        usdt_wallet=_get("USDT_TRC20_WALLET"),
        trongrid_api_key=_get("TRONGRID_API_KEY"),
        usdt_min_confirmations=_get_int("USDT_MIN_CONFIRMATIONS", 1),
        delivery_workers=max(1, _get_int("DELIVERY_WORKERS", 4)),
        delivery_queue_maxsize=max(10, _get_int("DELIVERY_QUEUE_MAXSIZE", 2000)),
        send_global_concurrency=max(1, _get_int("SEND_GLOBAL_CONCURRENCY", 8)),
        send_min_interval_seconds=max(
            0.1, _get_float("SEND_MIN_INTERVAL_SECONDS", 1.2)
        ),
        send_retry_attempts=max(0, _get_int("SEND_RETRY_ATTEMPTS", 2)),
        send_retry_base_seconds=max(0.1, _get_float("SEND_RETRY_BASE_SECONDS", 2.0)),
        flood_wait_max_seconds=max(1, _get_int("FLOOD_WAIT_MAX_SECONDS", 900)),
        log_level=_get_choice("LOG_LEVEL", LOG_LEVELS, "INFO"),
    )


def _get_choice(name: str, allowed: tuple[str, ...], default: str) -> str:
    """Значение из списка допустимых; опечатка не ломает запуск."""
    raw = (_get(name) or "").strip().upper()
    return raw if raw in allowed else default


settings = load_settings()
