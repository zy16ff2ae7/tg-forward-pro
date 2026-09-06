#!/usr/bin/env python3
"""Получение API_ID / API_HASH через официальный my.telegram.org.

Зачем этот скрипт: веб-форма my.telegram.org часто молча отказывает и не показывает
причину. Здесь тот же официальный flow, но с печатым ответом сервера — видно точный
текст ошибки. Плюс, если приложение на аккаунте уже создано, скрипт просто читает
готовые api_id/api_hash (это решает проблему в один заход).

Запуск:  python scripts/get_api_credentials.py
"""
from __future__ import annotations

import http.cookiejar
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://my.telegram.org"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
DUMP = Path("/tmp/my_telegram_apps.html")

API_ID_RE = re.compile(r"\b(\d{6,9})\b")
API_HASH_RE = re.compile(r"\b([0-9a-f]{32})\b")


class Session:
    """Мини-клиент: куки + печать сырых ответов сервера."""

    def __init__(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def call(
        self,
        path: str,
        data: dict[str, str] | None = None,
        referer: str | None = None,
    ) -> tuple[int, str]:
        body = urllib.parse.urlencode(data).encode() if data else None
        req = urllib.request.Request(BASE + path, data=body)
        req.add_header("User-Agent", UA)
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("Accept-Language", "en-US,en;q=0.9")
        if body:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("Referer", BASE + (referer or "/"))
        try:
            with self.opener.open(req, timeout=40) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 — сеть, таймауты, DNS
            return 0, f"{type(exc).__name__}: {exc}"


def show(label: str, status: int, text: str) -> None:
    """Печатает ответ сервера: именно здесь видна настоящая причина отказа."""
    clean = " ".join(text.split())
    print(f"\n--- {label}: HTTP {status}")
    print(clean[:800] if clean else "(пустой ответ)")
    if status != 200:
        print("^^^ это и есть причина, которую веб-форма не показывает")


def ask(prompt: str) -> str:
    return input(prompt).strip()


def login(sess: Session) -> bool:
    print("Вход в my.telegram.org (официальный flow). Код придёт в Telegram, не в SMS.")
    phone = ask("\nНомер телефона в международном формате (например +79161234567): ")
    if not phone:
        print("Номер не введён.")
        return False

    status, text = sess.call("/auth/send_password", {"phone": phone}, referer="/auth")
    show("POST /auth/send_password", status, text)
    if status != 200:
        return False

    random_hash = ""
    try:
        import json

        random_hash = json.loads(text).get("random_hash", "")
    except Exception:  # noqa: BLE001
        m = re.search(r'"random_hash"\s*:\s*"([^"]+)"', text)
        random_hash = m.group(1) if m else ""

    if not random_hash:
        print("Сервер не вернул random_hash — значит код отправлен не был.")
        return False

    code = ask("\nКод из Telegram: ")
    if not code:
        print("Код не введён.")
        return False

    status, text = sess.call(
        "/auth/login",
        {"phone": phone, "random_hash": random_hash, "password": code, "remember": "1"},
        referer="/auth",
    )
    show("POST /auth/login", status, text)
    if status != 200:
        return False
    print("\n✓ Авторизация прошла.")
    return True


def _write_dump(html: str) -> None:
    """Дамп страницы /apps. В нём лежит api_hash — файл только для себя (600)."""
    DUMP.write_text(html, encoding="utf-8")
    try:
        os.chmod(DUMP, 0o600)
    except OSError:
        pass


def cleanup_dump() -> None:
    """Удаляет дамп /apps: api_hash в нём больше не нужен, а /tmp общий.

    На неуспехе оставляем файл — там текст ошибки, который не показывает
    веб-форма. Поэтому вызываем только на успешных путях main().
    """
    try:
        DUMP.unlink(missing_ok=True)
    except OSError:
        pass


def read_apps(sess: Session) -> tuple[str, str]:
    """Читает страницу /apps и вытаскивает уже существующие api_id / api_hash."""
    status, html = sess.call("/apps", referer="/")
    _write_dump(html)
    print(f"\n--- GET /apps: HTTP {status}, сохранено в {DUMP}")

    if status != 200 or "<html" not in html.lower():
        show("GET /apps", status, html)
        return "", ""

    if "send_password" in html or "my_login_form" in html:
        print("Сессия не активна: вместо /apps отдалась форма входа.")
        return "", ""

    hashes = [h for h in API_HASH_RE.findall(html) if not set(h) <= {"0", "1"}]
    ids = API_ID_RE.findall(html)

    api_hash = hashes[0] if hashes else ""
    api_id = ""
    for cand in ids:
        # api_id — 6-8 знаков, это не год и не длина
        if len(cand) in (6, 7, 8) and cand not in {"000000", "123456"}:
            api_id = cand
            break

    return api_id, api_hash


def show_create_form(sess: Session) -> list[str]:
    """Показывает поля формы создания приложения (чтобы не угадывать названия)."""
    html = DUMP.read_text(encoding="utf-8", errors="replace")
    fields = re.findall(r'<input[^>]+name="([^"]+)"', html)
    fields += re.findall(r'<select[^>]+name="([^"]+)"', html)
    fields += re.findall(r'<textarea[^>]+name="([^"]+)"', html)
    uniq = [f for f in dict.fromkeys(fields)]  # сохраняем порядок, убираем дубли
    if uniq:
        print("Поля формы на странице:", ", ".join(uniq))
    return uniq


def create_app(sess: Session) -> tuple[str, str]:
    print("\nСоздаём новое приложение. Telegram просит: title, shortname, url, platform.")
    data = {
        "app_title": ask("Название (например: TG Forward): ") or "TG Forward",
        "app_shortname": ask("Короткое имя латиницей (например: tgforward): ") or "tgforward",
        "app_url": ask("URL (например: https://t.me/papina_do4a_bot): ") or "",
        "app_platform": ask(
            "Платформа (desktop / android / ios / web / other) [desktop]: "
        )
        or "desktop",
        "app_desc": ask("Описание (можно пустое): "),
    }
    status, text = sess.call("/apps/create", data, referer="/apps")
    show("POST /apps/create", status, text)
    if status != 200:
        return "", ""

    # Сервер отдаёт JSON с новыми кредами либо редиректит на страницу приложения
    m_id = re.search(r'"api_id"\s*:\s*(\d+)', text)
    m_hash = re.search(r'"api_hash"\s*:\s*"([0-9a-f]{32})"', text)
    if m_id and m_hash:
        return m_id.group(1), m_hash.group(1)

    m_href = re.search(r'/apps\?[^"\']*?(\d{5,9})', text)
    if m_href:
        status, html = sess.call(f"/apps?app_id={m_href.group(1)}", referer="/apps")
        _write_dump(html)
        return read_apps(sess)

    print("Не удалось распознать ответ — посмотрите дамп:", DUMP)
    return "", ""


def write_env(api_id: str, api_hash: str) -> None:
    """Аккуратно обновляет .env, сохраняя остальные значения и комментарии."""
    if not ENV_FILE.exists():
        print(f"Файла {ENV_FILE} нет — впишите значения вручную.")
        return
    if ask(f"\nЗаписать API_ID/API_HASH в {ENV_FILE}? (y/n): ").lower() != "y":
        print("Ок, оставляю как есть.")
        return

    lines = ENV_FILE.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    done_id = done_hash = False
    for line in lines:
        if re.match(r"^API_ID\s*=", line):
            out.append(f"API_ID={api_id}\n")
            done_id = True
        elif re.match(r"^API_HASH\s*=", line):
            out.append(f"API_HASH={api_hash}\n")
            done_hash = True
        else:
            out.append(line)
    if not done_id:
        out.append(f"API_ID={api_id}\n")
    if not done_hash:
        out.append(f"API_HASH={api_hash}\n")
    ENV_FILE.write_text("".join(out), encoding="utf-8")
    print("✓ .env обновлён.")


def main() -> int:
    sess = Session()

    warm_status, warm_text = sess.call("/auth")
    print(f"my.telegram.org доступен: HTTP {warm_status}")

    if not login(sess):
        print("\nНе удалось войти. Текст ошибки выше — по нему и смотрим причину.")
        return 1

    api_id, api_hash = read_apps(sess)

    if api_id and api_hash:
        print("\n=== Готово, приложение уже есть ===")
        print(f"API_ID={api_id}")
        print(f"API_HASH={api_hash}")
        write_env(api_id, api_hash)
        cleanup_dump()
        return 0

    print("\nГотовых приложений на аккаунте не найдено.")
    show_create_form(sess)
    if ask("\nПопробовать создать приложение? (y/n): ").lower() != "y":
        print("Ок, прерываюсь. Дамп страницы:", DUMP)
        return 1

    api_id, api_hash = create_app(sess)
    if api_id and api_hash:
        print("\n=== Готово ===")
        print(f"API_ID={api_id}")
        print(f"API_HASH={api_hash}")
        write_env(api_id, api_hash)
        cleanup_dump()
        return 0

    print("\nСоздать не получилось. Текст ошибки выше.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (KeyboardInterrupt, EOFError):
        print("\nПрервано.")
        sys.exit(130)
