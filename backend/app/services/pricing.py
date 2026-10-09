"""One authoritative pricing path: referral discount, then loyalty, then bonuses.

``PricingService`` decides nothing on its own. It is the orchestration layer
that puts the loyalty programme's 50% discount *between* the two things
``BonusService`` already prices, and it reuses that logic rather than copying
it:

    Referral > Loyalty > Bonus > list price — never two of them at once.

Every figure a buyer meets goes through here: the product card
(``card_quote``), the bonus question (``offer_for``), the CryptoBot invoice
amount (``preview``) and the purchase row itself (``resolve``). They differ in
one respect only — whether rows are locked and recorded — so the card, the
invoice and the stored sale cannot disagree.

While the loyalty programme is off (or not wired at all) every method is a
plain delegation to ``BonusService``: the shop prices exactly as it did before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.domain.loyalty import loyalty_quote
from app.services.bonuses import BonusOffer

if TYPE_CHECKING:
    from decimal import Decimal

    from app.domain.bonuses import PriceQuote
    from app.domain.entities import Purchase
    from app.domain.enums import Currency
    from app.domain.loyalty import LoyaltyPricing
    from app.domain.uow import UnitOfWork, UnitOfWorkFactory
    from app.services.bonuses import BonusService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PricingService:
    """Prices a checkout across the referral, loyalty and bonus programmes."""

    uow_factory: UnitOfWorkFactory
    bonuses: BonusService
    loyalty: LoyaltyPricing | None = None
    """The loyalty programme's 50% discount. ``None`` until it is wired."""

    @property
    def discount_percent(self) -> int:
        """Referral discount this shop gives, as a percentage."""
        return self.bonuses.discount_percent

    @property
    def _loyalty(self) -> LoyaltyPricing | None:
        """The loyalty side, only while it may price new checkouts."""
        if self.loyalty is None or not self.loyalty.enabled:
            return None
        return self.loyalty

    # ------------------------------------------------------------------ prices

    async def resolve(  # noqa: PLR0913 — one checkout, this many pricing facts
        self,
        uow: UnitOfWork,
        *,
        user_id: int,
        base_amount: Decimal,
        currency: Currency,
        use_bonus: bool,
        stars_per_usdt: Decimal | None = None,
    ) -> PriceQuote:
        """Price a checkout inside its transaction, locking what it will use.

        A loyalty discount that applies wins over bonuses even when bonuses were
        asked for: the bot never offers them then, so such a request is not the
        buyer's choice, and the two are never combined.
        """
        loyalty = self._loyalty
        if loyalty is None:
            return await self.bonuses.resolve(
                uow,
                user_id=user_id,
                base_amount=base_amount,
                currency=currency,
                use_bonus=use_bonus,
                stars_per_usdt=stars_per_usdt,
            )

        referral = await self.bonuses.resolve(
            uow,
            user_id=user_id,
            base_amount=base_amount,
            currency=currency,
            use_bonus=False,
            stars_per_usdt=stars_per_usdt,
        )
        if referral.uses_discount:
            return referral

        reward_id = await loyalty.lock_discount(uow, user_id)
        if reward_id is not None:
            loyal = loyalty_quote(base_amount, currency, reward_id=reward_id)
            if loyal is not None:
                if use_bonus:
                    logger.info("bonus_request_superseded_by_loyalty", user_id=user_id)
                return loyal

        if not use_bonus:
            return referral
        return await self.bonuses.resolve(
            uow,
            user_id=user_id,
            base_amount=base_amount,
            currency=currency,
            use_bonus=True,
            stars_per_usdt=stars_per_usdt,
        )

    async def preview(
        self,
        *,
        user_id: int,
        base_amount: Decimal,
        currency: Currency,
        use_bonus: bool,
        stars_per_usdt: Decimal | None = None,
    ) -> PriceQuote:
        """What ``resolve`` would charge right now. Read only; records nothing."""
        loyalty = self._loyalty
        if loyalty is None:
            return await self.bonuses.preview(
                user_id=user_id,
                base_amount=base_amount,
                currency=currency,
                use_bonus=use_bonus,
                stars_per_usdt=stars_per_usdt,
            )

        referral = await self.bonuses.preview(
            user_id=user_id,
            base_amount=base_amount,
            currency=currency,
            use_bonus=False,
            stars_per_usdt=stars_per_usdt,
        )
        if referral.uses_discount:
            return referral
        loyal = await self._loyalty_prediction(loyalty, user_id, base_amount, currency)
        if loyal is not None:
            return loyal
        if not use_bonus:
            return referral
        return await self.bonuses.preview(
            user_id=user_id,
            base_amount=base_amount,
            currency=currency,
            use_bonus=True,
            stars_per_usdt=stars_per_usdt,
        )

    async def card_quote(
        self,
        uow: UnitOfWork,
        *,
        user_id: int,
        base_amount: Decimal,
        currency: Currency,
        stars_per_usdt: Decimal | None = None,
    ) -> PriceQuote:
        """The card's price on one rail: ``preview`` without bonuses, in the card's transaction."""
        referral = await self.bonuses.card_quote(
            uow,
            user_id=user_id,
            base_amount=base_amount,
            currency=currency,
            stars_per_usdt=stars_per_usdt,
        )
        loyalty = self._loyalty
        if loyalty is None or referral.uses_discount:
            return referral
        if await loyalty.discount_available(uow, user_id):
            loyal = loyalty_quote(base_amount, currency)
            if loyal is not None:
                return loyal
        return referral

    async def offer_for(
        self,
        *,
        user_id: int,
        base_amount: Decimal,
        currency: Currency,
        stars_per_usdt: Decimal | None = None,
    ) -> BonusOffer:
        """The "spend your bonuses?" question — never asked when loyalty applies."""
        offer = await self.bonuses.offer_for(
            user_id=user_id,
            base_amount=base_amount,
            currency=currency,
            stars_per_usdt=stars_per_usdt,
        )
        loyalty = self._loyalty
        if loyalty is None or offer.quote_without_bonus.uses_discount:
            return offer
        loyal = await self._loyalty_prediction(loyalty, user_id, base_amount, currency)
        if loyal is None:
            return offer
        return BonusOffer(balance=offer.balance, quote_without_bonus=loyal)

    async def _loyalty_prediction(
        self,
        loyalty: LoyaltyPricing,
        user_id: int,
        base_amount: Decimal,
        currency: Currency,
    ) -> PriceQuote | None:
        """The loyalty price if the reward is due and the discount is not zero."""
        async with self.uow_factory() as uow:
            available = await loyalty.discount_available(uow, user_id)
        return loyalty_quote(base_amount, currency) if available else None

    # ------------------------------------------------------------------- holds

    async def hold_for(self, uow: UnitOfWork, purchase: Purchase, priced: PriceQuote) -> None:
        """Record what funds this purchase, in the transaction that created it."""
        await self.bonuses.hold_for(uow, purchase, priced)
        if priced.uses_loyalty and self.loyalty is not None:
            await self.loyalty.apply_discount(uow, purchase, priced)

    async def on_payment_confirmed(self, uow: UnitOfWork, purchase: Purchase) -> None:
        """Settle every hold of a paid purchase, in the transaction that paid it.

        The loyalty side settles whatever it holds even after the programme is
        switched off: a buyer billed at half price has used their reward.
        """
        await self.bonuses.on_payment_confirmed(uow, purchase)
        if purchase.loyalty_amount > 0 and self.loyalty is not None:
            await self.loyalty.consume_discount(uow, purchase)

    async def on_checkout_expired(self, uow: UnitOfWork, purchase: Purchase) -> None:
        """Give back every hold of an invoice that just expired. Idempotent."""
        await self.bonuses.on_checkout_expired(uow, purchase)
        if purchase.loyalty_amount > 0 and self.loyalty is not None:
            await self.loyalty.release_discount(uow, purchase)


__all__ = ["PricingService"]
