"""One answer per album, instead of one per file.

Telegram delivers an album as separate messages sharing a ``media_group_id``,
handled concurrently. Each file is handled on its own, at once — added to the
library when the owner sends it, to the draft of a content request when a
buyer does; the collector only gathers the outcomes and, once the album has
gone quiet for ``settle_seconds`` with no file still being handled, sends one
answer.

It lives in the bot process's memory. A restart in the middle of an album
loses the answer, never a file: every file was saved, or reported as not
saved, before it got here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from aiogram.exceptions import TelegramAPIError

from app.core.logging import get_logger
from app.daily_bot import texts
from app.daily_bot.keyboards import draft_keyboard

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aiogram import Bot
    from aiogram.types import InlineKeyboardMarkup

logger = get_logger(__name__)

ALBUM_SETTLE_SECONDS: Final = 2.0
"""How long an album must stay quiet before its summary goes out."""


class AlbumOutcome(StrEnum):
    """What became of one file of an album."""

    ADDED = "added"
    EXISTING = "existing"
    """Already in the library, active or disabled — or this very message again."""

    ARCHIVED = "archived"
    """In the archive: nothing was added, the owner may restore it."""

    REFUSED = "refused"
    FAILED = "failed"
    """Not saved: the database failed. Sending the album again is safe."""


@dataclass(frozen=True, slots=True)
class AlbumItem:
    """One file's outcome, and the item it concerns when there is one."""

    outcome: AlbumOutcome
    content_id: int | None = None


class DraftOutcome(StrEnum):
    """What became of one message sent into a content request's draft."""

    ADDED = "added"
    FULL = "full"
    """The draft already holds the most messages it may."""

    TOO_LONG = "too_long"
    REFUSED = "refused"
    """Not a kind of content the draft takes."""

    GONE = "gone"
    """No draft any more — expired, sent, cancelled, or the buyer lost access."""


@dataclass(frozen=True, slots=True)
class DraftItem:
    """One message's outcome, and the draft as it stood afterwards."""

    outcome: DraftOutcome
    count: int = 0
    draft_id: str | None = None
    notice: str | None = None
    """For ``GONE``: what the buyer is told instead of a summary."""


@dataclass(slots=True)
class _Album[ItemT]:
    pending: int = 0
    """Files still being handled: the answer waits for them."""

    version: int = 0
    items: list[ItemT] = field(default_factory=list)


class SettlingCollector[ItemT]:
    """Gathers the outcomes of an album's files and answers once."""

    def __init__(self, *, settle_seconds: float = ALBUM_SETTLE_SECONDS) -> None:
        self._settle = settle_seconds
        self._albums: dict[tuple[int, str], _Album[ItemT]] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def answer(self, items: Sequence[ItemT]) -> tuple[str, InlineKeyboardMarkup | None]:
        """The one answer for these outcomes."""
        raise NotImplementedError

    def begin(self, chat_id: int, group_id: str) -> None:
        """A file of the album is being handled: hold the answer back."""
        album = self._albums.setdefault((chat_id, group_id), _Album[ItemT]())
        album.pending += 1
        album.version += 1

    def finish(self, chat_id: int, group_id: str, item: ItemT, *, bot: Bot) -> None:
        """The file is done; the answer goes once the album has been quiet a while."""
        key = (chat_id, group_id)
        album = self._albums.setdefault(key, _Album[ItemT](pending=1))
        album.pending -= 1
        album.version += 1
        album.items.append(item)
        task = asyncio.create_task(self._summarise_later(key, album.version, bot))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for every summary still due — for shutdown and for tests."""
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def _summarise_later(self, key: tuple[int, str], version: int, bot: Bot) -> None:
        await asyncio.sleep(self._settle)
        album = self._albums.get(key)
        if album is None or album.version != version or album.pending:
            return  # a later file of the same album answers for it
        del self._albums[key]
        text, markup = self.answer(album.items)
        try:
            await bot.send_message(chat_id=key[0], text=text, reply_markup=markup)
        except TelegramAPIError as error:
            logger.warning("daily_album_summary_not_sent", chat_id=key[0], error=str(error))


class AlbumCollector(SettlingCollector[AlbumItem]):
    """The owner's albums: one summary of what reached the library."""

    def answer(self, items: Sequence[AlbumItem]) -> tuple[str, InlineKeyboardMarkup | None]:
        return _summary(items), None


class DraftAlbumCollector(SettlingCollector[DraftItem]):
    """A buyer's albums sent into a draft: one count, with the draft's buttons."""

    def answer(self, items: Sequence[DraftItem]) -> tuple[str, InlineKeyboardMarkup | None]:
        live = [item for item in items if item.outcome is not DraftOutcome.GONE]
        if not live:
            notice = next((item.notice for item in items if item.notice), texts.DRAFT_EXPIRED)
            return notice, None

        def count(outcome: DraftOutcome) -> int:
            return sum(1 for item in items if item.outcome is outcome)

        latest = max(live, key=lambda item: item.count)
        text = texts.draft_album(
            added=count(DraftOutcome.ADDED),
            count=latest.count,
            full=count(DraftOutcome.FULL),
            too_long=count(DraftOutcome.TOO_LONG),
            refused=count(DraftOutcome.REFUSED),
        )
        markup = draft_keyboard(latest.draft_id) if latest.draft_id is not None else None
        return text, markup


def _summary(items: Sequence[AlbumItem]) -> str:
    def ids(outcome: AlbumOutcome) -> list[int]:
        return [
            item.content_id
            for item in items
            if item.outcome is outcome and item.content_id is not None
        ]

    return texts.album_summary(
        added_ids=ids(AlbumOutcome.ADDED),
        existing_ids=ids(AlbumOutcome.EXISTING),
        archived_ids=ids(AlbumOutcome.ARCHIVED),
        refused_count=sum(1 for item in items if item.outcome is AlbumOutcome.REFUSED),
        failed_count=sum(1 for item in items if item.outcome is AlbumOutcome.FAILED),
    )


__all__ = [
    "ALBUM_SETTLE_SECONDS",
    "AlbumCollector",
    "AlbumItem",
    "AlbumOutcome",
    "DraftAlbumCollector",
    "DraftItem",
    "DraftOutcome",
    "SettlingCollector",
]
