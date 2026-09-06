"""Пути к локальным медиа-файлам, которые бот отправляет как иллюстрации."""
from __future__ import annotations

from pathlib import Path

from app.config import BASE_DIR

# Папка assets лежит в корне проекта и попадает в git. Основной баннер —
# welcome.jpg (ДОЧА в неоне); `python scripts/make_banner.py` рисует кодом
# запасной welcome-code.png на случай, если фото потеряется.
ASSETS_DIR: Path = BASE_DIR / "assets"

# Приветственный баннер — ДОЧА в розовом неоне.
WELCOME_PHOTO: Path = ASSETS_DIR / "welcome.jpg"
