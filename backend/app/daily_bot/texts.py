"""Texts of the Daily Content bot, in one place for review.

Nothing interpolated here comes from a user: numbers and times only.
"""

from __future__ import annotations

from typing import Final

from app.domain.daily import DAILY_ACCESS_MILESTONE, MINUTES_PER_DAY, WINDOW_HALF_WIDTH

STATUS_BUTTON: Final = "📊 Мой статус"
PAUSE_BUTTON: Final = "⏸ Пауза"
RESUME_BUTTON: Final = "▶️ Возобновить"

DISABLED: Final = "Раздел скоро откроется."
NO_CONTENT: Final = "Материалы скоро появятся — пришлём автоматически."
PAUSED: Final = "⏸ Пауза включена. Возобновить — кнопкой ниже."
RESUMED: Final = "▶️ Выдача возобновлена."
HELP: Final = (
    "Команды:\n/status — мой статус\n/pause — пауза\n/resume — возобновить\n/help — справка"
)
OWNER_HELP: Final = "Режим владельца. Приём контента появится в следующей версии бота."
TOO_FAST: Final = "Слишком много запросов. Подождите пару секунд."

COMMANDS: Final = (
    ("start", "Начать"),
    ("status", "Мой статус"),
    ("pause", "Пауза"),
    ("resume", "Возобновить"),
    ("help", "Справка"),
)
"""The bot's command menu: (command, description)."""

_HALF_WINDOW_MINUTES: Final = int(WINDOW_HALF_WIDTH.total_seconds()) // 60


def clock_label(minute_of_day: int) -> str:
    """``755`` → ``"12:35"``; wraps around midnight."""
    minute = minute_of_day % MINUTES_PER_DAY
    return f"{minute // 60:02d}:{minute % 60:02d}"


def welcome(anchor_minute: int) -> str:
    """Sent once, right before the first item."""
    return (
        f"👑 Добро пожаловать в VIP Daily! Каждый день около {clock_label(anchor_minute)} (МСК) "
        "здесь будет новый материал. Вот первый:"
    )


def no_access(completed_purchases: int) -> str:
    """For a buyer who has not reached the access threshold yet."""
    return (
        f"Daily-контент открывается с {DAILY_ACCESS_MILESTONE}-й покупки. "
        f"Сейчас у вас {completed_purchases} из {DAILY_ACCESS_MILESTONE}."
    )


def status(*, received: int, available: int, anchor_minute: int, paused: bool) -> str:
    """The "📊 My status" line."""
    opens = clock_label(anchor_minute - _HALF_WINDOW_MINUTES)
    closes = clock_label(anchor_minute + _HALF_WINDOW_MINUTES)
    state = "на паузе" if paused else "активна"
    return (
        f"📊 Получено: {received} · Доступно новых: {available} · "
        f"Окно: {opens}–{closes} МСК · Статус: {state}"
    )


__all__ = [
    "COMMANDS",
    "DISABLED",
    "HELP",
    "NO_CONTENT",
    "OWNER_HELP",
    "PAUSED",
    "PAUSE_BUTTON",
    "RESUMED",
    "RESUME_BUTTON",
    "STATUS_BUTTON",
    "TOO_FAST",
    "clock_label",
    "no_access",
    "status",
    "welcome",
]
