"""Domain enumerations shared by every layer."""

from __future__ import annotations

from enum import StrEnum


class Currency(StrEnum):
    """Currencies the shop can charge in."""

    XTR = "XTR"
    """Telegram Stars — an integer amount, settled inside Telegram."""

    USDT = "USDT"
    """Tether, settled through CryptoBot."""


class PaymentProvider(StrEnum):
    """How a purchase came to be paid for.

    ``STARS`` and ``CRYPTO`` are payment rails: they appear as buttons on a
    product card and bill the buyer through a provider. ``INTERNAL`` is not a
    rail. It marks a pack the shop hands over itself (a loyalty free pack): no
    invoice, no provider, no money. Code that takes a payment must therefore
    accept only ``PAYABLE_PROVIDERS`` and refuse anything else explicitly — never
    treat "not Stars" as "crypto".
    """

    STARS = "stars"
    CRYPTO = "crypto"
    INTERNAL = "internal"

    @property
    def is_payable(self) -> bool:
        """Whether this is a real payment rail a buyer can be billed through."""
        return self in PAYABLE_PROVIDERS

    @property
    def currency(self) -> Currency:
        """Currency this payment rail always charges in.

        Raises:
            ValueError: for ``INTERNAL``, which charges nothing. The currency an
                internal grant is *valued* in is decided by whoever records it.
        """
        if self is PaymentProvider.STARS:
            return Currency.XTR
        if self is PaymentProvider.CRYPTO:
            return Currency.USDT
        message = f"{self.value!r} is not a payment rail and charges in no currency"
        raise ValueError(message)


PAYABLE_PROVIDERS: tuple[PaymentProvider, ...] = (PaymentProvider.STARS, PaymentProvider.CRYPTO)
"""The payment rails, in the order their buttons appear on a product card."""


class PurchaseStatus(StrEnum):
    """Lifecycle of a single purchase.

    ``PENDING`` — invoice issued, no money received yet.
    ``PAID`` — payment confirmed by the provider.
    ``DELIVERED`` — the delivery link has been sent to the buyer.
    ``REFUNDED`` — Telegram Stars refund executed; access is revoked.
    ``EXPIRED`` — the invoice was never paid within its lifetime.
    """

    PENDING = "pending"
    PAID = "paid"
    DELIVERED = "delivered"
    REFUNDED = "refunded"
    EXPIRED = "expired"

    @property
    def grants_access(self) -> bool:
        """Whether this status entitles the buyer to the delivery link."""
        return self in _ACCESS_GRANTING


class BonusTransactionType(StrEnum):
    """Why a bonus ledger entry exists.

    The balance is the sum of the ledger, so every movement is one of these and
    nothing changes a balance without leaving a row behind.

    ``REFERRAL_REWARD`` — credit for a settled purchase by an invited buyer.
        Final once written: the shop does not reverse rewards.
    ``BONUS_RESERVED`` — debit taken when an invoice is issued, so the same
        units cannot be promised to two invoices at once.
    ``BONUS_SPENT`` — the reservation above, after the invoice was paid. Same
        row, same amount: only the reason it is held changes.
    ``BONUS_RESERVE_RELEASED`` — credit returning a reservation that was never
        paid for. This is not a refund: nothing was ever charged.
    """

    REFERRAL_REWARD = "referral_reward"
    BONUS_RESERVED = "bonus_reserved"
    BONUS_SPENT = "bonus_spent"
    BONUS_RESERVE_RELEASED = "bonus_reserve_released"

    @property
    def is_credit(self) -> bool:
        """Whether entries of this type increase the balance."""
        return self in _BONUS_CREDIT_TYPES


_BONUS_CREDIT_TYPES = frozenset(
    {
        BonusTransactionType.REFERRAL_REWARD,
        BonusTransactionType.BONUS_RESERVE_RELEASED,
    }
)

BONUS_HOLD_TYPES: tuple[BonusTransactionType, ...] = (
    BonusTransactionType.BONUS_RESERVED,
    BonusTransactionType.BONUS_SPENT,
)
"""Types that represent units already taken off a balance for one purchase."""


_ACCESS_GRANTING = frozenset({PurchaseStatus.PAID, PurchaseStatus.DELIVERED})

ACCESS_GRANTING_STATUSES: tuple[PurchaseStatus, ...] = (
    PurchaseStatus.PAID,
    PurchaseStatus.DELIVERED,
)
"""Statuses that must satisfy the "one paid copy per user" rule."""

PURCHASE_HISTORY_STATUSES: tuple[PurchaseStatus, ...] = (
    PurchaseStatus.PAID,
    PurchaseStatus.DELIVERED,
    PurchaseStatus.REFUNDED,
)
"""Statuses that mean "this buyer has bought before".

Referral eligibility is decided with this set, and ``REFUNDED`` is in it
deliberately: were it absent, a buyer could refund their way back to "never
bought anything" and harvest the first-purchase discount again.
"""


class LoyaltyRewardKind(StrEnum):
    """What a loyalty milestone grants. VIP is a status, not a reward."""

    DISCOUNT_50_PERCENT = "discount_50_percent"
    """Milestone 5: half off one later paid pack."""

    FREE_PACK = "free_pack"
    """Milestone 10: one pack handed over without payment."""


class LoyaltyRewardStatus(StrEnum):
    """Lifecycle of one loyalty reward.

    ``AVAILABLE`` — earned and unused.
    ``APPLIED`` — priced into a pending invoice; goes back to ``AVAILABLE`` if
        that invoice expires unpaid. A free pack is never in this state.
    ``CONSUMED`` — used by a paid purchase or a handed-over free pack. Final.
    """

    AVAILABLE = "available"
    APPLIED = "applied"
    CONSUMED = "consumed"


# ------------------------------------------------------------- Daily Content


class DailySubscriptionStatus(StrEnum):
    """Where a buyer stands with the Daily Content bot.

    ``INACTIVE`` — wrote to the bot, but had no access then: nothing is sent.
    ``ACTIVE`` — activated; receives one item a day in their own window.
    ``PAUSED`` — activated, but asked not to receive anything for now.

    A blocked bot is not a status of its own: it is a fact recorded next to
    whichever of these applies, and unblocking simply clears it.
    """

    INACTIVE = "inactive"
    ACTIVE = "active"
    PAUSED = "paused"


class DailyContentStatus(StrEnum):
    """Whether an item of the library takes part in deliveries.

    ``ACTIVE`` — offered to everybody who has not received it yet.
    ``DISABLED`` — taken out of deliveries for now, by the owner or because
        Telegram no longer accepts its file; can be enabled again.
    ``ARCHIVED`` — the soft delete: hidden from deliveries and from the
        default library view. Rows are never physically deleted — they are
        part of every buyer's delivery history.
    """

    ACTIVE = "active"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class DailyContentKind(StrEnum):
    """The kinds of Telegram message the library can hold and send again."""

    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    ANIMATION = "animation"
    DOCUMENT = "document"
    AUDIO = "audio"
    VOICE = "voice"
    VIDEO_NOTE = "video_note"


class DailyDeliveryTrigger(StrEnum):
    """Why a delivery exists.

    ``ACTIVATION`` — the item sent right away when the buyer first used the bot;
        it is that day's delivery.
    ``SCHEDULE`` — the daily item, due in the buyer's personal window.
    """

    ACTIVATION = "activation"
    SCHEDULE = "schedule"


class DailyDeliveryStatus(StrEnum):
    """Lifecycle of one buyer's delivery for one day.

    ``PENDING`` — waiting for its first or next attempt.
    ``SENDING`` — an attempt holds the row under a lease right now. A lease
        that runs out means the attempt died: the row is taken over and the
        *same* item is sent again.
    ``SENT`` — Telegram accepted the item. Final.
    ``NO_CONTENT`` — nothing left that the buyer has not received; looked at
        again until the day's deadline, in case new items arrive.
    ``FAILED`` — Telegram refused for good (the bot is blocked, the chat is
        gone, the item is unusable). Final.
    ``EXPIRED`` — the day's deadline passed without a successful send. Final.
    ``CANCELLED`` — the buyer paused or lost access before it was sent. Final.
    """

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    NO_CONTENT = "no_content"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


DAILY_DELIVERY_OPEN_STATUSES: tuple[DailyDeliveryStatus, ...] = (
    DailyDeliveryStatus.PENDING,
    DailyDeliveryStatus.SENDING,
    DailyDeliveryStatus.NO_CONTENT,
)
"""Statuses a delivery can still leave on its way to being sent or closed."""


class DailyAttemptOutcome(StrEnum):
    """How one send attempt ended.

    ``SENT`` — Telegram accepted the message.
    ``TRANSIENT`` — a temporary failure (network, 5xx, flood control): retried.
    ``PERMANENT`` — Telegram refused for a reason a retry cannot fix.
    ``BLOCKED`` — the buyer blocked the bot or deleted their account.
    ``CONTENT_INVALID`` — Telegram no longer accepts the item's file.
    ``LEASE_LOST`` — the attempt finished after its lease had been taken over;
        its result was not applied to the delivery.
    ``CRASHED`` — found unfinished when its lease ran out: the process died
        mid-send. Telegram may or may not have accepted the message.
    """

    SENT = "sent"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    BLOCKED = "blocked"
    CONTENT_INVALID = "content_invalid"
    LEASE_LOST = "lease_lost"
    CRASHED = "crashed"
