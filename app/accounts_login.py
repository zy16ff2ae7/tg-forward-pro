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

import re
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
    SessionPasswordNeededError,
)

from app.config import settings
from app.db import repo
from app.db.database import SessionLocal
from app.db.models import TelegramAccount
from app.errors import ConflictError, FeatureUnavailable, NotFoundError, ValidationError
from app.security import decrypt_session, encrypt_session
from app.telegram_client.manager import manager
from app.timeutil import utcnow

PHONE_RE = re.compile(r"^\+?\d{10,15}$")

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
    """

    stage: str
    phone: str
    account_id: int | None = None
    name: str | None = None
    attempts_left: int | None = None

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
    async with SessionLocal() as session:
        row = await repo.get_pending_login(session, user_id)
        if row is None:
            return False
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


async def start(user_id: int, phone_raw: str) -> LoginStep:
    """Шаг 1: просит Telegram выслать код на номер.

    Уже начатый вход на тот же номер не начинаем заново: код действует, и
    второй запрос только приблизит флуд-лимит на номере.
    """
    require_enabled()
    phone = normalize_phone(phone_raw)

    async with SessionLocal() as session:
        row = await repo.get_pending_login(session, user_id)
        pending_here = row is not None and row.phone == phone
        wait = await _pause_left(session, phone, row if pending_here else None)
        if wait:
            raise _too_early(phone, wait, row if pending_here else None)

    try:
        session_string, phone_code_hash = await manager.send_code(phone)
    except PhoneNumberInvalidError:
        raise ValidationError("Telegram не знает такой номер. Проверьте и введите заново.") from None
    except PhoneNumberBannedError:
        raise ValidationError("Этот номер заблокирован в Telegram. Подключите другой.") from None
    except ApiIdPublishedFloodError:
        # Отказ не человеку, а сервису: ключи api_id/api_hash взяты из
        # официального клиента, и Telegram не даёт входить по опубликованной
        # паре. Номер тут ни при чём, менять его бессмысленно.
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
        )
        await session.commit()

    logger.info("Вход #{}: код отправлен на {}", user_id, phone)
    return LoginStep(stage="code", phone=phone, attempts_left=MAX_CODE_ATTEMPTS)


async def _load_pending(user_id: int, expected_stage: str | None = None):
    """Достаёт незавершённый вход и расшифровывает временную сессию."""
    async with SessionLocal() as session:
        row = await repo.get_pending_login(session, user_id)
        if row is None:
            raise ConflictError(
                "Незавершённого входа нет. Начните заново: «Подключить аккаунт»."
            )
        data = (row.phone, row.session_encrypted, row.phone_code_hash, row.stage, int(row.attempts or 0))

    phone, encrypted, code_hash, stage, attempts = data
    if expected_stage is not None and stage != expected_stage:
        raise ConflictError(
            "Шаг входа не тот: сервис ждёт "
            + ("код из Telegram." if stage == STAGE_CODE else "облачный пароль.")
        )
    try:
        session_string = decrypt_session(encrypted)
    except Exception:  # noqa: BLE001 — ключ сменился или строка битая
        await cancel(user_id)
        raise ConflictError(
            "Не удалось восстановить временную сессию. Начните подключение заново."
        ) from None
    return phone, session_string, code_hash, attempts


async def submit_code(user_id: int, code_raw: str) -> LoginStep:
    """Шаг 2: код из Telegram. Дальше либо 2FA, либо готово."""
    require_enabled()
    code = re.sub(r"\D", "", str(code_raw or ""))
    if not code:
        raise ValidationError("В коде только цифры — пришлите их подряд, без пробелов.")

    phone, session_string, code_hash, attempts = await _load_pending(user_id, STAGE_CODE)

    try:
        session_string = await manager.sign_in_code(
            phone=phone, code=code, session_string=session_string, phone_code_hash=code_hash
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
            )
            await session.commit()
        return LoginStep(stage="password", phone=phone)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Вход #{}: ошибка входа по коду", user_id)
        await cancel(user_id)
        raise ConflictError(f"Ошибка входа: {type(exc).__name__}") from exc

    # Телефонный код мог оказаться достаточным, а мог потребовать 2FA — Telethon
    # сообщает об этом исключением выше. Здесь код принят полностью.
    return await _finish(user_id, phone, session_string)


async def submit_password(user_id: int, password: str) -> LoginStep:
    """Шаг 3: облачный пароль 2FA."""
    require_enabled()
    if not str(password or "").strip():
        raise ValidationError("Пароль пустой.")

    phone, session_string, code_hash, _ = await _load_pending(user_id, STAGE_PASSWORD)

    try:
        session_string = await manager.sign_in_password(str(password), session_string)
    except Exception as exc:  # noqa: BLE001 — пароль не подошёл, вход не рушим
        logger.info("Вход #{}: пароль 2FA не подошёл ({})", user_id, type(exc).__name__)
        raise ValidationError(
            f"Пароль не подошёл ({type(exc).__name__}). Попробуйте снова."
        ) from exc

    return await _finish(user_id, phone, session_string)


async def _finish(user_id: int, phone: str, session_string: str) -> LoginStep:
    """Проверяет сессию, сохраняет аккаунт и поднимает клиент."""
    ok, name, error = await manager.check_session(session_string)
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
            account_id = int(existing.id)
            await session.commit()
        else:
            account = await repo.add_account(
                session,
                user_id=user_id,
                phone=phone,
                session_encrypted=encrypt_session(session_string),
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
