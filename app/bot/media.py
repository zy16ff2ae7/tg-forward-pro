"""Пути к локальным медиа-файлам, которые бот отправляет как иллюстрации."""
from __future__ import annotations

from pathlib import Path

from app.config import BASE_DIR

# Папка assets лежит в корне проекта и попадает в git. Здесь живут
# сгенерированные баннеры: `python scripts/make_banner.py` создаёт welcome.png.
ASSETS_DIR: Path = BASE_DIR / "assets"

# Приветственный баннер — арт-деко в зелёных тонах.
WELCOME_PHOTO: Path = ASSETS_DIR / "welcome.png"
