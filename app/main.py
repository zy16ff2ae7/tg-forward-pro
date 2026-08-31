"""Точка входа: бот + пул Telegram-аккаунтов + фоновые задачи."""
from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import MenuButtonWebApp, WebAppInfo
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from loguru import logger

from app.bot.handlers import accounts, admin, menu, rules, subscription
from app.webapp_api import setup_webapp_routes
from app.config import BASE_DIR, settings
from app.db import repo
from app.db.database import SessionLocal, dispose_db, init_db
from app.errors import http_error_middleware, on_bot_error
from app.logging_setup import setup_logging
from app.payments import crypto
from app.telegram_client.manager import manager


async def notify_expiring(bot: Bot) -> None:
    """Напоминает о скором окончании абонемента."""
    async with SessionLocal() as session:
        subs = list(await repo.expiring_soon(session))
        user_ids = [sub.user_id for sub in subs]
        for user_id in user_ids:
            await repo.mark_reminded(session, user_id)
        await session.commit()

    from app.bot.keyboards import payment_menu

    for user_id in user_ids:
        try:
            await bot.send_message(
                user_id,
                "⏳ Абонемент заканчивается.\n\n"
                "Чтобы пересылка не остановилась, продлите его — это займёт минуту.",
                reply_markup=payment_menu(),
            )
        except Exception:  # noqa: BLE001
            logger.debug("Не смогли напомнить пользователю {}", user_id)


async def background_loop(bot: Bot) -> None:
    """Фоновые проверки: оплата USDT и напоминания о продлении."""
    while True:
        try:
            await asyncio.sleep(300)  # раз в 5 минут
            await crypto.check_pending(bot)
            await notify_expiring(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ошибка в фоновом цикле: {}", exc)


async def on_startup(bot: Bot) -> None:
    await init_db()
    logger.info("БД готова: {}", settings.database_url)

    await manager.start_all()
    manager.start_periodic_refresh(interval=60)
    asyncio.create_task(background_loop(bot))

    if settings.use_webhook:
        await bot.set_webhook(
            settings.public_url,
            secret_token=settings.webhook_secret,
            drop_pending_updates=False,
        )
        logger.info("Вебхук установлен: {}", settings.public_url)
    else:
        logger.info("Работаем на long polling")

    # Кнопка мини-аппа в меню бота (по умолчанию для всех пользователей)
    mini_url = settings.mini_app_url
    if mini_url:
        try:
            await bot.set_chat_menu_button(
                menu_button=MenuButtonWebApp(
                    text="Открыть кабинет", web_app=WebAppInfo(url=mini_url)
                )
            )
            logger.info("Кнопка мини-аппа установлена: {}", mini_url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось установить кнопку мини-аппа: {}", exc)
    else:
        logger.info(
            "Мини-апп доступен по /app, но для кнопки задайте WEBAPP_URL или WEBHOOK_URL"
        )


async def on_shutdown(bot: Bot) -> None:
    logger.info("Останавливаемся…")
    if settings.use_webhook:
        await bot.delete_webhook()
    await manager.stop_all()
    await dispose_db()


async def main() -> None:
    # Логи настраиваем до первого вызова логгера, ошибки конфигурации —
    # до всего остального: невалидный ключ лучше увидеть сразу, а не на первой пересылке.
    setup_logging(settings.log_level, BASE_DIR / "logs")
    settings.require()
    for warning in settings.warnings():
        logger.warning("{}", warning)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_routers(
        menu.router,
        accounts.router,
        rules.router,
        subscription.router,
        admin.router,
    )
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    # Без этого исключение в хендлере уходило только в лог, а пользователь
    # оставался с мёртвой кнопкой и без объяснения.
    dp.errors.register(on_bot_error)

    # HTTP-сервер поднимаем всегда: он раздаёт мини-апп и API,
    # а в режиме вебхука — ещё и принимает обновления Telegram.
    app = web.Application(middlewares=[http_error_middleware])
    if settings.use_webhook:
        SimpleRequestHandler(
            dispatcher=dp, bot=bot, secret_token=settings.webhook_secret
        ).register(app, path=settings.webhook_path)
        setup_application(app, dp, bot=bot)

    setup_webapp_routes(app, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)
    await site.start()
    logger.info(
        "HTTP-сервер слушает {}:{} — мини-апп: http://{}:{}/app/",
        settings.host,
        settings.port,
        settings.host,
        settings.port,
    )

    try:
        if settings.use_webhook:
            await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено пользователем")
