"""The database's own guarantees for referrals and bonuses.

Every assertion here is about a constraint, not about application logic. These
are the rules that have to hold even when two requests interleave in the worst
possible order, which is exactly why they live in the schema.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.core.exceptions import (
    ConflictError,
    InsufficientBonusBalanceError,
    ReferralAlreadySetError,
    ReferralNotFoundError,
    SelfReferralError,
    UserNotFoundError,
)
from app.domain.commands import BonusTransactionDraft, ReferralDraft
from app.domain.enums import BonusTransactionType, PurchaseStatus
from tests.db import product_draft, purchase_draft, seed_paid_purchase, user_draft

if TYPE_CHECKING:
    from app.domain.entities import Product, Purchase, User
    from app.infrastructure.db.repositories.bonuses import SqlAlchemyBonusRepository
    from app.infrastructure.db.repositories.products import SqlAlchemyProductRepository
    from app.infrastructure.db.repositories.purchases import SqlAlchemyPurchaseRepository
    from app.infrastructure.db.repositories.referrals import SqlAlchemyReferralRepository
    from app.infrastructure.db.repositories.users import SqlAlchemyUserRepository

INVITER = 8101
INVITED = 8102
STRANGER = 8103


@pytest.fixture
async def inviter(users: SqlAlchemyUserRepository) -> User:
    return await users.upsert(user_draft(INVITER, username="inviter"))


@pytest.fixture
async def invited(users: SqlAlchemyUserRepository) -> User:
    return await users.upsert(user_draft(INVITED, username="invited"))


@pytest.fixture
async def stranger(users: SqlAlchemyUserRepository) -> User:
    return await users.upsert(user_draft(STRANGER, username="stranger"))


async def _paid_purchase(
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    user: User,
) -> Purchase:
    """A settled purchase, which is what a discount or a reward can attach to."""
    product: Product = await products.create(product_draft())
    return await seed_paid_purchase(purchases, user, product)


# --- One referrer per buyer, for life


async def test_a_buyer_can_only_be_attributed_once(
    referrals: SqlAlchemyReferralRepository,
    inviter: User,
    invited: User,
    stranger: User,
) -> None:
    """The constraint behind "no overwriting": a second insert cannot exist."""
    await referrals.create(
        ReferralDraft(referrer_user_id=inviter.telegram_id, referred_user_id=invited.telegram_id)
    )

    with pytest.raises(ReferralAlreadySetError):
        await referrals.create(
            ReferralDraft(
                referrer_user_id=stranger.telegram_id,
                referred_user_id=invited.telegram_id,
            )
        )


async def test_a_user_cannot_invite_themselves(
    referrals: SqlAlchemyReferralRepository,
    inviter: User,
) -> None:
    with pytest.raises(SelfReferralError):
        await referrals.create(
            ReferralDraft(
                referrer_user_id=inviter.telegram_id,
                referred_user_id=inviter.telegram_id,
            )
        )


async def test_one_inviter_may_have_many_invitees(
    referrals: SqlAlchemyReferralRepository,
    inviter: User,
    invited: User,
    stranger: User,
) -> None:
    await referrals.create(
        ReferralDraft(referrer_user_id=inviter.telegram_id, referred_user_id=invited.telegram_id)
    )
    await referrals.create(
        ReferralDraft(referrer_user_id=inviter.telegram_id, referred_user_id=stranger.telegram_id)
    )

    assert await referrals.count_invited(inviter.telegram_id) == 2


# --- The one-time discount


async def test_the_discount_is_burned_once(
    referrals: SqlAlchemyReferralRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    inviter: User,
    invited: User,
) -> None:
    referral = await referrals.create(
        ReferralDraft(referrer_user_id=inviter.telegram_id, referred_user_id=invited.telegram_id)
    )
    purchase = await _paid_purchase(products, purchases, invited)

    used = await referrals.mark_discount_used(referral.id, purchase_id=purchase.id)

    assert not used.discount_available
    assert used.discount_purchase_id == purchase.id


async def test_burning_the_same_discount_again_is_a_no_op(
    referrals: SqlAlchemyReferralRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    inviter: User,
    invited: User,
) -> None:
    """A replayed payment notification for the same purchase changes nothing."""
    referral = await referrals.create(
        ReferralDraft(referrer_user_id=inviter.telegram_id, referred_user_id=invited.telegram_id)
    )
    purchase = await _paid_purchase(products, purchases, invited)
    first = await referrals.mark_discount_used(referral.id, purchase_id=purchase.id)

    second = await referrals.mark_discount_used(referral.id, purchase_id=purchase.id)

    assert second.discount_used_at == first.discount_used_at


async def test_a_second_purchase_cannot_claim_the_same_discount(
    referrals: SqlAlchemyReferralRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    inviter: User,
    invited: User,
) -> None:
    referral = await referrals.create(
        ReferralDraft(referrer_user_id=inviter.telegram_id, referred_user_id=invited.telegram_id)
    )
    first = await _paid_purchase(products, purchases, invited)
    second = await _paid_purchase(products, purchases, invited)
    await referrals.mark_discount_used(referral.id, purchase_id=first.id)

    with pytest.raises(ConflictError):
        await referrals.mark_discount_used(referral.id, purchase_id=second.id)


async def test_burning_a_discount_that_does_not_exist_is_reported(
    referrals: SqlAlchemyReferralRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    invited: User,
) -> None:
    purchase = await _paid_purchase(products, purchases, invited)
    with pytest.raises(ReferralNotFoundError):
        await referrals.mark_discount_used(uuid4(), purchase_id=purchase.id)


# --- Referral codes


async def test_a_referral_code_is_assigned_once_and_kept(
    users: SqlAlchemyUserRepository,
    inviter: User,
) -> None:
    first = await users.ensure_referral_code(inviter.telegram_id, candidate="abcdefghjk")
    second = await users.ensure_referral_code(inviter.telegram_id, candidate="zzzzzzzzzz")

    assert first == "abcdefghjk"
    assert second == first


async def test_a_referral_code_identifies_exactly_one_user(
    users: SqlAlchemyUserRepository,
    inviter: User,
    invited: User,
) -> None:
    await users.ensure_referral_code(inviter.telegram_id, candidate="abcdefghjk")

    with pytest.raises(ConflictError):
        await users.ensure_referral_code(invited.telegram_id, candidate="abcdefghjk")


async def test_a_code_resolves_back_to_its_owner(
    users: SqlAlchemyUserRepository,
    inviter: User,
) -> None:
    code = await users.ensure_referral_code(inviter.telegram_id, candidate="abcdefghjk")

    owner = await users.get_by_referral_code(code)

    assert owner is not None
    assert owner.telegram_id == inviter.telegram_id


async def test_refreshing_a_profile_never_clobbers_the_code_or_the_balance(
    users: SqlAlchemyUserRepository,
    bonuses: SqlAlchemyBonusRepository,
    inviter: User,
) -> None:
    """An upsert on every interaction must not reset what the user has earned."""
    code = await users.ensure_referral_code(inviter.telegram_id, candidate="abcdefghjk")
    await bonuses.add(
        BonusTransactionDraft(
            user_id=inviter.telegram_id,
            amount=135,
            type=BonusTransactionType.REFERRAL_REWARD,
        )
    )

    refreshed = await users.upsert(user_draft(INVITER, username="renamed"))

    assert refreshed.referral_code == code
    assert refreshed.bonus_balance == 135


# --- The bonus ledger


async def test_an_entry_moves_the_cached_balance(
    bonuses: SqlAlchemyBonusRepository,
    inviter: User,
) -> None:
    await bonuses.add(
        BonusTransactionDraft(
            user_id=inviter.telegram_id,
            amount=135,
            type=BonusTransactionType.REFERRAL_REWARD,
        )
    )

    assert await bonuses.balance(inviter.telegram_id) == 135
    assert await bonuses.recompute_balance(inviter.telegram_id) == 135


async def test_one_entry_of_each_kind_per_purchase(
    bonuses: SqlAlchemyBonusRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    inviter: User,
    invited: User,
) -> None:
    """The idempotency guarantee, asserted against the database directly.

    The second insert returns ``None`` rather than raising: a replayed
    notification is an expected event, not an error.
    """
    purchase = await _paid_purchase(products, purchases, invited)
    draft = BonusTransactionDraft(
        user_id=inviter.telegram_id,
        amount=135,
        type=BonusTransactionType.REFERRAL_REWARD,
        purchase_id=purchase.id,
    )

    first = await bonuses.add(draft)
    second = await bonuses.add(draft)

    assert first is not None
    assert second is None
    assert await bonuses.balance(inviter.telegram_id) == 135


async def test_a_balance_cannot_go_negative(
    bonuses: SqlAlchemyBonusRepository,
    inviter: User,
) -> None:
    """The last line of defence against spending the same units twice."""
    await bonuses.add(
        BonusTransactionDraft(
            user_id=inviter.telegram_id,
            amount=100,
            type=BonusTransactionType.REFERRAL_REWARD,
        )
    )

    with pytest.raises(InsufficientBonusBalanceError):
        await bonuses.add(
            BonusTransactionDraft(
                user_id=inviter.telegram_id,
                amount=-101,
                type=BonusTransactionType.BONUS_RESERVED,
            )
        )
    assert await bonuses.balance(inviter.telegram_id) == 100


async def test_a_reservation_becomes_a_spend_without_moving_the_balance(
    bonuses: SqlAlchemyBonusRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    inviter: User,
) -> None:
    """Settling a hold changes why the units are held, never how many."""
    await bonuses.add(
        BonusTransactionDraft(
            user_id=inviter.telegram_id,
            amount=200,
            type=BonusTransactionType.REFERRAL_REWARD,
        )
    )
    purchase = await _paid_purchase(products, purchases, inviter)
    await bonuses.add(
        BonusTransactionDraft(
            user_id=inviter.telegram_id,
            amount=-50,
            type=BonusTransactionType.BONUS_RESERVED,
            purchase_id=purchase.id,
        )
    )

    settled = await bonuses.settle_hold(purchase.id)

    assert settled is not None
    assert settled.type is BonusTransactionType.BONUS_SPENT
    assert settled.amount == -50
    assert await bonuses.balance(inviter.telegram_id) == 150


async def test_a_settled_hold_is_never_released(
    bonuses: SqlAlchemyBonusRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    inviter: User,
) -> None:
    """Units spent on a paid purchase are gone; only reservations come back."""
    await bonuses.add(
        BonusTransactionDraft(
            user_id=inviter.telegram_id,
            amount=200,
            type=BonusTransactionType.REFERRAL_REWARD,
        )
    )
    purchase = await _paid_purchase(products, purchases, inviter)
    await bonuses.add(
        BonusTransactionDraft(
            user_id=inviter.telegram_id,
            amount=-50,
            type=BonusTransactionType.BONUS_RESERVED,
            purchase_id=purchase.id,
        )
    )
    await bonuses.settle_hold(purchase.id)

    assert await bonuses.release_hold(purchase.id) is None
    assert await bonuses.balance(inviter.telegram_id) == 150


async def test_a_reservation_is_released_at_most_once(
    bonuses: SqlAlchemyBonusRepository,
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    inviter: User,
) -> None:
    await bonuses.add(
        BonusTransactionDraft(
            user_id=inviter.telegram_id,
            amount=200,
            type=BonusTransactionType.REFERRAL_REWARD,
        )
    )
    purchase = await _paid_purchase(products, purchases, inviter)
    await bonuses.add(
        BonusTransactionDraft(
            user_id=inviter.telegram_id,
            amount=-50,
            type=BonusTransactionType.BONUS_RESERVED,
            purchase_id=purchase.id,
        )
    )

    assert await bonuses.release_hold(purchase.id) is not None
    assert await bonuses.release_hold(purchase.id) is None
    assert await bonuses.balance(inviter.telegram_id) == 200


async def test_an_entry_for_an_unknown_user_is_refused(
    bonuses: SqlAlchemyBonusRepository,
) -> None:
    with pytest.raises(UserNotFoundError):
        await bonuses.add(
            BonusTransactionDraft(
                user_id=999_999,
                amount=1,
                type=BonusTransactionType.REFERRAL_REWARD,
            )
        )


async def test_locking_an_unknown_balance_is_refused(
    bonuses: SqlAlchemyBonusRepository,
) -> None:
    with pytest.raises(UserNotFoundError):
        await bonuses.lock_balance(999_999)


# --- Purchase history, which decides eligibility


async def test_purchase_history_is_reported_per_status(
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    invited: User,
) -> None:
    product: Product = await products.create(product_draft())
    await purchases.create(purchase_draft(invited, product, status=PurchaseStatus.PENDING))

    assert not await purchases.has_history(
        invited.telegram_id,
        statuses=(PurchaseStatus.PAID, PurchaseStatus.DELIVERED, PurchaseStatus.REFUNDED),
    )
    assert await purchases.has_history(
        invited.telegram_id,
        statuses=(PurchaseStatus.PENDING,),
    )


async def test_a_purchase_records_its_price_breakdown(
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    invited: User,
) -> None:
    """A later catalogue price change must not rewrite an old sale."""
    product: Product = await products.create(product_draft())
    stored = await purchases.create(
        purchase_draft(
            invited,
            product,
            amount=Decimal(865),
            base_amount=Decimal(1000),
            discount_amount=Decimal(0),
            bonus_amount=Decimal(135),
        )
    )

    assert stored.list_amount == Decimal(1000)
    assert stored.charged_amount == Decimal(865)
    assert stored.bonus_amount == Decimal(135)
    assert stored.was_reduced


async def test_a_purchase_predating_the_feature_reads_as_full_price(
    products: SqlAlchemyProductRepository,
    purchases: SqlAlchemyPurchaseRepository,
    invited: User,
) -> None:
    """``base_amount`` is NULL on historical rows, and that means "no discount"."""
    product: Product = await products.create(product_draft())
    stored = await purchases.create(purchase_draft(invited, product))

    assert stored.base_amount is None
    assert stored.list_amount == stored.amount
    assert not stored.was_reduced
