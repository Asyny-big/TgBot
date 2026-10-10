"""DailyLibraryService on the in-memory fakes: who may change the library, and how.

The fakes keep the database's duplicate rules (see ``test_repository_daily``);
these tests are about the service's own decisions. The bot's path over
PostgreSQL and Redis lives in ``test_daily_owner_flow``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from app.core.config import DailySettings
from app.core.exceptions import DailyContentRejectedError
from app.domain.commands import DailyContentDraft
from app.domain.daily import LibraryAction, next_content_status, owner_reason
from app.domain.enums import DailyContentKind, DailyContentStatus
from app.services.daily_library import (
    AddOutcome,
    DailyLibraryService,
    PreviewOutcome,
    StatusOutcome,
)
from tests.fakes import FakeUnitOfWorkFactory

if TYPE_CHECKING:
    from app.domain.entities import DailyContent

OWNER = 9900
STRANGER = 9901
T0 = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)

ACTIVE = DailyContentStatus.ACTIVE
DISABLED = DailyContentStatus.DISABLED
ARCHIVED = DailyContentStatus.ARCHIVED


@dataclass
class RecordingSender:
    """The preview's sender: records what it showed, or refuses when told to."""

    sent: list[tuple[int, int, bool]] = field(default_factory=list)
    refusal: Exception | None = None

    async def send(self, chat_id: int, content: DailyContent, *, protect_content: bool) -> int:
        if self.refusal is not None:
            raise self.refusal
        self.sent.append((chat_id, content.id, protect_content))
        return len(self.sent)


@dataclass
class Library:
    service: DailyLibraryService
    store: FakeUnitOfWorkFactory
    sender: RecordingSender


def _library(**settings: Any) -> Library:
    values: dict[str, Any] = {"enabled": True, "admin_telegram_ids": (OWNER,)}
    values.update(settings)
    store, sender = FakeUnitOfWorkFactory(), RecordingSender()
    service = DailyLibraryService(
        uow_factory=store,
        settings=DailySettings(**values),
        sender=sender,
        clock=lambda: T0,
    )
    return Library(service=service, store=store, sender=sender)


@pytest.fixture
def library() -> Library:
    return _library()


def _video(message_id: int, file: str = "clip") -> DailyContentDraft:
    return DailyContentDraft(
        kind=DailyContentKind.VIDEO,
        source_chat_id=OWNER,
        source_message_id=message_id,
        added_by=OWNER,
        file_id=f"{file}-file-{message_id}",
        file_unique_id=f"{file}-unique",
        duration=45,
        body="Ролик",
    )


def _text(message_id: int, body: str = "Мысль дня") -> DailyContentDraft:
    return DailyContentDraft(
        kind=DailyContentKind.TEXT,
        source_chat_id=OWNER,
        source_message_id=message_id,
        added_by=OWNER,
        body=body,
    )


def _items(library: Library) -> dict[int, DailyContent]:
    return library.store.unit.daily_content.items


# --------------------------------------------------------------------------- #
# The state machine                                                           #
# --------------------------------------------------------------------------- #

TRANSITIONS = [
    (ACTIVE, LibraryAction.DISABLE, DISABLED),
    (ACTIVE, LibraryAction.ENABLE, ACTIVE),
    (ACTIVE, LibraryAction.ARCHIVE, ARCHIVED),
    (ACTIVE, LibraryAction.UNDO, ARCHIVED),
    (ACTIVE, LibraryAction.RESTORE, ACTIVE),
    (DISABLED, LibraryAction.DISABLE, DISABLED),
    (DISABLED, LibraryAction.ENABLE, ACTIVE),
    (DISABLED, LibraryAction.ARCHIVE, ARCHIVED),
    (DISABLED, LibraryAction.UNDO, ARCHIVED),
    (DISABLED, LibraryAction.RESTORE, None),
    (ARCHIVED, LibraryAction.DISABLE, None),
    (ARCHIVED, LibraryAction.ENABLE, None),
    (ARCHIVED, LibraryAction.ARCHIVE, ARCHIVED),
    (ARCHIVED, LibraryAction.UNDO, ARCHIVED),
    (ARCHIVED, LibraryAction.RESTORE, ACTIVE),
]


@pytest.mark.parametrize(("current", "action", "expected"), TRANSITIONS)
def test_every_transition_of_an_item(
    current: DailyContentStatus,
    action: LibraryAction,
    expected: DailyContentStatus | None,
) -> None:
    assert next_content_status(current, action) is expected


def test_the_owners_reasons_say_it_was_the_owner() -> None:
    assert {owner_reason(action) for action in LibraryAction} == {
        "owner:disable",
        "owner:enable",
        "owner:archive",
        "owner:undo",
        "owner:restore",
    }


# --------------------------------------------------------------------------- #
# Adding                                                                      #
# --------------------------------------------------------------------------- #


async def test_a_new_item_is_added(library: Library) -> None:
    result = await library.service.add(OWNER, _video(1))

    assert result.outcome is AddOutcome.ADDED
    assert result.active_total == 1
    assert result.item is not None
    assert result.item.status is ACTIVE
    assert result.item.file_id == "clip-file-1"


async def test_the_same_message_again_is_the_item_it_became(library: Library) -> None:
    first = await library.service.add(OWNER, _video(1))
    again = await library.service.add(OWNER, _video(1))

    assert again.outcome is AddOutcome.ALREADY_ADDED
    assert again.item == first.item
    assert len(_items(library)) == 1


async def test_the_same_file_in_another_message_is_a_duplicate(library: Library) -> None:
    first = await library.service.add(OWNER, _video(1))
    other_caption = DailyContentDraft(
        kind=DailyContentKind.VIDEO,
        source_chat_id=OWNER,
        source_message_id=2,
        added_by=OWNER,
        file_id="clip-file-2",
        file_unique_id="clip-unique",
        body="Другая подпись",
    )

    result = await library.service.add(OWNER, other_caption)

    assert result.outcome is AddOutcome.DUPLICATE
    assert result.item == first.item
    assert len(_items(library)) == 1


async def test_exactly_the_same_text_is_a_duplicate(library: Library) -> None:
    first = await library.service.add(OWNER, _text(1))

    same = await library.service.add(OWNER, _text(2))
    different = await library.service.add(OWNER, _text(3, body="Мысль дня!"))

    assert same.outcome is AddOutcome.DUPLICATE
    assert same.item == first.item
    assert different.outcome is AddOutcome.ADDED
    assert len(_items(library)) == 2


async def test_a_disabled_twin_is_still_a_duplicate(library: Library) -> None:
    first = await library.service.add(OWNER, _video(1))
    assert first.item is not None
    await library.service.change_status(OWNER, first.item.id, LibraryAction.DISABLE)

    result = await library.service.add(OWNER, _video(2))

    assert result.outcome is AddOutcome.DUPLICATE
    assert result.item is not None
    assert result.item.status is DISABLED


@pytest.mark.parametrize(
    "drafts",
    [(_video(1), _video(2)), (_text(1), _text(2))],
    ids=["file", "text"],
)
async def test_an_archived_twin_is_offered_back_not_added_again(
    library: Library,
    drafts: tuple[DailyContentDraft, DailyContentDraft],
) -> None:
    original, again = drafts
    first = await library.service.add(OWNER, original)
    assert first.item is not None
    await library.service.change_status(OWNER, first.item.id, LibraryAction.ARCHIVE)

    result = await library.service.add(OWNER, again)

    assert result.outcome is AddOutcome.ARCHIVED_TWIN
    assert result.item is not None
    assert result.item.id == first.item.id
    assert result.item.status is ARCHIVED
    assert len(_items(library)) == 1


async def test_the_same_message_after_an_undo_is_still_the_item_it_became(
    library: Library,
) -> None:
    first = await library.service.add(OWNER, _video(1))
    assert first.item is not None
    await library.service.change_status(OWNER, first.item.id, LibraryAction.UNDO)

    replay = await library.service.add(OWNER, _video(1))

    assert replay.outcome is AddOutcome.ALREADY_ADDED
    assert replay.item is not None
    assert replay.item.status is ARCHIVED


async def test_nobody_but_the_owner_adds_anything(library: Library) -> None:
    result = await library.service.add(STRANGER, _video(1))

    assert result.outcome is AddOutcome.NOT_OWNER
    assert result.item is None
    assert _items(library) == {}


async def test_with_no_owner_configured_nobody_adds_anything() -> None:
    library = _library(admin_telegram_ids=())

    assert not library.service.is_owner(OWNER)
    assert (await library.service.add(OWNER, _video(1))).outcome is AddOutcome.NOT_OWNER
    assert _items(library) == {}


async def test_the_library_fills_while_daily_content_is_off() -> None:
    library = _library(enabled=False)

    result = await library.service.add(OWNER, _video(1))

    assert result.outcome is AddOutcome.ADDED
    assert library.store.unit.daily_deliveries.items == {}


async def test_adding_content_creates_no_delivery(library: Library) -> None:
    await library.service.add(OWNER, _video(1))
    await library.service.add(OWNER, _text(2))

    assert library.store.unit.daily_deliveries.items == {}


# --------------------------------------------------------------------------- #
# Changing the status                                                         #
# --------------------------------------------------------------------------- #


async def test_disable_and_enable(library: Library) -> None:
    added = await library.service.add(OWNER, _video(1))
    assert added.item is not None
    content_id = added.item.id

    disabled = await library.service.change_status(OWNER, content_id, LibraryAction.DISABLE)
    again = await library.service.change_status(OWNER, content_id, LibraryAction.DISABLE)
    enabled = await library.service.change_status(OWNER, content_id, LibraryAction.ENABLE)

    assert disabled.outcome is StatusOutcome.CHANGED
    assert disabled.item is not None
    assert disabled.item.status is DISABLED
    assert disabled.item.status_reason == "owner:disable"
    assert disabled.item.status_changed_at == T0
    assert again.outcome is StatusOutcome.UNCHANGED
    assert enabled.outcome is StatusOutcome.CHANGED
    assert enabled.item is not None
    assert enabled.item.status is ACTIVE


async def test_an_archived_item_comes_back_only_by_restoring(library: Library) -> None:
    added = await library.service.add(OWNER, _video(1))
    assert added.item is not None
    content_id = added.item.id
    await library.service.change_status(OWNER, content_id, LibraryAction.ARCHIVE)

    enabled = await library.service.change_status(OWNER, content_id, LibraryAction.ENABLE)
    disabled = await library.service.change_status(OWNER, content_id, LibraryAction.DISABLE)
    restored = await library.service.change_status(OWNER, content_id, LibraryAction.RESTORE)

    assert enabled.outcome is StatusOutcome.NOT_ALLOWED
    assert disabled.outcome is StatusOutcome.NOT_ALLOWED
    assert restored.outcome is StatusOutcome.CHANGED
    assert restored.item is not None
    assert restored.item.status is ACTIVE
    assert restored.item.status_reason == "owner:restore"


@pytest.mark.parametrize(
    "drafts",
    [(_video(1), _video(2)), (_text(1), _text(2))],
    ids=["file", "text"],
)
async def test_restoring_is_refused_while_a_live_twin_exists(
    library: Library,
    drafts: tuple[DailyContentDraft, DailyContentDraft],
) -> None:
    original, twin_draft = drafts
    first = await library.service.add(OWNER, original)
    assert first.item is not None
    await library.service.change_status(OWNER, first.item.id, LibraryAction.ARCHIVE)
    # The repository still lets the same file or text in once the first is
    # archived; only the service offers a restore instead.
    async with library.store() as uow:
        twin, _ = await uow.daily_content.add(twin_draft)

    result = await library.service.change_status(OWNER, first.item.id, LibraryAction.RESTORE)

    assert result.outcome is StatusOutcome.DUPLICATE
    assert result.twin == twin
    assert _items(library)[first.item.id].status is ARCHIVED


async def test_a_missing_item(library: Library) -> None:
    result = await library.service.change_status(OWNER, 404, LibraryAction.ARCHIVE)

    assert result.outcome is StatusOutcome.NOT_FOUND


async def test_nobody_but_the_owner_changes_a_status(library: Library) -> None:
    added = await library.service.add(OWNER, _video(1))
    assert added.item is not None

    result = await library.service.change_status(STRANGER, added.item.id, LibraryAction.ARCHIVE)

    assert result.outcome is StatusOutcome.NOT_OWNER
    assert _items(library)[added.item.id].status is ACTIVE


# --------------------------------------------------------------------------- #
# Reading and showing                                                         #
# --------------------------------------------------------------------------- #


async def test_stats_count_by_status_and_active_by_kind(library: Library) -> None:
    video = await library.service.add(OWNER, _video(1))
    await library.service.add(OWNER, _video(2, file="other"))
    text = await library.service.add(OWNER, _text(3))
    await library.service.add(OWNER, _text(4, body="Ещё мысль"))
    assert video.item is not None
    assert text.item is not None
    await library.service.change_status(OWNER, video.item.id, LibraryAction.DISABLE)
    await library.service.change_status(OWNER, text.item.id, LibraryAction.ARCHIVE)

    stats = await library.service.stats(OWNER)

    assert stats is not None
    assert (stats.active, stats.disabled, stats.archived, stats.total) == (2, 1, 1, 4)
    assert stats.active_by_kind == {DailyContentKind.VIDEO: 1, DailyContentKind.TEXT: 1}
    assert await library.service.stats(STRANGER) is None


async def test_the_card_is_the_owners(library: Library) -> None:
    added = await library.service.add(OWNER, _video(1))
    assert added.item is not None

    assert await library.service.card(OWNER, added.item.id) == added.item
    assert await library.service.card(STRANGER, added.item.id) is None
    assert await library.service.card(OWNER, 404) is None


async def test_a_preview_goes_to_the_owner_as_a_buyer_would_get_it(library: Library) -> None:
    added = await library.service.add(OWNER, _video(1))
    assert added.item is not None

    shown = await library.service.preview(OWNER, added.item.id)
    stranger = await library.service.preview(STRANGER, added.item.id)
    missing = await library.service.preview(OWNER, 404)

    assert shown.outcome is PreviewOutcome.SENT
    assert library.sender.sent == [(OWNER, added.item.id, True)]
    assert stranger.outcome is PreviewOutcome.NOT_OWNER
    assert missing.outcome is PreviewOutcome.NOT_FOUND
    assert library.store.unit.daily_deliveries.items == {}


async def test_a_refused_preview_says_why(library: Library) -> None:
    added = await library.service.add(OWNER, _video(1))
    assert added.item is not None
    library.sender.refusal = DailyContentRejectedError("wrong file identifier")

    result = await library.service.preview(OWNER, added.item.id)

    assert result.outcome is PreviewOutcome.FAILED
    assert result.error == "wrong file identifier"
