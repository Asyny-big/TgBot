"""Texts of the Daily Content bot, in one place for review.

The bot sends HTML. The buyer's texts interpolate numbers and times only; the
owner's cards also show a caption and a status reason, and those are escaped.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING, Final

from app.domain.daily import (
    DAILY_ACCESS_MILESTONE,
    DAILY_TIMEZONE,
    MINUTES_PER_DAY,
    WINDOW_HALF_WIDTH,
)
from app.domain.enums import DailyContentKind, DailyContentStatus

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.daily_bot.intake import Refusal
    from app.domain.daily import LibraryStats
    from app.domain.entities import DailyContent

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
OWNER_HELP: Final = (
    "👑 Режим владельца.\n"
    "Пришлите или перешлите сюда фото, видео, GIF, документ, аудио, голосовое "
    "или кружок — материал сразу попадёт в библиотеку. Текст добавляется "
    "кнопкой «➕ Добавить».\n\n"
    "/stats — библиотека в цифрах\n"
    "/item 121 — карточка материала #121\n"
    "/help — эта справка"
)
TOO_FAST: Final = "Слишком много запросов. Подождите пару секунд."

COMMANDS: Final = (
    ("start", "Начать"),
    ("status", "Мой статус"),
    ("pause", "Пауза"),
    ("resume", "Возобновить"),
    ("help", "Справка"),
)
"""The bot's command menu: (command, description)."""

OWNER_COMMANDS: Final = (
    ("stats", "Библиотека в цифрах"),
    ("item", "Карточка материала: /item 121"),
    ("help", "Справка"),
)
"""The command menu shown in the owner's own chat."""

_HALF_WINDOW_MINUTES: Final = int(WINDOW_HALF_WIDTH.total_seconds()) // 60


# --------------------------------------------------------------------------- #
# The owner's library                                                         #
# --------------------------------------------------------------------------- #

UNDO_BUTTON: Final = "↩️ Отменить"
DISABLE_BUTTON: Final = "🚫 Отключить"
ENABLE_BUTTON: Final = "✅ Включить"
ARCHIVE_BUTTON: Final = "🗄 В архив"
RESTORE_BUTTON: Final = "♻️ Восстановить"
SHOW_BUTTON: Final = "👁 Показать"
ADD_TEXT_BUTTON: Final = "➕ Добавить"
SKIP_TEXT_BUTTON: Final = "✖️ Не добавлять"

TEXT_PROMPT: Final = "Добавить этот текст в библиотеку?"
TEXT_SKIPPED: Final = "Текст не добавлен."
TEXT_GONE: Final = "Не нашёл исходное сообщение. Отправьте текст ещё раз."
NOT_SAVED: Final = "❌ Не сохранено (ошибка базы). Перешлите сообщение ещё раз."
ITEM_USAGE: Final = "Номер материала: /item 121"
UNCHANGED: Final = "Без изменений."
NOT_ALLOWED: Final = "Из архива материал возвращается кнопкой «♻️ Восстановить»."
UNAVAILABLE: Final = "Недоступно."

KIND_LABELS: Final = {
    DailyContentKind.TEXT: "текст",
    DailyContentKind.PHOTO: "фото",
    DailyContentKind.VIDEO: "видео",
    DailyContentKind.ANIMATION: "GIF",
    DailyContentKind.DOCUMENT: "документ",
    DailyContentKind.AUDIO: "аудио",
    DailyContentKind.VOICE: "голосовое",
    DailyContentKind.VIDEO_NOTE: "кружок",
}
STATUS_LABELS: Final = {
    DailyContentStatus.ACTIVE: "активен",
    DailyContentStatus.DISABLED: "отключён",
    DailyContentStatus.ARCHIVED: "в архиве",
}
_REFUSED_LABELS: Final = {
    "sticker": "стикер",
    "poll": "опрос",
    "location": "геопозиция",
    "venue": "место на карте",
    "contact": "контакт",
    "dice": "кубик",
    "game": "игра",
    "story": "история",
    "paid_media": "платное медиа",
    "live_photo": "живое фото",
    "checklist": "чек-лист",
    "invoice": "счёт",
}
_SUPPORTED: Final = "текст, фото, видео, GIF, документ, аудио, голосовое, кружок"
_CAPTION_PREVIEW: Final = 80


def duration_label(seconds: int) -> str:
    """``45`` → ``"0:45"``, ``3725`` → ``"1:02:05"``."""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def item_label(item: DailyContent) -> str:
    """``"#121 · видео 0:45"``."""
    label = f"#{item.id} · {KIND_LABELS[item.kind]}"
    return f"{label} {duration_label(item.duration)}" if item.duration else label


def added(item: DailyContent, *, active_total: int) -> str:
    return f"✅ Добавлено {item_label(item)} · активных: {active_total}"


def already_added(item: DailyContent) -> str:
    return f"✅ Уже добавлено {item_label(item)} · {STATUS_LABELS[item.status]}"


def duplicate(item: DailyContent) -> str:
    return f"⚠️ Уже есть {item_label(item)} · {STATUS_LABELS[item.status]}"


def archived_twin(item: DailyContent) -> str:
    return f"🗄 Этот материал уже был в библиотеке: {item_label(item)}, в архиве. Восстановить?"


def refused(refusal: Refusal) -> str:
    if refusal.protected:
        return "⛔ Это сообщение защищено от пересылки — его нельзя добавить."
    what = _REFUSED_LABELS.get(refusal.content_type, "этот тип сообщений")
    return f"⛔ Не поддерживается: {what}. Можно: {_SUPPORTED}."


def restore_blocked(twin: DailyContent) -> str:
    return f"⚠️ Нельзя восстановить: такой же материал уже есть — {item_label(twin)}."


def item_not_found(content_id: int) -> str:
    return f"Материал #{content_id} не найден."


def preview_failed(error: str) -> str:
    """A pop-up answer, not a message: plain text, never HTML."""
    return f"Не удалось показать: {error}"


def card(item: DailyContent) -> str:
    """The owner's card of one item."""
    added_at = item.created_at.astimezone(DAILY_TIMEZONE).strftime("%d.%m.%Y %H:%M")
    lines = [
        f"<b>{item_label(item)}</b> · {STATUS_LABELS[item.status]}",
        f"Добавлен: {added_at} МСК",
    ]
    if item.body:
        excerpt = (
            item.body if len(item.body) <= _CAPTION_PREVIEW else f"{item.body[:_CAPTION_PREVIEW]}…"
        )
        lines.append(
            f"{'Текст' if item.kind is DailyContentKind.TEXT else 'Подпись'}: {escape(excerpt)}"
        )
    if item.file_name:
        lines.append(f"Файл: {escape(item.file_name)}")
    lines.append(f"Источник: чат {item.source_chat_id}, сообщение {item.source_message_id}")
    if item.status is not DailyContentStatus.ACTIVE and item.status_reason:
        lines.append(f"Причина: {escape(item.status_reason)}")
    return "\n".join(lines)


def stats(numbers: LibraryStats) -> str:
    """``/stats``."""
    lines = [
        "📚 Библиотека",
        (
            f"Активных: {numbers.active} · отключено: {numbers.disabled} · "
            f"в архиве: {numbers.archived}"
        ),
    ]
    kinds = [
        f"{KIND_LABELS[kind]} {count}"
        for kind in DailyContentKind
        if (count := numbers.active_by_kind.get(kind, 0))
    ]
    lines.append("Активные по типам: " + (" · ".join(kinds) if kinds else "пока пусто"))
    return "\n".join(lines)


def album_summary(
    *,
    added_ids: Sequence[int],
    existing_ids: Sequence[int],
    archived_ids: Sequence[int],
    refused_count: int,
    failed_count: int,
) -> str:
    """One answer for a whole album."""
    total = len(added_ids) + len(existing_ids) + len(archived_ids) + refused_count + failed_count
    lines = [f"📦 Альбом: {total}"]
    if added_ids:
        lines.append(f"✅ Добавлено {len(added_ids)}: {_ids(added_ids)}")
    if existing_ids:
        lines.append(f"⚠️ Уже было {len(existing_ids)}: {_ids(existing_ids)}")
    if archived_ids:
        lines.append(
            f"🗄 Было в архиве {len(archived_ids)}: {_ids(archived_ids)} — вернуть через /item"
        )
    if refused_count:
        lines.append(f"⛔ Не поддерживается: {refused_count}")
    if failed_count:
        lines.append(f"❌ Не сохранено: {failed_count} — перешлите альбом ещё раз")
    return "\n".join(lines)


def _ids(content_ids: Sequence[int]) -> str:
    return ", ".join(f"#{content_id}" for content_id in sorted(content_ids))


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
    "ADD_TEXT_BUTTON",
    "ARCHIVE_BUTTON",
    "COMMANDS",
    "DISABLED",
    "DISABLE_BUTTON",
    "ENABLE_BUTTON",
    "HELP",
    "ITEM_USAGE",
    "KIND_LABELS",
    "NOT_ALLOWED",
    "NOT_SAVED",
    "NO_CONTENT",
    "OWNER_COMMANDS",
    "OWNER_HELP",
    "PAUSED",
    "PAUSE_BUTTON",
    "RESTORE_BUTTON",
    "RESUMED",
    "RESUME_BUTTON",
    "SHOW_BUTTON",
    "SKIP_TEXT_BUTTON",
    "STATUS_BUTTON",
    "STATUS_LABELS",
    "TEXT_GONE",
    "TEXT_PROMPT",
    "TEXT_SKIPPED",
    "TOO_FAST",
    "UNAVAILABLE",
    "UNCHANGED",
    "UNDO_BUTTON",
    "added",
    "album_summary",
    "already_added",
    "archived_twin",
    "card",
    "clock_label",
    "duplicate",
    "duration_label",
    "item_label",
    "item_not_found",
    "no_access",
    "preview_failed",
    "refused",
    "restore_blocked",
    "stats",
    "status",
    "welcome",
]
