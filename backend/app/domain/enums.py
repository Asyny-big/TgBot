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
