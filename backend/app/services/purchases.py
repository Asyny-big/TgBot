"""Purchase use cases.

This service owns the purchase lifecycle only: creating purchases, confirming
payments, moving statuses and answering "does this buyer already own it?".
It never sends a message — handing the link to the buyer belongs to
``DeliveryService``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.exceptions import (
    DuplicatePurchaseError,
    ProductInactiveError,
    ProductNotFoundError,
    ProviderNotSupportedError,
    PurchaseNotFoundError,
    UserNotFoundError,
)
from app.core.logging import get_logger
from app.domain.cards import PaymentOption, ProductCard
from app.domain.commands import PurchaseDraft
from app.domain.locks import payment_lock_key, purchase_lock_key

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.commands import UserDraft
    from app.domain.entities import Product, Purchase, User
    from app.domain.enums import PaymentProvider
    from app.domain.locks import LockManager
    from app.domain.uow import UnitOfWorkFactory

logger = get_logger(__name__)

PAYMENT_CONFIRMATION_WAIT_SECONDS = 5.0
"""Money already moved: wait for a busy lock instead of dropping the callback."""


@dataclass(frozen=True, slots=True)
class PurchaseService:
    """Business rules of buying exactly one product through one rail."""

    uow_factory: UnitOfWorkFactory
    locks: LockManager
    invoice_ttl: timedelta = timedelta(minutes=30)

    async def open_card(self, profile: UserDraft, slug: str) -> ProductCard:
        """Resolve a deep link into a card, remembering the visitor.

        Raises:
            ProductNotFoundError: the slug belongs to no product.
            ProductInactiveError: the product exists but is not on sale.
        """
        async with self.uow_factory() as uow:
            await uow.users.upsert(profile)
            product = await uow.products.get_by_slug(slug)
            if product is None:
                raise ProductNotFoundError(slug=slug)
            if not product.is_active:
                raise ProductInactiveError(slug=slug)
            owned = await uow.purchases.find_access_granting(profile.telegram_id, product.id)

        options = tuple(
            PaymentOption(
                provider=provider,
                amount=self._price(product, provider),
                currency=provider.currency,
            )
            for provider in product.available_providers
        )
        logger.info(
            "card_opened",
            telegram_id=profile.telegram_id,
            slug=slug,
            owned=owned is not None,
        )
        return ProductCard(product=product, options=options, owned_purchase=owned)

    async def remember_user(self, profile: UserDraft) -> User:
        """Store or refresh the Telegram profile snapshot."""
        async with self.uow_factory() as uow:
            return await uow.users.upsert(profile)

    async def start_purchase(
        self,
        *,
        user_id: int,
        product_id: UUID,
        provider: PaymentProvider,
        external_id: str,
    ) -> Purchase:
        """Record a pending purchase for an invoice that was just created.

        The distributed lock (bounded by its TTL) keeps two simultaneous
        ``/start`` presses from producing two invoices for the same product.

        Raises:
            LockBusyError: another attempt for this buyer and product is running.
            ProductNotFoundError, ProductInactiveError: product unavailable.
            ProviderNotSupportedError: the product has no price for this rail.
            DuplicatePurchaseError: the buyer already owns the product.
            UserNotFoundError: the buyer was never recorded (the card must be
                opened first, which is what stores the Telegram profile).
        """
        async with (
            self.locks.lock(purchase_lock_key(user_id, product_id)),
            self.uow_factory() as uow,
        ):
            if await uow.users.get(user_id) is None:
                raise UserNotFoundError(telegram_id=user_id)
            product = await uow.products.get(product_id)
            if product is None:
                raise ProductNotFoundError(product_id=str(product_id))
            if not product.is_active:
                raise ProductInactiveError(product_id=str(product_id))
            if not product.supports(provider):
                raise ProviderNotSupportedError(
                    product_id=str(product_id),
                    provider=provider.value,
                )
            if await uow.purchases.find_access_granting(user_id, product_id) is not None:
                raise DuplicatePurchaseError(
                    product_id=str(product_id),
                    user_id=user_id,
                )

            purchase = await uow.purchases.create(
                PurchaseDraft(
                    user_id=user_id,
                    product_id=product_id,
                    provider=provider,
                    amount=Decimal(self._price(product, provider)),
                    currency=provider.currency,
                    external_id=external_id,
                )
            )

        logger.info(
            "purchase_started",
            purchase_id=str(purchase.id),
            user_id=user_id,
            product_id=str(product_id),
            provider=provider.value,
            external_id=external_id,
        )
        return purchase

    async def confirm_payment(
        self,
        *,
        provider: PaymentProvider,
        external_id: str,
        telegram_charge_id: str | None = None,
        paid_at: datetime | None = None,
    ) -> Purchase:
        """Mark the invoice as paid. Idempotent for replayed notifications.

        Delivery is deliberately *not* triggered here: the caller passes the
        confirmed purchase to ``DeliveryService``.

        Raises:
            PurchaseNotFoundError: no purchase behind this invoice.
            ConflictError: the purchase was refunded.
            DuplicatePurchaseError: the buyer already owns a paid copy.
        """
        async with (
            self.locks.lock(
                payment_lock_key(provider, external_id),
                wait_seconds=PAYMENT_CONFIRMATION_WAIT_SECONDS,
            ),
            self.uow_factory() as uow,
        ):
            purchase = await uow.purchases.get_by_external_id(provider, external_id)
            if purchase is None:
                raise PurchaseNotFoundError(provider=provider.value, external_id=external_id)
            confirmed = await uow.purchases.mark_paid(
                purchase.id,
                paid_at=paid_at,
                telegram_charge_id=telegram_charge_id,
            )

        logger.info(
            "payment_confirmed",
            purchase_id=str(confirmed.id),
            provider=provider.value,
            external_id=external_id,
            amount=str(confirmed.amount),
            currency=confirmed.currency.value,
        )
        return confirmed

    async def mark_delivered(self, purchase_id: UUID, *, delivered_url: str) -> Purchase:
        """Record that the buyer received the link. Called by ``DeliveryService``.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
            ConflictError: the purchase is not paid.
        """
        async with self.uow_factory() as uow:
            delivered = await uow.purchases.mark_delivered(
                purchase_id,
                delivered_url=delivered_url,
            )
        logger.info("purchase_delivered", purchase_id=str(purchase_id))
        return delivered

    async def refund(self, purchase_id: UUID) -> Purchase:
        """Revoke access after a refund. Idempotent.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
        """
        async with self.uow_factory() as uow:
            refunded = await uow.purchases.mark_refunded(purchase_id)
        logger.info("purchase_refunded", purchase_id=str(purchase_id))
        return refunded

    async def refund_by_charge_id(self, telegram_charge_id: str) -> Purchase:
        """Revoke access using the Telegram charge id from a refund update.

        Raises:
            PurchaseNotFoundError: no purchase carries this charge id.
        """
        async with self.uow_factory() as uow:
            purchase = await uow.purchases.get_by_charge_id(telegram_charge_id)
            if purchase is None:
                raise PurchaseNotFoundError(telegram_charge_id=telegram_charge_id)
            refunded = await uow.purchases.mark_refunded(purchase.id)
        logger.info(
            "purchase_refunded",
            purchase_id=str(refunded.id),
            telegram_charge_id=telegram_charge_id,
        )
        return refunded

    async def get(self, purchase_id: UUID) -> Purchase:
        """Return a purchase by id.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
        """
        async with self.uow_factory() as uow:
            purchase = await uow.purchases.get(purchase_id)
        if purchase is None:
            raise PurchaseNotFoundError(purchase_id=str(purchase_id))
        return purchase

    async def find_owned(self, user_id: int, product_id: UUID) -> Purchase | None:
        """Return the buyer's paid or delivered purchase of this product."""
        async with self.uow_factory() as uow:
            return await uow.purchases.find_access_granting(user_id, product_id)

    async def list_pending(
        self,
        provider: PaymentProvider,
        *,
        limit: int = 100,
    ) -> tuple[Purchase, ...]:
        """Pending purchases awaiting reconciliation with the provider."""
        async with self.uow_factory() as uow:
            return await uow.purchases.list_pending(provider, limit=limit)

    async def expire_stale(self, *, now: datetime | None = None) -> int:
        """Expire pending purchases whose invoice lifetime has passed."""
        moment = now or datetime.now(UTC)
        async with self.uow_factory() as uow:
            expired = await uow.purchases.expire_pending(moment - self.invoice_ttl)
        if expired:
            logger.info("pending_purchases_expired", count=expired)
        return expired

    @staticmethod
    def _price(product: Product, provider: PaymentProvider) -> int | Decimal:
        """Price in the rail's own currency; the rail is known to be supported."""
        value = product.price_for(provider)
        if value is None:  # pragma: no cover — guarded by supports() upstream
            raise ProviderNotSupportedError(
                product_id=str(product.id),
                provider=provider.value,
            )
        return value
