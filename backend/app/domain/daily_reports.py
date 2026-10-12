"""Daily Content: what the admin panel reads — filters and rows, no rules.

The panel only *reads* through these; every change goes through the services
that own it (the library service, the request service), so a page of the panel
can never bypass a rule the bot obeys.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date, datetime

    from app.domain.daily import LibraryStats
    from app.domain.entities import (
        DailyAlert,
        DailyContent,
        DailyContentRequest,
        DailyDelivery,
        DailyDeliveryAttempt,
        DailyOutboxMessage,
        DailyRequestEvent,
        DailyRequestItem,
        DailySubscription,
    )
    from app.domain.enums import (
        DailyContentKind,
        DailyContentStatus,
        DailyDeliveryStatus,
        DailyRequestStatus,
        DailySubscriptionStatus,
    )


# --------------------------------------------------------------------------- #
# Filters                                                                     #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DailyLibraryFilters:
    """The library listing's filters.

    ``search`` matches an item's number exactly, or its text, caption or file
    name in part.
    """

    status: DailyContentStatus | None = None
    kind: DailyContentKind | None = None
    search: str | None = None


@dataclass(frozen=True, slots=True)
class DailySubscriberFilters:
    """The subscriber listing's filters; ``search`` matches the Telegram id or username."""

    status: DailySubscriptionStatus | None = None
    blocked: bool | None = None
    banned: bool | None = None
    search: str | None = None


@dataclass(frozen=True, slots=True)
class DailyDeliveryFilters:
    """The delivery listing's filters."""

    status: DailyDeliveryStatus | None = None
    user_id: int | None = None
    delivery_date: date | None = None


@dataclass(frozen=True, slots=True)
class DailyRequestFilters:
    """The content request listing's filters."""

    status: DailyRequestStatus | None = None
    user_id: int | None = None


# --------------------------------------------------------------------------- #
# Rows                                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyBuyerName:
    """How the shop knows a buyer — for display only."""

    username: str | None = None
    first_name: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyLibraryEntry:
    """A library item, and how many buyers have it."""

    item: DailyContent
    sent_count: int
    """Deliveries of this item that reached a buyer."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DailySubscriberEntry:
    """A buyer's Daily subscription, with what the panel shows next to it."""

    subscription: DailySubscription
    buyer: DailyBuyerName
    completed_purchases: int
    """The loyalty programme's own count — the one access is decided on."""

    received: int
    last_received_at: datetime | None
    open_requests: int


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyDeliveryEntry:
    """A delivery, with the buyer's name."""

    delivery: DailyDelivery
    buyer: DailyBuyerName


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyDeliveryDetail:
    """A delivery with its attempts and the item it holds."""

    entry: DailyDeliveryEntry
    attempts: tuple[DailyDeliveryAttempt, ...]
    item: DailyContent | None


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyRequestEntry:
    """A content request, as a row of the listing."""

    request: DailyContentRequest
    buyer: DailyBuyerName
    item_count: int
    reply_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyReply:
    """One reply of the owner's, and where its message stands in the outbox."""

    event: DailyRequestEvent
    """The ``reply_queued`` event: the text, who wrote it, when."""

    message: DailyOutboxMessage | None
    outcome: DailyRequestEvent | None = None
    """``reply_sent`` or ``reply_failed`` once the outbox knows."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyRequestDetail:
    """Everything the panel shows of one request."""

    entry: DailyRequestEntry
    items: tuple[DailyRequestItem, ...]
    events: tuple[DailyRequestEvent, ...]
    replies: tuple[DailyReply, ...]
    banned: bool
    """Whether the buyer is banned from sending requests now."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DailySubscriberDetail:
    """A subscriber, their latest deliveries and their requests."""

    entry: DailySubscriberEntry
    deliveries: tuple[DailyDelivery, ...]
    requests: tuple[DailyContentRequest, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyOverviewCounts:
    """The counters of the overview page, read in one go."""

    subscriptions: Mapping[DailySubscriptionStatus, int]
    blocked: int
    banned: int
    deliveries_today: Mapping[DailyDeliveryStatus, int]
    requests: Mapping[DailyRequestStatus, int]
    outbox_open: int
    """Messages waiting to be sent, or being sent."""

    outbox_failed_recently: int
    """Messages given up on, or dropped unsent, since the cut-off."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyOverview:
    """The overview page."""

    enabled: bool
    today: date
    counts: DailyOverviewCounts
    library: LibraryStats
    alerts: tuple[DailyAlert, ...]


__all__ = [
    "DailyBuyerName",
    "DailyDeliveryDetail",
    "DailyDeliveryEntry",
    "DailyDeliveryFilters",
    "DailyLibraryEntry",
    "DailyLibraryFilters",
    "DailyOverview",
    "DailyOverviewCounts",
    "DailyReply",
    "DailyRequestDetail",
    "DailyRequestEntry",
    "DailyRequestFilters",
    "DailySubscriberDetail",
    "DailySubscriberEntry",
    "DailySubscriberFilters",
]
