"""Domain entities.

Immutable, infrastructure-free representations of the business objects. The
persistence layer maps rows onto these; services and delivery layers never see
SQLAlchemy models.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Final

from app.domain.enums import (
    DAILY_DELIVERY_OPEN_STATUSES,
    PAYABLE_PROVIDERS,
    BonusTransactionType,
    Currency,
    DailyAlertKey,
    DailyAlertState,
    DailyAttemptOutcome,
    DailyContentKind,
    DailyContentStatus,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    DailyOutboxChannel,
    DailyOutboxKind,
    DailyOutboxStatus,
    DailyRequestEventKind,
    DailyRequestStatus,
    DailySubscriptionStatus,
    LoyaltyRewardKind,
    LoyaltyRewardStatus,
    PaymentProvider,
    PurchaseStatus,
)

if TYPE_CHECKING:
    from datetime import date, datetime
    from uuid import UUID

_NO_MONEY: Final = Decimal(0)
"""Default for a money field that simply did not happen."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Product:
    """A single digital good, reachable only through its deep link."""

    id: UUID
    slug: str
    title: str
    description: str
    photo_file_id: str | None
    delivery_url: str
    price_stars: int | None
    price_usdt: Decimal | None
    is_active: bool
    created_at: datetime
    updated_at: datetime

    def price_for(self, provider: PaymentProvider) -> int | Decimal | None:
        """Price on this payment rail, or ``None`` when it cannot be bought there.

        Every rail is named explicitly: ``INTERNAL`` is not a rail, so it has no
        price here and nothing can ever be *bought* through it.
        """
        if provider is PaymentProvider.STARS:
            return self.price_stars
        if provider is PaymentProvider.CRYPTO:
            return self.price_usdt
        return None

    def supports(self, provider: PaymentProvider) -> bool:
        """Whether the product can be bought through this provider."""
        return self.price_for(provider) is not None

    @property
    def available_providers(self) -> tuple[PaymentProvider, ...]:
        """Payment rails with a configured price, in display order."""
        return tuple(provider for provider in PAYABLE_PROVIDERS if self.supports(provider))

    @property
    def stars_per_usdt(self) -> Decimal | None:
        """How many Telegram Stars one USDT is worth *for this product*.

        The shop declares this itself by pricing the item in both currencies, so
        it needs no external price feed and no fixed rate that could go stale:
        the two prices on the row already say what the merchant considers
        equivalent. Used to express a USDT payment in Stars when crediting a
        referral reward, and recorded on the ledger entry so repricing the
        product later never recomputes an old reward.

        ``None`` when the product carries only one of the two prices — there is
        then no declared equivalence, and the caller credits nothing rather than
        inventing a rate.
        """
        if self.price_stars is None or self.price_usdt is None or self.price_usdt <= 0:
            return None
        return Decimal(self.price_stars) / self.price_usdt


@dataclass(frozen=True, slots=True, kw_only=True)
class User:
    """A Telegram user who opened at least one product card."""

    telegram_id: int
    username: str | None
    first_name: str | None
    language_code: str | None
    created_at: datetime
    last_seen_at: datetime

    referral_code: str | None = None
    """Permanent, opaque code behind this user's invitation link."""

    bonus_balance: int = 0
    """Spendable bonus units. Not Telegram Stars, not withdrawable, not money."""

    @property
    def display_name(self) -> str:
        """Human readable label for the admin panel."""
        if self.username:
            return f"@{self.username}"
        if self.first_name:
            return self.first_name
        return str(self.telegram_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class Purchase:
    """One attempt to buy one product through one provider."""

    id: UUID
    user_id: int
    product_id: UUID
    provider: PaymentProvider
    status: PurchaseStatus
    amount: Decimal
    currency: Currency
    external_id: str
    telegram_charge_id: str | None
    delivered_url: str | None
    created_at: datetime
    paid_at: datetime | None
    delivered_at: datetime | None

    base_amount: Decimal | None = None
    """List price at the time of sale. ``None`` on rows predating the feature."""

    discount_amount: Decimal = _NO_MONEY
    """Referral discount taken off the list price."""

    bonus_amount: Decimal = _NO_MONEY
    """Part of the price covered from the buyer's bonus balance."""

    loyalty_amount: Decimal = _NO_MONEY
    """Part of the list price waived by a loyalty reward.

    The whole list price on an ``INTERNAL`` free pack, whose ``amount`` is 0.
    """

    @property
    def grants_access(self) -> bool:
        """Whether this purchase entitles the buyer to the delivery link."""
        return self.status.grants_access

    @property
    def charged_amount(self) -> Decimal:
        """What the buyer was actually billed. Always ``amount``."""
        return self.amount

    @property
    def list_amount(self) -> Decimal:
        """List price of this sale, falling back to the billed amount.

        Purchases created before the referral feature existed carry no
        breakdown, and for those the billed amount *was* the list price.
        """
        return self.base_amount if self.base_amount is not None else self.amount

    @property
    def was_reduced(self) -> bool:
        """Whether anything was taken off the list price for this sale."""
        return self.discount_amount > 0 or self.bonus_amount > 0 or self.loyalty_amount > 0


@dataclass(frozen=True, slots=True, kw_only=True)
class PurchaseRecord:
    """A purchase joined with its buyer and product, for admin listings."""

    purchase: Purchase
    user: User
    product: Product


@dataclass(frozen=True, slots=True, kw_only=True)
class Referral:
    """One buyer, permanently attributed to the person who invited them.

    The attribution never moves: if A invited B, a later link from C cannot
    take B over. That is a unique constraint in the database, not a convention.
    """

    id: UUID
    referrer_user_id: int
    referred_user_id: int
    created_at: datetime
    discount_used_at: datetime | None = None
    discount_purchase_id: UUID | None = None

    @property
    def discount_available(self) -> bool:
        """Whether the first-purchase discount is still unused."""
        return self.discount_used_at is None


@dataclass(frozen=True, slots=True, kw_only=True)
class BonusTransaction:
    """One movement of the internal bonus balance.

    Bonuses are not Telegram Stars and not money: they cannot be withdrawn or
    transferred, and they exist only to reduce a future price in this shop.
    """

    id: UUID
    user_id: int
    amount: int
    """Signed bonus units: positive credits the balance, negative debits it."""

    type: BonusTransactionType
    referral_id: UUID | None = None
    purchase_id: UUID | None = None
    stars_per_usdt: Decimal | None = None
    """Rate used to express a USDT payment in Stars, when one was needed.

    Recorded so an old reward is never recomputed: repricing the product
    tomorrow changes the rate for tomorrow's sales only.
    """

    created_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class LoyaltyReward:
    """One loyalty milestone reward of one buyer, once it has been recorded.

    Rewards are recorded lazily, the first time one is actually used, so a
    buyer who qualifies but never used theirs has no row at all. VIP has no
    reward: it is read off the number of completed purchases.
    """

    id: UUID
    user_id: int
    milestone: int
    kind: LoyaltyRewardKind
    status: LoyaltyRewardStatus
    earned_at: datetime
    applied_purchase_id: UUID | None = None
    consumed_purchase_id: UUID | None = None
    applied_at: datetime | None = None
    consumed_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class BonusSummary:
    """Everything the "My bonuses" screen shows.

    Deliberately four numbers and a link. The section is a way to invite a
    friend and see the balance, not an analytics dashboard.
    """

    user_id: int
    balance: int
    referral_code: str
    invited_count: int
    referral_purchase_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ReferralRecord:
    """A referral relationship joined with both parties, for the admin panel."""

    referral: Referral
    referrer: User
    referred: User
    referred_purchase_count: int
    reward_total: int


# ------------------------------------------------------------- Daily Content


@dataclass(frozen=True, slots=True, kw_only=True)
class DailySubscription:
    """One buyer's relationship with the Daily Content bot.

    Created the first time the buyer writes to the bot. Activated — and given
    their personal delivery window — the first time they write while they have
    access; until then it stays inactive and nothing is ever sent.
    """

    user_id: int
    status: DailySubscriptionStatus
    first_interaction_at: datetime
    activated_at: datetime | None = None
    anchor_minute: int | None = None
    """Minute of the day (Europe/Moscow) of the activation: the centre of the
    buyer's daily window, which runs one hour either side of it."""

    paused_at: datetime | None = None
    blocked_at: datetime | None = None
    """Set while the buyer has the bot blocked; cleared when they unblock it."""

    exhausted_notified_at: datetime | None = None
    requests_banned_at: datetime | None = None
    """Set while the buyer may not send content requests."""

    requests_banned_by: str | None = None
    """Who banned them: ``admin:<username>``."""

    created_at: datetime
    updated_at: datetime

    @property
    def is_blocked(self) -> bool:
        """Whether the buyer currently has the bot blocked."""
        return self.blocked_at is not None

    @property
    def receives_deliveries(self) -> bool:
        """Activated, not paused and not blocked: due an item every day."""
        return self.status is DailySubscriptionStatus.ACTIVE and not self.is_blocked


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyContent:
    """One item of the shared Daily Content library.

    Stored as Telegram identifiers, never as a downloaded file: ``file_id`` is
    what the bot sends again, ``file_unique_id`` recognises the same file
    forwarded twice, and the source chat/message records where the owner
    added it from.
    """

    id: int
    status: DailyContentStatus
    kind: DailyContentKind
    source_chat_id: int
    source_message_id: int
    added_by: int
    created_at: datetime
    updated_at: datetime
    status_changed_at: datetime
    body: str | None = None
    """The message text, or the media caption."""

    entities: tuple[dict[str, object], ...] = ()
    """Telegram formatting entities of ``body``, as Telegram sent them."""

    file_id: str | None = None
    file_unique_id: str | None = None
    file_size: int | None = None
    file_name: str | None = None
    mime_type: str | None = None
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    status_reason: str | None = None

    @property
    def is_active(self) -> bool:
        """Whether the item takes part in deliveries."""
        return self.status is DailyContentStatus.ACTIVE


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyDelivery:
    """One buyer's Daily Content delivery for one day.

    ``delivery_date`` is the Europe/Moscow date of the window's centre, and a
    buyer has at most one delivery per date — whatever retries, restarts or
    concurrent workers happen. Once an item is chosen for a delivery it never
    changes: a retry sends the same item again.
    """

    id: int
    user_id: int
    delivery_date: date
    trigger: DailyDeliveryTrigger
    status: DailyDeliveryStatus
    scheduled_for: datetime
    window_opens_at: datetime
    window_closes_at: datetime
    deadline_at: datetime
    next_attempt_at: datetime
    attempts: int
    created_at: datetime
    updated_at: datetime
    content_id: int | None = None
    lease_token: UUID | None = None
    lease_until: datetime | None = None
    sent_at: datetime | None = None
    telegram_message_id: int | None = None
    last_outcome: DailyAttemptOutcome | None = None
    last_error: str | None = None

    @property
    def is_open(self) -> bool:
        """Whether this delivery can still be sent or closed."""
        return self.status in DAILY_DELIVERY_OPEN_STATUSES


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyDeliveryAttempt:
    """One attempt to send a delivery's item, for the delivery history.

    ``finished_at`` is empty while the attempt runs — and stays empty if the
    process dies mid-send, until the lease runs out and it is closed as
    ``CRASHED``.
    """

    id: int
    delivery_id: int
    attempt_no: int
    lease_token: UUID
    started_at: datetime
    content_id: int | None = None
    finished_at: datetime | None = None
    outcome: DailyAttemptOutcome | None = None
    error: str | None = None
    retry_after_seconds: int | None = None
    telegram_message_id: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyOutboxMessage:
    """One message the Daily runtime must get to somebody, other than a delivery.

    Sent with the same discipline as deliveries: a lease while an attempt is
    under way, retries for uncertain failures, and a deadline after which it
    is no longer worth sending. ``dedupe_key`` makes enqueueing idempotent:
    the same alert or notice is queued once, whoever asks twice.
    """

    id: int
    kind: DailyOutboxKind
    channel: DailyOutboxChannel
    chat_id: int
    body: str
    """The message, HTML; anything interpolated was escaped when it was queued."""

    dedupe_key: str
    status: DailyOutboxStatus
    attempts: int
    next_attempt_at: datetime
    deadline_at: datetime
    created_at: datetime
    updated_at: datetime
    content_id: int | None = None
    """A library item to send instead of ``body`` — then a label only."""

    request_item_id: int | None = None
    """A buyer's attachment to send instead of ``body`` — then a label only."""

    lease_token: UUID | None = None
    lease_until: datetime | None = None
    sent_at: datetime | None = None
    telegram_message_id: int | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyAlert:
    """The state of one of the owner's alerts.

    An alert notifies on its transitions — firing, resolved — and reminds at
    most once per cooldown while it keeps firing; it never repeats itself on
    every evaluation.
    """

    key: DailyAlertKey
    state: DailyAlertState
    notifications: int
    """How many notifications it has queued so far: the next one's number."""

    value: dict[str, object]
    """What the last evaluation observed."""

    updated_at: datetime
    fired_at: datetime | None = None
    notified_at: datetime | None = None
    resolved_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyContentRequest:
    """A buyer's suggestion of content, as the owner sees and handles it.

    ``version`` grows with every change made from the admin panel: a change
    based on an older version is refused, so two tabs never overwrite each
    other silently.
    """

    id: int
    user_id: int
    status: DailyRequestStatus
    client_draft_id: UUID
    """The draft it came from: submitting the same draft twice is one request."""

    purchases_at_submit: int
    """Completed purchases when it was sent — for display only, never for access."""

    version: int
    created_at: datetime
    updated_at: datetime
    admin_note: str | None = None
    closed_at: datetime | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyRequestItem:
    """One message of a content request: a text, or a file with its caption."""

    id: int
    request_id: int
    position: int
    kind: DailyContentKind
    source_chat_id: int
    source_message_id: int
    created_at: datetime
    body: str | None = None
    entities: tuple[dict[str, object], ...] = ()
    file_id: str | None = None
    file_unique_id: str | None = None
    file_size: int | None = None
    file_name: str | None = None
    mime_type: str | None = None
    duration: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DailyRequestEvent:
    """One line of a content request's audit trail."""

    id: int
    request_id: int
    kind: DailyRequestEventKind
    actor: str
    """``user:<id>``, ``admin:<username>`` or ``system``."""

    created_at: datetime
    from_status: DailyRequestStatus | None = None
    to_status: DailyRequestStatus | None = None
    body: str | None = None
    outbox_message_id: int | None = None
    idempotency_key: str | None = None
