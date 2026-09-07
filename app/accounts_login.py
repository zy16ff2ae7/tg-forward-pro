"""Подключение личного аккаунта: один сценарий на бота и на кабинет.

Раньше вход существовал только в боте, и вся логика жила прямо в хендлерах
(``app/bot/handlers/accounts.py``): шаг хранился во FSM aiogram, ошибки
превращались в текст сообщений там же. Кабинет — это HTTP без FSM, поэтому
кнопка «Подключить аккаунт» в мини-аппе умела единственное: выкинуть человека
в чат с ботом. Он выходил и не возвращался.

Здесь шаги входа отвязаны от способа общения:

* состояние шага — строка ``pending_logins`` в БД, а не память процесса. Один и
  тот же незавершённый вход виден и боту, и кабинету, и переживает перезапуск;
* ошибки — исключения ``app.errors`` с текстом для человека. Кабинет отдаёт их
  как JSON (middleware), бот показывает тем же текстом;
* неверный код не убивает вход. Опечатка в цифре — самая частая ошибка на этом
  шаге, а прошлое поведение стирало pending и заставляло запрашивать код заново.
  Попытки считаются в БД, и только когда их слишком много, вход сбрасывается:
  так и опечатка прощается, и перебор кода не бесконечен.

Что *не* делается здесь: ни одного сообщения и ни одного HTTP-ответа. Вызвавшая
сторона сама решает, как показать ``LoginStep``.
"""
from __future__ import annotations

import asyncio
import base64
import io
import re
import time
from dataclasses import dataclass

from loguru import logger
from telethon.errors import (
    ApiIdInvalidError,
    ApiIdPublishedFloodError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
)

from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.db.models import TelegramAccount
from app.errors import ConflictError, FeatureUnavailable, NotFoundError, ValidationError
from app.security import decrypt_session, encrypt_session
from app.telegram_client.manager import LoginCreds, manager
from app.timeutil import utcnow

PHONE_RE = re.compile(r"^\+?\d{10,15}$")
API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def normalize_creds(api_id: object, api_hash: object) -> LoginCreds | None:
    """Свои ключи API — парой или никак.

    Оба пустые — ``None`` (вход ключами сервиса). Один без другого, нечисловой
    id или непохожий на хэш мусор — ошибка ввода, а не поход в Telegram:
    сервер и так скажет «не принял», но позже и загадочнее.
    """
    id_text = str(api_id or "").strip()
    hash_text = str(api_hash or "").strip()
    if not id_text and not hash_text:
        return None
    if not id_text or not hash_text:
        raise ValidationError("Свои ключи вводятся парой: и API ID, и API Hash.")
    if not id_text.isdigit() or int(id_text) <= 0:
        raise ValidationError("API ID — число из my.telegram.org/apps.")
    if not API_HASH_RE.match(hash_text):
        raise ValidationError("API Hash — 32 шестнадцатеричных знака.")
    return LoginCreds(api_id=int(id_text), api_hash=hash_text.lower())

# Больше похоже на перебор, чем на опечатку: сбрасываем вход и просим новый код.
MAX_CODE_ATTEMPTS = 5

# Повторный запрос кода раньше этого срока — почти всегда следствие двойного
# нажатия. Telegram на такие запросы отвечает флудом на сам номер, поэтому
# дешевле не ходить туда вовсе.
RESEND_COOLDOWN_SECONDS = 60

LOGIN_UNAVAILABLE_TEXT = (
    "Подключение аккаунтов временно на настройке. Кабинет уже работает, "
    "пересылка включится после подключения MTProto-шлюза сервиса."
)

# Стадии в БД исторически называются так; наружу отдаём короткие имена.
STAGE_CODE = "waiting_code"
STAGE_PASSWORD = "waiting_password"
_PUBLIC_STAGE = {STAGE_CODE: "code", STAGE_PASSWORD: "password"}


@dataclass(slots=True)
class LoginStep:
    """Чего сервис ждёт от человека дальше.

    ``stage``: ``code`` — ждём код из Telegram, ``password`` — облачный пароль
    2FA, ``done`` — аккаунт подключён (тогда заполнены ``account_id`` и ``name``).
    ``delivery`` — куда Telegram положил код (``via``: ``app``/``sms``/``call``…,
    ``next`` — способ повтора, ``timeout`` — через сколько секунд повтор доступен).
    """

    stage: str
    phone: str
    account_id: int | None = None
    name: str | None = None
    attempts_left: int | None = None
    delivery: dict | None = None

    @property
    def done(self) -> bool:
        return self.stage == "done"

    def as_dict(self) -> dict:
        data: dict = {"stage": self.stage, "phone": self.phone}
        if self.account_id is not None:
            data["account_id"] = self.account_id
        if self.name:
            data["name"] = self.name
        if self.attempts_left is not None:
            data["attempts_left"] = self.attempts_left
        if self.delivery is not None:
            data["delivery"] = self.delivery
        return data


@dataclass(slots=True)
class PendingView:
    """Незавершённый вход — то, что можно показать, не раскрывая сессию."""

    phone: str
    stage: str
    attempts_left: int

    def as_dict(self) -> dict:
        return {
            "exists": True,
            "phone": self.phone,
            "stage": self.stage,
            "attempts_left": self.attempts_left,
        }


def require_enabled() -> None:
    """Отказ, если MTProto-шлюз не подключён: вход физически невозможен."""
    if not settings.public_login_enabled:
        raise FeatureUnavailable(
            LOGIN_UNAVAILABLE_TEXT,
            feature="account_login",
            status=settings.account_login_status,
        )


def normalize_phone(raw: str | None) -> str:
    """Приводит ввод к ``+79001234567``. Мусор — отказ, а не молчаливая правка.

    Пробелы, дефисы и скобки люди ставят как привыкли, это не ошибка ввода.
    А вот буквы и короткие огрызки номера отправлять в Telegram незачем:
    он ответит ошибкой, но потратит на это лимит запросов номера.
    """
    phone = re.sub(r"[\s\-()]+", "", str(raw or ""))
    if not PHONE_RE.match(phone):
        raise ValidationError(
            "Нужен номер в международном формате, например +79001234567."
        )
    return phone if phone.startswith("+") else "+" + phone


async def pending(user_id: int) -> PendingView | None:
    """Незавершённый вход пользователя, если он есть."""
    async with SessionLocal() as session:
        row = await repo.get_pending_login(session, user_id)
        if row is None:
            return None
        return PendingView(
            phone=row.phone,
            stage=_PUBLIC_STAGE.get(row.stage, "code"),
            attempts_left=max(MAX_CODE_ATTEMPTS - int(row.attempts or 0), 0),
        )


async def cancel(user_id: int) -> bool:
    """Забывает незавершённый вход. True — было что забывать.

    Отметку о времени отправки кода (``phone_code_sends``) не трогаем: пауза
    перед новым кодом принадлежит номеру, а не попытке входа. Иначе «Отмена»
    становилась способом запросить код ещё раз без паузы — а Telegram считает
    частые запросы флудом и закрывает вход на этот номер на часы.
    """
    dropped_qr = await qr_cancel(user_id)
    async with SessionLocal() as session:
        row = await repo.get_pending_login(session, user_id)
        if row is None:
            return dropped_qr
        await repo.delete_pending_login(session, user_id)
        await session.commit()
    return True


async def _pause_left(session, phone: str, pending: object | None) -> int:
    """Сколько секунд ещё нельзя запрашивать код на этот номер.

    Смотрим и метку номера, и время начатого входа: на базах, заведённых до
    появления ``phone_code_sends``, метки ещё нет, а pending уже есть.
    """
    stamps = [await repo.code_sent_at(session, phone)]
    if pending is not None:
        stamps.append(getattr(pending, "created_at", None))
    now = utcnow()
    ages = [(now - stamp).total_seconds() for stamp in stamps if stamp is not None]
    if not ages:
        return 0
    left = RESEND_COOLDOWN_SECONDS - min(ages)
    return max(int(left) + 1, 0) if left > 0 else 0


def _too_early(phone: str, wait: int, pending: object | None) -> ConflictError:
    """Отказ на слишком ранний повтор — с указанием, где человек теперь стоит.

    ``details`` читают и кабинет, и бот: код уже у человека — значит ждём код, а
    не номер, и выкидывать его на первый шаг незачем.
    """
    if pending is not None:
        stage = _PUBLIC_STAGE.get(getattr(pending, "stage", ""), "code")
        left = max(MAX_CODE_ATTEMPTS - int(getattr(pending, "attempts", 0) or 0), 0)
        return ConflictError(
            f"Код на {phone} уже отправлен. Введите его или подождите {wait} сек, "
            "чтобы запросить новый.",
            details={"stage": stage, "phone": phone, "wait": wait, "attempts_left": left},
        )
    return ConflictError(
        f"Код на {phone} отправляли меньше минуты назад. Подождите {wait} сек: "
        "частые запросы Telegram считает флудом и может закрыть вход на этот "
        "номер на несколько часов.",
        details={"stage": "phone", "phone": phone, "wait": wait},
    )


async def start(
    user_id: int,
    phone_raw: str,
    *,
    resend: bool = False,
    api_id: object = None,
    api_hash: object = None,
) -> LoginStep:
    """Шаг 1: просит Telegram выслать код на номер.

    Уже начатый вход на тот же номер не начинаем заново: код действует, и
    второй запрос только приблизит флуд-лимит на номере.

    ``resend=True`` — «код не пришёл, прислать ещё раз»: продолжает ту же
    попытку через ``auth.ResendCode``, и Telegram обычно переключается на
    следующий способ доставки (приложение → SMS → звонок). Код из прошлого
    сообщения после повтора мёртв — вводить надо новый. Пауза в минуту здесь
    не проверяется: когда повтор доступен, решает сам Telegram (иначе ответит
    флудом с точным сроком). Без незавершённого входа на этот номер повтор
    вырождается в обычный новый запрос.
    """
    require_enabled()
    phone = normalize_phone(phone_raw)
    creds = normalize_creds(api_id, api_hash)
    # Один вход на человека: начатый QR умирает, иначе два живых клиента
    # делят внимание и человек не понимает, что сканировать.
    await qr_cancel(user_id)

    async with SessionLocal() as session:
        row = await repo.get_pending_login(session, user_id)
        pending_here = row is not None and row.phone == phone
        if resend and pending_here and row.stage == STAGE_CODE:
            return await _resend(user_id, phone)
        wait = await _pause_left(session, phone, row if pending_here else None)
        if wait:
            raise _too_early(phone, wait, row if pending_here else None)

    try:
        session_string, phone_code_hash, delivery = await manager.send_code(
            phone, creds
        )
    except PhoneNumberInvalidError:
        raise ValidationError("Telegram не знает такой номер. Проверьте и введите заново.") from None
    except PhoneNumberBannedError:
        raise ValidationError("Этот номер заблокирован в Telegram. Подключите другой.") from None
    except ApiIdPublishedFloodError:
        # Отказ не человеку, а сервису: ключи api_id/api_hash взяты из
        # официального клиента, и Telegram не даёт входить по опубликованной
        # паре. Номер тут ни при чём, менять его бессмысленно.
        if creds is not None:
            raise ValidationError(
                "Telegram не даёт входить по этим ключам: пара опубликована. "
                "Возьмите свои api_id и api_hash на my.telegram.org/apps."
            ) from None
        logger.error(
            "Вход #{}: Telegram отказал — ключи API опубликованы. Нужны свои "
            "API_ID/API_HASH с my.telegram.org/apps",
            user_id,
        )
        raise FeatureUnavailable(
            "Подключение аккаунтов сейчас невозможно: у сервиса публичные ключи "
            "Telegram API. Владельцу — получить свои api_id и api_hash на "
            "my.telegram.org/apps и прописать в .env.",
            feature="account_login",
            status="api_keys_public",
        ) from None
    except ApiIdInvalidError:
        if creds is not None:
            raise ValidationError(
                "Telegram не принял эти ключи API. Сверьте api_id и api_hash "
                "с my.telegram.org/apps."
            ) from None
        logger.error("Вход #{}: Telegram не принял API_ID/API_HASH сервиса", user_id)
        raise FeatureUnavailable(
            "Подключение аккаунтов сейчас невозможно: Telegram не принял ключи "
            "API сервиса. Владельцу — проверить API_ID и API_HASH в .env.",
            feature="account_login",
            status="api_keys_invalid",
        ) from None
    except FloodWaitError as exc:
        # Точный срок ожидания важнее фразы «попробуйте позже»: иначе человек
        # долбит кнопку и продлевает лимит.
        wait = int(getattr(exc, "seconds", 0) or 0)
        human = f"{wait // 60} мин" if wait >= 60 else f"{wait} сек"
        raise ConflictError(
            f"Telegram просит подождать {human} перед новым запросом кода на этот номер."
        ) from None
    except Exception as exc:  # noqa: BLE001 — текст ошибки нужен человеку
        logger.exception("Не удалось отправить код на {}", phone)
        raise ConflictError(f"Не удалось отправить код: {type(exc).__name__}") from exc

    async with SessionLocal() as session:
        # Метка номера ставится в той же транзакции, что и шаг входа: пауза
        # должна начаться, даже если человек тут же нажмёт «Отмена».
        await repo.note_code_sent(session, phone)
        await repo.save_pending_login(
            session,
            user_id=user_id,
            phone=phone,
            session_encrypted=encrypt_session(session_string),
            phone_code_hash=phone_code_hash,
            stage=STAGE_CODE,
            attempts=0,
            api_id=creds.api_id if creds else None,
            api_hash_encrypted=encrypt_session(creds.api_hash) if creds else None,
        )
        await session.commit()

    logger.info("Вход #{}: код отправлен на {}", user_id, phone)
    return LoginStep(
        stage="code", phone=phone, attempts_left=MAX_CODE_ATTEMPTS, delivery=delivery
    )


async def _resend(user_id: int, phone: str) -> LoginStep:
    """Повтор кода по кнопке «Прислать ещё раз» — через auth.ResendCode.

    Попытка та же, способ доставки следующий, счётчик опечаток сбрасывается:
    код новый, и старые ошибки к нему отношения не имеют. Отказ сервера
    (например, повтор ещё недоступен) остаётся на шаге кода — вход не рушим,
    человеку называем точный срок.
    """
    _, session_string, code_hash, _, creds = await _load_pending(user_id, STAGE_CODE)

    try:
        session_string, phone_code_hash, delivery = await manager.resend_code(
            phone=phone,
            session_string=session_string,
            phone_code_hash=code_hash,
            creds=creds,
        )
    except PhoneCodeExpiredError:
        # Попытка целиком протухла — повторять нечего, начинаем вход заново.
        await cancel(user_id)
        raise ConflictError(
            "Код устарел. Запросите новый — Telegram пришлёт его на тот же номер."
        ) from None
    except SendCodeUnavailableError:
        # Telegram не даёт другого способа доставки на этот номер (только
        # приложение, без SMS и звонка): повторять нечего, код уже в чате
        # «Telegram». Вход не рушим — остаёмся на шаге кода.
        raise ConflictError(
            "Повтор недоступен: Telegram шлёт код на этот номер только в "
            "приложение, без SMS и звонка. Ищите сообщение от «Telegram» — "
            "код уже там; новый запрос тоже придёт туда.",
            details={"stage": "code", "phone": phone,
                      "attempts_left": MAX_CODE_ATTEMPTS},
        ) from None
    except FloodWaitError as exc:
        wait = int(getattr(exc, "seconds", 0) or 0)
        human = f"{wait // 60} мин" if wait >= 60 else f"{wait} сек"
        raise ConflictError(
            f"Telegram просит подождать {human} перед повтором кода на этот номер.",
            details={"stage": "code", "phone": phone, "wait": wait,
                      "attempts_left": MAX_CODE_ATTEMPTS},
        ) from None
    except Exception as exc:  # noqa: BLE001 — текст ошибки нужен человеку
        logger.exception("Не удалось повторить код на {}", phone)
        raise ConflictError(
            f"Не удалось повторить код: {type(exc).__name__}",
            details={"stage": "code", "phone": phone,
                      "attempts_left": MAX_CODE_ATTEMPTS},
        ) from exc

    async with SessionLocal() as session:
        await repo.note_code_sent(session, phone)
        await repo.save_pending_login(
            session,
            user_id=user_id,
            phone=phone,
            session_encrypted=encrypt_session(session_string),
            phone_code_hash=phone_code_hash,
            stage=STAGE_CODE,
            attempts=0,
            api_id=creds.api_id if creds else None,
            api_hash_encrypted=encrypt_session(creds.api_hash) if creds else None,
        )
        await session.commit()

    logger.info("Вход #{}: код повторно отправлен на {}", user_id, phone)
    return LoginStep(
        stage="code", phone=phone, attempts_left=MAX_CODE_ATTEMPTS, delivery=delivery
    )


async def _load_pending(user_id: int, expected_stage: str | None = None):
    """Достаёт незавершённый вход: сессию, хэш кода и ключи попытки."""
    async with SessionLocal() as session:
        row = await repo.get_pending_login(session, user_id)
        if row is None:
            raise ConflictError(
                "Незавершённого входа нет. Начните заново: «Подключить аккаунт»."
            )
        data = (
            row.phone,
            row.session_encrypted,
            row.phone_code_hash,
            row.stage,
            int(row.attempts or 0),
            row.api_id,
            row.api_hash_encrypted,
        )

    phone, encrypted, code_hash, stage, attempts, api_id, api_hash_enc = data
    if expected_stage is not None and stage != expected_stage:
        raise ConflictError(
            "Шаг входа не тот: сервис ждёт "
            + ("код из Telegram." if stage == STAGE_CODE else "облачный пароль.")
        )
    try:
        session_string = decrypt_session(encrypted)
        api_hash = decrypt_session(api_hash_enc) if api_hash_enc else None
    except Exception:  # noqa: BLE001 — ключ сменился или строка битая
        await cancel(user_id)
        raise ConflictError(
            "Не удалось восстановить временную сессию. Начните подключение заново."
        ) from None
    creds = (
        LoginCreds(api_id=int(api_id), api_hash=api_hash)
        if api_id and api_hash
        else None
    )
    return phone, session_string, code_hash, attempts, creds


async def submit_code(user_id: int, code_raw: str) -> LoginStep:
    """Шаг 2: код из Telegram. Дальше либо 2FA, либо готово."""
    require_enabled()
    code = re.sub(r"\D", "", str(code_raw or ""))
    if not code:
        raise ValidationError("В коде только цифры — пришлите их подряд, без пробелов.")

    phone, session_string, code_hash, attempts, creds = await _load_pending(
        user_id, STAGE_CODE
    )

    try:
        session_string = await manager.sign_in_code(
            phone=phone,
            code=code,
            session_string=session_string,
            phone_code_hash=code_hash,
            creds=creds,
        )
    except PhoneCodeInvalidError:
        # Опечатку прощаем: код ещё действует, повтор ничего не стоит.
        left = MAX_CODE_ATTEMPTS - (attempts + 1)
        if left <= 0:
            await cancel(user_id)
            raise ConflictError(
                "Код не подошёл слишком много раз. Начните подключение заново — "
                "Telegram пришлёт новый код."
            ) from None
        async with SessionLocal() as session:
            await repo.bump_login_attempts(session, user_id)
            await session.commit()
        raise ValidationError(
            f"Код не подошёл. Осталось попыток: {left}.",
            # Число нужно кабинету, чтобы обновить подсказку под полем: разбирать
            # текст ошибки он не должен.
            details={"attempts_left": left},
        ) from None
    except PhoneCodeExpiredError:
        await cancel(user_id)
        raise ConflictError(
            "Код устарел. Запросите новый — Telegram пришлёт его на тот же номер."
        ) from None
    except SessionPasswordNeededError:
        # Telethon возвращает сессию и в этом случае, но полагаться на это не
        # будем: сохраняем ту, что уже есть, и переводим шаг на пароль.
        async with SessionLocal() as session:
            await repo.save_pending_login(
                session,
                user_id=user_id,
                phone=phone,
                session_encrypted=encrypt_session(session_string),
                phone_code_hash=code_hash,
                stage=STAGE_PASSWORD,
                attempts=0,
                api_id=creds.api_id if creds else None,
                api_hash_encrypted=encrypt_session(creds.api_hash) if creds else None,
            )
            await session.commit()
        return LoginStep(stage="password", phone=phone)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Вход #{}: ошибка входа по коду", user_id)
        await cancel(user_id)
        raise ConflictError(f"Ошибка входа: {type(exc).__name__}") from exc

    # Телефонный код мог оказаться достаточным, а мог потребовать 2FA — Telethon
    # сообщает об этом исключением выше. Здесь код принят полностью.
    return await _finish(user_id, phone, session_string, creds)


async def submit_password(user_id: int, password: str) -> LoginStep:
    """Шаг 3: облачный пароль 2FA."""
    require_enabled()
    if not str(password or "").strip():
        raise ValidationError("Пароль пустой.")

    phone, session_string, code_hash, _, creds = await _load_pending(
        user_id, STAGE_PASSWORD
    )

    try:
        session_string = await manager.sign_in_password(
            str(password), session_string, creds
        )
    except Exception as exc:  # noqa: BLE001 — пароль не подошёл, вход не рушим
        logger.info("Вход #{}: пароль 2FA не подошёл ({})", user_id, type(exc).__name__)
        raise ValidationError(
            f"Пароль не подошёл ({type(exc).__name__}). Попробуйте снова."
        ) from exc

    return await _finish(user_id, phone, session_string, creds)


# ───────────────────────────── Вход по QR-коду ─────────────────────────────


# Сколько живёт QR-сессия. Сканируют обычно в первую минуту; пять минут —
# с запасом на «открыть вторую камеру», дальше токен протухает сам.
QR_TTL_SECONDS = 300


@dataclass(slots=True)
class _QrWait:
    """Живая QR-сессия: клиент ждёт сканирования в фоне.

    В отличие от входа по номеру, здесь состояние обязано жить в памяти:
    Telethon-клиент в БД не положишь. Перезапуск убивает ожидание — статус
    честно скажет «начните заново», а не повиснет.
    """

    client: object | None
    url: str
    creds: LoginCreds | None
    expires_at: float
    task: asyncio.Task | None = None
    result: LoginStep | None = None
    error: str | None = None
    need_password: bool = False


_qr_sessions: dict[int, _QrWait] = {}


def _qr_image(url: str) -> str:
    """QR-код картинкой (data URI): клиент показывает как есть, без библиотек."""
    import qrcode

    buffer = io.BytesIO()
    qrcode.make(url).save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{encoded}"


async def _qr_drop(user_id: int) -> None:
    """Убирает QR-сессию: соединение закрыто, задача снята, следов нет."""
    wait = _qr_sessions.pop(user_id, None)
    if wait is None:
        return
    task, wait.task = wait.task, None
    # Себя вотчер не отменяет: он и так на выходе, а CancelledError изнутри
    # выглядел бы отменой снаружи.
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()
    client, wait.client = wait.client, None
    if client is not None:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 — закрытие не должно ронять отмену
            pass


async def qr_start(
    user_id: int, *, api_id: object = None, api_hash: object = None
) -> dict:
    """Начинает вход по QR: клиент ждёт сканирования, наружу — код картинкой.

    Один вход на человека: телефонный pending и прошлый QR умирают — иначе
    два живых клиента делят внимание. Возвращает ссылку, картинку и срок.
    """
    require_enabled()
    creds = normalize_creds(api_id, api_hash)
    await qr_cancel(user_id)
    await cancel(user_id)
    client = await manager.create_login_client(creds)
    try:
        qr = await client.qr_login()
    except Exception:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        raise
    wait = _QrWait(
        client=client,
        url=str(qr.url),
        creds=creds,
        expires_at=time.monotonic() + QR_TTL_SECONDS,
    )
    wait.task = asyncio.create_task(_qr_watch(user_id, wait, qr))
    _qr_sessions[user_id] = wait
    logger.info("Вход #{}: QR-сессия начата", user_id)
    return {
        "url": wait.url,
        "image": _qr_image(wait.url),
        "expires_in": QR_TTL_SECONDS,
    }


async def _qr_watch(user_id: int, wait: _QrWait, qr: object) -> None:
    """Фон: ждёт сканирования и доводит вход до конца.

    Результат кладёт в сессию — его забирает статус. Клиент после успеха
    отключает, но запись оставляет: иначе статус нечего будет отдать.
    """
    from telethon.errors import SessionPasswordNeededError as PasswordNeeded

    try:
        await qr.wait()  # type: ignore[union-attr]
    except PasswordNeeded:
        wait.need_password = True
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — токен протух или сеть упала
        logger.info("Вход #{}: QR-ожидание сорвалось ({})", user_id, type(exc).__name__)
        wait.error = "QR-код устарел. Начните заново — это полминуты."
        await _qr_drop(user_id)
        # Запись нужна статусу, а _qr_drop её снёс — возвращаем с ошибкой.
        _qr_sessions[user_id] = wait
        wait.client = None
        wait.task = None
        return
    try:
        me = await wait.client.get_me()  # type: ignore[union-attr]
        phone = f"+{me.phone}" if getattr(me, "phone", None) else ""
        session_string = wait.client.session.save()  # type: ignore[union-attr]
        wait.result = await _finish(user_id, phone, session_string, wait.creds)
    except Exception as exc:  # noqa: BLE001 — вход не удался, причина — наружу
        logger.info("Вход #{}: QR-вход не удался ({})", user_id, type(exc).__name__)
        wait.error = str(exc) or "Вход не удался. Попробуйте заново."
    client, wait.client = wait.client, None
    wait.task = None
    if client is not None:
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001
            pass


async def qr_status(user_id: int) -> LoginStep | dict:
    """Что с QR-входом: ждём, нужен пароль, готово или начинайте заново."""
    require_enabled()
    wait = _qr_sessions.get(user_id)
    if wait is None:
        raise ConflictError("QR-вход не начат или уже закрыт. Начните заново.")
    if wait.error is not None:
        await _qr_drop(user_id)
        raise ConflictError(wait.error)
    if wait.result is not None:
        await _qr_drop(user_id)
        return wait.result
    if wait.need_password:
        return {"stage": "password"}
    if time.monotonic() >= wait.expires_at:
        await _qr_drop(user_id)
        raise ConflictError("QR-код устарел. Начните заново — это полминуты.")
    return {"stage": "waiting"}


async def qr_password(user_id: int, password: str) -> LoginStep:
    """Облачный пароль после сканирования QR (у кого включён 2FA)."""
    require_enabled()
    if not str(password or "").strip():
        raise ValidationError("Пароль пустой.")
    wait = _qr_sessions.get(user_id)
    if wait is None or not wait.need_password or wait.client is None:
        raise ConflictError("Пароль сейчас не ждут. Начните вход заново.")
    try:
        await wait.client.sign_in(password=str(password))  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 — пароль не подошёл, сессия жива
        logger.info("Вход #{}: QR-пароль не подошёл ({})", user_id, type(exc).__name__)
        raise ValidationError(
            f"Пароль не подошёл ({type(exc).__name__}). Попробуйте снова."
        ) from exc
    try:
        me = await wait.client.get_me()  # type: ignore[union-attr]
        phone = f"+{me.phone}" if getattr(me, "phone", None) else ""
        session_string = wait.client.session.save()  # type: ignore[union-attr]
        return await _finish(user_id, phone, session_string, wait.creds)
    finally:
        await _qr_drop(user_id)


async def qr_cancel(user_id: int) -> bool:
    """Отменяет QR-вход. False — отменять было нечего."""
    if user_id not in _qr_sessions:
        return False
    await _qr_drop(user_id)
    return True


async def _finish(
    user_id: int, phone: str, session_string: str, creds: LoginCreds | None = None
) -> LoginStep:
    """Проверяет сессию, сохраняет аккаунт и поднимает клиент.

    Ключи попытки едут в строку аккаунта: сессия привязана к api_id, и
    поднимать её чужими ключами бессмысленно. Повторный вход обновляет
    и ключи — вдруг человек переехал на свои.
    """
    ok, name, error = await manager.check_session(session_string, creds)
    if not ok:
        await cancel(user_id)
        raise ConflictError(f"Аккаунт не подтверждён: {error}. Попробуйте заново.")

    async with SessionLocal() as session:
        await repo.delete_pending_login(session, user_id)
        # Повторный вход с того же номера — это замена сессии, а не второй
        # аккаунт. Иначе в БД появлялась вторая строка с тем же телефоном, и
        # сервис поднимал два клиента на одну сессию Telegram: так получают
        # AuthKeyDuplicated и теряют доступ к аккаунту вообще.
        existing = next(
            (
                item
                for item in await repo.list_accounts(session, user_id)
                if item.phone == phone
            ),
            None,
        )
        if existing is not None:
            existing.session_encrypted = encrypt_session(session_string)
            existing.is_active = True
            existing.last_error = None
            existing.api_id = creds.api_id if creds else None
            existing.api_hash_encrypted = (
                encrypt_session(creds.api_hash) if creds else None
            )
            account_id = int(existing.id)
            await session.commit()
        else:
            account = await repo.add_account(
                session,
                user_id=user_id,
                phone=phone,
                session_encrypted=encrypt_session(session_string),
                api_id=creds.api_id if creds else None,
                api_hash_encrypted=encrypt_session(creds.api_hash) if creds else None,
            )
            await session.commit()
            account_id = int(account.id)

        db_account = await session.get(TelegramAccount, account_id)
        started = False
        if db_account is not None:
            started = await manager.start_account(db_account, session_string)
            if started:
                await repo.set_account_error(session, db_account, None)
            elif not db_account.last_error:
                # Вход прошёл, а клиент не поднялся — обычно это сеть. Раньше
                # такой аккаунт тут же выключался с подписью «Не запустился»:
                # человек только что прошёл три шага входа и сразу видел
                # нерабочий аккаунт, который сервис больше не пробовал поднять.
                await repo.note_account_trouble(
                    session, db_account, "Пока не вышел на связь — пробуем снова"
                )
            await session.commit()

    await manager.refresh_rules()
    logger.info(
        "Вход #{}: аккаунт {} {} (id {}), клиент {}",
        user_id,
        phone,
        "переподключён" if existing is not None else "подключён",
        account_id,
        "поднят" if started else "не поднялся",
    )
    return LoginStep(stage="done", phone=phone, account_id=account_id, name=name)


async def retry(user_id: int, account_id: int) -> dict:
    """Ещё одна попытка поднять аккаунт — по кнопке «Попробовать снова».

    Кнопка нужна для беды, которая проходит сама: сеть отвалилась, Telegram не
    ответил, сервис поднялся раньше сети. Сервис и сам вернётся к такому
    аккаунту (см. ``_revive_loop``), но ждать до трёх минут, глядя на «офлайн»,
    незачем — человек вправе попросить сразу.
    """
    require_enabled()
    async with SessionLocal() as session:
        account = await repo.get_account(session, account_id, user_id)
        if account is None:
            raise NotFoundError("Аккаунт не найден")
        phone = account.phone

    online, error = await manager.retry_account(account_id)
    if online:
        await manager.refresh_rules()
    logger.info(
        "Повтор #{}: аккаунт {} (id {}) — {}",
        user_id,
        phone,
        account_id,
        "на связи" if online else f"не поднялся ({error})",
    )
    return {"phone": phone, "online": online, "error": None if online else error}


async def disconnect(user_id: int, account_id: int) -> str:
    """Отключает аккаунт: гасит клиент и убирает запись вместе с сессией."""
    async with SessionLocal() as session:
        account = await repo.get_account(session, account_id, user_id)
        if account is None:
            raise NotFoundError("Аккаунт не найден")
        phone = account.phone

    await manager.stop_account(account_id)
    async with SessionLocal() as session:
        account = await repo.get_account(session, account_id, user_id)
        if account is not None:
            await session.delete(account)
            await session.commit()
    await manager.refresh_rules()
    logger.info("Аккаунт {} (id {}) отключён пользователем #{}", phone, account_id, user_id)
    return phone
