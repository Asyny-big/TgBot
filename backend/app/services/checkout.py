"""Checkout orchestration.

The one invariant this module exists to protect:

    **an invoice is created only when the buyer presses a payment button.**

Opening a product card goes through ``PurchaseService.open_card``, which reads
the product and the buyer's history and writes no purchase and no invoice. Only
``start_stars_checkout`` / ``start_crypto_checkout`` — reachable exclusively from
a button press — create a ``pending`` purchase and an invoice.

Settlement is orchestration too, in the mandated order and nothing more:

    PurchaseService.confirm_payment() → DeliveryService.deliver_purchase()
    (which itself confirms via PurchaseService.mark_delivered())
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from app.core.exceptions import DuplicatePurchaseError
from app.core.logging import get_logger
from app.domain.enums import PaymentProvider
from app.domain.payments import InvoiceRequest

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from app.domain.delivery import DeliveryResult
    from app.domain.entities import Product, Purchase
    from app.domain.payments import CryptoInvoiceGateway, StarsInvoiceSender
    from app.services.delivery import DeliveryService
    from app.services.purchases import PurchaseService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class CryptoCheckout:
    """A created CryptoBot invoice, ready to be paid."""

    purchase: Purchase
    pay_url: str


@dataclass(frozen=True, slots=True)
class CheckoutService:
    """Turns a pressed payment button into an invoice, and a payment into delivery."""

    purchases: PurchaseService
    delivery: DeliveryService
    stars: StarsInvoiceSender
    crypto: CryptoInvoiceGateway

    async def start_stars_checkout(self, *, user_id: int, product: Product) -> Purchase:
        """Create a pending purchase and send the Stars invoice message.

        The purchase is recorded first: it is local and cheap, and its payload is
        what Telegram echoes back with the payment. If Telegram then refuses to
        send the invoice the purchase simply expires unpaid.

        Raises:
            LockBusyError: the buyer is already checking this product out.
            DuplicatePurchaseError: the buyer already owns the product.
            ProductInactiveError, ProviderNotSupportedError: cannot be sold this way.
            PaymentGatewayError: Telegram refused to send the invoice.
        """
        payload = uuid4().hex
        purchase = await self.purchases.start_purchase(
            user_id=user_id,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            external_id=payload,
        )
        await self.stars.send_invoice(
            InvoiceRequest(
                user_id=user_id,
                product_id=product.id,
                title=product.title,
                description=product.description,
                amount=purchase.amount,
                currency=purchase.currency,
                payload=payload,
            )
        )
        logger.info(
            "stars_checkout_started",
            purchase_id=str(purchase.id),
            user_id=user_id,
            product_id=str(product.id),
        )
        return purchase

    async def start_crypto_checkout(self, *, user_id: int, product: Product) -> CryptoCheckout:
        """Create a CryptoBot invoice and the matching pending purchase.

        The provider assigns the invoice id that later webhooks are matched on,
        so the invoice must exist first. Ownership is checked before that call so
        a buyer who already owns the product never reaches the provider.

        Raises:
            LockBusyError: the buyer is already checking this product out.
            DuplicatePurchaseError: the buyer already owns the product.
            ProductInactiveError, ProviderNotSupportedError: cannot be sold this way.
            PaymentGatewayError: Crypto Pay refused or is unreachable.
        """
        if await self.purchases.find_owned(user_id, product.id) is not None:
            raise DuplicatePurchaseError(product_id=str(product.id), user_id=user_id)

        price = product.price_for(PaymentProvider.CRYPTO)
        invoice = await self.crypto.create_invoice(
            InvoiceRequest(
                user_id=user_id,
                product_id=product.id,
                title=product.title,
                description=product.description,
                amount=price if price is not None else 0,
                currency=PaymentProvider.CRYPTO.currency,
                payload=uuid4().hex,
            )
        )
        purchase = await self.purchases.start_purchase(
            user_id=user_id,
            product_id=product.id,
            provider=PaymentProvider.CRYPTO,
            external_id=invoice.external_id,
        )
        logger.info(
            "crypto_checkout_started",
            purchase_id=str(purchase.id),
            user_id=user_id,
            product_id=str(product.id),
            invoice_id=invoice.external_id,
        )
        return CryptoCheckout(purchase=purchase, pay_url=invoice.pay_url or "")

    async def settle_payment(
        self,
        *,
        provider: PaymentProvider,
        external_id: str,
        telegram_charge_id: str | None = None,
        paid_at: datetime | None = None,
    ) -> DeliveryResult:
        """Confirm a payment and then deliver, in that order.

        Idempotent end to end: a replayed notification confirms nothing new and
        delivers nothing twice.

        Raises:
            PurchaseNotFoundError: the invoice belongs to no purchase.
            LockBusyError: another worker is already settling this payment.
            ConflictError: the purchase was refunded.
        """
        purchase = await self.purchases.confirm_payment(
            provider=provider,
            external_id=external_id,
            telegram_charge_id=telegram_charge_id,
            paid_at=paid_at,
        )
        return await self.delivery.deliver_purchase(purchase.id)

    async def redeliver(self, purchase_id: UUID) -> DeliveryResult:
        """Hand the link over again to a buyer who already owns the product."""
        return await self.delivery.redeliver(purchase_id)
