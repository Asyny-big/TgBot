"""Delivery port and value objects.

The delivery *mechanism* (Telegram) lives behind this port, so the delivery
service can be tested without a bot and swapped without touching business rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.entities import Purchase


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryMessage:
    """Everything the transport needs to hand the link to the buyer."""

    chat_id: int
    purchase_id: UUID
    product_title: str
    delivery_url: str
    is_repeat: bool = False
    """True when the buyer already owned the product and asked for it again."""


class DeliveryStatus(StrEnum):
    """Outcome of one delivery attempt sequence."""

    SENT = "sent"
    ALREADY_DELIVERED = "already_delivered"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryResult:
    """Result of a delivery request, including how hard it had to try."""

    status: DeliveryStatus
    purchase_id: UUID
    attempts: int = 0
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        """Whether the buyer now has the link."""
        return self.status in (DeliveryStatus.SENT, DeliveryStatus.ALREADY_DELIVERED)


@dataclass(frozen=True, slots=True, kw_only=True)
class RecordedDelivery:
    """A purchase recorded as delivered, and the loyalty milestone that reached."""

    purchase: Purchase
    milestone: int | None = None
    """5, 10 or 15 when this delivery — a genuine ``paid → delivered`` — was
    the buyer's 5th, 10th or 15th completed purchase. ``None`` otherwise, and
    always while the loyalty programme is off."""


class DeliveryGateway(Protocol):
    """Transport that puts the delivery message in front of the buyer.

    Implementations must translate their own transport failures into
    ``DeliveryTransientError`` (worth retrying) or ``DeliveryPermanentError``
    (retrying cannot help — the bot is blocked, the chat is gone).
    """

    async def send(self, message: DeliveryMessage) -> None:
        """Deliver the message or raise a delivery error."""
        ...
