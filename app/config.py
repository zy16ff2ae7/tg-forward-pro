"""Конфигурация сервиса. Все значения читаются из окружения/.env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv

from app.fsperms import group_or_world_accessible
from app.webapp_build import build_stamp

BASE_DIR = Path(__file__).resolve().parent.parent

LOG_LEVELS = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Контуры оплаты, см. Settings.pay_mode
PAY_MODES = ("external", "stars", "inline")

# Пары api_id:api_hash из открытого кода официальных клиентов. Они кочуют по
# статьям и готовым скриптам, поэтому Telegram считает их опубликованными и
# может отказать по ним в коде на вход (см. Settings.api_keys_are_public).
PUBLIC_API_PAIRS = frozenset(
    {
        "2040:b18441a1ff607e10a989891a5462e627",  # Telegram Desktop
        "6:eb06d4abfb49dc3eeb1aeb98ae0f581e",  # Telegram Desktop, старая
        "17349:344583e45741c457fe1862106095a5eb",  # Telegram Android
        "21724:3e0cb5efcd52300aec5994fdfc5b6fa8",  # Telegram Android, вторая
        "4:014b35b6184100b085b0d0572f9b5103",  # Telegram iOS
    }
)

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
    # Автовыдачи пробного периода нет: бесплатные дни — только за подписку
    # на канал (BONUS_DAYS, команда /bonus). Ноль отключает grant_trial.
    trial_days: int = 0
    max_rules_free: int = 3
    renew_remind_days: int = 3
    # Возврат ушедших: через сколько дней после конца слать письмо
    # с личным промокодом. 0 — не возвращать.
    winback_days_after: int = 7
    # Размер личного промокода в письме возврата. 0 — письма без кода нет:
    # дёргать ушедшего без подарка — спам, а не возврат.
    winback_percent: int = 10

    # Подарок за подписку на канал сервиса: разово, один раз на аккаунт.
    # Пусто — бонуса нет нигде: ни карточки в кабинете, ни кнопки в боте.
    bonus_channel: str | None = None
    bonus_days: int = 3

    # Реферальная программа: друг пришёл по ссылке — обоим плюс столько дней.
    # Ноль выключает программу целиком: ссылок нет ни в боте, ни в кабинете.
    # Дней пригласившему — за первый оплаченный абонемент друга.
    # Бонус скромный осознанно: большой лёгкий бонус тут же начинают фармить.
    # 0 — вся программа выключена.
    referral_days: int = 5
    # Скидка каждому из двоих за приход друга: оба получают личный
    # одноразовый промокод на −N% к следующей оплате. 0 — только дни.
    referral_discount_percent: int = 5
    # Юзернейм бота без @ — нужен, чтобы собрать ссылку-приглашение
    # (t.me/<имя>?start=ref_<id>). Пусто — показываем только код.
    bot_username: str = ""

    # Оплата
    # Где пользователь платит:
    #   external (по умолчанию) — внутри Telegram только звёзды, карта и USDT
    #     живут на обычной веб-странице, которая открывается во внешнем браузере;
    #   stars    — звёзды и ручная выдача, карта и USDT выключены везде;
    #   inline   — всё внутри бота (старое поведение).
    # Правила Telegram (ToS для разработчиков, п. 6.2) требуют продавать
    # цифровые товары внутри Telegram только за Stars. Режим external уносит
    # оплату картой и криптой за пределы Telegram — это снижает риск, но не
    # обнуляет его: решение остаётся за владельцем сервиса.
    pay_mode: str = "external"
    external_payments_url: str | None = None
    yookassa_shop_id: str | None = None
    yookassa_secret_key: str | None = None
    yookassa_return_url: str | None = None
    usdt_wallet: str | None = None
    trongrid_api_key: str | None = None
    usdt_min_confirmations: int = 1
    # Бэкапы: каталог (относительный — от корня проекта), глубина, пароль
    # шифрования. Пароль живёт ВНЕ сервера — иначе он сгорит вместе с ним.
    backup_dir: str = "backups"
    backup_keep: int = 7
    backup_passphrase: str | None = None

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

        Мини-апп всегда раздаётся по пути /app/ — открываем сразу туда,
        без редиректа с корня (надёжнее внутри Telegram WebView, где
        initData передаётся через объект WebApp, а не через URL).

        В адресе стоит метка сборки. WebView помнит документ по URL и после
        выката открывает его из кэша — со старой вёрсткой; новый адрес не
        оставляет ему выбора. Метка считается по содержимому файлов, так что
        меняется она только вместе с самим мини-аппом.
        """
        if self.webapp_url:
            base = self.webapp_url.rstrip("/")
        elif self.webhook_url:
            base = self.webhook_url.rstrip("/")
        else:
            return None
        if base.endswith("/app"):
            url = base + "/"
        else:
            url = base + "/app/"
        stamp = build_stamp(self.webapp_dir)
        return f"{url}?v={stamp}" if stamp else url

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
    def api_keys_are_public(self) -> bool:
        """Ключи MTProto взяты из официального клиента, а не получены на себя.

        Такие пары гуляют по инструкциям и коду, и Telegram помечает их как
        опубликованные: на запрос кода для входа он может ответить
        ``ApiIdPublishedFloodError``. Может и не ответить — на этом сервисе
        (api_id 2040) вход по номеру проходил и аккаунт поднимался. Поэтому это
        риск, а не запрет: сказать про него при старте стоит, но обещать, что
        подключить аккаунт не выйдет, — значит соврать про рабочий вход.
        """
        pair = f"{self.api_id}:{(self.api_hash or '').strip().lower()}"
        return pair in PUBLIC_API_PAIRS

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

    # ─────────────────── Подарок за подписку на канал ────────────────────

    @property
    def bonus_chat(self) -> str | None:
        """Канал бонуса в виде, понятном Bot API: ``@username`` или ``-100…``.

        В ``.env`` его пишут как угодно — ``@name``, ``name``,
        ``https://t.me/name``. Приватная ссылка-приглашение (``t.me/+hash``)
        не годится совсем: ``getChatMember`` по ней не работает, а значит
        проверить подписку нечем — такой бонус считаем ненастроенным.
        """
        raw = (self.bonus_channel or "").strip()
        if not raw:
            return None
        if raw.startswith("-") and raw[1:].isdigit():
            return raw
        low = raw.lower()
        for prefix in ("https://", "http://"):
            if low.startswith(prefix):
                raw, low = raw[len(prefix) :], low[len(prefix) :]
        for prefix in ("t.me/", "telegram.me/"):
            if low.startswith(prefix):
                raw = raw[len(prefix) :]
                break
        name = raw.strip("/").lstrip("@")
        if not name or name.startswith("+"):
            return None
        return "@" + name

    @property
    def bonus_url(self) -> str | None:
        """Ссылка на канал для кнопки «Открыть канал»."""
        chat = self.bonus_chat
        if not chat or not chat.startswith("@"):
            return None
        return "https://t.me/" + chat[1:]

    @property
    def bonus_enabled(self) -> bool:
        """Есть ли что дарить и где проверять подписку."""
        return bool(self.bonus_chat) and self.bonus_days > 0

    @property
    def referral_enabled(self) -> bool:
        """Включена ли реферальная программа: ноль дней — выключена."""
        return self.referral_days > 0

    # ─────────────────────── Готовность способов оплаты ───────────────────────

    @property
    def stars_ready(self) -> bool:
        """Звёзды работают всегда: это встроенный механизм Bot API."""
        return True

    @property
    def yookassa_ready(self) -> bool:
        """Карта/СБП работают только при полной паре ключей ЮKassa.

        Один ключ без второго — это не «почти работает», а гарантированная
        ошибка на создании инвойса, поэтому такой вариант считаем выключенным.
        """
        return bool(
            (self.yookassa_shop_id or "").strip()
            and (self.yookassa_secret_key or "").strip()
        )

    @property
    def usdt_ready(self) -> bool:
        """USDT включается, только если задан настоящий TRC-20 кошелёк."""
        wallet = (self.usdt_wallet or "").strip()
        if not wallet:
            return False
        # TRC-20 адрес: 34 символа base58, начинается с T.
        if len(wallet) != 34 or not wallet.startswith("T"):
            return False
        allowed = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
        return set(wallet) <= allowed

    def payment_methods(self) -> list[str]:
        """Список реально работающих способов оплаты в порядке показа.

        Здесь только про настройки: способ попадает в список, если у него есть
        ключи. Где именно им платят — внутри Telegram или на внешней странице —
        решают ``inline_payment_methods`` и ``external_payment_methods``.

        Ручная выдача доступна администратору всегда — это запасной путь,
        если ни один автоматический провайдер не настроен.
        """
        methods = ["stars"] if self.stars_ready else []
        if self.yookassa_ready:
            methods.append("yookassa")
        if self.usdt_ready:
            methods.append("usdt")
        methods.append("manual")
        return methods

    # ──────────────────────── Контур оплаты (где платят) ──────────────────────

    @property
    def pay_page_url(self) -> str | None:
        """Адрес страницы оплаты картой/криптой — вне Telegram.

        По умолчанию это наш же сервис по пути ``/pay``: отдельный хостинг
        поднимать не нужно. ``EXTERNAL_PAYMENTS_URL`` перебивает адрес, если
        оплату вынесли на другой домен.
        """
        if self.external_payments_url:
            return self.external_payments_url.rstrip("/")
        base = self._public_base()
        if not base:
            return None
        return base + "/pay"

    def _public_base(self) -> str | None:
        """Публичный корень сервиса (без завершающего слеша)."""
        if self.webapp_url:
            base = self.webapp_url.rstrip("/")
        elif self.webhook_url:
            base = self.webhook_url.rstrip("/")
        else:
            return None
        if base.endswith("/app"):
            base = base[: -len("/app")]
        return base or None

    def inline_payment_methods(self) -> list[str]:
        """Способы, которые показываем кнопками внутри Telegram.

        В режимах ``external`` и ``stars`` внутри Telegram остаются звёзды и
        заявка администратору. Карта и крипта — либо на внешней странице, либо
        выключены совсем.
        """
        if self.pay_mode == "inline":
            return self.payment_methods()
        return [m for m in self.payment_methods() if m in ("stars", "manual")]

    def external_payment_methods(self) -> list[str]:
        """Способы, доступные на внешней странице оплаты.

        Без известного адреса страницы список пуст: способ, на который некуда
        отправить, недоступен. Иначе кнопка карты пропадала бы из меню молча —
        ни ссылки, ни пометки «скоро».
        """
        if self.pay_mode != "external" or not self.pay_page_url:
            return []
        return [m for m in self.payment_methods() if m in ("yookassa", "usdt")]

    @property
    def external_payments_ready(self) -> bool:
        """Есть ли куда уводить за оплатой картой/криптой."""
        return bool(self.external_payment_methods())

    def method_available(self, method: str) -> bool:
        """Можно ли прямо сейчас платить этим способом — где угодно."""
        return method in self.inline_payment_methods() or method in self.external_payment_methods()

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
        elif self.api_keys_are_public:
            problems.append(
                "API_ID/API_HASH — публичная пара официального клиента. Telegram "
                "может отказать в коде на вход (ApiIdPublishedFloodError) — кабинет "
                "тогда так и скажет; иногда вход проходит. Свои ключи: "
                "my.telegram.org/apps, но менять пару при уже подключённых "
                "аккаунтах нельзя: их сессии привязаны к прежнему api_id и после "
                "подмены потребуют входа заново"
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
        if (self.usdt_wallet or "").strip() and not self.usdt_ready:
            problems.append(
                "USDT_TRC20_WALLET не похож на адрес TRC-20 "
                "(нужны 34 символа, начиная с T) — оплата USDT отключена"
            )
        if self.pay_mode == "external" and not self.pay_page_url:
            problems.append(
                "PAY_MODE=external, но адрес страницы оплаты неизвестен — "
                "задайте WEBAPP_URL, WEBHOOK_URL или EXTERNAL_PAYMENTS_URL, "
                "иначе останутся только звёзды и заявка администратору"
            )
        if self.pay_mode == "inline" and (self.yookassa_ready or self.usdt_ready):
            problems.append(
                "PAY_MODE=inline: карта и USDT продаются внутри Telegram. "
                "По правилам Telegram (ToS для разработчиков, п. 6.2) цифровые "
                "товары внутри Telegram продают за Stars — риск блокировки бота "
                "на вас. Безопаснее PAY_MODE=external"
            )
        if self.external_payments_url and not self.external_payments_url.startswith("https://"):
            problems.append(
                "EXTERNAL_PAYMENTS_URL без https:// — Telegram не откроет такую "
                "ссылку, а платить по http небезопасно"
            )
        if (self.bonus_channel or "").strip() and not self.bonus_chat:
            problems.append(
                "BONUS_CHANNEL не похож на публичный канал (@имя или -100…): "
                "по приватной ссылке-приглашению подписку не проверить — "
                "подарок за подписку отключён"
            )
        if self.bonus_enabled:
            problems.append(
                f"Подарок за подписку включён ({self.bonus_days} дн., {self.bonus_chat}) — "
                "бот должен быть администратором этого канала, иначе Telegram не "
                "покажет ему подписчиков и проверка всегда будет отвечать «не вижу вас»"
            )
        if self.delivery_workers < 1:
            problems.append("DELIVERY_WORKERS меньше 1 — очередь доставки не сможет работать")
        if self.delivery_queue_maxsize < 10:
            problems.append("DELIVERY_QUEUE_MAXSIZE слишком мал — при всплеске посты будут отбрасываться")
        if self.send_min_interval_seconds < 0.5:
            problems.append("SEND_MIN_INTERVAL_SECONDS ниже 0.5 — высокий риск FloodWait")
        problems.extend(self._permission_warnings())
        return problems

    def _permission_warnings(self) -> list[str]:
        """Файлы с секретами, которые видны не только владельцу.

        Точка входа сначала пытается починить права сама (fsperms), так что сюда
        попадает только то, что починить не удалось: чужой владелец, монтирование
        без прав POSIX. Молчать об этом нельзя — в .env лежит токен бота, а в
        базе зашифрованные сессии пользователей.
        """
        problems: list[str] = []
        for name in ("data", "logs"):
            directory = BASE_DIR / name
            if directory.is_dir() and group_or_world_accessible(directory):
                problems.append(
                    f"Папка {name}/ доступна не только владельцу — "
                    f"выполните: chmod 700 {directory}"
                )
        for pattern in (".env", "data/*.db", "logs/*.log"):
            for path in sorted(BASE_DIR.glob(pattern)):
                if path.is_file() and group_or_world_accessible(path):
                    problems.append(
                        f"{path.name} читается не только владельцем — "
                        f"выполните: chmod 600 {path}"
                    )
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
        trial_days=_get_int("TRIAL_DAYS", 0),
        max_rules_free=_get_int("MAX_RULES_FREE", 3),
        renew_remind_days=_get_int("RENEW_REMIND_DAYS", 3),
        winback_days_after=max(0, _get_int("WINBACK_DAYS_AFTER", 7)),
        winback_percent=max(
            0, min(90, _get_int("WINBACK_PERCENT", 10))
        ),
        bonus_channel=_get("BONUS_CHANNEL"),
        bonus_days=max(0, _get_int("BONUS_DAYS", 3)),
        referral_days=max(0, _get_int("REFERRAL_DAYS", 5)),
        referral_discount_percent=max(
            0, min(90, _get_int("REFERRAL_DISCOUNT_PERCENT", 5))
        ),
        bot_username=(_get("BOT_USERNAME", "") or "").lstrip("@"),
        pay_mode=_get_mode("PAY_MODE", PAY_MODES, "external"),
        external_payments_url=_get("EXTERNAL_PAYMENTS_URL"),
        yookassa_shop_id=_get("YOOKASSA_SHOP_ID"),
        yookassa_secret_key=_get("YOOKASSA_SECRET_KEY"),
        yookassa_return_url=_get("YOOKASSA_RETURN_URL"),
        usdt_wallet=_get("USDT_TRC20_WALLET"),
        trongrid_api_key=_get("TRONGRID_API_KEY"),
        usdt_min_confirmations=_get_int("USDT_MIN_CONFIRMATIONS", 1),
        backup_dir=_get("BACKUP_DIR") or "backups",
        backup_keep=max(1, _get_int("BACKUP_KEEP", 7)),
        backup_passphrase=_get("BACKUP_PASSPHRASE") or None,
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


def _get_mode(name: str, allowed: tuple[str, ...], default: str) -> str:
    """То же, но для значений в нижнем регистре (``PAY_MODE``)."""
    raw = (_get(name) or "").strip().lower()
    return raw if raw in allowed else default


settings = load_settings()
