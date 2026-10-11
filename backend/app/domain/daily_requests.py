"""Daily Content: buyers' content requests and the owner's actors — pure rules.

A buyer with 20 or more completed purchases (the loyalty programme's own
count) may suggest content: a draft of up to ten messages, sent as one
request. The limits here are the product's, not the database's:

* ten messages a draft, a text of at most 2000 characters;
* three requests waiting for the owner at once (new or in review);
* five requests a day.

A draft lives in the bot's FSM storage for half an hour after its last change;
a request exists only once the buyer sends it.

The owner moves a request between any two statuses — the history keeps every
move — and may answer it any number of times; each answer is a message of the
Daily outbox, sent with the same pacing as everything else.

Everything done to a request or to the library is attributed: ``user:<id>``,
``admin:<username>``, or ``system`` for what the runtime records itself.
"""

from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from app.domain.entities import DailyContent
from app.domain.enums import DailyContentKind, DailyContentStatus, DailyRequestStatus

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.commands import DailyContentDraft
    from app.domain.entities import DailyRequestItem

REQUEST_MAX_ITEMS: Final = 10
REQUEST_TEXT_LIMIT: Final = 2000
REQUEST_OPEN_LIMIT: Final = 3
REQUEST_SUBMIT_LIMIT: Final = 5
REQUEST_SUBMIT_WINDOW: Final = timedelta(hours=24)
REQUEST_NOTE_LIMIT: Final = 2000
REQUEST_REPLY_LIMIT: Final = 3500
"""Leaves room in Telegram's 4096 characters for the reply's heading."""

REQUEST_DRAFT_TTL_SECONDS: Final = 30 * 60
"""A draft untouched this long is forgotten."""

REQUEST_REPLY_TTL: Final = timedelta(days=3)
"""An answer not sent by then is dropped and recorded as failed."""

OPEN_REQUEST_STATUSES: Final = frozenset({DailyRequestStatus.NEW, DailyRequestStatus.IN_REVIEW})
CLOSED_REQUEST_STATUSES: Final = frozenset(
    {DailyRequestStatus.COMPLETED, DailyRequestStatus.REJECTED}
)

SYSTEM_ACTOR: Final = "system"


class RequestAccess(StrEnum):
    """Whether a buyer may start or send a content request, and if not, why."""

    ALLOWED = "allowed"
    DISABLED = "disabled"
    """Daily Content is switched off."""

    OWNER = "owner"
    """The owner's accounts suggest nothing to themselves."""

    NOT_SUBSCRIBED = "not_subscribed"
    """Never activated in the Daily bot."""

    NOT_ELIGIBLE = "not_eligible"
    """Fewer than 20 completed purchases."""

    BANNED = "banned"
    TOO_MANY_OPEN = "too_many_open"
    RATE_LIMITED = "rate_limited"


class ItemProblem(StrEnum):
    """Why a message cannot join a draft."""

    TEXT_TOO_LONG = "text_too_long"
    DRAFT_FULL = "draft_full"


def item_problem(draft: DailyContentDraft, *, items_so_far: int) -> ItemProblem | None:
    """What keeps this message out of a draft that already holds ``items_so_far``."""
    if items_so_far >= REQUEST_MAX_ITEMS:
        return ItemProblem.DRAFT_FULL
    if (
        draft.kind is DailyContentKind.TEXT
        and draft.body is not None
        and len(draft.body) > REQUEST_TEXT_LIMIT
    ):
        return ItemProblem.TEXT_TOO_LONG
    return None


def is_closed(status: DailyRequestStatus) -> bool:
    """Completed or rejected: the request no longer waits for the owner."""
    return status in CLOSED_REQUEST_STATUSES


def reply_key(request_id: int, client_key: UUID) -> str:
    """Idempotency key of one answer: the panel sends the same key on a retry."""
    return f"reply:{request_id}:{client_key}"


def reply_outcome_key(outbox_message_id: int) -> str:
    """One ``reply_sent``/``reply_failed`` per answer, however often it is reported."""
    return f"reply-outcome:{outbox_message_id}"


def preview_key(content_id: int, owner_id: int, client_key: UUID) -> str:
    """Outbox key of a library item sent to one of the owner's accounts."""
    return f"preview:{content_id}:{owner_id}:{client_key}"


def attachment_key(item_id: int, owner_id: int, client_key: UUID) -> str:
    """Outbox key of a request's attachment sent to one of the owner's accounts."""
    return f"attachment:{item_id}:{owner_id}:{client_key}"


def attachment_as_content(item: DailyRequestItem) -> DailyContent:
    """A request's message in the shape the Daily sender takes — never stored.

    Sent by its ``file_id``, else copied from the buyer's own message, exactly
    as a library item is.
    """
    return DailyContent(
        id=item.id,
        status=DailyContentStatus.ACTIVE,
        kind=item.kind,
        source_chat_id=item.source_chat_id,
        source_message_id=item.source_message_id,
        added_by=item.source_chat_id,
        created_at=item.created_at,
        updated_at=item.created_at,
        status_changed_at=item.created_at,
        body=item.body,
        entities=item.entities,
        file_id=item.file_id,
        file_unique_id=item.file_unique_id,
        file_size=item.file_size,
        file_name=item.file_name,
        mime_type=item.mime_type,
        duration=item.duration,
    )


def user_actor(user_id: int) -> str:
    return f"user:{user_id}"


def admin_actor(username: str) -> str:
    return f"admin:{username}"


def admin_reason(username: str, action: str) -> str:
    """``status_reason`` of an admin's change to the library."""
    return f"admin:{username}:{action}"


__all__ = [
    "CLOSED_REQUEST_STATUSES",
    "OPEN_REQUEST_STATUSES",
    "REQUEST_DRAFT_TTL_SECONDS",
    "REQUEST_MAX_ITEMS",
    "REQUEST_NOTE_LIMIT",
    "REQUEST_OPEN_LIMIT",
    "REQUEST_REPLY_LIMIT",
    "REQUEST_REPLY_TTL",
    "REQUEST_SUBMIT_LIMIT",
    "REQUEST_SUBMIT_WINDOW",
    "REQUEST_TEXT_LIMIT",
    "SYSTEM_ACTOR",
    "ItemProblem",
    "RequestAccess",
    "admin_actor",
    "admin_reason",
    "attachment_as_content",
    "attachment_key",
    "is_closed",
    "item_problem",
    "preview_key",
    "reply_key",
    "reply_outcome_key",
    "user_actor",
]
