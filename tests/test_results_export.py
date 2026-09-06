"""Собранное можно досмотреть и забрать файлом.

Три обещания, каждое из которых раньше не выполнялось:

* **страницы** — парсер кладёт до 10 000 участников, а шторка просила всегда одну
  и ту же первую сотню. Со сдвигом видно весь список, и страницы не пересекаются;
* **файл** — списка нельзя было вынести из приложения вообще. Теперь бот
  присылает CSV в чат: внутри Telegram это единственный надёжный путь, WebView
  скачанное не сохраняет. Проверяем и отказы — пустую задачу, отсутствие бота,
  чужую задачу и заблокированного бота;
* **честные кнопки** — «Результаты» предлагались автоподписке, которая ничего не
  собирает, и всегда отвечали «Пока пусто».

Отдельно — безопасность самого файла: имя в Telegram человек пишет себе сам, и
«=HYPERLINK(...)» в имени не должен стать формулой в Excel у того, кто открыл
выгрузку.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app import exports
from app.bot import keyboards as kb
from app.bot.handlers import rules as bot_rules
from app.db import repo
from app.db.database import session_scope
from app.db.models import Rule
from app.telegram_client.jobs import task_title
from tests.helpers import TEST_USER_ID, FakeCallback, RecordingBot, make_collector

pytestmark = pytest.mark.usefixtures("database")


# ─────────────────────────────── Страницы ─────────────────────────────────────


async def test_pages_do_not_repeat_the_first_hundred(client, auth_headers, create_user, create_account):
    """Сдвиг работает: вторая страница — продолжение, а не тот же список."""
    rule_id = await make_collector(create_user, create_account, count=5)

    first = await client.get(f"/api/tasks/{rule_id}/results?limit=3", headers=auth_headers)
    head = await first.json()
    second = await client.get(
        f"/api/tasks/{rule_id}/results?limit=3&offset=3", headers=auth_headers
    )
    tail = await second.json()

    assert [item["id"] for item in head["items"]] == [5, 4, 3]
    assert head["total"] == 5 and head["offset"] == 0 and head["has_more"] is True
    assert [item["id"] for item in tail["items"]] == [2, 1]
    assert tail["offset"] == 3 and tail["has_more"] is False


async def test_huge_limit_does_not_reach_the_database(client, auth_headers, create_user, create_account):
    """«Дай миллион» — не повод для миллионного запроса, но и не ошибка.

    Одна страница ограничена тысячей строк: остальное берут файлом, а не одним
    JSON на десять тысяч записей. Отрицательный сдвиг — тоже не ошибка, просто
    начало списка, и в ответе он должен быть уже приведённым к нулю.
    """
    rule_id = await make_collector(create_user, create_account, count=1005)

    response = await client.get(
        f"/api/tasks/{rule_id}/results?limit=99999999&offset=-5", headers=auth_headers
    )
    data = await response.json()

    assert response.status == 200
    assert len(data["items"]) == 1000
    assert data["offset"] == 0 and data["has_more"] is True and data["total"] == 1005


# ──────────────────────────────── Файл ────────────────────────────────────────


async def test_export_sends_csv_to_the_chat(bot_client, auth_headers, create_user, create_account):
    """Файл уходит документом в переписку — с BOM, «;» и всеми записями."""
    rule_id = await make_collector(create_user, create_account, count=4)
    bot = RecordingBot()
    test_client = await bot_client(bot)

    response = await test_client.post(f"/api/tasks/{rule_id}/export", headers=auth_headers)
    data = await response.json()

    assert response.status == 200 and data["sent"] == 4 and data["total"] == 4
    assert len(bot.documents) == 1
    chat_id, filename, raw, caption = bot.documents[0]
    assert chat_id == TEST_USER_ID
    assert filename.startswith("audience-") and filename.endswith(".csv")
    assert raw.startswith(b"\xef\xbb\xbf")  # без BOM Excel открывает кракозябры
    text = raw.decode("utf-8-sig")
    assert text.splitlines()[0] == "id;ник;имя;телефон;когда (UTC)"
    assert "guest_4" in text and "Гость 1" in text
    assert "Собранная аудитория" in caption and "Театр у моря" in caption


async def test_export_of_checks_has_its_own_columns(bot_client, auth_headers, create_user, create_account):
    """У чеков свои колонки: ссылка и текст, а не ник с телефоном."""
    rule_id = await make_collector(create_user, create_account, kind="checks", count=2)
    bot = RecordingBot()
    test_client = await bot_client(bot)

    await test_client.post(f"/api/tasks/{rule_id}/export", headers=auth_headers)

    filename, raw = bot.documents[0][1], bot.documents[0][2]
    text = raw.decode("utf-8-sig")
    assert filename.startswith("checks-")
    assert text.splitlines()[0].endswith("ссылка;текст;чат;сообщение")
    assert "https://t.me/g/2" in text


async def test_export_shifts_time_to_the_owner_clock(bot_client, auth_headers, create_user, create_account):
    """Часовой пояс из кабинета — в колонке и в её подписи."""
    rule_id = await make_collector(create_user, create_account, count=1)
    bot = RecordingBot()
    test_client = await bot_client(bot)

    await test_client.post(f"/api/tasks/{rule_id}/export?tz=180", headers=auth_headers)

    text = bot.documents[0][2].decode("utf-8-sig")
    assert "когда (UTC+3)" in text.splitlines()[0]


async def test_export_without_findings_is_refused(bot_client, auth_headers, create_user, create_account):
    """Пустая задача — понятный отказ, а не пустой файл в переписке."""
    rule_id = await make_collector(create_user, create_account, count=0)
    bot = RecordingBot()
    test_client = await bot_client(bot)

    response = await test_client.post(f"/api/tasks/{rule_id}/export", headers=auth_headers)

    assert response.status == 409
    assert "нечего" in (await response.json())["error"]
    assert bot.documents == []


async def test_export_needs_a_bot(client, auth_headers, create_user, create_account):
    """Без бота файл отдать некому — говорим это словами, а не пятисоткой."""
    rule_id = await make_collector(create_user, create_account, count=2)

    response = await client.post(f"/api/tasks/{rule_id}/export", headers=auth_headers)
    data = await response.json()

    assert response.status == 503 and data["feature"] == "export"


async def test_export_survives_a_blocked_bot(bot_client, auth_headers, create_user, create_account):
    """Бота заблокировали — кабинет получает отказ, а не «файл отправлен»."""
    rule_id = await make_collector(create_user, create_account, count=2)
    bot = RecordingBot(fail_for={TEST_USER_ID})
    test_client = await bot_client(bot)

    response = await test_client.post(f"/api/tasks/{rule_id}/export", headers=auth_headers)

    assert response.status == 502
    assert "чат с ботом" in (await response.json())["error"]


async def test_export_of_someone_else_task_is_not_found(bot_client, auth_headers, create_user, create_account):
    """Чужая задача — 404, и никакого файла в чат."""
    other_id = await create_user()
    account_id = await create_account(other_id)
    async with session_scope() as session:
        rule = Rule(
            user_id=other_id, account_id=account_id, source_id=-1, target_id=-2, kind="parser"
        )
        session.add(rule)
        await session.flush()
        rule_id = rule.id
        await repo.add_collected_items(
            session, rule_id, other_id, "parser", [{"user_id": 1, "username": "x"}]
        )
    await create_user(id=TEST_USER_ID)
    bot = RecordingBot()
    test_client = await bot_client(bot)

    response = await test_client.post(f"/api/tasks/{rule_id}/export", headers=auth_headers)

    assert response.status == 404 and bot.documents == []


# ─────────────────────── Сам файл: формулы и телефоны ─────────────────────────


def test_name_with_a_formula_stays_text():
    """Имя из Telegram не должно выполняться при открытии файла."""
    item = type("Row", (), {"payload": {"user_id": 7, "name": '=HYPERLINK("http://evil")'}, "created_at": None})()

    text = exports.collected_csv("parser", [item]).decode("utf-8-sig")

    assert "'=HYPERLINK" in text
    assert "\n=HYPERLINK" not in text and ";=HYPERLINK" not in text


def test_phone_keeps_only_digits():
    """«+7 999…» таблица приняла бы за формулу и потеряла бы плюс."""
    item = type("Row", (), {"payload": {"user_id": 7, "phone": "+7 999 123-45-67"}, "created_at": None})()

    text = exports.collected_csv("parser", [item]).decode("utf-8-sig")

    assert "79991234567" in text


def test_time_in_the_file_is_local_too():
    """Часовой пояс должен доехать и до самих строк, а не только до заголовка."""
    item = type("Row", (), {"payload": {"user_id": 7}, "created_at": datetime(2026, 9, 6, 12, 0)})()

    utc = exports.collected_csv("parser", [item]).decode("utf-8-sig")
    local = exports.collected_csv("parser", [item], tz_minutes=180).decode("utf-8-sig")

    assert "06.09.2026 12:00" in utc
    assert "06.09.2026 15:00" in local


def test_unknown_collector_still_gives_a_file():
    """Новый тип сборщика не должен отнимать у человека собранное."""
    item = type("Row", (), {"payload": {"что-то": "новое"}, "created_at": datetime(2026, 9, 6, 12, 0)})()

    text = exports.collected_csv("невиданное", [item]).decode("utf-8-sig")

    assert "данные" in text.splitlines()[0]
    assert "новое" in text


def test_caption_admits_it_did_not_take_everything():
    """Выгрузили не всё — подпись обязана сказать это, а не только число."""
    partial = exports.export_caption("parser", rule_title="Парсер", sent=10_000, total=12_345)
    whole = exports.export_caption("parser", rule_title="Парсер", sent=42, total=42)

    assert "10000 из 12345" in partial
    assert "из" not in whole.split(":")[1].split("\n")[0]


def test_caption_does_not_break_on_angle_brackets():
    """Название задачи берётся из чата — и в нём бывает «<»."""
    caption = exports.export_caption("checks", rule_title="Ловец <b> чеков", sent=1, total=1)

    assert "&lt;b&gt;" in caption


# ─────────────────────────── То же самое в боте ───────────────────────────────


async def test_bot_sends_the_same_file(create_user, create_account):
    """Кнопка «⬇️ Файлом» в боте присылает тот же CSV, что и кабинет."""
    rule_id = await make_collector(create_user, create_account, count=3)
    bot = RecordingBot()
    callback = FakeCallback(f"rule:export:{rule_id}", bot)

    await bot_rules.export_rule_results(callback)

    chat_id, filename, raw, caption = bot.documents[0]
    text = raw.decode("utf-8-sig")
    assert chat_id == TEST_USER_ID and filename.startswith("audience-")
    assert text.splitlines()[0] == "id;ник;имя;телефон;когда (UTC)"
    assert len(text.strip().splitlines()) == 4  # заголовок и три находки
    assert "Собранная аудитория" in caption and "Театр у моря" in caption
    # У нажатия один ответ — он достаётся итогу, а не «готовлю файл».
    assert callback.answers == [("Файл отправлен: 3 стр.", False)]


async def test_bot_export_of_empty_task_says_so(create_user, create_account):
    """Пустая задача — всплывающий отказ, а не файл с одним заголовком."""
    rule_id = await make_collector(create_user, create_account, count=0)
    bot = RecordingBot()
    callback = FakeCallback(f"rule:export:{rule_id}", bot)

    await bot_rules.export_rule_results(callback)

    assert bot.documents == []
    assert "нечего" in callback.alerts
    assert len(callback.answers) == 1


async def test_bot_export_survives_a_blocked_bot(create_user, create_account):
    """Телеграм не принял файл — человеку говорят это, а не молчат."""
    rule_id = await make_collector(create_user, create_account, count=2)
    bot = RecordingBot(fail_for={TEST_USER_ID})
    callback = FakeCallback(f"rule:export:{rule_id}", bot)

    await bot_rules.export_rule_results(callback)

    assert "не принял файл" in callback.alerts
    # Единственный ответ на нажатие — отказ. Потрать его раньше на «готовлю
    # файл», и объяснять уже нечем: Телеграм второй ответ не покажет.
    assert len(callback.answers) == 1


async def test_bot_export_of_someone_else_task_is_refused(create_user, create_account):
    """Чужую задачу боту тоже не выгрузить."""
    other_id = await create_user()
    account_id = await create_account(other_id)
    async with session_scope() as session:
        rule = Rule(
            user_id=other_id, account_id=account_id, source_id=-1, target_id=-2, kind="parser"
        )
        session.add(rule)
        await session.flush()
        rule_id = rule.id
        await repo.add_collected_items(
            session, rule_id, other_id, "parser", [{"user_id": 1, "username": "x"}]
        )
    await create_user(id=TEST_USER_ID)
    bot = RecordingBot()
    callback = FakeCallback(f"rule:export:{rule_id}", bot)

    await bot_rules.export_rule_results(callback)

    assert bot.documents == [] and "не найдена" in callback.alerts


async def test_bot_results_point_to_the_file(create_user, create_account):
    """Список в боте обрезан по лимиту сообщения — он говорит, где взять целиком."""
    rule_id = await make_collector(create_user, create_account, count=25)
    bot = RecordingBot()
    callback = FakeCallback(f"rule:results:{rule_id}", bot)

    await bot_rules.show_rule_results(callback)

    text, markup = callback.message.edits[-1]
    assert "Показаны последние 20 из 25" in text
    assert "⬇️ Файлом" in text
    assert any("Файлом" in label for label in _labels(markup))


# ──────────────────────────── Честные кнопки ──────────────────────────────────


def _labels(markup) -> list[str]:
    return [button.text for row in markup.inline_keyboard for button in row]


def test_autosubscribe_has_no_results_button():
    """Автоподписка ничего не собирает — кнопка всегда отвечала «Пока пусто»."""
    rule = Rule(id=1, kind="autosubscribe", enabled=True, archived=False, mode="copy")

    assert not any("Результаты" in label for label in _labels(kb.rule_menu(rule)))


def test_export_button_appears_only_with_findings():
    """«⬇️ Файлом» показываем, когда файл получится непустым."""
    rule = Rule(id=2, kind="parser", enabled=True, archived=False, mode="copy")

    empty = _labels(kb.rule_menu(rule, collected=0))
    filled = _labels(kb.rule_menu(rule, collected=5))

    assert any("Результаты" in label for label in empty)
    assert not any("Файлом" in label for label in empty)
    assert any("Файлом" in label for label in filled)


def test_title_travels_into_the_caption():
    """Подпись называет задачу теми же словами, что и её карточка."""
    rule = Rule(id=3, kind="parser", source_title="Театр у моря", source_id=-1001)

    caption = exports.export_caption("parser", rule_title=task_title(rule), sent=1, total=1)

    assert "Парсер аудитории: Театр у моря" in caption
