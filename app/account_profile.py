"""Validated, explicit changes to a user's Telegram profile."""
from __future__ import annotations

import base64
import binascii
import io
from datetime import date, datetime, timezone
from typing import Any

from PIL import Image, UnidentifiedImageError
from telethon.errors import FloodWaitError, RPCError
from telethon.tl import functions, types

from app.errors import AppError, ValidationError

MAX_PHOTO_BYTES = 512 * 1024


def validate_changes(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict) or not body:
        raise ValidationError("Укажите изменения профиля")
    if set(body) - {"first_name", "last_name", "about", "birthday", "photo"}:
        raise ValidationError("Неизвестное поле профиля")
    changes = dict(body)
    for key, limit in (("first_name", 64), ("last_name", 64), ("about", 70)):
        if key in changes:
            value = changes[key]
            if not isinstance(value, str) or len(value) > limit:
                raise ValidationError(f"Поле {key}: максимум {limit} символов")
            if key == "first_name" and not value.strip():
                raise ValidationError("Имя не может быть пустым")
    if "birthday" in changes:
        value = changes["birthday"]
        if value is not None:
            if not isinstance(value, dict) or set(value) - {"day", "month", "year"}:
                raise ValidationError("Неверная дата рождения")
            try:
                day, month, year = value["day"], value["month"], value.get("year")
                if any(type(n) is not int for n in (day, month)) or (year is not None and type(year) is not int):
                    raise ValueError
                birthday = date(year if year is not None else 2000, month, day)
                if year is not None and (birthday > date.today() or year < date.today().year - 150):
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise ValidationError("Проверьте дату рождения; год можно не указывать") from None
    if "photo" in changes:
        value = changes["photo"]
        if not isinstance(value, str) or len(value) > MAX_PHOTO_BYTES * 4 // 3 + 4:
            raise ValidationError("Фото: JPEG или PNG до 512 КБ")
        try:
            raw = base64.b64decode(value, validate=True)
            if not raw or len(raw) > MAX_PHOTO_BYTES:
                raise ValueError
            with Image.open(io.BytesIO(raw)) as img:
                if img.format not in ("JPEG", "PNG") or max(img.size) > 4096 or min(img.size) < 160:
                    raise ValueError
                img.verify()
            with Image.open(io.BytesIO(raw)) as img:
                photo = io.BytesIO()
                img.convert("RGB").save(photo, "JPEG", quality=90)
                photo.name = "profile.jpg"
                photo.seek(0)
                changes["photo"] = photo
        except (ValueError, binascii.Error, OSError, UnidentifiedImageError, Image.DecompressionBombError):
            raise ValidationError("Фото: JPEG/PNG, от 160 до 4096 пикселей, до 512 КБ") from None
    return changes


def telegram_error(exc: Exception) -> AppError:
    code = type(exc).__name__
    if isinstance(exc, FloodWaitError):
        seconds = max(1, int(exc.seconds))
        return AppError(f"Telegram просит подождать {seconds} сек", status=429,
                        details={"code": code, "retry_at": datetime.fromtimestamp(
                            datetime.now(timezone.utc).timestamp() + seconds, timezone.utc).isoformat()})
    hints = {
        "AboutTooLongError": "Описание слишком длинное для этого аккаунта",
        "BirthdayInvalidError": "Telegram отклонил дату рождения",
        "PhotoInvalidDimensionsError": "Telegram отклонил размеры фото",
        "ImageProcessFailedError": "Telegram не смог обработать фото",
        "PeerFloodError": "Telegram ограничил аккаунт. Проверьте статус в @SpamBot",
    }
    return AppError(hints.get(code, "Telegram отклонил изменение. Обновите профиль и повторите."),
                    status=409, details={"code": code})


async def read_profile(client: Any) -> dict[str, Any]:
    try:
        full = await client(functions.users.GetFullUserRequest(types.InputUserSelf()), flood_sleep_threshold=0)
        user = full.users[0]
        birthday = getattr(full.full_user, "birthday", None)
        return {"first_name": user.first_name or "", "last_name": user.last_name or "",
                "about": full.full_user.about or "", "has_photo": bool(user.photo),
                "birthday": {"day": birthday.day, "month": birthday.month, "year": birthday.year} if birthday else None}
    except RPCError as exc:
        raise telegram_error(exc) from None


async def update_profile(client: Any, changes: dict[str, Any]) -> dict[str, Any]:
    # Each Telegram operation is independent; report partial success explicitly.
    applied = []
    try:
        profile = {k: changes[k] for k in ("first_name", "last_name", "about") if k in changes}
        if profile:
            await client(functions.account.UpdateProfileRequest(**profile), flood_sleep_threshold=0)
            applied.extend(profile)
        if "birthday" in changes:
            birthday = changes["birthday"]
            await client(functions.account.UpdateBirthdayRequest(
                birthday=types.Birthday(**birthday) if birthday is not None else None), flood_sleep_threshold=0)
            applied.append("birthday")
        if "photo" in changes:
            uploaded = await client.upload_file(changes["photo"])
            await client(functions.photos.UploadProfilePhotoRequest(file=uploaded), flood_sleep_threshold=0)
            applied.append("photo")
    except (RPCError, OSError, TimeoutError) as exc:
        error = telegram_error(exc)
        error.details["applied"] = applied
        if applied:
            labels = {"first_name": "имя", "last_name": "фамилия", "about": "описание", "birthday": "дата рождения", "photo": "фото"}
            error.message = "Часть изменений сохранена (" + ", ".join(labels[k] for k in applied) + "). " + error.message
        raise error from None
    return {"applied": applied}
