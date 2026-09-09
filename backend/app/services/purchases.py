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
    ConflictError,
    DuplicatePurchaseError,
    ProductInactiveError,
    ProductNotFoundError,
    ProviderNotSupportedError,
    PurchaseNotFoundError,
    UserNotFoundError,
)
from app.core.logging import get_logger
from app.domain.bonuses import BonusPolicy, discount_for, plain_quote
from app.domain.cards import PaymentOption, ProductCard
from app.domain.commands import PurchaseDraft
from app.domain.locks import payment_lock_key, purchase_lock_key

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.bonuses import CheckoutPricing, PriceQuote
    from app.domain.commands import UserDraft
    from app.domain.entities import Product, Purchase, User
    from app.domain.enums import Currency, PaymentProvider
    from app.domain.locks import LockManager
    from app.domain.uow import UnitOfWork, UnitOfWorkFactory

logger = get_logger(__name__)

PAYMENT_CONFIRMATION_WAIT_SECONDS = 5.0
"""Money already moved: wait for a busy lock instead of dropping the callback."""


@dataclass(frozen=True, slots=True)
class PurchaseService:
    """Business rules of buying exactly one product through one rail."""

    uow_factory: UnitOfWorkFactory
    locks: LockManager
    invoice_ttl: timedelta = timedelta(minutes=30)
    pricing: CheckoutPricing | None = None
    """Optional referral/bonus pricing.

    ``None`` is the shop as it was before the referral feature existed: the
    list price is charged, nothing is held and nothing is recorded. Everything
    below therefore has exactly one extra branch, and the default path through
    it is byte for byte the old behaviour.
    """

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
            # Asked in the same transaction the card already opened, so showing
            # the reduced price costs no extra round trip. Returns False for
            # every ordinary visitor, and instantly when the feature is off.
            discounted = owned is None and await self._discount_eligible(
                uow,
                profile.telegram_id,
            )

        percent = self.discount_percent if discounted else 0
        options = tuple(
            self._payment_option(product, provider, discount_percent=percent)
            for provider in product.available_providers
        )
        logger.info(
            "card_opened",
            telegram_id=profile.telegram_id,
            slug=slug,
            owned=owned is not None,
            discounted=discounted,
        )
        return ProductCard(
            product=product,
            options=options,
            owned_purchase=owned,
            discount_percent=percent,
        )

    @property
    def discount_percent(self) -> int:
        """Referral discount the shop is configured to give, as a percentage."""
        return 0 if self.pricing is None else self.pricing.discount_percent

    async def _discount_eligible(self, uow: UnitOfWork, user_id: int) -> bool:
        """Whether this visitor's next purchase carries the referral discount."""
        if self.pricing is None:
            return False
        return await self.pricing.discount_eligible(uow, user_id)

    def _payment_option(
        self,
        product: Product,
        provider: PaymentProvider,
        *,
        discount_percent: int,
    ) -> PaymentOption:
        """One button, with the reduced price attached when one applies.

        ``amount`` stays the list price whatever happens: the discount is a
        property of this visitor's next sale, not of the catalogue.
        """
        listed = self._price(product, provider)
        if discount_percent <= 0:
            return PaymentOption(
                provider=provider,
                amount=listed,
                currency=provider.currency,
            )
        policy = BonusPolicy(discount_percent=discount_percent)
        discount = discount_for(Decimal(listed), provider.currency, policy)
        return PaymentOption(
            provider=provider,
            amount=listed,
            currency=provider.currency,
            discounted_amount=Decimal(listed) - discount if discount > 0 else None,
        )

    async def remember_user(self, profile: UserDraft) -> User:
        """Store or refresh the Telegram profile snapshot."""
        async with self.uow_factory() as uow:
            return await uow.users.upsert(profile)

    async def start_purchase(  # noqa: PLR0913 — one checkout, this many facts
        self,
        *,
        user_id: int,
        product_id: UUID,
        provider: PaymentProvider,
        external_id: str,
        use_bonus: bool = False,
        expected_amount: Decimal | None = None,
    ) -> Purchase:
        """Record a pending purchase for an invoice that was just created.

        The distributed lock (bounded by its TTL) keeps two simultaneous
        ``/start`` presses from producing two invoices for the same product.

        Pricing runs inside this transaction, not before it. That is the whole
        reason the referral feature cannot mis-bill anybody: the discount, the
        bonus reservation and the purchase row are one commit.

        ``expected_amount`` is for the rails that must create the provider
        invoice *first* (CryptoBot assigns the id the webhook is matched on). If
        the price computed here disagrees with the amount already invoiced, the
        purchase is refused rather than recorded at a different number, and the
        orphaned invoice simply expires unpaid.

        Raises:
            LockBusyError: another attempt for this buyer and product is running.
            ProductNotFoundError, ProductInactiveError: product unavailable.
            ProviderNotSupportedError: the product has no price for this rail.
            DuplicatePurchaseError: the buyer already owns the product.
            UserNotFoundError: the buyer was never recorded (the card must be
                opened first, which is what stores the Telegram profile).
            InsufficientBonusBalanceError: bonuses were requested but the
                balance moved since the price was shown.
            ConflictError: the price moved away from ``expected_amount``.
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

            base_amount = Decimal(self._price(product, provider))
            priced = await self._price_checkout(
                uow,
                user_id=user_id,
                base_amount=base_amount,
                currency=provider.currency,
                use_bonus=use_bonus,
                stars_per_usdt=product.stars_per_usdt,
            )
            self._require_expected_amount(priced, expected_amount, product_id=product_id)

            purchase = await uow.purchases.create(
                PurchaseDraft(
                    user_id=user_id,
                    product_id=product_id,
                    provider=provider,
                    amount=priced.charged_amount,
                    base_amount=priced.base_amount,
                    discount_amount=priced.discount_amount,
                    bonus_amount=priced.bonus_amount,
                    currency=provider.currency,
                    external_id=external_id,
                )
            )
            if self.pricing is not None:
                await self.pricing.hold_for(uow, purchase, priced)

        logger.info(
            "purchase_started",
            purchase_id=str(purchase.id),
            user_id=user_id,
            product_id=str(product_id),
            provider=provider.value,
            external_id=external_id,
            amount=str(purchase.amount),
            discount=str(purchase.discount_amount),
            bonus=str(purchase.bonus_amount),
        )
        return purchase

    async def _price_checkout(  # noqa: PLR0913 — one checkout, this many pricing facts
        self,
        uow: UnitOfWork,
        *,
        user_id: int,
        base_amount: Decimal,
        currency: Currency,
        use_bonus: bool,
        stars_per_usdt: Decimal | None = None,
    ) -> PriceQuote:
        """The list price, or whatever the referral feature makes of it."""
        if self.pricing is None:
            return plain_quote(base_amount, currency)
        return await self.pricing.resolve(
            uow,
            user_id=user_id,
            base_amount=base_amount,
            currency=currency,
            use_bonus=use_bonus,
            stars_per_usdt=stars_per_usdt,
        )

    @staticmethod
    def _require_expected_amount(
        priced: PriceQuote,
        expected_amount: Decimal | None,
        *,
        product_id: UUID,
    ) -> None:
        """Refuse to record a purchase at a price other than the invoiced one."""
        if expected_amount is None or priced.charged_amount == expected_amount:
            return
        logger.error(
            "checkout_price_moved",
            product_id=str(product_id),
            invoiced=str(expected_amount),
            recomputed=str(priced.charged_amount),
        )
        message = "The price changed while the invoice was being created"
        raise ConflictError(
            message,
            product_id=str(product_id),
            invoiced=str(expected_amount),
            recomputed=str(priced.charged_amount),
        )

    async def quote_amount(
        self,
        *,
        user_id: int,
        product: Product,
        provider: PaymentProvider,
        use_bonus: bool = False,
    ) -> Decimal:
        """What this checkout would be billed, without recording anything.

        Read only, and therefore only a *prediction*: the rails that must create
        the provider invoice before the purchase use it to bill the provider,
        and ``start_purchase`` then re-derives the same number under a lock and
        refuses the sale if they disagree.
        """
        base_amount = Decimal(self._price(product, provider))
        if self.pricing is None:
            return base_amount
        priced = await self.pricing.preview(
            user_id=user_id,
            base_amount=base_amount,
            currency=provider.currency,
            use_bonus=use_bonus,
            stars_per_usdt=product.stars_per_usdt,
        )
        return priced.charged_amount

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
            if self.pricing is not None:
                # Same transaction as the payment: the discount is burned and
                # the bonus reservation becomes a spend exactly once, however
                # many times the provider replays this notification.
                await self.pricing.on_payment_confirmed(uow, confirmed)

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

    async def product_for_checkout(self, product_id: UUID) -> Product:
        """Return a product that may be sold right now.

        Raises:
            ProductNotFoundError: no product with this id.
            ProductInactiveError: the product is not on sale.
        """
        async with self.uow_factory() as uow:
            product = await uow.products.get(product_id)
        if product is None:
            raise ProductNotFoundError(product_id=str(product_id))
        if not product.is_active:
            raise ProductInactiveError(product_id=str(product_id))
        return product

    async def find_by_invoice(
        self,
        *,
        provider: PaymentProvider,
        external_id: str,
    ) -> Purchase | None:
        """Return the purchase behind a provider invoice id, or ``None``."""
        async with self.uow_factory() as uow:
            return await uow.purchases.get_by_external_id(provider, external_id)

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
