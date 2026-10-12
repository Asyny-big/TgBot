"""Referral use cases.

Owns the relationship between an inviter and an invited buyer, and nothing
else: what a purchase costs lives in ``BonusService``, and what a message says
lives in the bot layer.

Two rules shape everything here.

*Anyone may invite.* A user does not have to buy anything to get a link. The
bonus screen mints a code on first open, so the flow "A never bought anything →
A invites B → B buys → A earns" is fully supported.

*An attribution is permanent.* If A invited B, no later link can move B to C.
That is a unique constraint in the database, so two links opened in the same
second cannot both win.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.exceptions import (
    ConflictError,
    ReferralAlreadySetError,
    SelfReferralError,
)
from app.core.logging import get_logger
from app.domain.commands import ReferralDraft
from app.domain.entities import BonusSummary
from app.domain.enums import PURCHASE_HISTORY_STATUSES
from app.domain.referrals import (
    ReferralOutcome,
    ReferralRegistration,
    generate_referral_code,
    parse_referral_payload,
    referral_payload,
)

if TYPE_CHECKING:
    from app.core.config import ReferralSettings, TelegramSettings
    from app.domain.commands import UserDraft
    from app.domain.entities import Referral, ReferralRecord
    from app.domain.pagination import Page, PageRequest, ReferralFilters
    from app.domain.uow import UnitOfWork, UnitOfWorkFactory

logger = get_logger(__name__)

CODE_ASSIGNMENT_ATTEMPTS = 5
"""Retries on a code collision. With ~49 bits of entropy, one is already plenty."""


@dataclass(frozen=True, slots=True)
class ReferralService:
    """Records who invited whom, and answers the bonus screen's questions."""

    uow_factory: UnitOfWorkFactory
    telegram: TelegramSettings
    settings: ReferralSettings

    @property
    def enabled(self) -> bool:
        """Whether the referral programme is switched on at all."""
        return self.settings.enabled

    @property
    def preview_directory_url(self) -> str | None:
        """The single hop channel that lists the current preview channels."""
        return self.settings.preview_directory_url

    async def register(
        self,
        *,
        payload: str,
        profile: UserDraft,
    ) -> ReferralRegistration:
        """Attribute the visitor to the owner of this invitation payload.

        The visitor's Telegram profile is stored first, exactly as opening a
        product card does, so an invited user exists before anything references
        them. Every outcome other than ``LINKED`` leaves the database untouched.
        """
        if not self.enabled:
            return ReferralRegistration(outcome=ReferralOutcome.DISABLED)
        code = parse_referral_payload(payload)
        if code is None:
            return ReferralRegistration(outcome=ReferralOutcome.UNKNOWN_CODE)

        async with self.uow_factory() as uow:
            await uow.users.upsert(profile)
            referrer = await uow.users.get_by_referral_code(code)
            if referrer is None:
                logger.info("referral_code_unknown", telegram_id=profile.telegram_id)
                return ReferralRegistration(outcome=ReferralOutcome.UNKNOWN_CODE)

            refusal = await self._refusal(uow, referrer_id=referrer.telegram_id, profile=profile)
            if refusal is not None:
                return refusal
            return await self._link(uow, referrer_id=referrer.telegram_id, profile=profile)

    async def _refusal(
        self,
        uow: UnitOfWork,
        *,
        referrer_id: int,
        profile: UserDraft,
    ) -> ReferralRegistration | None:
        """Why this invitation cannot be accepted, or ``None`` if it can.

        All three reasons are ordinary user situations, and none of them writes
        anything: opening your own link, opening a second person's link, or
        arriving as somebody who has bought here before.
        """
        if referrer_id == profile.telegram_id:
            logger.info("referral_self_attempt", telegram_id=profile.telegram_id)
            return ReferralRegistration(
                outcome=ReferralOutcome.SELF_REFERRAL,
                referrer_user_id=referrer_id,
            )

        existing = await uow.referrals.get_by_referred(profile.telegram_id)
        if existing is not None:
            # Never overwritten: the first inviter keeps the buyer for good.
            logger.info(
                "referral_already_linked",
                telegram_id=profile.telegram_id,
                referrer_user_id=existing.referrer_user_id,
            )
            return ReferralRegistration(
                outcome=ReferralOutcome.ALREADY_LINKED,
                referrer_user_id=existing.referrer_user_id,
            )

        if await self._has_purchase_history(uow, profile.telegram_id):
            # Decided by purchase history, never by how old the Telegram
            # account looks.
            logger.info("referral_not_eligible", telegram_id=profile.telegram_id)
            return ReferralRegistration(
                outcome=ReferralOutcome.NOT_ELIGIBLE,
                referrer_user_id=referrer_id,
            )
        return None

    async def _link(
        self,
        uow: UnitOfWork,
        *,
        referrer_id: int,
        profile: UserDraft,
    ) -> ReferralRegistration:
        """Write the relationship, letting the database settle any race."""
        try:
            referral = await uow.referrals.create(
                ReferralDraft(
                    referrer_user_id=referrer_id,
                    referred_user_id=profile.telegram_id,
                )
            )
        except ReferralAlreadySetError:
            # Two links opened in the same instant; the database picked one.
            linked = await uow.referrals.get_by_referred(profile.telegram_id)
            return ReferralRegistration(
                outcome=ReferralOutcome.ALREADY_LINKED,
                referrer_user_id=linked.referrer_user_id if linked else None,
            )
        except SelfReferralError:  # pragma: no cover — refused before we get here
            return ReferralRegistration(outcome=ReferralOutcome.SELF_REFERRAL)

        logger.info(
            "referral_linked",
            referral_id=str(referral.id),
            referrer_user_id=referral.referrer_user_id,
            referred_user_id=referral.referred_user_id,
        )
        return ReferralRegistration(
            outcome=ReferralOutcome.LINKED,
            referrer_user_id=referral.referrer_user_id,
        )

    async def summary(self, profile: UserDraft) -> BonusSummary:
        """Everything the "My bonuses" screen shows, minting a code if needed.

        Raises:
            ConflictError: a fresh code could not be assigned. The caller shows
                a soft failure; nothing about the shop is affected.
        """
        async with self.uow_factory() as uow:
            await uow.users.upsert(profile)
            code = await self._ensure_code(uow, profile.telegram_id)
            balance = await uow.bonuses.balance(profile.telegram_id)
            invited = await uow.referrals.count_invited(profile.telegram_id)
            purchases = await uow.referrals.count_referral_purchases(profile.telegram_id)

        return BonusSummary(
            user_id=profile.telegram_id,
            balance=balance,
            referral_code=code,
            invited_count=invited,
            referral_purchase_count=purchases,
        )

    def link_for(self, code: str) -> str:
        """Public invitation link for a referral code."""
        return self.telegram.deep_link(referral_payload(code))

    async def referral_of(self, user_id: int) -> Referral | None:
        """The relationship that owns this buyer, if any."""
        async with self.uow_factory() as uow:
            return await uow.referrals.get_by_referred(user_id)

    async def search(self, filters: ReferralFilters, page: PageRequest) -> Page[ReferralRecord]:
        """Referral relationships for the admin panel. Read only."""
        async with self.uow_factory() as uow:
            return await uow.referrals.search(filters, page)

    async def _ensure_code(self, uow: UnitOfWork, telegram_id: int) -> str:
        """Return this user's permanent code, retrying past a rare collision."""
        for _ in range(CODE_ASSIGNMENT_ATTEMPTS):
            try:
                return await uow.users.ensure_referral_code(
                    telegram_id,
                    candidate=generate_referral_code(),
                )
            except ConflictError:
                continue
        message = "Could not assign a referral code"
        raise ConflictError(message, telegram_id=telegram_id)

    @staticmethod
    async def _has_purchase_history(uow: UnitOfWork, telegram_id: int) -> bool:
        """Whether this buyer has ever bought anything.

        A refunded purchase counts as history on purpose: if it did not, a buyer
        could refund their way back to "new" and harvest the discount again.
        """
        return await uow.purchases.has_history(
            telegram_id,
            statuses=PURCHASE_HISTORY_STATUSES,
        )


__all__ = ["ReferralService"]
