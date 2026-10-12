"""Daily Content schemas of the admin panel.

Nothing here carries a file, a ``file_id`` or a token: the panel shows what an
item *is* — kind, caption, name, size, length — and asks the Daily bot to send
it to the owner's own Telegram when the owner wants to see it.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.api.schemas.common import ApiModel
from app.domain.daily import LibraryAction, can_suggest_content
from app.domain.daily_requests import REQUEST_NOTE_LIMIT, REQUEST_REPLY_LIMIT
from app.domain.enums import (
    DailyAlertKey,
    DailyAlertState,
    DailyAttemptOutcome,
    DailyContentKind,
    DailyContentStatus,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    DailyOutboxKind,
    DailyOutboxStatus,
    DailyRequestEventKind,
    DailyRequestStatus,
    DailySubscriptionStatus,
)
from app.domain.patch import UNSET, Maybe

if TYPE_CHECKING:
    from app.domain.daily import LibraryStats
    from app.domain.daily_reports import (
        DailyBuyerName,
        DailyDeliveryDetail,
        DailyDeliveryEntry,
        DailyLibraryEntry,
        DailyOverview,
        DailyReply,
        DailyRequestDetail,
        DailyRequestEntry,
        DailySubscriberDetail,
        DailySubscriberEntry,
    )
    from app.domain.entities import (
        DailyAlert,
        DailyContent,
        DailyContentRequest,
        DailyDelivery,
        DailyDeliveryAttempt,
        DailyOutboxMessage,
        DailyRequestEvent,
        DailyRequestItem,
    )


# --------------------------------------------------------------------------- #
# Shared                                                                      #
# --------------------------------------------------------------------------- #


class DailyBuyerResponse(ApiModel):
    """How the shop knows a buyer."""

    user_id: int
    username: str | None
    first_name: str | None

    @classmethod
    def of(cls, user_id: int, name: DailyBuyerName) -> DailyBuyerResponse:
        return cls(user_id=user_id, username=name.username, first_name=name.first_name)


class DailyOutboxMessageResponse(ApiModel):
    """A message of the Daily outbox: where it stands."""

    id: int
    kind: DailyOutboxKind
    status: DailyOutboxStatus
    chat_id: int
    attempts: int
    next_attempt_at: datetime
    deadline_at: datetime
    sent_at: datetime | None
    last_error: str | None
    created_at: datetime

    @classmethod
    def from_domain(cls, message: DailyOutboxMessage) -> DailyOutboxMessageResponse:
        return cls(
            id=message.id,
            kind=message.kind,
            status=message.status,
            chat_id=message.chat_id,
            attempts=message.attempts,
            next_attempt_at=message.next_attempt_at,
            deadline_at=message.deadline_at,
            sent_at=message.sent_at,
            last_error=message.last_error,
            created_at=message.created_at,
        )


class DailySendToOwnerRequest(ApiModel):
    """ "Send it to me": the key makes a repeated click — or a retry — one send."""

    idempotency_key: UUID


class DailySendToOwnerResponse(ApiModel):
    """The messages taking the item to the owner's accounts."""

    messages: list[DailyOutboxMessageResponse]
    created: bool
    """``false``: the same key came back; nothing new was queued."""


# --------------------------------------------------------------------------- #
# Overview                                                                    #
# --------------------------------------------------------------------------- #


class DailyKindCountResponse(ApiModel):
    kind: DailyContentKind
    count: int


class DailyLibraryCountsResponse(ApiModel):
    active: int
    disabled: int
    archived: int
    total: int
    active_by_kind: list[DailyKindCountResponse]

    @classmethod
    def from_domain(cls, stats: LibraryStats) -> DailyLibraryCountsResponse:
        return cls(
            active=stats.active,
            disabled=stats.disabled,
            archived=stats.archived,
            total=stats.total,
            active_by_kind=[
                DailyKindCountResponse(kind=kind, count=count)
                for kind, count in sorted(stats.active_by_kind.items())
            ],
        )


class DailySubscriptionCountsResponse(ApiModel):
    inactive: int
    active: int
    paused: int
    blocked: int
    """Subscribers who have the bot blocked, whatever their status."""

    banned: int
    """Subscribers banned from sending content requests."""


class DailyDeliveryCountsResponse(ApiModel):
    pending: int
    sending: int
    sent: int
    no_content: int
    failed: int
    expired: int
    cancelled: int


class DailyRequestCountsResponse(ApiModel):
    new: int
    in_review: int
    completed: int
    rejected: int


class DailyOutboxCountsResponse(ApiModel):
    open: int
    """Waiting to be sent, or being sent."""

    failed_recently: int
    """Given up on, or dropped unsent, in the last 24 hours."""


class DailyAlertResponse(ApiModel):
    """One of the owner's alerts."""

    key: DailyAlertKey
    state: DailyAlertState
    notifications: int
    value: dict[str, Any]
    updated_at: datetime
    fired_at: datetime | None
    notified_at: datetime | None
    resolved_at: datetime | None

    @classmethod
    def from_domain(cls, alert: DailyAlert) -> DailyAlertResponse:
        return cls(
            key=alert.key,
            state=alert.state,
            notifications=alert.notifications,
            value=dict(alert.value),
            updated_at=alert.updated_at,
            fired_at=alert.fired_at,
            notified_at=alert.notified_at,
            resolved_at=alert.resolved_at,
        )


class DailyOverviewResponse(ApiModel):
    """The Daily Content overview page."""

    enabled: bool
    today: date
    """Today in Moscow — the date deliveries are counted for."""

    subscriptions: DailySubscriptionCountsResponse
    deliveries_today: DailyDeliveryCountsResponse
    requests: DailyRequestCountsResponse
    outbox: DailyOutboxCountsResponse
    library: DailyLibraryCountsResponse
    alerts: list[DailyAlertResponse]

    @classmethod
    def from_domain(cls, overview: DailyOverview) -> DailyOverviewResponse:
        counts = overview.counts
        subscriptions = counts.subscriptions
        deliveries = counts.deliveries_today
        requests = counts.requests
        return cls(
            enabled=overview.enabled,
            today=overview.today,
            subscriptions=DailySubscriptionCountsResponse(
                inactive=subscriptions.get(DailySubscriptionStatus.INACTIVE, 0),
                active=subscriptions.get(DailySubscriptionStatus.ACTIVE, 0),
                paused=subscriptions.get(DailySubscriptionStatus.PAUSED, 0),
                blocked=counts.blocked,
                banned=counts.banned,
            ),
            deliveries_today=DailyDeliveryCountsResponse(
                pending=deliveries.get(DailyDeliveryStatus.PENDING, 0),
                sending=deliveries.get(DailyDeliveryStatus.SENDING, 0),
                sent=deliveries.get(DailyDeliveryStatus.SENT, 0),
                no_content=deliveries.get(DailyDeliveryStatus.NO_CONTENT, 0),
                failed=deliveries.get(DailyDeliveryStatus.FAILED, 0),
                expired=deliveries.get(DailyDeliveryStatus.EXPIRED, 0),
                cancelled=deliveries.get(DailyDeliveryStatus.CANCELLED, 0),
            ),
            requests=DailyRequestCountsResponse(
                new=requests.get(DailyRequestStatus.NEW, 0),
                in_review=requests.get(DailyRequestStatus.IN_REVIEW, 0),
                completed=requests.get(DailyRequestStatus.COMPLETED, 0),
                rejected=requests.get(DailyRequestStatus.REJECTED, 0),
            ),
            outbox=DailyOutboxCountsResponse(
                open=counts.outbox_open,
                failed_recently=counts.outbox_failed_recently,
            ),
            library=DailyLibraryCountsResponse.from_domain(overview.library),
            alerts=[DailyAlertResponse.from_domain(alert) for alert in overview.alerts],
        )


# --------------------------------------------------------------------------- #
# Library                                                                     #
# --------------------------------------------------------------------------- #


class DailyLibraryActionName(StrEnum):
    """What the panel can do to a library item. Nothing deletes."""

    DISABLE = "disable"
    ENABLE = "enable"
    ARCHIVE = "archive"
    RESTORE = "restore"

    @property
    def action(self) -> LibraryAction:
        return LibraryAction(self.value)


class DailyContentResponse(ApiModel):
    """A library item, as the panel shows it — never its file."""

    id: int
    status: DailyContentStatus
    kind: DailyContentKind
    body: str | None
    """The text, or the media's caption."""

    file_name: str | None
    mime_type: str | None
    file_size: int | None
    duration: int | None
    width: int | None
    height: int | None
    added_by: int
    created_at: datetime
    status_changed_at: datetime
    status_reason: str | None
    """``owner:<action>``, ``admin:<username>:<action>``, or the engine's own."""

    sent_count: int | None
    """Buyers who received it; ``null`` where it is not counted."""

    @classmethod
    def from_domain(
        cls,
        item: DailyContent,
        *,
        sent_count: int | None = None,
    ) -> DailyContentResponse:
        return cls(
            id=item.id,
            status=item.status,
            kind=item.kind,
            body=item.body,
            file_name=item.file_name,
            mime_type=item.mime_type,
            file_size=item.file_size,
            duration=item.duration,
            width=item.width,
            height=item.height,
            added_by=item.added_by,
            created_at=item.created_at,
            status_changed_at=item.status_changed_at,
            status_reason=item.status_reason,
            sent_count=sent_count,
        )

    @classmethod
    def from_entry(cls, entry: DailyLibraryEntry) -> DailyContentResponse:
        return cls.from_domain(entry.item, sent_count=entry.sent_count)


class DailyLibraryStatusRequest(ApiModel):
    action: DailyLibraryActionName


# --------------------------------------------------------------------------- #
# Deliveries                                                                  #
# --------------------------------------------------------------------------- #


class DailyDeliveryResponse(ApiModel):
    """One buyer's delivery for one day."""

    id: int
    user_id: int
    buyer: DailyBuyerResponse | None
    delivery_date: date
    trigger: DailyDeliveryTrigger
    status: DailyDeliveryStatus
    scheduled_for: datetime
    window_opens_at: datetime
    window_closes_at: datetime
    deadline_at: datetime
    next_attempt_at: datetime
    attempts: int
    content_id: int | None
    sent_at: datetime | None
    last_outcome: DailyAttemptOutcome | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(
        cls,
        delivery: DailyDelivery,
        *,
        buyer: DailyBuyerResponse | None = None,
    ) -> DailyDeliveryResponse:
        return cls(
            id=delivery.id,
            user_id=delivery.user_id,
            buyer=buyer,
            delivery_date=delivery.delivery_date,
            trigger=delivery.trigger,
            status=delivery.status,
            scheduled_for=delivery.scheduled_for,
            window_opens_at=delivery.window_opens_at,
            window_closes_at=delivery.window_closes_at,
            deadline_at=delivery.deadline_at,
            next_attempt_at=delivery.next_attempt_at,
            attempts=delivery.attempts,
            content_id=delivery.content_id,
            sent_at=delivery.sent_at,
            last_outcome=delivery.last_outcome,
            last_error=delivery.last_error,
            created_at=delivery.created_at,
            updated_at=delivery.updated_at,
        )

    @classmethod
    def from_entry(cls, entry: DailyDeliveryEntry) -> DailyDeliveryResponse:
        delivery = entry.delivery
        return cls.from_domain(
            delivery,
            buyer=DailyBuyerResponse.of(delivery.user_id, entry.buyer),
        )


class DailyDeliveryAttemptResponse(ApiModel):
    id: int
    attempt_no: int
    started_at: datetime
    finished_at: datetime | None
    content_id: int | None
    outcome: DailyAttemptOutcome | None
    error: str | None
    retry_after_seconds: int | None
    telegram_message_id: int | None

    @classmethod
    def from_domain(cls, attempt: DailyDeliveryAttempt) -> DailyDeliveryAttemptResponse:
        return cls(
            id=attempt.id,
            attempt_no=attempt.attempt_no,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
            content_id=attempt.content_id,
            outcome=attempt.outcome,
            error=attempt.error,
            retry_after_seconds=attempt.retry_after_seconds,
            telegram_message_id=attempt.telegram_message_id,
        )


class DailyDeliveryDetailResponse(ApiModel):
    delivery: DailyDeliveryResponse
    attempts: list[DailyDeliveryAttemptResponse]
    item: DailyContentResponse | None

    @classmethod
    def from_domain(cls, detail: DailyDeliveryDetail) -> DailyDeliveryDetailResponse:
        return cls(
            delivery=DailyDeliveryResponse.from_entry(detail.entry),
            attempts=[DailyDeliveryAttemptResponse.from_domain(item) for item in detail.attempts],
            item=DailyContentResponse.from_domain(detail.item) if detail.item is not None else None,
        )


# --------------------------------------------------------------------------- #
# Requests                                                                    #
# --------------------------------------------------------------------------- #


class DailyRequestResponse(ApiModel):
    """A content request; ``version`` is what a change must name."""

    id: int
    user_id: int
    buyer: DailyBuyerResponse | None
    status: DailyRequestStatus
    version: int
    purchases_at_submit: int
    admin_note: str | None
    item_count: int | None
    reply_count: int | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None

    @classmethod
    def from_domain(
        cls,
        request: DailyContentRequest,
        *,
        buyer: DailyBuyerResponse | None = None,
        item_count: int | None = None,
        reply_count: int | None = None,
    ) -> DailyRequestResponse:
        return cls(
            id=request.id,
            user_id=request.user_id,
            buyer=buyer,
            status=request.status,
            version=request.version,
            purchases_at_submit=request.purchases_at_submit,
            admin_note=request.admin_note,
            item_count=item_count,
            reply_count=reply_count,
            created_at=request.created_at,
            updated_at=request.updated_at,
            closed_at=request.closed_at,
        )

    @classmethod
    def from_entry(cls, entry: DailyRequestEntry) -> DailyRequestResponse:
        request = entry.request
        return cls.from_domain(
            request,
            buyer=DailyBuyerResponse.of(request.user_id, entry.buyer),
            item_count=entry.item_count,
            reply_count=entry.reply_count,
        )


class DailyRequestItemResponse(ApiModel):
    """One message of a request — its text, or what its file is."""

    id: int
    position: int
    kind: DailyContentKind
    body: str | None
    file_name: str | None
    mime_type: str | None
    file_size: int | None
    duration: int | None
    has_file: bool
    created_at: datetime

    @classmethod
    def from_domain(cls, item: DailyRequestItem) -> DailyRequestItemResponse:
        return cls(
            id=item.id,
            position=item.position,
            kind=item.kind,
            body=item.body,
            file_name=item.file_name,
            mime_type=item.mime_type,
            file_size=item.file_size,
            duration=item.duration,
            has_file=item.file_id is not None,
            created_at=item.created_at,
        )


class DailyRequestEventResponse(ApiModel):
    """A line of the request's history."""

    id: int
    kind: DailyRequestEventKind
    actor: str
    """``user:<id>``, ``admin:<username>`` or ``system``."""

    created_at: datetime
    from_status: DailyRequestStatus | None
    to_status: DailyRequestStatus | None
    body: str | None
    outbox_message_id: int | None

    @classmethod
    def from_domain(cls, event: DailyRequestEvent) -> DailyRequestEventResponse:
        return cls(
            id=event.id,
            kind=event.kind,
            actor=event.actor,
            created_at=event.created_at,
            from_status=event.from_status,
            to_status=event.to_status,
            body=event.body,
            outbox_message_id=event.outbox_message_id,
        )


class DailyReplyState(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"


class DailyReplyResponse(ApiModel):
    """One answer of the owner's and its fate."""

    state: DailyReplyState
    event: DailyRequestEventResponse
    message: DailyOutboxMessageResponse | None
    outcome: DailyRequestEventResponse | None

    @classmethod
    def from_domain(cls, reply: DailyReply) -> DailyReplyResponse:
        outcome = reply.outcome
        if outcome is None:
            state = DailyReplyState.QUEUED
        elif outcome.kind is DailyRequestEventKind.REPLY_SENT:
            state = DailyReplyState.SENT
        else:
            state = DailyReplyState.FAILED
        return cls(
            state=state,
            event=DailyRequestEventResponse.from_domain(reply.event),
            message=(
                DailyOutboxMessageResponse.from_domain(reply.message)
                if reply.message is not None
                else None
            ),
            outcome=DailyRequestEventResponse.from_domain(outcome) if outcome is not None else None,
        )


class DailyRequestDetailResponse(ApiModel):
    request: DailyRequestResponse
    items: list[DailyRequestItemResponse]
    events: list[DailyRequestEventResponse]
    replies: list[DailyReplyResponse]
    banned: bool
    """Whether the buyer is banned from sending requests now."""

    @classmethod
    def from_domain(cls, detail: DailyRequestDetail) -> DailyRequestDetailResponse:
        return cls(
            request=DailyRequestResponse.from_entry(detail.entry),
            items=[DailyRequestItemResponse.from_domain(item) for item in detail.items],
            events=[DailyRequestEventResponse.from_domain(event) for event in detail.events],
            replies=[DailyReplyResponse.from_domain(reply) for reply in detail.replies],
            banned=detail.banned,
        )


class DailyRequestPatchRequest(ApiModel):
    """A change of status or note, as read at ``version``.

    Omitting a field leaves it alone; ``admin_note: null`` (or empty) clears the
    note. ``status`` cannot be null.
    """

    version: int = Field(ge=1)
    status: DailyRequestStatus | None = None
    admin_note: str | None = Field(default=None, max_length=REQUEST_NOTE_LIMIT)

    @model_validator(mode="after")
    def _status_is_not_null(self) -> Self:
        if "status" in self.model_fields_set and self.status is None:
            msg = "status cannot be null"
            raise ValueError(msg)
        return self

    @property
    def status_change(self) -> Maybe[DailyRequestStatus]:
        return self.status if self.status is not None else UNSET

    @property
    def note_change(self) -> Maybe[str | None]:
        return self.admin_note if "admin_note" in self.model_fields_set else UNSET


class DailyReplyRequest(ApiModel):
    """An answer to the buyer; the key makes a retry the same answer."""

    text: str = Field(min_length=1, max_length=REQUEST_REPLY_LIMIT)
    idempotency_key: UUID


class DailyReplyResultResponse(ApiModel):
    reply: DailyReplyResponse
    created: bool
    """``false``: the same key came back — the answer was queued before."""


# --------------------------------------------------------------------------- #
# Subscribers                                                                 #
# --------------------------------------------------------------------------- #


class DailySubscriberResponse(ApiModel):
    """A buyer's Daily subscription and its numbers."""

    user_id: int
    username: str | None
    first_name: str | None
    status: DailySubscriptionStatus
    first_interaction_at: datetime
    activated_at: datetime | None
    anchor_minute: int | None
    """Minute of the day, Moscow time: the centre of the buyer's window."""

    paused_at: datetime | None
    blocked_at: datetime | None
    requests_banned_at: datetime | None
    requests_banned_by: str | None
    completed_purchases: int
    received: int
    last_received_at: datetime | None
    open_requests: int
    can_suggest: bool
    """20 or more completed purchases and no ban: "💡 Suggest content" is theirs."""

    @classmethod
    def from_entry(cls, entry: DailySubscriberEntry) -> DailySubscriberResponse:
        subscription = entry.subscription
        return cls(
            user_id=subscription.user_id,
            username=entry.buyer.username,
            first_name=entry.buyer.first_name,
            status=subscription.status,
            first_interaction_at=subscription.first_interaction_at,
            activated_at=subscription.activated_at,
            anchor_minute=subscription.anchor_minute,
            paused_at=subscription.paused_at,
            blocked_at=subscription.blocked_at,
            requests_banned_at=subscription.requests_banned_at,
            requests_banned_by=subscription.requests_banned_by,
            completed_purchases=entry.completed_purchases,
            received=entry.received,
            last_received_at=entry.last_received_at,
            open_requests=entry.open_requests,
            can_suggest=(
                can_suggest_content(entry.completed_purchases)
                and subscription.requests_banned_at is None
            ),
        )


class DailySubscriberDetailResponse(ApiModel):
    subscriber: DailySubscriberResponse
    deliveries: list[DailyDeliveryResponse]
    requests: list[DailyRequestResponse]

    @classmethod
    def from_domain(cls, detail: DailySubscriberDetail) -> DailySubscriberDetailResponse:
        return cls(
            subscriber=DailySubscriberResponse.from_entry(detail.entry),
            deliveries=[DailyDeliveryResponse.from_domain(item) for item in detail.deliveries],
            requests=[DailyRequestResponse.from_domain(item) for item in detail.requests],
        )


class DailyRequestsBanRequest(ApiModel):
    banned: bool
