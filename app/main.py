"""Точка входа: бот + пул Telegram-аккаунтов + фоновые задачи."""
from __future__ import annotations

import asyncio
from contextlib import suppress

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
from app.fsperms import harden_runtime_files
from app.logging_setup import setup_logging
from app.payments import crypto, yookassa
from app.telegram_client.manager import manager

# Как часто фоновый цикл проверяет платежи и подписки
BACKGROUND_INTERVAL_SECONDS = 300

# Ссылку на фоновую задачу держим, чтобы остановить её на выходе. Без ссылки
# задачу может собрать сборщик мусора, а на shutdown она продолжала бы писать
# в закрытую БД.
_background_task: asyncio.Task | None = None


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
                reply_markup=payment_menu(user_id),
            )
        except Exception:  # noqa: BLE001
            logger.debug("Не смогли напомнить пользователю {}", user_id)


async def trim_logs(_bot: Bot) -> None:
    """Подрезает журнал пересылок и убирает строки удалённых задач.

    Раньше не чистился никем: рассылка по сотне чатов пишет по строке на каждую
    отправку, и за месяцы таблица становилась самой большой в базе, хотя нужна
    только для ответа «работает ли задача и на чём сломалась».

    Заодно уходят находки и записи журнала задач, которых больше нет: SQLite
    отдаёт номер удалённой задачи следующей созданной, и она получала вместе с
    ним чужой сбой на карточке. Новые удаления чистят за собой сами, этот
    проход лечит базы, где задачи удаляли раньше.
    """
    async with SessionLocal() as session:
        dropped = await repo.trim_forward_logs(session)
        orphans = await repo.drop_orphan_records(session)
        await session.commit()
    if dropped:
        logger.info("Журнал пересылок: убрано старых записей — {}", dropped)
    if orphans:
        logger.info(
            "Убраны следы удалённых задач: {}",
            ", ".join(f"{table} — {count}" for table, count in orphans.items()),
        )


async def run_background_checks(bot: Bot) -> None:
    """Прогоняет фоновые проверки по очереди, независимо друг от друга.

    Раньше все три стояли под одним ``try``: недоступность TronGrid означала,
    что в этом проходе не зачтётся и оплата картой, и напоминания не уйдут.
    Человек заплатил картой, а доступа нет — из-за чужого провайдера. Поэтому
    каждая проверка отвечает только за себя.
    """
    checks = (
        ("USDT", crypto.check_pending),
        ("ЮKassa", yookassa.check_pending),
        ("напоминания о продлении", notify_expiring),
        ("уборка базы", trim_logs),
    )
    for name, check in checks:
        try:
            await check(bot)
        except asyncio.CancelledError:
            # Остановка сервиса — не ошибка проверки: пробрасываем дальше,
            # иначе цикл не остановить.
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Фоновая проверка «{}» не прошла: {}", name, exc)


async def background_loop(bot: Bot) -> None:
    """Фоновые дела: оплата USDT и картой, напоминания о продлении, уборка базы.

    Первый проход — сразу, без ожидания: пока сервис перезапускался, перевод
    мог уже прийти, и заставлять человека ждать пять минут не за что.

    Карту проверяем здесь же, а не только кнопкой «Я оплатил» в боте: на внешней
    странице оплаты такой кнопки нет — человек уходит на страницу банка и
    обратно может не вернуться, а доступ всё равно должен включиться.
    """
    while True:
        await run_background_checks(bot)
        await asyncio.sleep(BACKGROUND_INTERVAL_SECONDS)


async def stop_background_loop() -> None:
    """Останавливает фоновый цикл и дожидается его выхода."""
    global _background_task
    task, _background_task = _background_task, None
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def on_startup(bot: Bot) -> None:
    global _background_task

    await init_db()
    logger.info("БД готова: {}", settings.database_url)

    await manager.start_all()
    manager.start_periodic_refresh(interval=60)
    _background_task = asyncio.create_task(background_loop(bot), name="background-loop")

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
    # Сначала фон, потом клиенты и БД: иначе цикл успеет обратиться к
    # закрытому движку и напишет в лог ошибку на пустом месте.
    await stop_background_loop()
    await manager.stop_all()
    await dispose_db()


async def main() -> None:
    # Логи настраиваем до первого вызова логгера, ошибки конфигурации —
    # до всего остального: невалидный ключ лучше увидеть сразу, а не на первой пересылке.
    setup_logging(settings.log_level, BASE_DIR / "logs")
    settings.require()
    # Права на .env, базу и логи чиним сами: файл с токеном не должен читаться
    # всей машиной, а забыть про chmod после деплоя слишком легко.
    fixed = harden_runtime_files(BASE_DIR)
    if fixed:
        logger.info("Права ужаты до 600/700: {}", ", ".join(fixed))
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
