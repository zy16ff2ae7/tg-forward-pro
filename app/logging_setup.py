"""Настройка логирования.

Вызывается один раз из точки входа. Раньше настройка жила на уровне модуля
в main.py и срабатывала просто от импорта — это мешало тестам и любому
повторному импорту.
"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from app.fsperms import SECRET_DIR_MODE, harden, private_opener

LEVELS = ("TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """Пишет в stderr и в файл с ротацией. Уровень приводится к известному."""
    resolved = (level or "INFO").upper().strip()
    if resolved not in LEVELS:
        resolved = "INFO"

    logger.remove()
    logger.add(sys.stderr, level=resolved)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        # В логах отладочного уровня видны номера телефонов и ошибки Telethon —
        # читать их должен только владелец сервиса.
        harden(log_dir, SECRET_DIR_MODE)
        log_file = log_dir / "app.log"
        harden(log_file)
        logger.add(
            log_file,
            level=resolved,
            rotation="10 MB",
            retention="14 days",
            compression="zip",
            # Каждый файл после ротации тоже создаётся с правами 600.
            opener=private_opener,
        )
