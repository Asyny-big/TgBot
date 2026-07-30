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
    """Payment rails available on a product card."""

    STARS = "stars"
    CRYPTO = "crypto"

    @property
    def currency(self) -> Currency:
        """Currency this provider always charges in."""
        if self is PaymentProvider.STARS:
            return Currency.XTR
        return Currency.USDT


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


_ACCESS_GRANTING = frozenset({PurchaseStatus.PAID, PurchaseStatus.DELIVERED})

ACCESS_GRANTING_STATUSES: tuple[PurchaseStatus, ...] = (
    PurchaseStatus.PAID,
    PurchaseStatus.DELIVERED,
)
"""Statuses that must satisfy the "one paid copy per user" rule."""
