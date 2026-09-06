"""Единая обработка ошибок для всех трёх входов в сервис.

Ошибки одного вида ловятся в одном месте: HTTP-mini-апп, хендлеры бота и
фоновые задачи теперь не изобретают каждый свой формат ответа.
"""
from __future__ import annotations

import json
from typing import Any

from aiohttp import web
from aiogram import Bot
from aiogram.types import ErrorEvent
from loguru import logger


class AppError(Exception):
    """Базовая ошибка приложения.

    ``message`` — текст, который можно показать пользователю: он не содержит
    путей, SQL и содержимого секретов.
    """

    status = 500

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        # details — машиночитаемая часть отказа (например, остаток попыток кода).
        # Клиенту нельзя разбирать текст сообщения, чтобы достать из него число.
        self.details = dict(details or {})
        if status is not None:
            self.status = status


class ValidationError(AppError):
    """Некорректный ввод пользователя."""

    status = 400


class NotFoundError(AppError):
    """Объект не найден или не принадлежит пользователю."""

    status = 404


class ConflictError(AppError):
    """Действие невозможно в текущем состоянии объекта."""

    status = 409


class FeatureUnavailable(AppError):
    """Функция отключена настройками (например, не подключён MTProto-шлюз)."""

    status = 503

    def __init__(self, message: str, *, feature: str = "", status: str = "") -> None:
        super().__init__(message)
        self.feature = feature
        self.feature_status = status


def _json_error(payload: dict[str, Any], status: int) -> web.Response:
    return web.json_response(payload, status=status, dumps=_dumps)


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def error_payload(error: AppError) -> dict[str, Any]:
    """Тело JSON-ответа для известной ошибки."""
    # Ключ error ставим последним: details не должны его переопределить.
    payload: dict[str, Any] = {**error.details, "error": error.message}
    if isinstance(error, FeatureUnavailable):
        payload["feature"] = error.feature
        payload["status"] = error.feature_status
    return payload


@web.middleware
async def http_error_middleware(request: web.Request, handler):
    """Отдаёт ошибки мини-аппа в JSON, а не HTML-трассировкой aiohttp.

    Редиректы и прочие HTTP-исключения пропускаются: это нормальный поток
    управления, а не сбой.
    """
    try:
        return await handler(request)
    except web.HTTPException:
        # редиректы и 404 — нормальный поток управления, а не сбой
        raise
    except AppError as exc:
        return _json_error(error_payload(exc), exc.status)
    except Exception:
        logger.exception("Необработанная ошибка в {} {}", request.method, request.path)
        return _json_error({"error": "Внутренняя ошибка сервера"}, 500)


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    """Базовая гигиена HTTP-ответов мини-аппа.

    X-Frame-Options специально НЕ ставим: мини-апп живёт во фрейме Telegram,
    и запрет фреймов его сломает. Фрейминг снаружи закрывается проверкой
    initData на каждом запросе.
    """
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


async def on_bot_error(event: ErrorEvent, bot: Bot | None = None) -> bool:
    """Глобальный перехватчик ошибок aiogram.

    Без него исключение в хендлере просто падало в логи, а пользователь видел
    мёртвую кнопку. Здесь ошибка логируется, а пользователю уходит понятное
    сообщение — насколько это возможно.
    """
    exception = getattr(event, "exception", None)
    logger.exception("Ошибка в обработчике {}: {}", type(event.update).__name__, exception)

    message = getattr(getattr(event, "update", None), "message", None)
    callback = getattr(getattr(event, "update", None), "callback_query", None)
    target = message or (callback.message if callback else None)

    if target is None:
        return True

    text = (
        exception.message
        if isinstance(exception, AppError)
        else "Не получилось обработать запрос. Попробуйте ещё раз или зайдите в кабинет заново."
    )
    try:
        if callback is not None:
            await callback.answer(text, show_alert=True)
        else:
            await target.answer(text)
    except Exception:  # noqa: BLE001 — отвечать некуда, ошибка уже залогирована
        logger.debug("Не удалось отправить пользователю сообщение об ошибке")
    return True
