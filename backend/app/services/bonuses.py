"""Bonus balance use cases: pricing a checkout and paying an inviter.

This module holds the two things that touch money, and each one is placed where
it cannot go half-done.

**Pricing** runs inside the caller's transaction — the same one that inserts the
purchase. That matters more than it looks: the discount, the reservation of
bonus units and the purchase row commit together or not at all, so there is no
window in which a buyer is billed an amount whose funding was never recorded.
It also means the referral feature has no separate failure mode at checkout: it
is not a network call that can time out, it is the same PostgreSQL transaction
that already had to succeed for a sale to exist.

**Reward accrual** runs after delivery, in its own transaction, and is allowed
to fail. A buyer who paid must get their link whatever the ledger thinks, so a
failure here is logged and repaired later by housekeeping — the same shape as
the existing reconciliation loop for lost payment webhooks.

Rewards are final. A refund does not reverse a reward, does not create a debt
and does not touch a balance; the existing refund flow is untouched by this
module on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.exceptions import (
    AppError,
    BonusesNotAvailableError,
    InsufficientBonusBalanceError,
)
from app.core.logging import get_logger
from app.domain.bonuses import (
    BonusPolicy,
    PriceQuote,
    plain_quote,
    quote,
    reward_bonuses,
)
from app.domain.commands import BonusTransactionDraft
from app.domain.enums import (
    PURCHASE_HISTORY_STATUSES,
    BonusTransactionType,
    Currency,
    PurchaseStatus,
)
from app.domain.locks import bonus_lock_key
from app.domain.notifications import RewardNotice

if TYPE_CHECKING:
    from decimal import Decimal
    from uuid import UUID

    from app.core.config import ReferralSettings
    from app.domain.entities import Purchase, Referral
    from app.domain.locks import LockManager
    from app.domain.notifications import RewardNotifier
    from app.domain.uow import UnitOfWork, UnitOfWorkFactory

logger = get_logger(__name__)

BONUS_LOCK_WAIT_SECONDS = 3.0
"""A buyer pressing pay deserves a short wait rather than an error."""


@dataclass(frozen=True, slots=True, kw_only=True)
class BonusOffer:
    """What the bot may offer a buyer before it issues an invoice.

    ``available`` is false whenever bonuses must not be offered at all — the
    programme is off, the balance is empty, or this is the buyer's first
    purchase under a referral, where the discount applies instead.
    """

    available: bool = False
    balance: int = 0
    units: int = 0
    quote_with_bonus: PriceQuote | None = None
    quote_without_bonus: PriceQuote


@dataclass(frozen=True, slots=True)
class BonusService:
    """Prices checkouts, holds bonus units, and credits inviters."""

    uow_factory: UnitOfWorkFactory
    locks: LockManager
    settings: ReferralSettings
    notifier: RewardNotifier | None = None

    @property
    def policy(self) -> BonusPolicy:
        """The configured numbers as a domain value object."""
        return BonusPolicy.from_settings(self.settings)

    @property
    def discount_percent(self) -> int:
        """Referral discount this shop gives, as a percentage."""
        return self.settings.discount_percent if self.settings.enabled else 0

    # ------------------------------------------------------------------ pricing

    async def resolve(
        self,
        uow: UnitOfWork,
        *,
        user_id: int,
        base_amount: Decimal,
        currency: Currency,
        use_bonus: bool,
    ) -> PriceQuote:
        """Price a checkout and hold whatever funds it, in the caller's transaction.

        Raises:
            InsufficientBonusBalanceError: the buyer asked to spend bonuses they
                no longer have. Refusing is deliberate: silently charging the
                full price would surprise somebody who just saw a lower one.
            BonusesNotAvailableError: bonuses were requested on a rail that
                cannot spend them.
        """
        if not self.settings.enabled:
            return plain_quote(base_amount, currency)

        if use_bonus and currency is not Currency.XTR:
            # A bonus is a Star, so it can only reduce a Stars invoice. The bot
            # never offers the choice on a crypto card, so reaching this means a
            # hand-crafted callback — refuse it rather than quietly ignore it.
            raise BonusesNotAvailableError(user_id=user_id, currency=currency.value)

        referral = await uow.referrals.get_by_referred(user_id)
        discount_eligible = await self._discount_eligible(uow, user_id, referral=referral)

        balance = 0
        if use_bonus and not discount_eligible:
            # Row locked: two checkouts started at the same instant cannot both
            # be told the same units are free.
            balance = await uow.bonuses.lock_balance(user_id)

        priced = quote(
            base_amount,
            currency,
            policy=self.policy,
            referral_id=referral.id if referral is not None else None,
            discount_eligible=discount_eligible,
            balance=balance,
            use_bonus=use_bonus and not discount_eligible,
        )

        if use_bonus and not discount_eligible and not priced.uses_bonus:
            raise InsufficientBonusBalanceError(user_id=user_id, balance=balance)
        return priced

    async def preview(
        self,
        *,
        user_id: int,
        base_amount: Decimal,
        currency: Currency,
        use_bonus: bool,
    ) -> PriceQuote:
        """Price a checkout without holding anything. Read only."""
        offer = await self.offer_for(
            user_id=user_id,
            base_amount=base_amount,
            currency=currency,
        )
        if use_bonus and offer.available and offer.quote_with_bonus is not None:
            return offer.quote_with_bonus
        return offer.quote_without_bonus

    async def discount_eligible(self, uow: UnitOfWork, user_id: int) -> bool:
        """Whether this buyer's next purchase carries the referral discount.

        Answered from the caller's transaction, which is what lets the product
        card show the reduced price without a second round trip.
        """
        if not self.settings.enabled:
            return False
        referral = await uow.referrals.get_by_referred(user_id)
        return await self._discount_eligible(uow, user_id, referral=referral)

    async def hold_for(self, uow: UnitOfWork, purchase: Purchase, priced: PriceQuote) -> None:
        """Record the reservation behind a purchase that was just created.

        Separate from ``resolve`` only because the purchase id does not exist
        until the row is inserted; both run in the same transaction.
        """
        if priced.bonus_units <= 0:
            return

        entry = await uow.bonuses.add(
            BonusTransactionDraft(
                user_id=purchase.user_id,
                amount=-priced.bonus_units,
                type=BonusTransactionType.BONUS_RESERVED,
                purchase_id=purchase.id,
                # No rate: bonuses are only ever spent on a Stars invoice, and
                # one bonus is one Star.
            )
        )
        if entry is None:  # pragma: no cover — a fresh purchase id cannot collide
            logger.warning("bonus_hold_already_recorded", purchase_id=str(purchase.id))
            return
        logger.info(
            "bonus_reserved",
            purchase_id=str(purchase.id),
            user_id=purchase.user_id,
            units=priced.bonus_units,
        )

    async def on_payment_confirmed(self, uow: UnitOfWork, purchase: Purchase) -> None:
        """Turn the holds behind a paid purchase into settled facts.

        The referral discount is burned *here* rather than when the invoice was
        issued: an abandoned invoice must not consume the buyer's one discount.
        """
        if not self.settings.enabled:
            return

        await uow.bonuses.settle_hold(purchase.id)

        if purchase.discount_amount <= 0:
            return
        referral = await uow.referrals.get_by_referred(purchase.user_id)
        if referral is None:  # pragma: no cover — a discount implies a referral
            logger.warning("discount_without_referral", purchase_id=str(purchase.id))
            return
        await uow.referrals.mark_discount_used(referral.id, purchase_id=purchase.id)
        logger.info(
            "referral_discount_used",
            purchase_id=str(purchase.id),
            referral_id=str(referral.id),
            discount=str(purchase.discount_amount),
        )

    async def offer_for(
        self,
        *,
        user_id: int,
        base_amount: Decimal,
        currency: Currency,
    ) -> BonusOffer:
        """Price both branches so the bot can ask "spend your bonuses?".

        Read only: nothing is held until a button is actually pressed.
        """
        without = plain_quote(base_amount, currency)
        if not self.settings.enabled:
            return BonusOffer(quote_without_bonus=without)
        if currency is not Currency.XTR:
            # Bonuses are a Stars discount. A crypto card is never asked about
            # them, so the ordinary two-tap flow is preserved there.
            return BonusOffer(quote_without_bonus=without)

        async with self.uow_factory() as uow:
            referral = await uow.referrals.get_by_referred(user_id)
            discount_eligible = await self._discount_eligible(uow, user_id, referral=referral)
            balance = await uow.bonuses.balance(user_id)

        referral_id = referral.id if referral is not None else None
        without = quote(
            base_amount,
            currency,
            policy=self.policy,
            referral_id=referral_id,
            discount_eligible=discount_eligible,
        )
        if discount_eligible or balance <= 0:
            # On a first referral purchase bonuses are not offered at all: the
            # discount applies instead, which keeps the economics simple and
            # removes a family of edge cases.
            return BonusOffer(balance=balance, quote_without_bonus=without)

        with_bonus = quote(
            base_amount,
            currency,
            policy=self.policy,
            balance=balance,
            use_bonus=True,
        )
        if not with_bonus.uses_bonus:
            return BonusOffer(balance=balance, quote_without_bonus=without)

        return BonusOffer(
            available=True,
            balance=balance,
            units=with_bonus.bonus_units,
            quote_with_bonus=with_bonus,
            quote_without_bonus=without,
        )

    async def balance_of(self, user_id: int) -> int:
        """Spendable bonus units of one user."""
        async with self.uow_factory() as uow:
            return await uow.bonuses.balance(user_id)

    # ------------------------------------------------------------------ rewards

    async def accrue(self, purchase_id: UUID) -> None:
        """Credit the buyer's inviter for a settled purchase. Never raises.

        Called right after a sale is marked delivered. Failure is contained: the
        buyer already has their link, and housekeeping repairs the ledger.
        """
        await self.accrue_once(purchase_id)

    async def accrue_once(self, purchase_id: UUID) -> bool:
        """Credit the inviter and report whether this call was the one that did.

        Returns ``False`` for "nothing to do" as well as for failure, which are
        the same thing to a caller that only needs to know if it made progress.
        """
        if not self.settings.enabled:
            return False
        try:
            notice = await self._accrue(purchase_id)
        except AppError as error:
            logger.error(  # noqa: TRY400 — a ledger hiccup must not fail a delivery
                "referral_reward_failed",
                purchase_id=str(purchase_id),
                error=str(error),
            )
            return False
        if notice is None:
            return False
        await self._notify(notice)
        return True

    async def invite_buyer(self, user_id: int) -> None:
        """Offer the referral programme to somebody who just bought. Never raises.

        A one-line invitation with a button, not a requirement: the shop works
        exactly the same for a buyer who ignores it forever.
        """
        if not self.settings.enabled or self.notifier is None:
            return
        try:
            await self.notifier.notify_purchase_complete(user_id)
        except Exception as error:
            logger.info("purchase_hint_not_delivered", user_id=user_id, error=str(error))

    async def accrue_missing(self, *, limit: int = 100) -> int:
        """Credit inviters for delivered sales that carry no reward yet.

        The safety net for a process that died between marking a sale delivered
        and writing the ledger entry.
        """
        if not self.settings.enabled:
            return 0
        async with self.uow_factory() as uow:
            pending = await uow.bonuses.purchases_awaiting_reward(limit=limit)

        accrued = 0
        for purchase_id in pending:
            if await self.accrue_once(purchase_id):
                accrued += 1
        if accrued:
            logger.info("referral_rewards_backfilled", count=accrued)
        return accrued

    async def release_stale_holds(self, *, limit: int = 100) -> int:
        """Give back bonus units held by invoices that can no longer be paid."""
        if not self.settings.enabled:
            return 0

        async with self.uow_factory() as uow:
            stale = await uow.bonuses.stale_holds(limit=limit)

        released = 0
        for purchase_id in stale:
            async with self.uow_factory() as uow:
                purchase = await uow.purchases.get(purchase_id)
                if purchase is None:  # pragma: no cover — protected by the FK
                    continue
                async with self.locks.lock(bonus_lock_key(purchase.user_id)):
                    if await uow.bonuses.release_hold(purchase_id) is not None:
                        released += 1
        if released:
            logger.info("bonus_reserves_released", count=released)
        return released

    async def _accrue(self, purchase_id: UUID) -> RewardNotice | None:
        """Write the reward entry, or return ``None`` when there is nothing to do."""
        async with self.uow_factory() as uow:
            purchase = await uow.purchases.get(purchase_id)
            if purchase is None:
                logger.warning("reward_for_unknown_purchase", purchase_id=str(purchase_id))
                return None

            # Only a delivered sale earns a reward. A pending, expired, failed
            # or refunded purchase earns nothing, and neither does an invoice.
            if purchase.status is not PurchaseStatus.DELIVERED:
                return None

            referral = await uow.referrals.get_by_referred(purchase.user_id)
            if referral is None:
                return None

            units, rate = await self._reward_for(uow, purchase)
            if units <= 0:
                return None

            async with self.locks.lock(bonus_lock_key(referral.referrer_user_id)):
                entry = await uow.bonuses.add(
                    BonusTransactionDraft(
                        user_id=referral.referrer_user_id,
                        amount=units,
                        type=BonusTransactionType.REFERRAL_REWARD,
                        referral_id=referral.id,
                        purchase_id=purchase.id,
                        stars_per_usdt=rate,
                    )
                )
                if entry is None:
                    # A replayed notification. The reward exists exactly once.
                    logger.info(
                        "referral_reward_already_credited",
                        purchase_id=str(purchase_id),
                        referrer_user_id=referral.referrer_user_id,
                    )
                    return None
                balance = await uow.bonuses.balance(referral.referrer_user_id)

        logger.info(
            "referral_reward_credited",
            purchase_id=str(purchase_id),
            referral_id=str(referral.id),
            referrer_user_id=referral.referrer_user_id,
            units=units,
            paid_amount=str(purchase.amount),
            currency=purchase.currency.value,
        )
        return RewardNotice(
            user_id=referral.referrer_user_id,
            units=units,
            balance=balance,
            paid_amount=purchase.amount,
            currency=purchase.currency,
        )

    async def _notify(self, notice: RewardNotice) -> None:
        """Tell the inviter, best effort.

        A notification is a courtesy, never a financial transaction: a blocked
        chat must not undo a credited reward.
        """
        if self.notifier is None:
            return
        try:
            await self.notifier.notify_reward(notice)
        except Exception as error:
            logger.info(
                "reward_notice_not_delivered",
                user_id=notice.user_id,
                error=str(error),
            )

    async def _reward_for(
        self,
        uow: UnitOfWork,
        purchase: Purchase,
    ) -> tuple[int, Decimal | None]:
        """Bonuses this sale earns its inviter, and the rate used to get there.

        A Stars sale needs no conversion and carries no rate. A USDT sale is
        expressed in Stars through the rate the *product itself* declares by
        carrying both prices — read here, inside the caller's transaction, and
        stored on the ledger entry so repricing the product later cannot
        recompute an old reward.

        Returns ``(0, ...)`` for "nothing to credit", which covers a USDT
        product that declares no rate at all.
        """
        rate = await self._stars_per_usdt(uow, purchase)
        if purchase.currency is not Currency.XTR and rate is None:
            logger.warning(
                "reward_without_declared_rate",
                purchase_id=str(purchase.id),
                product_id=str(purchase.product_id),
                currency=purchase.currency.value,
            )
            return 0, None

        units = reward_bonuses(
            purchase.amount,
            purchase.currency,
            policy=self.policy,
            stars_per_usdt=rate,
        )
        if units <= 0:
            logger.info(
                "referral_reward_below_minimum",
                purchase_id=str(purchase.id),
                amount=str(purchase.amount),
                currency=purchase.currency.value,
            )
        return units, rate

    @staticmethod
    async def _stars_per_usdt(uow: UnitOfWork, purchase: Purchase) -> Decimal | None:
        """The rate this sale's product declares, or ``None`` for a Stars sale."""
        if purchase.currency is Currency.XTR:
            return None
        product = await uow.products.get(purchase.product_id)
        return None if product is None else product.stars_per_usdt

    async def _discount_eligible(
        self,
        uow: UnitOfWork,
        user_id: int,
        *,
        referral: Referral | None,
    ) -> bool:
        """Whether the one-time first-purchase discount applies right now.

        Three questions, all answered from the same transaction: is this buyer
        attributed to somebody, is their one discount still unused, and are they
        genuinely a first-time buyer.
        """
        if referral is None or self.settings.discount_percent <= 0:
            return False
        if not referral.discount_available:
            return False
        return not await uow.purchases.has_history(
            user_id,
            statuses=PURCHASE_HISTORY_STATUSES,
        )


__all__ = ["BonusOffer", "BonusService"]
