"""Daily Content: the owner's library — adding items, switching them off and on.

Only the owner's accounts (``DAILY_ADMIN_TELEGRAM_IDS``) may change the
library; the service checks that itself, whatever the caller already checked.
With no owner configured nobody can.

**An item is a file or a text.** The same Telegram message added twice is the
item it already became; the same file, or exactly the same text, sent again in
another message is a duplicate of the item that holds it. When that item is
archived nothing new is created either: the owner is offered to restore it,
so a buyer who received it once never gets it again under another number.

**Nothing is deleted.** Items move between active, disabled and archived, and
only active ones are offered to new deliveries.

Adding content delivers nothing and plans nothing: that stays the delivery
engine's job. It works whether Daily Content is switched on or not, so the
library can be filled before the launch.

The admin panel changes items through ``change_status_as_admin``: the same
lock, the same transitions and the same duplicate check as the owner's
buttons, attributed ``admin:<username>:<action>`` — so the owner in Telegram
and an admin in the panel can never interleave on one item.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from app.core.exceptions import DailyContentDuplicateError, DailySendError
from app.core.logging import get_logger
from app.domain.daily import next_content_status, owner_reason
from app.domain.daily_requests import admin_actor, admin_reason
from app.domain.enums import DailyContentKind, DailyContentStatus

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.config import DailySettings
    from app.domain.commands import DailyContentDraft
    from app.domain.daily import DailyContentSender, LibraryAction, LibraryStats
    from app.domain.entities import DailyContent
    from app.domain.uow import UnitOfWork, UnitOfWorkFactory

logger = get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class AddOutcome(StrEnum):
    """What adding one message to the library did."""

    ADDED = "added"
    """A new item."""

    ALREADY_ADDED = "already_added"
    """This very message was added before: the item it became."""

    DUPLICATE = "duplicate"
    """The same file or text is already in the library, active or disabled."""

    ARCHIVED_TWIN = "archived_twin"
    """The same file or text is in the archive: nothing was added, restore it."""

    NOT_OWNER = "not_owner"
    """Not one of the owner's accounts: nothing was looked at."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AddResult:
    """The outcome of adding one message."""

    outcome: AddOutcome
    item: DailyContent | None = None
    """The new item, the one this message already became, or its twin."""

    active_total: int = 0
    """Active items in the library afterwards."""


class StatusOutcome(StrEnum):
    """What an owner's action on an item did."""

    CHANGED = "changed"
    UNCHANGED = "unchanged"
    """Already where the action leads."""

    NOT_ALLOWED = "not_allowed"
    """Not from this status — an archived item comes back only by restoring."""

    DUPLICATE = "duplicate"
    """Restoring it would put the same file or text into the library twice."""

    NOT_FOUND = "not_found"
    NOT_OWNER = "not_owner"


@dataclass(frozen=True, slots=True, kw_only=True)
class StatusResult:
    """The outcome of an owner's action, and the item as it is now."""

    outcome: StatusOutcome
    item: DailyContent | None = None
    twin: DailyContent | None = None
    """For ``DUPLICATE``: the live item that already holds the file or text."""


class PreviewOutcome(StrEnum):
    """What showing an item to the owner did."""

    SENT = "sent"
    FAILED = "failed"
    NOT_FOUND = "not_found"
    NOT_OWNER = "not_owner"


@dataclass(frozen=True, slots=True, kw_only=True)
class PreviewResult:
    """The outcome of a preview; ``error`` says why Telegram refused it."""

    outcome: PreviewOutcome
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DailyLibraryService:
    """The owner's side of the Daily Content library."""

    uow_factory: UnitOfWorkFactory
    settings: DailySettings
    sender: DailyContentSender | None = None
    """Shows an item to the owner exactly as a buyer gets it; only the Daily
    bot's process has one — the admin panel sends through the outbox instead."""

    clock: Callable[[], datetime] = field(default=_utc_now)

    def is_owner(self, user_id: int) -> bool:
        """Whether this account is one of the owner's; nobody when none is configured."""
        return user_id in self.settings.admin_telegram_ids

    async def add(self, actor_id: int, draft: DailyContentDraft) -> AddResult:
        """Add one message of the owner's to the library."""
        if not self.is_owner(actor_id):
            logger.warning("daily_library_refused", actor_id=actor_id, action="add")
            return AddResult(outcome=AddOutcome.NOT_OWNER)
        async with self.uow_factory() as uow:
            twin = await self._twin_of(uow, draft)
            if twin is not None and not _same_message(twin, draft):
                outcome = (
                    AddOutcome.ARCHIVED_TWIN
                    if twin.status is DailyContentStatus.ARCHIVED
                    else AddOutcome.DUPLICATE
                )
                return await self._result(uow, outcome, twin)
            try:
                item, created = await uow.daily_content.add(draft)
            except DailyContentDuplicateError as duplicate:
                # Another message with the same file or text won the race.
                existing = await uow.daily_content.get(duplicate.existing_id)
                return await self._result(uow, AddOutcome.DUPLICATE, existing)
            result = await self._result(
                uow,
                AddOutcome.ADDED if created else AddOutcome.ALREADY_ADDED,
                item,
            )
        if created:
            logger.info(
                "daily_content_added",
                content_id=item.id,
                kind=item.kind.value,
                added_by=actor_id,
            )
        return result

    async def change_status(
        self,
        actor_id: int,
        content_id: int,
        action: LibraryAction,
    ) -> StatusResult:
        """Disable, enable, archive or restore an item. Nothing is ever deleted."""
        if not self.is_owner(actor_id):
            logger.warning("daily_library_refused", actor_id=actor_id, action=action.value)
            return StatusResult(outcome=StatusOutcome.NOT_OWNER)
        return await self._change_status(
            content_id,
            action,
            reason=owner_reason(action),
            actor=str(actor_id),
        )

    async def change_status_as_admin(
        self,
        username: str,
        content_id: int,
        action: LibraryAction,
    ) -> StatusResult:
        """The owner's action, taken from the admin panel by ``username``."""
        return await self._change_status(
            content_id,
            action,
            reason=admin_reason(username, action.value),
            actor=admin_actor(username),
        )

    async def _change_status(
        self,
        content_id: int,
        action: LibraryAction,
        *,
        reason: str,
        actor: str,
    ) -> StatusResult:
        async with self.uow_factory() as uow:
            current = await uow.daily_content.lock(content_id)
            if current is None:
                return StatusResult(outcome=StatusOutcome.NOT_FOUND)
            target = next_content_status(current.status, action)
            if target is None:
                return StatusResult(outcome=StatusOutcome.NOT_ALLOWED, item=current)
            if target is current.status:
                return StatusResult(outcome=StatusOutcome.UNCHANGED, item=current)
            try:
                changed = await uow.daily_content.set_status(
                    content_id,
                    target,
                    at=self.clock(),
                    reason=reason,
                )
            except DailyContentDuplicateError as duplicate:
                twin = await uow.daily_content.get(duplicate.existing_id)
                return StatusResult(outcome=StatusOutcome.DUPLICATE, item=current, twin=twin)
        logger.info(
            "daily_content_status_changed",
            content_id=content_id,
            status=target.value,
            action=action.value,
            actor=actor,
        )
        return StatusResult(outcome=StatusOutcome.CHANGED, item=changed)

    async def card(self, actor_id: int, content_id: int) -> DailyContent | None:
        """One item for the owner's eyes; ``None`` when missing or not the owner."""
        if not self.is_owner(actor_id):
            return None
        async with self.uow_factory() as uow:
            return await uow.daily_content.get(content_id)

    async def stats(self, actor_id: int) -> LibraryStats | None:
        """The library in numbers; ``None`` for anybody but the owner."""
        if not self.is_owner(actor_id):
            return None
        async with self.uow_factory() as uow:
            return await uow.daily_content.stats()

    async def preview(self, actor_id: int, content_id: int) -> PreviewResult:
        """Send an item to the owner's own chat, as a buyer would get it.

        A preview, not a delivery: nothing is recorded for anybody.
        """
        if not self.is_owner(actor_id):
            return PreviewResult(outcome=PreviewOutcome.NOT_OWNER)
        item = await self.card(actor_id, content_id)
        if item is None:
            return PreviewResult(outcome=PreviewOutcome.NOT_FOUND)
        if self.sender is None:  # pragma: no cover — the bot's process always has one
            return PreviewResult(outcome=PreviewOutcome.FAILED, error="No Daily bot here")
        try:
            await self.sender.send(
                actor_id,
                item,
                protect_content=self.settings.protect_content,
            )
        except DailySendError as error:
            logger.warning("daily_content_preview_failed", content_id=content_id, error=str(error))
            return PreviewResult(outcome=PreviewOutcome.FAILED, error=str(error))
        return PreviewResult(outcome=PreviewOutcome.SENT)

    @staticmethod
    async def _twin_of(uow: UnitOfWork, draft: DailyContentDraft) -> DailyContent | None:
        """The item already holding this file or text — live first, else archived."""
        if draft.file_unique_id is not None:
            return await uow.daily_content.find_by_file(draft.file_unique_id)
        if draft.kind is DailyContentKind.TEXT and draft.body is not None:
            return await uow.daily_content.find_by_text(draft.body)
        return None

    @staticmethod
    async def _result(
        uow: UnitOfWork,
        outcome: AddOutcome,
        item: DailyContent | None,
    ) -> AddResult:
        return AddResult(
            outcome=outcome,
            item=item,
            active_total=await uow.daily_content.count_active(),
        )


def _same_message(item: DailyContent, draft: DailyContentDraft) -> bool:
    """Whether ``item`` came from the very message ``draft`` describes."""
    return (item.source_chat_id, item.source_message_id) == (
        draft.source_chat_id,
        draft.source_message_id,
    )


__all__ = [
    "AddOutcome",
    "AddResult",
    "DailyLibraryService",
    "PreviewOutcome",
    "PreviewResult",
    "StatusOutcome",
    "StatusResult",
]
