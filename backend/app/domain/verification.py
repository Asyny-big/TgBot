"""Manual payment verification.

Support scenario: a buyer says "I paid and got nothing". An administrator asks
the shop to re-check that one purchase. The operation is read-only against our
own data, authoritative against the provider, and idempotent: running it twice
never charges, never double-delivers and never rewrites history.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.delivery import DeliveryResult
    from app.domain.enums import PaymentProvider, PurchaseStatus
    from app.domain.payments import PaymentState


class VerificationOutcome(StrEnum):
    """What the check found, and what it did about it."""

    ALREADY_DELIVERED = "already_delivered"
    """Nothing to do: the buyer already has the link."""

    DELIVERED_NOW = "delivered_now"
    """The payment was fine, delivery had failed earlier and now succeeded."""

    SETTLED_AND_DELIVERED = "settled_and_delivered"
    """The provider confirms payment we had not recorded; it is now delivered."""

    DELIVERY_FAILED = "delivery_failed"
    """Payment confirmed, but the buyer still cannot be reached."""

    STILL_UNPAID = "still_unpaid"
    """The provider says the invoice has not been paid."""

    EXPIRED_UNPAID = "expired_unpaid"
    """The invoice expired without payment."""

    NO_PROVIDER_EVIDENCE = "no_provider_evidence"
    """No stored payment data and no provider lookup available (Telegram Stars)."""

    REFUNDED = "refunded"
    """The purchase was refunded; access stays revoked."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    """The provider could not be reached; try again later."""

    @property
    def is_resolved(self) -> bool:
        """Whether the buyer has the link after this check."""
        return self in _RESOLVED


_RESOLVED = frozenset(
    {
        VerificationOutcome.ALREADY_DELIVERED,
        VerificationOutcome.DELIVERED_NOW,
        VerificationOutcome.SETTLED_AND_DELIVERED,
    }
)


@dataclass(frozen=True, slots=True, kw_only=True)
class VerificationReport:
    """Everything the administrator needs to see after pressing "check payment"."""

    purchase_id: UUID
    provider: PaymentProvider
    outcome: VerificationOutcome
    status_before: PurchaseStatus
    status_after: PurchaseStatus
    provider_state: PaymentState | None = None
    """What the provider said, when the provider could be asked."""

    delivery: DeliveryResult | None = None
    detail: str | None = None
