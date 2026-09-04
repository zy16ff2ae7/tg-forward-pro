"""Метка сборки мини-аппа — ею ломается кэш Telegram WebView.

Telegram Desktop и мобильные клиенты держат `styles.css` и `app.js` в своём
кэше и после выката не спрашивают сервер о свежести: пользователь открывает
кабинет и видит старую вёрстку, хотя на диске сервера уже новая. Ответы
статики раньше уходили вообще без `Cache-Control`, и клиент выбирал время
жизни сам (Chromium — эвристикой от `Last-Modified`).

Лечим адресами: ссылки на css/js получают `?v=<метка>`, а метка считается по
**содержимому** файлов. Одинаковые файлы — одинаковая метка, лишних промахов
кэша нет; любая правка меняет адрес, и клиенту приходится качать заново.
"""
from __future__ import annotations

from hashlib import blake2b
from pathlib import Path
import re

# Файлы, из которых складывается метка: те, что клиент кэширует.
ASSETS = ("index.html", "styles.css", "app.js")

# Относительные ссылки на css/js. Двоеточие в пути исключено, поэтому
# https://telegram.org/js/telegram-web-app.js остаётся как есть; «?» и «#» —
# чтобы не приписать метку второй раз.
_ASSET_REF = re.compile(r'(?P<attr>href|src)="(?P<path>[^":?#]+\.(?:css|js))"')

# Ключ — папка мини-аппа, значение — (подпись файлов, метка). Пересчитываем
# хеш только когда у файлов изменились mtime или размер.
_cache: dict[str, tuple[tuple, str]] = {}


def _signature(directory: Path) -> tuple:
    """Быстрый снимок состояния файлов: по нему решаем, нужен ли пересчёт."""
    snapshot = []
    for name in ASSETS:
        try:
            stat = (directory / name).stat()
        except OSError:
            continue
        snapshot.append((name, stat.st_mtime_ns, stat.st_size))
    return tuple(snapshot)


def build_stamp(directory: Path) -> str:
    """Метка содержимого мини-аппа. Пустая строка, если файлов нет."""
    signature = _signature(directory)
    if not signature:
        return ""

    key = str(directory)
    cached = _cache.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]

    digest = blake2b(digest_size=6)
    for name, mtime_ns, size in signature:
        digest.update(name.encode("utf-8"))
        try:
            digest.update((directory / name).read_bytes())
        except OSError:
            # Файл исчез между stat и чтением — берём хотя бы его размеры,
            # чтобы метка осталась определённой и не совпала со старой.
            digest.update(f"{mtime_ns}:{size}".encode("utf-8"))
    stamp = digest.hexdigest()
    _cache[key] = (signature, stamp)
    return stamp


def add_version(html: str, stamp: str) -> str:
    """Дописывает `?v=<метка>` к относительным ссылкам на css/js в HTML."""
    if not stamp:
        return html
    return _ASSET_REF.sub(
        lambda m: '{attr}="{path}?v={stamp}"'.format(
            attr=m.group("attr"), path=m.group("path"), stamp=stamp
        ),
        html,
    )


def cache_control_for(has_version: bool) -> str:
    """Какой `Cache-Control` ставить статике мини-аппа.

    Адрес с меткой указывает на неизменяемое содержимое — его можно держать в
    кэше сколько угодно. Без метки клиент обязан переспросить сервер: ETag
    вернёт 304, и трафика это почти не стоит, зато старая вёрстка не залипает.
    """
    if has_version:
        return "public, max-age=31536000, immutable"
    return "no-cache"
