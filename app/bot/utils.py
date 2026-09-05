"""Мелкие помощники для хендлеров."""
from __future__ import annotations

from pathlib import Path

from aiogram.types import CallbackQuery, FSInputFile, Message

from app.config import settings
from app.db.database import SessionLocal
from app.db import repo
from app.db.models import User

# Предел Telegram на подпись к медиа. Текст длиннее он не обрезает, а отвечает
# отказом на sendPhoto — «message caption is too long».
CAPTION_LIMIT = 1024


async def ensure_user(
    event: Message | CallbackQuery, grant_trial: bool = True
) -> User:
    """Регистрирует пользователя при первом заходе, выдаёт пробный период."""
    source = event if isinstance(event, Message) else event.message
    from_user = event.from_user
    assert source is not None and from_user is not None

    async with SessionLocal() as session:
        user, created = await repo.get_or_create_user(
            session,
            user_id=from_user.id,
            username=from_user.username,
            full_name=from_user.full_name,
        )
        if created and grant_trial:
            await repo.grant_trial(session, user.id)
        await session.commit()
        # перечитываем, чтобы получить связанные объекты в той же сессии
        fresh = await repo.get_user(session, from_user.id)
        assert fresh is not None
        return fresh


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


async def smart_edit(message: Message, text: str, reply_markup=None, **kwargs):
    """Правит сообщение меню — неважно, текст это или подпись к медиа.

    С тех пор как /start отправляет баннер-картинку, у сообщения с меню есть
    только caption: обычный edit_text падает с «there is no text in the message
    to edit», и кнопка молча перестаёт работать — пользователь видит, что ничего
    не происходит. Здесь сначала выбирается подходящий способ правки, а если
    Telegram её не разрешает (например, текст длиннее лимита подписи) —
    уходит новое сообщение, и интерфейс не зависает.
    """
    from aiogram.exceptions import TelegramBadRequest

    async def _fallback() -> Message:
        return await message.answer(text, reply_markup=reply_markup, **kwargs)

    if message.photo or message.document or message.video or message.animation:
        if len(text) <= 1024:  # лимит Telegram на подпись к медиа
            try:
                return await message.edit_caption(
                    caption=text, reply_markup=reply_markup, **kwargs
                )
            except TelegramBadRequest as exc:
                if "not modified" in (exc.message or "").lower():
                    return False
                return await _fallback()
        return await _fallback()

    try:
        return await message.edit_text(text=text, reply_markup=reply_markup, **kwargs)
    except TelegramBadRequest as exc:
        if "not modified" in (exc.message or "").lower():
            return False
        return await _fallback()


async def answer_with_banner(
    message: Message, photo: Path, text: str, reply_markup=None, **kwargs
) -> Message:
    """Баннер с текстом: подписью, пока влезает, иначе картинка и текст отдельно.

    Подпись длиннее 1024 символов Telegram не обрезает — он отказывает во всём
    сообщении. На /start это значило, что человек в ответ не получал ничего: ни
    картинки, ни меню, ни объяснения. Приветствие доросло до 1090 символов, и
    вход в бота перестал работать — в журнале копились «message caption is too
    long». Теперь длинный текст уходит вторым сообщением, а меню — вместе с
    текстом: его же потом правит smart_edit.
    """
    if not photo.exists():
        return await message.answer(text, reply_markup=reply_markup, **kwargs)
    if len(text) <= CAPTION_LIMIT:
        return await message.answer_photo(
            FSInputFile(photo), caption=text, reply_markup=reply_markup, **kwargs
        )
    await message.answer_photo(FSInputFile(photo))
    return await message.answer(text, reply_markup=reply_markup, **kwargs)


def parse_callback(data: str, prefix: str) -> list[str]:
    """Разбирает callback_data вида 'rule:open:12' → ['rule', 'open', '12']."""
    data = data or ""
    if not data.startswith(prefix):
        return []
    return data.split(":")


async def subscription_gate(event: Message | CallbackQuery) -> bool:
    """Проверяет абонемент. Если его нет — шлёт подсказку и возвращает False."""
    message = event if isinstance(event, Message) else event.message
    assert message is not None and event.from_user is not None

    async with SessionLocal() as session:
        active = await repo.has_active_subscription(session, event.from_user.id)

    if active:
        return True

    text = (
        "⛔️ Абонемент не активен.\n\n"
        "Пока он не оплачен, пересылка стоит на паузе. Оплатить можно в разделе «Подписка»."
    )
    from app.bot.keyboards import payment_menu

    if isinstance(event, CallbackQuery):
        await event.answer()
        await message.answer(text, reply_markup=payment_menu(event.from_user.id))
    else:
        await message.answer(text, reply_markup=payment_menu(event.from_user.id))
    return False
