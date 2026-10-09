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
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

from app.core.exceptions import (
    AppError,
    CheckoutAlreadyPendingError,
    DuplicatePurchaseError,
    PaymentAlreadyReceivedError,
    PaymentGatewayError,
    PendingInvoiceExistsError,
)
from app.core.logging import get_logger
from app.domain.enums import PaymentProvider, PurchaseStatus
from app.domain.payments import InvoiceRequest, PaymentState
from app.domain.verification import VerificationOutcome, VerificationReport

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from app.domain.delivery import DeliveryResult
    from app.domain.entities import Product, Purchase
    from app.domain.payments import CryptoInvoiceGateway, StarsInvoiceSender
    from app.services.delivery import DeliveryService
    from app.services.purchases import CheckoutSession, PurchaseService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class CryptoCheckout:
    """A created CryptoBot invoice, ready to be paid."""

    purchase: Purchase
    pay_url: str
    resumed: bool = False
    """The buyer's existing invoice, handed back instead of a new one."""


@dataclass(frozen=True, slots=True)
class CheckoutService:
    """Turns a pressed payment button into an invoice, and a payment into delivery."""

    purchases: PurchaseService
    delivery: DeliveryService
    stars: StarsInvoiceSender
    crypto: CryptoInvoiceGateway

    async def start_stars_checkout(
        self,
        *,
        user_id: int,
        product: Product,
        use_bonus: bool = False,
    ) -> Purchase:
        """Create a pending purchase and send the Stars invoice message.

        The purchase is recorded first: it is local and cheap, and its payload is
        what Telegram echoes back with the payment. If Telegram then refuses to
        send the invoice the purchase simply expires unpaid.

        The invoice is issued for ``purchase.amount``, which is whatever pricing
        decided inside that transaction — so a discount or a bonus spend can
        never disagree with what the buyer is asked to pay.

        The loyalty 50% discount is held by exactly one unpaid invoice, so two
        things happen only to invoices that carry it:

        * pressing ⭐ again for the same product replaces the unpaid one: it is
          expired here, its discount released, and the new invoice gets it. The
          old Telegram invoice is then refused at pre-checkout;
        * an invoice Telegram refused to send is expired at once and its
          discount released, instead of holding it for the invoice lifetime.

        Every other invoice behaves exactly as it always has.

        Raises:
            LockBusyError: the buyer is already checking this product out.
            DuplicatePurchaseError: the buyer already owns the product.
            ProductInactiveError, ProviderNotSupportedError: cannot be sold this way.
            InsufficientBonusBalanceError: bonuses were requested but are gone.
            PaymentGatewayError: Telegram refused to send the invoice.
        """
        payload = uuid4().hex
        async with self.purchases.checkout_session(user_id, product.id) as session:
            await self._supersede_unpaid_loyalty_invoices(session)
            purchase = await session.record(
                provider=PaymentProvider.STARS,
                external_id=payload,
                use_bonus=use_bonus,
            )
        try:
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
        except PaymentGatewayError:
            if purchase.loyalty_amount > 0:
                # Nobody can pay an invoice that was never sent: give the
                # discount back now rather than after the invoice lifetime.
                await self.purchases.withdraw_unsent_checkout(purchase.id)
                logger.info("loyalty_stars_invoice_withdrawn", purchase_id=str(purchase.id))
            raise
        logger.info(
            "stars_checkout_started",
            purchase_id=str(purchase.id),
            user_id=user_id,
            product_id=str(product.id),
        )
        return purchase

    @staticmethod
    async def _supersede_unpaid_loyalty_invoices(session: CheckoutSession) -> None:
        """Expire this buyer's unpaid Stars invoices for this product that hold the discount.

        Only those: a plain unpaid Stars invoice is left exactly as it was.
        """
        for pending in await session.pending(PaymentProvider.STARS):
            if pending.loyalty_amount > 0:
                await session.expire(pending)
                logger.info(
                    "loyalty_stars_invoice_superseded",
                    purchase_id=str(pending.id),
                    user_id=pending.user_id,
                )

    async def start_crypto_checkout(
        self,
        *,
        user_id: int,
        product: Product,
        use_bonus: bool = False,
    ) -> CryptoCheckout:
        """Create a CryptoBot invoice and the matching pending purchase.

        A buyer holds at most one live invoice per product. The whole checkout
        runs under the buyer's purchase lock, so a second press cannot reach
        Crypto Pay while the first is in flight, and an invoice that already
        exists is asked about before a new one is ever made:

        * still payable on the same terms → that invoice is returned again;
        * still payable on other terms (the price moved, or the bonus choice
          differs) → ``PendingInvoiceExistsError``; nothing new is issued;
        * expired at Crypto Pay → the purchase is expired here too, whatever it
          held is released, and only then is a new invoice made;
        * already paid → ``PaymentAlreadyReceivedError``; settlement is running;
        * Crypto Pay unreachable or does not know it → ``PaymentGatewayError``;
          a new invoice is never made on a guess.

        The provider assigns the invoice id that later webhooks are matched on,
        so a new invoice must exist before its purchase row. The price is
        therefore computed twice: once to bill the provider, then
        authoritatively inside the purchase transaction, which refuses the
        purchase if the two disagree. That is the safe direction: an unmatched
        CryptoBot invoice expires unpaid, whereas a purchase recorded at a
        different amount than the buyer was shown would be a real defect.

        Raises:
            LockBusyError: the buyer is already checking this product out.
            CheckoutAlreadyPendingError: another checkout recorded its invoice
                first (the database index, should the lock ever have expired).
            DuplicatePurchaseError: the buyer already owns the product.
            PendingInvoiceExistsError: an unpaid invoice on other terms exists.
            PaymentAlreadyReceivedError: the existing invoice was just paid.
            ProductInactiveError, ProviderNotSupportedError: cannot be sold this way.
            InsufficientBonusBalanceError: bonuses were requested but are gone.
            ConflictError: the price moved between the invoice and the purchase.
            PaymentGatewayError: Crypto Pay refused or is unreachable.
        """
        async with self.purchases.checkout_session(user_id, product.id) as session:
            if await session.owned() is not None:
                raise DuplicatePurchaseError(product_id=str(product.id), user_id=user_id)

            resumed = await self._resume_crypto_checkout(
                session,
                product=product,
                use_bonus=use_bonus,
            )
            if resumed is not None:
                return resumed

            amount = await session.quote(product, PaymentProvider.CRYPTO, use_bonus=use_bonus)
            invoice = await self.crypto.create_invoice(
                InvoiceRequest(
                    user_id=user_id,
                    product_id=product.id,
                    title=product.title,
                    description=product.description,
                    amount=amount,
                    currency=PaymentProvider.CRYPTO.currency,
                    payload=uuid4().hex,
                )
            )
            try:
                purchase = await session.record(
                    provider=PaymentProvider.CRYPTO,
                    external_id=invoice.external_id,
                    use_bonus=use_bonus,
                    expected_amount=amount,
                )
            except CheckoutAlreadyPendingError:
                # Only reachable if the lock expired mid-checkout and another
                # press recorded its invoice first. That one is the buyer's;
                # this one is never shown to them and expires unpaid.
                logger.warning(
                    "crypto_invoice_orphaned",
                    user_id=user_id,
                    product_id=str(product.id),
                    invoice_id=invoice.external_id,
                )
                raise

        logger.info(
            "crypto_checkout_started",
            purchase_id=str(purchase.id),
            user_id=user_id,
            product_id=str(product.id),
            invoice_id=invoice.external_id,
        )
        return CryptoCheckout(purchase=purchase, pay_url=invoice.pay_url or "")

    async def _resume_crypto_checkout(
        self,
        session: CheckoutSession,
        *,
        product: Product,
        use_bonus: bool,
    ) -> CryptoCheckout | None:
        """Settle what to do with the buyer's existing invoices for this product.

        Returns the checkout to hand back, or ``None`` once nothing payable is
        left and a new invoice may be made. Before this fix a buyer could hold
        several, so every pending one is checked, newest first.
        """
        pending = await session.pending(PaymentProvider.CRYPTO)
        if not pending:
            return None

        snapshots = await self.crypto.fetch_invoices([purchase.external_id for purchase in pending])
        for purchase in pending:
            snapshot = snapshots.get(purchase.external_id)
            if snapshot is None:
                # Not "expired": the provider simply did not say. Guessing here
                # is how a buyer ends up with two payable invoices.
                logger.warning(
                    "crypto_pending_invoice_unknown",
                    purchase_id=str(purchase.id),
                    invoice_id=purchase.external_id,
                )
                message = "Crypto Pay does not report the buyer's pending invoice"
                raise PaymentGatewayError(message, invoice_id=purchase.external_id)

            if snapshot.state is PaymentState.EXPIRED:
                await session.expire(purchase)
                continue

            if snapshot.state is PaymentState.PAID:
                raise PaymentAlreadyReceivedError(
                    purchase_id=str(purchase.id),
                    invoice_id=purchase.external_id,
                )

            if not snapshot.pay_url:
                message = "Crypto Pay returned a payable invoice without a payment URL"
                raise PaymentGatewayError(message, invoice_id=purchase.external_id)

            if not await self._same_terms(session, purchase, product=product, use_bonus=use_bonus):
                logger.info(
                    "crypto_pending_invoice_on_other_terms",
                    purchase_id=str(purchase.id),
                    invoice_id=purchase.external_id,
                    amount=str(purchase.amount),
                    use_bonus=use_bonus,
                )
                raise PendingInvoiceExistsError(
                    amount=purchase.amount,
                    currency=purchase.currency.value,
                    pay_url=snapshot.pay_url,
                    purchase_id=str(purchase.id),
                )

            logger.info(
                "crypto_checkout_resumed",
                purchase_id=str(purchase.id),
                user_id=purchase.user_id,
                product_id=str(purchase.product_id),
                invoice_id=purchase.external_id,
            )
            return CryptoCheckout(purchase=purchase, pay_url=snapshot.pay_url, resumed=True)
        return None

    @staticmethod
    async def _same_terms(
        session: CheckoutSession,
        purchase: Purchase,
        *,
        product: Product,
        use_bonus: bool,
    ) -> bool:
        """Whether a pending invoice is what this press would be billed now.

        A discount, a loyalty discount or a bonus reservation is held *by* that
        invoice, so pricing the checkout afresh would not see it as available
        and would disagree for no reason. For those the terms are compared
        instead — same list price, same bonus choice. A loyalty discount never
        combines with bonuses, so for it the bonus choice changes nothing. A
        plain invoice is simply re-priced: that also catches a discount the
        buyer has become entitled to since.
        """
        listed = product.price_for(PaymentProvider.CRYPTO)
        if listed is None or purchase.list_amount != Decimal(listed):
            return False
        if purchase.loyalty_amount > 0:
            return True
        if (purchase.bonus_amount > 0) != use_bonus:
            return False
        if purchase.discount_amount > 0 or purchase.bonus_amount > 0:
            return True
        fresh = await session.quote(product, PaymentProvider.CRYPTO, use_bonus=use_bonus)
        return fresh == purchase.amount

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

    async def verify_payment(self, purchase_id: UUID) -> VerificationReport:
        """Re-check one purchase against the provider and finish it if needed.

        Used by the admin panel when a buyer reports a payment without a link.
        Fully idempotent:

        * already delivered → nothing is sent, nothing changes;
        * paid but undelivered → delivery is retried;
        * pending → the provider is asked for the truth. CryptoBot is queried
          through its API; Telegram Stars has no lookup endpoint, so the stored
          charge id is used — its presence is Telegram's own confirmation that
          the payment happened;
        * refunded or genuinely unpaid → reported, never "fixed".

        Raises:
            PurchaseNotFoundError: no purchase with this id.
            LockBusyError: a payment or delivery for it is being processed.
        """
        purchase = await self.purchases.get(purchase_id)
        status_before = purchase.status

        if purchase.status is PurchaseStatus.REFUNDED:
            return self._report(purchase, VerificationOutcome.REFUNDED, status_before)

        if purchase.status is PurchaseStatus.DELIVERED:
            return self._report(purchase, VerificationOutcome.ALREADY_DELIVERED, status_before)

        if purchase.status is PurchaseStatus.PAID:
            # The money is in; only the hand-over is missing.
            result = await self.delivery.deliver_purchase(purchase_id)
            outcome = (
                VerificationOutcome.DELIVERED_NOW
                if result.succeeded
                else VerificationOutcome.DELIVERY_FAILED
            )
            refreshed = await self.purchases.get(purchase_id)
            return self._report(refreshed, outcome, status_before, delivery=result)

        if purchase.provider is PaymentProvider.STARS:
            return await self._verify_stars(purchase, status_before)
        if purchase.provider is PaymentProvider.CRYPTO:
            return await self._verify_crypto(purchase, status_before)
        # Not a payment rail (an internal loyalty grant). A database check keeps
        # those out of pending and expired, so this is unreachable — and if it
        # ever were reached there is no provider to ask, so none is asked.
        logger.warning(
            "verification_without_payment_rail",
            purchase_id=str(purchase.id),
            provider=purchase.provider.value,
            status=purchase.status.value,
        )
        return self._report(
            purchase,
            VerificationOutcome.NO_PROVIDER_EVIDENCE,
            status_before,
            detail="This purchase was not paid through a payment provider.",
        )

    async def _verify_stars(
        self,
        purchase: Purchase,
        status_before: PurchaseStatus,
    ) -> VerificationReport:
        """Stars has no invoice lookup: the stored charge id is the evidence."""
        if not purchase.telegram_charge_id:
            return self._report(
                purchase,
                VerificationOutcome.NO_PROVIDER_EVIDENCE,
                status_before,
                detail=(
                    "Telegram Stars provides no invoice lookup and no charge id was "
                    "recorded for this invoice, so no payment ever reached the bot."
                ),
            )
        return await self._settle_and_report(
            purchase,
            status_before,
            telegram_charge_id=purchase.telegram_charge_id,
            provider_state=PaymentState.PAID,
        )

    async def _verify_crypto(
        self,
        purchase: Purchase,
        status_before: PurchaseStatus,
    ) -> VerificationReport:
        """Ask Crypto Pay what really happened to this invoice."""
        try:
            states = await self.crypto.fetch_states([purchase.external_id])
        except AppError as error:
            logger.warning(
                "verification_provider_unavailable",
                purchase_id=str(purchase.id),
                error=str(error),
            )
            return self._report(
                purchase,
                VerificationOutcome.PROVIDER_UNAVAILABLE,
                status_before,
                detail=str(error),
            )

        state = states.get(purchase.external_id)
        if state is PaymentState.PAID:
            return await self._settle_and_report(
                purchase,
                status_before,
                provider_state=state,
            )
        if state is PaymentState.EXPIRED:
            return self._report(
                purchase,
                VerificationOutcome.EXPIRED_UNPAID,
                status_before,
                provider_state=state,
            )
        return self._report(
            purchase,
            VerificationOutcome.STILL_UNPAID,
            status_before,
            provider_state=state,
            detail=None if state else "The provider does not know this invoice any more.",
        )

    async def _settle_and_report(
        self,
        purchase: Purchase,
        status_before: PurchaseStatus,
        *,
        provider_state: PaymentState,
        telegram_charge_id: str | None = None,
    ) -> VerificationReport:
        """Confirm the payment we had missed, then deliver."""
        result = await self.settle_payment(
            provider=purchase.provider,
            external_id=purchase.external_id,
            telegram_charge_id=telegram_charge_id,
        )
        outcome = (
            VerificationOutcome.SETTLED_AND_DELIVERED
            if result.succeeded
            else VerificationOutcome.DELIVERY_FAILED
        )
        refreshed = await self.purchases.get(purchase.id)
        logger.info(
            "purchase_verified",
            purchase_id=str(purchase.id),
            provider=purchase.provider.value,
            outcome=outcome.value,
            status_before=status_before.value,
            status_after=refreshed.status.value,
        )
        return self._report(
            refreshed,
            outcome,
            status_before,
            provider_state=provider_state,
            delivery=result,
        )

    @staticmethod
    def _report(  # noqa: PLR0913 — a report simply has this many fields
        purchase: Purchase,
        outcome: VerificationOutcome,
        status_before: PurchaseStatus,
        *,
        provider_state: PaymentState | None = None,
        delivery: DeliveryResult | None = None,
        detail: str | None = None,
    ) -> VerificationReport:
        return VerificationReport(
            purchase_id=purchase.id,
            provider=purchase.provider,
            outcome=outcome,
            status_before=status_before,
            status_after=purchase.status,
            provider_state=provider_state,
            delivery=delivery,
            detail=detail,
        )
