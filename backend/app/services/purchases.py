"""Purchase use cases.

This service owns the purchase lifecycle only: creating purchases, confirming
payments, moving statuses and answering "does this buyer already own it?".
It never sends a message — handing the link to the buyer belongs to
``DeliveryService``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.exceptions import (
    ConflictError,
    DuplicatePurchaseError,
    FreePackAlreadyUsedError,
    FreePackNotAvailableError,
    FreePackPendingCheckoutError,
    LoyaltyDisabledError,
    ProductInactiveError,
    ProductNotFoundError,
    ProviderNotSupportedError,
    PurchaseNotFoundError,
    UserNotFoundError,
)
from app.core.logging import get_logger
from app.domain.bonuses import PriceQuote, plain_quote
from app.domain.cards import PaymentOption, ProductCard
from app.domain.commands import PurchaseDraft
from app.domain.delivery import RecordedDelivery
from app.domain.enums import (
    PAYABLE_PROVIDERS,
    LoyaltyRewardStatus,
    PaymentProvider,
    PurchaseStatus,
)
from app.domain.locks import payment_lock_key, purchase_lock_key
from app.domain.loyalty import (
    LOYALTY_DISCOUNT_PERCENT,
    FreePackGrant,
    free_pack_external_id,
    free_pack_value,
    loyalty_quote,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from uuid import UUID

    from app.domain.bonuses import CheckoutPricing
    from app.domain.commands import UserDraft
    from app.domain.entities import LoyaltyReward, Product, Purchase, User
    from app.domain.enums import Currency
    from app.domain.locks import LockManager
    from app.domain.loyalty import PurchaseLoyalty
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
    """Optional pricing: referral discount, loyalty discount, bonuses.

    ``None`` is the shop as it was before the referral feature existed: the
    list price is charged, nothing is held and nothing is recorded. Everything
    below therefore has exactly one extra branch, and the default path through
    it is byte for byte the old behaviour.
    """
    loyalty: PurchaseLoyalty | None = None
    """Optional loyalty programme: the free pack and the milestone ranking.

    With ``None`` — or the programme switched off — no card offers the free
    pack, nothing can claim it, and a delivery is recorded exactly as before.
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
            # Priced in the same transaction the card already opened, through
            # the very path a checkout uses — so the card never shows a price
            # the invoice would then disagree with. An owned product shows no
            # buttons, so it is not priced at all.
            quotes = (
                []
                if owned is not None
                else [
                    await self._card_quote(uow, profile.telegram_id, product, provider)
                    for provider in product.available_providers
                ]
            )
            free_pack = owned is None and await self._free_pack_offered(uow, profile.telegram_id)

        options = tuple(
            self._payment_option(product, provider, quotes[index] if quotes else None)
            for index, provider in enumerate(product.available_providers)
        )
        referral = any(priced.uses_discount for priced in quotes)
        loyalty = any(priced.uses_loyalty for priced in quotes)
        logger.info(
            "card_opened",
            telegram_id=profile.telegram_id,
            slug=slug,
            owned=owned is not None,
            discounted=referral,
            loyalty=loyalty,
            free_pack=free_pack,
        )
        return ProductCard(
            product=product,
            options=options,
            owned_purchase=owned,
            discount_percent=self.discount_percent if referral else 0,
            loyalty_percent=LOYALTY_DISCOUNT_PERCENT if loyalty else 0,
            free_pack_available=free_pack,
        )

    async def _free_pack_offered(self, uow: UnitOfWork, user_id: int) -> bool:
        """Whether the card offers this visitor the pack as their free pack."""
        if self.loyalty is None or not self.loyalty.enabled:
            return False
        return await self.loyalty.free_pack_available(uow, user_id)

    @property
    def discount_percent(self) -> int:
        """Referral discount the shop is configured to give, as a percentage."""
        return 0 if self.pricing is None else self.pricing.discount_percent

    async def _card_quote(
        self,
        uow: UnitOfWork,
        user_id: int,
        product: Product,
        provider: PaymentProvider,
    ) -> PriceQuote:
        """This visitor's price on one rail, as the card shows it.

        A discount held by the visitor's own unpaid invoice for this pack is
        invisible to a fresh quote, yet the button bills it: the ⭐ invoice is
        sent again, the 💎 one handed back, and an unpaid ⭐ invoice's loyalty
        discount moves to a new 💎 one. So the card shows it as well.
        """
        base_amount = Decimal(self._price(product, provider))
        if self.pricing is None:
            return plain_quote(base_amount, provider.currency)
        held = await self._held_discount(uow, user_id, product, provider, base_amount)
        if held is not None:
            return held
        return await self.pricing.card_quote(
            uow,
            user_id=user_id,
            base_amount=base_amount,
            currency=provider.currency,
            stars_per_usdt=product.stars_per_usdt,
        )

    async def _held_discount(
        self,
        uow: UnitOfWork,
        user_id: int,
        product: Product,
        provider: PaymentProvider,
        base_amount: Decimal,
    ) -> PriceQuote | None:
        """The discount this rail's button bills from the visitor's own unpaid invoice.

        The newest unpaid invoice on this rail keeps its discount: it is sent or
        handed back again, or — once it has run out or the price moved — the
        discount it gives up goes to its replacement. A referral discount is
        only mirrored at the list price it was taken off. Without a live 💎
        invoice, a 💎 button takes the loyalty discount an unpaid ⭐ invoice for
        this pack holds.
        """
        pending = await uow.purchases.pending_checkouts(user_id, product.id, provider)
        newest = pending[0] if pending else None
        if newest is not None and newest.loyalty_amount > 0:
            return loyalty_quote(base_amount, provider.currency)
        if newest is not None and newest.discount_amount > 0 and newest.list_amount == base_amount:
            return PriceQuote(
                base_amount=base_amount,
                discount_amount=newest.discount_amount,
                currency=provider.currency,
            )
        if provider is PaymentProvider.CRYPTO and (newest is None or self.is_stale(newest)):
            stars = await uow.purchases.pending_checkouts(
                user_id, product.id, PaymentProvider.STARS
            )
            if any(purchase.loyalty_amount > 0 for purchase in stars):
                return loyalty_quote(base_amount, provider.currency)
        return None

    def _payment_option(
        self,
        product: Product,
        provider: PaymentProvider,
        priced: PriceQuote | None,
    ) -> PaymentOption:
        """One button, with the reduced price attached when one applies.

        ``amount`` stays the list price whatever happens: a reduction is a
        property of this visitor's next sale, not of the catalogue.
        """
        listed = self._price(product, provider)
        reduced = (
            priced.charged_amount
            if priced is not None and priced.charged_amount < Decimal(listed)
            else None
        )
        return PaymentOption(
            provider=provider,
            amount=listed,
            currency=provider.currency,
            discounted_amount=reduced,
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
        async with self.checkout_session(user_id, product_id) as session:
            return await session.record(
                provider=provider,
                external_id=external_id,
                use_bonus=use_bonus,
                expected_amount=expected_amount,
            )

    @asynccontextmanager
    async def checkout_session(
        self,
        user_id: int,
        product_id: UUID,
    ) -> AsyncIterator[CheckoutSession]:
        """Hold the purchase lock of one buyer and product for a whole checkout.

        For rails that must talk to the provider *before* the purchase row
        exists: everything from "is there already an invoice?" to the insert
        happens under one lock, so a second press cannot reach the provider
        while the first is still in flight. The session refuses to be used once
        the lock is released.

        Raises:
            LockBusyError: another checkout for this buyer and product is running.
        """
        async with self.locks.lock(purchase_lock_key(user_id, product_id)):
            session = CheckoutSession(self, user_id=user_id, product_id=product_id)
            try:
                yield session
            finally:
                session.close()

    async def _record_purchase(  # noqa: PLR0913 — one checkout, this many facts
        self,
        *,
        user_id: int,
        product_id: UUID,
        provider: PaymentProvider,
        external_id: str,
        use_bonus: bool,
        expected_amount: Decimal | None,
    ) -> Purchase:
        """Validate, price and insert the purchase. The caller holds the lock."""
        async with self.uow_factory() as uow:
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
                    loyalty_amount=priced.loyalty_amount,
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

    async def _grant_free_pack(self, *, user_id: int, product_id: UUID) -> FreePackGrant:
        """Hand this product over as the buyer's free pack. The caller holds the lock.

        One transaction: the reward is locked, the ``internal`` purchase is
        inserted already paid — amount 0, the whole list price waived — and the
        reward is consumed by it. Either both happen or neither does.

        Pressing again for the pack already handed over returns that same
        purchase, so a delivery that failed can simply be retried.

        Raises:
            LoyaltyDisabledError: the programme is off.
            FreePackNotAvailableError: fewer than 10 completed purchases.
            FreePackAlreadyUsedError: the free pack went to another product.
            ProductNotFoundError, ProductInactiveError: product unavailable.
            DuplicatePurchaseError: the buyer already owns the product.
            FreePackPendingCheckoutError: an unpaid invoice for it exists.
            UserNotFoundError: the buyer was never recorded.
        """
        rewards = self.loyalty
        if rewards is None or not rewards.enabled:
            raise LoyaltyDisabledError(user_id=user_id)
        async with self.uow_factory() as uow:
            if await uow.users.get(user_id) is None:
                raise UserNotFoundError(telegram_id=user_id)
            product = await uow.products.get(product_id)
            if product is None:
                raise ProductNotFoundError(product_id=str(product_id))

            reward = await rewards.lock_free_pack(uow, user_id)
            if reward is None:
                raise FreePackNotAvailableError(user_id=user_id)
            if reward.status is LoyaltyRewardStatus.CONSUMED:
                return await self._free_pack_handed_over(uow, reward, product_id)

            if not product.is_active:
                raise ProductInactiveError(product_id=str(product_id))
            if await uow.purchases.find_access_granting(user_id, product_id) is not None:
                raise DuplicatePurchaseError(product_id=str(product_id), user_id=user_id)
            for provider in PAYABLE_PROVIDERS:
                if await uow.purchases.pending_checkouts(user_id, product_id, provider):
                    raise FreePackPendingCheckoutError(
                        product_id=str(product_id),
                        provider=provider.value,
                    )

            value, currency = free_pack_value(product)
            purchase = await uow.purchases.create(
                PurchaseDraft(
                    user_id=user_id,
                    product_id=product_id,
                    provider=PaymentProvider.INTERNAL,
                    status=PurchaseStatus.PAID,
                    amount=Decimal(0),
                    base_amount=value,
                    loyalty_amount=value,
                    currency=currency,
                    external_id=free_pack_external_id(reward.id),
                )
            )
            await rewards.consume_free_pack(uow, reward, purchase)

        logger.info(
            "free_pack_granted",
            purchase_id=str(purchase.id),
            user_id=user_id,
            product_id=str(product_id),
            reward_id=str(reward.id),
            value=str(value),
            currency=currency.value,
        )
        return FreePackGrant(purchase=purchase)

    @staticmethod
    async def _free_pack_handed_over(
        uow: UnitOfWork,
        reward: LoyaltyReward,
        product_id: UUID,
    ) -> FreePackGrant:
        """A press for a free pack already claimed: that pack again, or a refusal."""
        handed = (
            await uow.purchases.get(reward.consumed_purchase_id)
            if reward.consumed_purchase_id is not None
            else None
        )
        if handed is None or handed.product_id != product_id or not handed.grants_access:
            raise FreePackAlreadyUsedError(
                reward_id=str(reward.id),
                consumed_purchase_id=str(reward.consumed_purchase_id),
            )
        logger.info("free_pack_already_granted", purchase_id=str(handed.id))
        return FreePackGrant(purchase=handed, repeated=True)

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
            ProviderNotSupportedError: ``provider`` is not a payment rail. An
                internal grant is never "paid"; nothing may confirm one.
            PurchaseNotFoundError: no purchase behind this invoice.
            ConflictError: the purchase was refunded.
            DuplicatePurchaseError: the buyer already owns a paid copy.
        """
        if not provider.is_payable:
            raise ProviderNotSupportedError(provider=provider.value, external_id=external_id)
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
        """Record that the buyer received the link. See ``record_delivery``.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
            ConflictError: the purchase is not paid.
        """
        recorded = await self.record_delivery(purchase_id, delivered_url=delivered_url)
        return recorded.purchase

    async def record_delivery(self, purchase_id: UUID, *, delivered_url: str) -> RecordedDelivery:
        """Record that the buyer received the link, and which milestone that reached.

        Called by ``DeliveryService`` once the link is sent. The one place a
        loyalty milestone is decided, and only while the programme is on — off,
        this is the same single ``UPDATE`` it always was, with no lock and no
        count.

        On, in one transaction: the purchase row is locked and read fresh, then
        the buyer's progress lock is taken — the same per-buyer key on every
        path that delivers — then the purchase is marked delivered. Only a
        genuine ``paid → delivered`` is ranked; a purchase delivered already
        reaches nothing, however often it is recorded again. The caller sends
        any notice after this returns, i.e. after the commit.

        Raises:
            PurchaseNotFoundError: no purchase with this id.
            ConflictError: the purchase is not paid.
        """
        loyalty = self.loyalty if self.loyalty is not None and self.loyalty.enabled else None
        milestone: int | None = None
        async with self.uow_factory() as uow:
            if loyalty is None:
                delivered = await uow.purchases.mark_delivered(
                    purchase_id,
                    delivered_url=delivered_url,
                )
            else:
                before = await uow.purchases.get_for_update(purchase_id)
                if before is None:
                    raise PurchaseNotFoundError(purchase_id=str(purchase_id))
                await loyalty.lock_progress(uow, before.user_id)
                delivered = await uow.purchases.mark_delivered(
                    purchase_id,
                    delivered_url=delivered_url,
                )
                transitioned = (
                    before.status is PurchaseStatus.PAID
                    and delivered.status is PurchaseStatus.DELIVERED
                )
                if transitioned:
                    milestone = await loyalty.milestone_reached(uow, delivered.user_id)
        logger.info("purchase_delivered", purchase_id=str(purchase_id))
        return RecordedDelivery(purchase=delivered, milestone=milestone)

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

    async def is_superseded(self, purchase: Purchase) -> bool:
        """Whether the buyer started a later checkout of this product on the same rail."""
        async with self.uow_factory() as uow:
            return await uow.purchases.has_later_checkout(purchase)

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

    def is_stale(self, purchase: Purchase, *, now: datetime | None = None) -> bool:
        """Whether this purchase's invoice lifetime has passed: what ``expire_stale`` expires."""
        moment = now or datetime.now(UTC)
        return purchase.created_at < moment - self.invoice_ttl

    async def withdraw_unsent_checkout(self, purchase_id: UUID) -> Purchase:
        """Expire a pending purchase whose invoice never reached the buyer.

        Whatever it held is given back in the same transaction. Safe without the
        checkout lock: only a still-pending row moves, under its own row lock, so
        a purchase that was paid in the meantime is left untouched.
        """
        return await self._expire_checkout(purchase_id)

    async def _expire_checkout(self, purchase_id: UUID) -> Purchase:
        """Expire one pending purchase and release what it held. The caller holds the lock."""
        async with self.uow_factory() as uow:
            expired = await uow.purchases.expire(purchase_id)
            if expired.status is PurchaseStatus.EXPIRED and self.pricing is not None:
                await self.pricing.on_checkout_expired(uow, expired)
        logger.info(
            "pending_purchase_expired",
            purchase_id=str(purchase_id),
            status=expired.status.value,
        )
        return expired

    async def pending_checkout(
        self,
        user_id: int,
        product_id: UUID,
        provider: PaymentProvider,
    ) -> Purchase | None:
        """The buyer's newest unpaid invoice for this product on this rail, if any.

        Its bonus choice was made when it was issued: pressing the same payment
        button again hands it back, or replaces it once it has run out, on that
        same choice. Read only.
        """
        pending = await self._pending_checkouts(user_id, product_id, provider)
        return pending[0] if pending else None

    async def _pending_checkouts(
        self,
        user_id: int,
        product_id: UUID,
        provider: PaymentProvider,
    ) -> tuple[Purchase, ...]:
        async with self.uow_factory() as uow:
            return await uow.purchases.pending_checkouts(user_id, product_id, provider)

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


class CheckoutSession:
    """What a checkout may do while it holds its buyer's purchase lock.

    Only ``PurchaseService.checkout_session`` hands these out, and every method
    refuses to run once that lock is released — so the "the caller holds the
    lock" methods behind it cannot be reached without the lock by accident.
    """

    __slots__ = ("_open", "_service", "product_id", "user_id")

    def __init__(self, service: PurchaseService, *, user_id: int, product_id: UUID) -> None:
        self._service = service
        self.user_id = user_id
        self.product_id = product_id
        self._open = True

    def close(self) -> None:
        """Called when the lock is released; the session is dead from then on."""
        self._open = False

    async def owned(self) -> Purchase | None:
        """The buyer's paid or delivered purchase of this product, if any."""
        self._require_open()
        return await self._service.find_owned(self.user_id, self.product_id)

    async def pending(self, provider: PaymentProvider) -> tuple[Purchase, ...]:
        """The buyer's pending purchases of this product on one rail, newest first."""
        self._require_open()
        return await self._service._pending_checkouts(self.user_id, self.product_id, provider)

    async def expire(self, purchase: Purchase) -> Purchase:
        """Expire one of this buyer's pending purchases of this product.

        For an invoice the provider reports as expired, or one a newer invoice
        replaces; whatever it held is released in the same transaction.
        Anything no longer pending is returned untouched.
        """
        self._require_open()
        self._require_own(purchase.user_id, purchase.product_id)
        return await self._service._expire_checkout(purchase.id)

    async def quote(
        self,
        product: Product,
        provider: PaymentProvider,
        *,
        use_bonus: bool = False,
    ) -> Decimal:
        """What this checkout would be billed right now. Records nothing."""
        self._require_open()
        self._require_own(self.user_id, product.id)
        return await self._service.quote_amount(
            user_id=self.user_id,
            product=product,
            provider=provider,
            use_bonus=use_bonus,
        )

    async def record(
        self,
        *,
        provider: PaymentProvider,
        external_id: str,
        use_bonus: bool = False,
        expected_amount: Decimal | None = None,
    ) -> Purchase:
        """Insert the pending purchase for an invoice. See ``start_purchase``."""
        self._require_open()
        return await self._service._record_purchase(
            user_id=self.user_id,
            product_id=self.product_id,
            provider=provider,
            external_id=external_id,
            use_bonus=use_bonus,
            expected_amount=expected_amount,
        )

    async def grant_free_pack(self) -> FreePackGrant:
        """Hand this product over as the buyer's free pack. See ``_grant_free_pack``."""
        self._require_open()
        return await self._service._grant_free_pack(
            user_id=self.user_id,
            product_id=self.product_id,
        )

    def _require_open(self) -> None:
        if not self._open:
            message = "The checkout session was used after its purchase lock was released"
            raise RuntimeError(message)

    def _require_own(self, user_id: int, product_id: UUID) -> None:
        if (user_id, product_id) != (self.user_id, self.product_id):
            message = "The checkout session is locked for a different buyer or product"
            raise ValueError(message)
