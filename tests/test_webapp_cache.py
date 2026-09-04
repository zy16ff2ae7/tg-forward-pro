"""Кэш мини-аппа: после выката клиент обязан увидеть новую вёрстку.

Telegram WebView держит `styles.css` и `app.js` у себя и о свежести сервер не
спрашивает — кабинет открывался со старой вёрсткой, хотя на сервере лежала
новая. Поэтому в адресах файлов стоит метка сборки. Тесты закрывают именно эту
механику: метка следует за содержимым, HTML не кэшируется, а статика получает
осмысленный `Cache-Control`.
"""
from __future__ import annotations

from pathlib import Path

from app.config import Settings, settings
from app.webapp_build import add_version, build_stamp, cache_control_for, stale_shell_loader

INDEX_HTML = (
    '<link rel="stylesheet" href="styles.css">'
    '<script src="https://telegram.org/js/telegram-web-app.js"></script>'
    '<script src="app.js"></script>'
)


def _make_webapp(directory: Path, css: str = "body{margin:0}") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (directory / "styles.css").write_text(css, encoding="utf-8")
    (directory / "app.js").write_text("void 0;", encoding="utf-8")


# ───────────────────────────── метка сборки ───────────────────────────────────


def test_stamp_follows_content_not_mtime(tmp_path: Path):
    """Правка файла меняет метку, простая перезапись тем же байтом — нет.

    rsync при выкате обновляет время правки у всех файлов подряд. Если бы метка
    считалась по mtime, каждый деплой сбрасывал бы кэш целиком, включая
    неизменившиеся файлы.
    """
    _make_webapp(tmp_path)
    first = build_stamp(tmp_path)
    assert first

    css = tmp_path / "styles.css"
    css.write_text(css.read_text(encoding="utf-8"), encoding="utf-8")
    assert build_stamp(tmp_path) == first

    css.write_text("body{color:red}", encoding="utf-8")
    assert build_stamp(tmp_path) != first


def test_stamp_empty_without_files(tmp_path: Path):
    assert build_stamp(tmp_path / "нет-такой-папки") == ""


# ──────────────────────── подстановка метки в HTML ────────────────────────────


def test_version_added_only_to_local_assets():
    out = add_version(INDEX_HTML, "abc123")
    assert 'href="styles.css?v=abc123"' in out
    assert 'src="app.js?v=abc123"' in out
    # Скрипт Telegram лежит на чужом домене — трогать его нельзя.
    assert 'src="https://telegram.org/js/telegram-web-app.js"' in out


def test_version_not_doubled():
    assert add_version('href="styles.css?v=old"', "new") == 'href="styles.css?v=old"'


def test_version_skipped_without_stamp():
    assert add_version(INDEX_HTML, "") == INDEX_HTML


def test_cache_control_rules():
    # Адрес с меткой указывает на неизменяемое содержимое, без метки —
    # на «какое сейчас есть», и его надо перепроверять.
    assert cache_control_for(True) == "public, max-age=31536000, immutable"
    assert cache_control_for(False) == "no-cache"


# ──────────────────────────── HTTP-контракт ───────────────────────────────────


async def test_index_serves_versioned_assets_and_is_not_cached(client):
    response = await client.get("/app/")
    assert response.status == 200
    # Единственный документ, через который клиент узнаёт новые адреса файлов.
    assert response.headers["Cache-Control"] == "no-store"

    stamp = build_stamp(settings.webapp_dir)
    body = await response.text()
    assert f'href="styles.css?v={stamp}"' in body
    assert f'src="app.js?v={stamp}"' in body


async def test_static_without_version_must_revalidate(client):
    response = await client.get("/app/styles.css")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-cache"


async def test_static_with_version_is_cached_forever(client):
    response = await client.get(f"/app/styles.css?v={build_stamp(settings.webapp_dir)}")
    assert response.status == 200
    assert "immutable" in response.headers["Cache-Control"]


# ─────────────────────── адрес кнопки «Открыть кабинет» ───────────────────────


def test_mini_app_url_carries_build_stamp(tmp_path: Path):
    """Метка стоит и в адресе документа: WebView помнит страницу по URL."""
    _make_webapp(tmp_path)
    url = Settings(webhook_url="https://example.com/", webapp_dir=tmp_path).mini_app_url
    assert url == f"https://example.com/app/?v={build_stamp(tmp_path)}"


def test_mini_app_url_stays_clean_without_webapp_files(tmp_path: Path):
    config = Settings(webhook_url="https://example.com/", webapp_dir=tmp_path / "нет")
    assert config.mini_app_url == "https://example.com/app/"


# ──────────────────── спасение старого каркаса из кэша ────────────────────────


def test_loader_sends_stale_shell_to_versioned_url():
    """Скрипт-спасатель уводит на адрес с меткой и сохраняет hash с initData."""
    code = stale_shell_loader("abc123")
    assert '"abc123"' in code
    assert "params.set('v', stamp)" in code
    # Без hash Telegram не передаст initData и кабинет уйдёт в демо-режим.
    assert "location.hash" in code
    # Второй раз по тому же адресу не перезагружаемся — иначе цикл.
    assert "params.get('v') === stamp" in code


def test_loader_is_empty_action_without_stamp():
    assert "!stamp" in stale_shell_loader("")


async def test_bundle_without_version_returns_loader(client):
    """Старая копия index.html просит `app.js` без метки — ей нельзя новый код."""
    response = await client.get("/app/app.js")
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"

    body = await response.text()
    assert "location.replace" in body
    assert build_stamp(settings.webapp_dir) in body
    # Настоящий бандл в такой ответ попасть не должен.
    assert "async function boot()" not in body


async def test_bundle_with_current_version_is_the_real_file(client):
    stamp = build_stamp(settings.webapp_dir)
    response = await client.get(f"/app/app.js?v={stamp}")
    assert response.status == 200
    assert "immutable" in response.headers["Cache-Control"]

    body = await response.text()
    assert "async function boot()" in body
    assert "location.replace" in body  # сам бандл тоже умеет обновляться


async def test_bundle_with_foreign_version_returns_loader(client):
    """Метка из прошлого выката — тоже старый каркас, его надо перезагрузить."""
    response = await client.get("/app/app.js?v=устарела")
    assert response.status == 200
    body = await response.text()
    assert "async function boot()" not in body
    assert build_stamp(settings.webapp_dir) in body


async def test_health_reports_build_stamp(client):
    """По этой метке кабинет понимает, что открыт по старому адресу."""
    response = await client.get("/api/health")
    assert response.status == 200
    assert (await response.json())["build"] == build_stamp(settings.webapp_dir)
