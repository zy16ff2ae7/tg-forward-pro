"""Общие типы юзебот-слоя (лежат отдельно, чтобы не было циклических импортов)."""
from __future__ import annotations

from dataclasses import dataclass, field

from app.telegram_client.filters import FilterConfig


@dataclass(slots=True)
class RuleSnapshot:
    """Снимок правила для быстрой работы в обработчике сообщений."""

    id: int
    user_id: int
    target_id: int
    mode: str
    delay_seconds: int
    # account_id нужен планировщику авто-постера и логам. Держим его в конце
    # блока обязательных полей, чтобы новые поля добавлялись в конец и не
    # ломали уже написанные вызовы (все они передают аргументы по имени).
    account_id: int = 0
    filters: FilterConfig = field(default_factory=FilterConfig)
    # forward — обычная пересылка; остальные значения — задачи из jobs.py
    kind: str = "forward"
    source_id: int = 0
    # Названия чатов нужны для понятных логов и заголовков: по одним id не скажешь,
    # что за канал, а в моменты ошибок видеть название важнее экономии памяти.
    source_title: str = ""
    target_title: str = ""
    # Актуальное состояние правила (нужно планировщику авто-постера, чтобы
    # не слать в паузу/архив и не держать кэш лишних правил).
    enabled: bool = True
    archived: bool = False
    # Сколько уже отправлено. Рассылке по чатам этим счётчиком задаётся место в
    # очереди получателей, поэтому после перезапуска она продолжает с того же
    # чата, а не начинает круг заново.
    forwarded_count: int = 0


# Почему сообщение не ушло. Значения попадают в /api/health, поэтому короткие
# и стабильные: по ним видно, чинить фильтр, подписку или подключение.
SKIP_SERVICE = "service_message"      # вступления, смена аватара и т.п.
SKIP_EMPTY = "empty_message"          # ни текста, ни медиа
SKIP_FILTER = "filtered"              # не прошло фильтры правила
SKIP_NO_SUBSCRIPTION = "no_subscription"
SKIP_FILTER_ERROR = "filter_error"    # сам фильтр упал — правило надо править
SKIP_JOB = "job"                      # это не пересылка, а задача из jobs.py
SKIP_DAILY_CAP = "daily_cap"            # дневной лимит отправок исчерпан


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """Итог обработки одного сообщения одним правилом.

    Раньше обработчик возвращал просто bool, и все пропуски выглядели
    одинаково: в диагностике нельзя было отличить «фильтр не пропустил» от
    «у пользователя кончилась подписка». Первое — норма, второе — потеря денег.
    """

    sent: bool
    reason: str = ""

    def __bool__(self) -> bool:
        """Совместимость с кодом, который ждал bool."""
        return self.sent


SENT = DeliveryResult(sent=True)


def skipped(reason: str) -> DeliveryResult:
    return DeliveryResult(sent=False, reason=reason)
