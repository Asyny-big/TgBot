"""Daily Content requests: the pure rules — limits, keys, actors."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.domain.commands import DailyContentDraft
from app.domain.daily_requests import (
    REQUEST_MAX_ITEMS,
    REQUEST_TEXT_LIMIT,
    ItemProblem,
    admin_actor,
    admin_reason,
    attachment_as_content,
    attachment_key,
    is_closed,
    item_problem,
    preview_key,
    reply_key,
    reply_outcome_key,
    user_actor,
)
from app.domain.entities import DailyRequestItem
from app.domain.enums import DailyContentKind, DailyContentStatus, DailyRequestStatus

KEY = UUID("00000000-0000-4000-8000-000000000001")


def _text(body: str) -> DailyContentDraft:
    return DailyContentDraft(
        kind=DailyContentKind.TEXT,
        source_chat_id=1,
        source_message_id=1,
        added_by=1,
        body=body,
    )


def _video(caption: str) -> DailyContentDraft:
    return DailyContentDraft(
        kind=DailyContentKind.VIDEO,
        source_chat_id=1,
        source_message_id=2,
        added_by=1,
        body=caption,
        file_id="file",
        file_unique_id="unique",
    )


@pytest.mark.parametrize(
    ("draft", "items_so_far", "expected"),
    [
        (_text("x" * REQUEST_TEXT_LIMIT), 0, None),
        (_text("x" * (REQUEST_TEXT_LIMIT + 1)), 0, ItemProblem.TEXT_TOO_LONG),
        (_text("idea"), REQUEST_MAX_ITEMS - 1, None),
        (_text("idea"), REQUEST_MAX_ITEMS, ItemProblem.DRAFT_FULL),
        # A full draft is full whatever comes next.
        (_text("x" * (REQUEST_TEXT_LIMIT + 1)), REQUEST_MAX_ITEMS, ItemProblem.DRAFT_FULL),
        # A caption is Telegram's to limit, not the draft's.
        (_video("x" * (REQUEST_TEXT_LIMIT + 1)), 0, None),
    ],
    ids=["longest-text", "text-too-long", "tenth", "eleventh", "full-first", "long-caption"],
)
def test_what_keeps_a_message_out_of_a_draft(
    draft: DailyContentDraft,
    items_so_far: int,
    expected: ItemProblem | None,
) -> None:
    assert item_problem(draft, items_so_far=items_so_far) is expected


def test_completed_and_rejected_requests_are_closed() -> None:
    assert [status for status in DailyRequestStatus if is_closed(status)] == [
        DailyRequestStatus.COMPLETED,
        DailyRequestStatus.REJECTED,
    ]


def test_every_change_is_attributed() -> None:
    assert user_actor(42) == "user:42"
    assert admin_actor("alice") == "admin:alice"
    assert admin_reason("alice", "disable") == "admin:alice:disable"


def test_keys_name_what_they_make_idempotent() -> None:
    assert reply_key(7, KEY) == f"reply:7:{KEY}"
    assert reply_outcome_key(31) == "reply-outcome:31"
    assert preview_key(5, 100, KEY) == f"preview:5:100:{KEY}"
    assert attachment_key(9, 100, KEY) == f"attachment:9:100:{KEY}"
    # Every key fits the outbox's and the history's 200 characters.
    assert len(reply_key(2**62, KEY)) <= 200
    assert len(attachment_key(2**62, 2**62, KEY)) <= 200


def test_an_attachment_is_sent_like_a_library_item() -> None:
    at = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
    item = DailyRequestItem(
        id=12,
        request_id=3,
        position=2,
        kind=DailyContentKind.VOICE,
        source_chat_id=777,
        source_message_id=55,
        created_at=at,
        body="listen",
        entities=({"type": "bold", "offset": 0, "length": 6},),
        file_id="voice-file",
        file_unique_id="voice-unique",
        file_size=12_000,
        mime_type="audio/ogg",
        duration=9,
    )

    content = attachment_as_content(item)

    assert content.status is DailyContentStatus.ACTIVE
    assert (content.kind, content.file_id, content.body, content.entities) == (
        DailyContentKind.VOICE,
        "voice-file",
        "listen",
        item.entities,
    )
    # Copied from the buyer's own message when Telegram refuses the file id.
    assert (content.source_chat_id, content.source_message_id) == (777, 55)
    assert (content.duration, content.mime_type, content.file_size) == (9, "audio/ogg", 12_000)
