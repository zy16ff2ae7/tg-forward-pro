"""Автоперевод чужих постов на язык читателя.

Задача «пересылка канала» часто забирает посты из иноязычных источников.
Поле ``translate_to`` у задачи включает перевод каждого поста перед
публикацией. Переводчик — бесплатная точка Google без ключа: её хватает на
поток постов, а если она недоступна, пост уходит как есть — с пометкой в
логе, а не дыркой в канале. Ретраи не делаем специально: пересылка ждёт
перевода, и каждая лишняя секунда — задержка поста у читателя.
"""
from __future__ import annotations

import re

import aiohttp
from loguru import logger

from app.errors import ValidationError

LANG_RE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")
_ENDPOINT = "https://translate.googleapis.com/translate_a/single"
# Посты длиннее в Telegram не отправишь, а переводчику лишнее ни к чему.
_TEXT_CAP = 4000


def normalize_lang(raw: object) -> str:
    """Код языка из формы — в каноничный вид (``zh-cn`` → ``zh-CN``).

    Пустая строка — честное «не переводить». Мусор отклоняется: молча
    выключенный перевод выглядит как сломанная задача.
    """
    code = str(raw or "").strip()
    if not code:
        return ""
    lang, dash, region = code.partition("-")
    code = lang.lower() + (dash + region.upper() if dash else "")
    if not LANG_RE.match(code):
        raise ValidationError("Язык перевода — кодом: ru, en, uk, zh-CN")
    return code


async def translate_text(
    text: str, target: str, *, source: str = "auto", timeout: float = 8
) -> str:
    """Переводит текст. Любая неудача — исключением: вызывающий решает сам."""
    params = {"client": "gtx", "sl": source, "tl": target, "dt": "t"}
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout)
    ) as session:
        async with session.post(
            _ENDPOINT, params=params, data={"q": text[:_TEXT_CAP]}
        ) as response:
            response.raise_for_status()
            data = await response.json()
    sentences = data[0] if isinstance(data, list) and data else []
    result = "".join(seg[0] for seg in sentences if seg and seg[0]).strip()
    if not result:
        raise RuntimeError("переводчик вернул пусто")
    return result


async def maybe_translate(text: str, target: str) -> str:
    """Перевод с запасным выходом: не вышло — возвращается исходник."""
    if not text or not target:
        return text
    try:
        return await translate_text(text, target)
    except Exception as exc:  # noqa: BLE001 — перевод не должен ронять пост
        logger.warning("Перевод на {} не удался ({}): пост уйдёт как есть", target, exc)
        return text
