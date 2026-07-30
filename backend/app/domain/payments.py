"""Payment ports.

An invoice is only ever created because the buyer pressed a payment button —
opening a card must not touch a payment provider. The ports below are therefore
"checkout" operations, never side effects of viewing a product.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from decimal import Decimal
    from uuid import UUID

    from app.domain.enums import Currency


@dataclass(frozen=True, slots=True, kw_only=True)
class InvoiceRequest:
    """What the provider needs in order to bill the buyer once."""

    user_id: int
    product_id: UUID
    title: str
    description: str
    amount: int | Decimal
    currency: Currency
    payload: str
    """Our own reference, echoed back by the provider with the payment."""


@dataclass(frozen=True, slots=True, kw_only=True)
class Invoice:
    """A provider invoice that is waiting to be paid."""

    external_id: str
    """Provider side identifier used to match the payment to the purchase."""

    pay_url: str | None = None
    """Where the buyer pays. ``None`` for Telegram Stars: the invoice *is* a message."""


class PaymentState(StrEnum):
    """Provider view of an invoice, used when reconciling lost notifications."""

    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"


class CryptoInvoiceGateway(Protocol):
    """Creates invoices at an external crypto payment provider."""

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        """Create an invoice and return its provider id and payment URL.

        Raises:
            PaymentGatewayError: the provider refused or could not be reached.
        """
        ...

    async def fetch_states(self, external_ids: Sequence[str]) -> Mapping[str, PaymentState]:
        """Current provider state of the given invoices (reconciliation)."""
        ...


class StarsInvoiceSender(Protocol):
    """Sends a Telegram Stars invoice into the buyer's chat."""

    async def send_invoice(self, request: InvoiceRequest) -> None:
        """Deliver the invoice message.

        Raises:
            PaymentGatewayError: Telegram refused to send the invoice.
        """
        ...
