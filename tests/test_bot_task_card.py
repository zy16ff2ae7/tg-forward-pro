"""Карточка задачи в боте говорит о ней правду.

Один текст показывал всем задачам одни и те же две последние строки — «Задержка:
N сек» и «Сработало раз: N», — и большинству типов обе врали:

* **парсер** ничего не отправляет, поэтому счётчик отправок у него навсегда
  ноль: карточка писала «Сработало раз: 0» поверх тысяч собранных участников;
* **постинг и рассылка** живут расписанием, а его в боте не было видно вовсе —
  за интервалом, окном и паузой приходилось идти в кабинет. Задержка их не
  касается: её отрабатывает только путь входящего сообщения;
* **автоподписка** вступает в чаты, а не «срабатывает раз».

То же число, что чинит карточку, включает и кнопку «⬇️ Файлом» — во всех местах,
где бот показывает меню задачи, а не только внутри «Результатов».
"""
from __future__ import annotations

import pytest

from app.bot import texts
from app.bot.handlers import rules as bot_rules
from app.db.models import Rule
from app.timeutil import tz_suffix
from tests.helpers import FakeCallback, RecordingBot, button_labels, make_collector

pytestmark = pytest.mark.usefixtures("database")


def card(kind: str, *, collected: int = 0, **fields) -> str:
    """Карточка по полям задачи. Базы не касается: текст от неё не зависит."""
    fields.setdefault("id", 1)
    fields.setdefault("mode", "copy")
    fields.setdefault("enabled", True)
    fields.setdefault("archived", False)
    fields.setdefault("delay_seconds", 0)
    fields.setdefault("forwarded_count", 0)
    return texts.rule_card(Rule(kind=kind, **fields), collected=collected)


# ─────────────────────────── Что задача сделала ───────────────────────────────


def test_parser_counts_findings_not_sends():
    """У парсера сделанное — собранные участники, а не отправки."""
    text = card(
        "parser",
        collected=1234,
        forwarded_count=0,
        filters={"limit": 500},
        source_title="Театр у моря",
    )

    assert "Собрано: 1234" in text
    assert "Сработало раз" not in text
    assert "Лимит за запуск: 500" in text


def test_checks_card_says_caught():
    """У ловца чеков находки «поймано» — теми же словами, что и в шторке."""
    text = card("checks", collected=7, source_title="Канал", target_title="Мой склад")

    assert "Поймано: 7" in text
    assert "Собрано:" not in text and "Сработало раз" not in text


def test_autosubscribe_counts_joined_chats():
    """Автоподписка ничего не собирает и не отправляет — она вступает."""
    text = card(
        "autosubscribe",
        forwarded_count=5,
        filters={"subscribe_to": ["@one", "@two"]},
    )

    assert "Вступили в чаты: 5" in text
    assert "Сработало раз" not in text and "Собрано" not in text
    assert "Каналов в списке: 2" in text


def test_forward_keeps_delay_and_sends():
    """Обычной пересылке обе прежние строки как раз подходят — не трогаем."""
    text = card(
        "forward",
        delay_seconds=60,
        forwarded_count=842,
        source_title="Источник",
        target_title="Приёмник",
    )

    assert "Задержка: 60 сек" in text
    assert "Сработало раз: 842" in text
    assert "Режим: копия (без метки)" in text


# ─────────────────────────── Чем задача живёт ─────────────────────────────────


def test_poster_shows_its_schedule():
    """Расписание постинга видно в боте: интервал, окно и чьи это часы."""
    text = card(
        "poster",
        forwarded_count=12,
        target_id=-1001,
        filters={
            "interval_seconds": 1800,
            "window_start": "10:00",
            "window_end": "20:00",
            "window_tz": 180,
            "targets": [-1002],
        },
    )

    assert "Раз в 30 мин" in text
    assert "Окно: 10:00–20:00 UTC+3" in text
    assert "Задержка" not in text  # постинга она не касается
    assert "Чатов: <b>2</b>" in text
    assert "Сработало раз: 12" in text


def test_poster_without_a_timezone_says_server_clock():
    """Часы не выбрали — окно считается по серверу, и об этом честно сказано.

    Заодно короткий интервал: «раз в 0 мин» не бывает, поэтому минуту таких
    расписаний округляем вверх.
    """
    text = card("poster", filters={"interval_seconds": 30, "window_start": "09:00"})

    assert "по часам сервера" in text and "UTC" not in text
    assert "Раз в 1 мин" in text


def test_mailing_shows_the_walk_between_chats():
    """Рассылка идёт по чатам поштучно: важны пауза и число кругов."""
    text = card("mailing", filters={"gap_seconds": 7, "repeats": 3})

    assert "Пауза между чатами: 7 сек" in text
    assert "Кругов: 3" in text
    assert "Задержка" not in text


def test_mailing_without_repeats_walks_forever():
    """Нулевые круги — это «без конца», а не «нуль кругов»."""
    text = card("mailing", filters={"gap_seconds": 5, "repeats": 0})

    assert "Кругов: без конца" in text


def test_parser_and_autosubscribe_say_how_they_start():
    """Разовые задачи запускаются кнопкой — и карточка это называет.

    Называет ровно один раз: пока состояние задачи и есть «по кнопке», отдельная
    строка «Запуск: по кнопке» была бы дубляжом — она возвращается, когда
    состояние заняла другая причина (пауза, сбой, нет связи).
    """
    parser = card("parser", collected=0, filters={"limit": 200})
    paused = card("parser", collected=0, enabled=False, filters={"limit": 200})
    by_button = card("autosubscribe", delay_seconds=45, source_id=None)
    by_links = card("autosubscribe", delay_seconds=45, source_id=-1001)
    silent = card("autosubscribe", delay_seconds=0, source_id=-1001)

    assert "Состояние: по кнопке 🖐" in parser
    assert parser.count("по кнопке") == 1 and "Задержка" not in parser
    assert "Запуск: по кнопке" in paused and "Состояние: на паузе ⏸" in paused
    assert by_button.count("по кнопке") == 1 and "ссылкам" not in by_button
    assert "Задержка" not in by_button  # без источника задержке нечего ждать
    assert "Запуск: по кнопке и по ссылкам из источника" in by_links
    assert "Задержка: 45 сек" in by_links
    assert "Задержка" not in silent


def test_tz_suffix_names_odd_offsets():
    """Подпись часов одна на файл и на карточку — включая получасовые пояса."""
    assert tz_suffix(None) == "UTC" and tz_suffix(0) == "UTC"
    assert tz_suffix(180) == "UTC+3"
    assert tz_suffix(330) == "UTC+5:30"
    assert tz_suffix(-210) == "UTC-3:30"


# ──────────────────── То же число во всех местах меню ─────────────────────────


async def test_open_shows_the_real_count_and_the_file_button(create_user, create_account):
    """Открыли задачу — сразу видно собранное и есть чем его забрать."""
    rule_id = await make_collector(create_user, create_account, count=3)
    callback = FakeCallback(f"rule:open:{rule_id}", RecordingBot())

    await bot_rules.open_rule(callback)

    text, markup = callback.message.edits[-1]
    assert "Собрано: 3" in text and "Сработало раз" not in text
    assert any("Файлом" in label for label in button_labels(markup))


async def test_pause_does_not_take_away_the_findings(create_user, create_account):
    """Остановленная задача не теряет собранное — и кнопку файла тоже."""
    rule_id = await make_collector(create_user, create_account, count=2)
    callback = FakeCallback(f"rule:toggle:{rule_id}", RecordingBot())

    await bot_rules.toggle_rule(callback)

    text, markup = callback.message.edits[-1]
    assert "на паузе" in text and "Собрано: 2" in text
    assert any("Файлом" in label for label in button_labels(markup))


async def test_run_now_offers_the_file_right_away(create_user, create_account, monkeypatch):
    """Итог сбора — то самое место, где файл нужен: не гоняем за ним в «Результаты»."""
    rule_id = await make_collector(create_user, create_account, count=3)

    async def fake_run(rule):
        return {"ok": True, "collected": 3}

    monkeypatch.setattr(bot_rules.manager, "run_task_now", fake_run)
    callback = FakeCallback(f"rule:run:{rule_id}", RecordingBot())

    await bot_rules.run_rule_now(callback)

    text, markup = callback.message.edits[-1]
    assert "<b>3</b>" in text
    assert any("Файлом" in label for label in button_labels(markup))
