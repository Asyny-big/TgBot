"""One answer per album the owner sends, instead of one per file.

Telegram delivers an album as separate messages sharing a ``media_group_id``,
handled concurrently. Each file is added to the library on its own, at once;
the collector only gathers the outcomes and, once the album has gone quiet for
``settle_seconds`` with no file still being saved, sends one summary.

It lives in the bot process's memory. A restart in the middle of an album
loses the summary, never a file: every file was saved, or reported as not
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

if TYPE_CHECKING:
    from aiogram import Bot

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


@dataclass(slots=True)
class _Album:
    pending: int = 0
    """Files still being saved: the summary waits for them."""

    version: int = 0
    items: list[AlbumItem] = field(default_factory=list)


class AlbumCollector:
    """Gathers the outcomes of an album's files and answers once."""

    def __init__(self, *, settle_seconds: float = ALBUM_SETTLE_SECONDS) -> None:
        self._settle = settle_seconds
        self._albums: dict[tuple[int, str], _Album] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    def begin(self, chat_id: int, group_id: str) -> None:
        """A file of the album is being saved: hold the summary back."""
        album = self._albums.setdefault((chat_id, group_id), _Album())
        album.pending += 1
        album.version += 1

    def finish(self, chat_id: int, group_id: str, item: AlbumItem, *, bot: Bot) -> None:
        """The file is done; the summary goes once the album has been quiet a while."""
        key = (chat_id, group_id)
        album = self._albums.setdefault(key, _Album(pending=1))
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
        try:
            await bot.send_message(chat_id=key[0], text=_summary(album.items))
        except TelegramAPIError as error:
            logger.warning("daily_album_summary_not_sent", chat_id=key[0], error=str(error))


def _summary(items: list[AlbumItem]) -> str:
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


__all__ = ["ALBUM_SETTLE_SECONDS", "AlbumCollector", "AlbumItem", "AlbumOutcome"]
