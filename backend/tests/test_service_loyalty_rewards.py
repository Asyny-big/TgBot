"""The 50% reward's transitions at their edges: the anomalies settlement survives.

The happy paths run end to end in ``test_loyalty_discount``. These pin down the
branches a race or a bug could reach — a reward applied elsewhere, consumed
elsewhere, missing, or a repository refusing — and the rule for all of them:
a payment that already arrived is never broken by the reward. The repository's
own refusals are checked against PostgreSQL at the end.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.core.config import LoyaltySettings
from app.core.exceptions import ConflictError
from app.domain.commands import ProductDraft, PurchaseDraft, UserDraft
from app.domain.enums import (
    Currency,
    LoyaltyRewardKind,
    LoyaltyRewardStatus,
    PaymentProvider,
    PurchaseStatus,
)
from app.domain.loyalty import DISCOUNT_MILESTONE, loyalty_quote
from app.infrastructure.db.models import LoyaltyRewardModel
from app.infrastructure.db.repositories.loyalty import SqlAlchemyLoyaltyRepository
from app.infrastructure.db.repositories.products import SqlAlchemyProductRepository
from app.infrastructure.db.repositories.purchases import SqlAlchemyPurchaseRepository
from app.infrastructure.db.repositories.users import SqlAlchemyUserRepository
from app.services.loyalty import LoyaltyService
from tests.fakes import FakeUnitOfWork, FakeUnitOfWorkFactory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.entities import LoyaltyReward, Purchase

BUYER = 9601
ON = LoyaltySettings(enabled=True)


async def _setup(unit: FakeUnitOfWork, *, received: int = 5) -> tuple[LoyaltyService, Purchase]:
    """A buyer with ``received`` delivered packs and one pending discounted purchase."""
    await unit.users.upsert(UserDraft(telegram_id=BUYER))
    product = await unit.products.create(
        ProductDraft(
            slug=f"p{uuid4().hex[:8]}",
            title="Pack",
            description="",
            delivery_url="https://t.me/+p",
            price_stars=1000,
        )
    )
    for _ in range(received):
        paid = await unit.purchases.create(
            PurchaseDraft(
                user_id=BUYER,
                product_id=product.id,
                provider=PaymentProvider.STARS,
                amount=Decimal(100),
                currency=Currency.XTR,
                external_id=uuid4().hex,
                status=PurchaseStatus.PAID,
            )
        )
        await unit.purchases.mark_delivered(paid.id, delivered_url="https://t.me/+p")
    pending = await unit.purchases.create(
        PurchaseDraft(
            user_id=BUYER,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            amount=Decimal(500),
            base_amount=Decimal(1000),
            loyalty_amount=Decimal(500),
            currency=Currency.XTR,
            external_id=uuid4().hex,
        )
    )
    return LoyaltyService(uow_factory=FakeUnitOfWorkFactory(unit), settings=ON), pending


def _reward(unit: FakeUnitOfWork) -> LoyaltyReward:
    (reward,) = [r for r in unit.loyalty.rewards if r.milestone == DISCOUNT_MILESTONE]
    return reward


# --------------------------------------------------------------------------- #
# What counts as available                                                    #
# --------------------------------------------------------------------------- #


async def test_below_the_milestone_nothing_is_available_or_recorded() -> None:
    unit = FakeUnitOfWork()
    service, _ = await _setup(unit, received=4)

    assert not await service.discount_available(unit, BUYER)
    assert await service.lock_discount(unit, BUYER) is None
    assert unit.loyalty.rewards == []


@pytest.mark.parametrize(
    ("status", "available"),
    [
        (LoyaltyRewardStatus.AVAILABLE, True),
        (LoyaltyRewardStatus.APPLIED, False),
        (LoyaltyRewardStatus.CONSUMED, False),
    ],
)
async def test_a_recorded_reward_decides_availability(
    status: LoyaltyRewardStatus,
    available: bool,
) -> None:
    unit = FakeUnitOfWork()
    service, pending = await _setup(unit)
    await unit.loyalty.ensure_reward(BUYER, DISCOUNT_MILESTONE)
    reward = _reward(unit)
    if status is LoyaltyRewardStatus.APPLIED:
        await unit.loyalty.mark_applied(reward.id, pending.id)
    if status is LoyaltyRewardStatus.CONSUMED:
        await unit.loyalty.mark_consumed(reward.id, pending.id)

    assert await service.discount_available(unit, BUYER) is available
    assert (await service.lock_discount(unit, BUYER) is not None) is available


# --------------------------------------------------------------------------- #
# Apply refuses what it must                                                  #
# --------------------------------------------------------------------------- #


async def test_applying_without_a_locked_reward_is_refused() -> None:
    unit = FakeUnitOfWork()
    service, pending = await _setup(unit)
    priced = loyalty_quote(Decimal(1000), Currency.XTR)
    assert priced is not None

    with pytest.raises(ConflictError):
        await service.apply_discount(unit, pending, priced)


async def test_applying_a_reward_that_is_no_longer_available_is_refused() -> None:
    unit = FakeUnitOfWork()
    service, pending = await _setup(unit)
    reward_id = await service.lock_discount(unit, BUYER)
    assert reward_id is not None
    await unit.loyalty.mark_consumed(reward_id, uuid4())
    priced = loyalty_quote(Decimal(1000), Currency.XTR, reward_id=reward_id)
    assert priced is not None

    with pytest.raises(ConflictError):
        await service.apply_discount(unit, pending, priced)


# --------------------------------------------------------------------------- #
# Consume never breaks a payment                                              #
# --------------------------------------------------------------------------- #


async def test_a_reward_applied_to_another_invoice_is_consumed_by_the_paid_one() -> None:
    unit = FakeUnitOfWork()
    service, pending = await _setup(unit)
    reward_id = await service.lock_discount(unit, BUYER)
    assert reward_id is not None
    other = uuid4()
    await unit.loyalty.mark_applied(reward_id, other)

    await service.consume_discount(unit, pending)

    reward = _reward(unit)
    assert reward.status is LoyaltyRewardStatus.CONSUMED
    assert reward.consumed_purchase_id == pending.id


async def test_a_reward_consumed_by_another_purchase_is_left_alone() -> None:
    unit = FakeUnitOfWork()
    service, pending = await _setup(unit)
    reward_id = await service.lock_discount(unit, BUYER)
    assert reward_id is not None
    first = uuid4()
    await unit.loyalty.mark_consumed(reward_id, first)

    await service.consume_discount(unit, pending)  # does not raise

    assert _reward(unit).consumed_purchase_id == first


async def test_consuming_when_no_reward_was_ever_recorded_does_not_raise() -> None:
    unit = FakeUnitOfWork()
    service, pending = await _setup(unit)

    await service.consume_discount(unit, pending)

    assert unit.loyalty.rewards == []


async def test_a_refusing_repository_does_not_break_the_payment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = FakeUnitOfWork()
    service, pending = await _setup(unit)
    reward_id = await service.lock_discount(unit, BUYER)
    assert reward_id is not None
    await unit.loyalty.mark_applied(reward_id, pending.id)

    async def refuse(*args: object, **kwargs: object) -> object:
        del args, kwargs
        message = "simulated refusal"
        raise ConflictError(message)

    monkeypatch.setattr(unit.loyalty, "mark_consumed", refuse)

    await service.consume_discount(unit, pending)  # logged, not raised

    assert _reward(unit).status is LoyaltyRewardStatus.APPLIED


# --------------------------------------------------------------------------- #
# Release touches only its own hold                                           #
# --------------------------------------------------------------------------- #


async def test_release_leaves_a_reward_applied_elsewhere_alone() -> None:
    unit = FakeUnitOfWork()
    service, pending = await _setup(unit)
    reward_id = await service.lock_discount(unit, BUYER)
    assert reward_id is not None
    other = uuid4()
    await unit.loyalty.mark_applied(reward_id, other)

    await service.release_discount(unit, pending)

    assert _reward(unit).applied_purchase_id == other


async def test_release_without_a_recorded_reward_does_nothing() -> None:
    unit = FakeUnitOfWork()
    service, pending = await _setup(unit)

    await service.release_discount(unit, pending)

    assert unit.loyalty.rewards == []


# --------------------------------------------------------------------------- #
# The SQL repository's own refusals (PostgreSQL)                              #
# --------------------------------------------------------------------------- #


@pytest.fixture
def loyalty_repository(db_session: AsyncSession) -> SqlAlchemyLoyaltyRepository:
    return SqlAlchemyLoyaltyRepository(db_session)


async def _two_purchases(session: AsyncSession) -> tuple[Purchase, Purchase]:
    """Two pending discounted purchases of this buyer, for different products."""
    users = SqlAlchemyUserRepository(session)
    products = SqlAlchemyProductRepository(session)
    purchases = SqlAlchemyPurchaseRepository(session)
    await users.upsert(UserDraft(telegram_id=BUYER))
    made = []
    for _ in range(2):
        product = await products.create(
            ProductDraft(
                slug=f"r{uuid4().hex[:8]}",
                title="Pack",
                description="",
                delivery_url="https://t.me/+p",
                price_stars=1000,
            )
        )
        made.append(
            await purchases.create(
                PurchaseDraft(
                    user_id=BUYER,
                    product_id=product.id,
                    provider=PaymentProvider.STARS,
                    amount=Decimal(500),
                    base_amount=Decimal(1000),
                    loyalty_amount=Decimal(500),
                    currency=Currency.XTR,
                    external_id=uuid4().hex,
                )
            )
        )
    return made[0], made[1]


async def test_the_sql_repository_refuses_every_wrong_transition(
    loyalty_repository: SqlAlchemyLoyaltyRepository,
    db_session: AsyncSession,
) -> None:
    first, second = await _two_purchases(db_session)
    await loyalty_repository.ensure_reward(BUYER, DISCOUNT_MILESTONE)
    await loyalty_repository.ensure_reward(BUYER, DISCOUNT_MILESTONE)  # idempotent
    reward = await loyalty_repository.lock_reward(BUYER, DISCOUNT_MILESTONE)
    assert reward is not None
    assert reward.kind is LoyaltyRewardKind.DISCOUNT_50_PERCENT
    assert reward.status is LoyaltyRewardStatus.AVAILABLE

    # Nothing to release yet, and an unknown reward is refused.
    assert await loyalty_repository.mark_released(reward.id, first.id) is None
    with pytest.raises(ConflictError):
        await loyalty_repository.mark_applied(uuid4(), first.id)

    applied = await loyalty_repository.mark_applied(reward.id, first.id)
    assert applied.applied_purchase_id == first.id
    # Applied once: not available to a second purchase.
    with pytest.raises(ConflictError):
        await loyalty_repository.mark_applied(reward.id, second.id)
    # Releasing on behalf of the wrong purchase changes nothing.
    assert await loyalty_repository.mark_released(reward.id, second.id) is None

    consumed = await loyalty_repository.mark_consumed(reward.id, first.id)
    assert consumed.status is LoyaltyRewardStatus.CONSUMED
    # A replay for the same purchase is a no-op; another purchase is refused.
    assert (await loyalty_repository.mark_consumed(reward.id, first.id)).id == reward.id
    with pytest.raises(ConflictError):
        await loyalty_repository.mark_consumed(reward.id, second.id)
    assert await loyalty_repository.mark_released(reward.id, first.id) is None


@pytest.mark.parametrize(
    ("status", "constraint"),
    [
        (LoyaltyRewardStatus.APPLIED, "uq_loyalty_rewards_applied_purchase_id"),
        (LoyaltyRewardStatus.CONSUMED, "uq_loyalty_rewards_consumed_purchase_id"),
    ],
)
async def test_a_refused_transition_leaves_the_transaction_usable(
    loyalty_repository: SqlAlchemyLoyaltyRepository,
    db_session: AsyncSession,
    status: LoyaltyRewardStatus,
    constraint: str,
) -> None:
    """A constraint refusal costs only that change — never the caller's transaction.

    ``first`` already holds (or used) another buyer's reward, so this buyer's
    reward cannot be applied to (consumed by) it. The repository reports a
    conflict, and the session carries on: that is what lets a payment settle
    even when its reward cannot be.
    """
    first, second = await _two_purchases(db_session)
    other_buyer = BUYER + 1
    await SqlAlchemyUserRepository(db_session).upsert(UserDraft(telegram_id=other_buyer))
    now = datetime.now(UTC)
    held = {
        LoyaltyRewardStatus.APPLIED: {"applied_purchase_id": first.id, "applied_at": now},
        LoyaltyRewardStatus.CONSUMED: {"consumed_purchase_id": first.id, "consumed_at": now},
    }[status]
    db_session.add(
        LoyaltyRewardModel(
            id=uuid4(),
            user_id=other_buyer,
            milestone=DISCOUNT_MILESTONE,
            kind=LoyaltyRewardKind.DISCOUNT_50_PERCENT,
            status=status,
            **held,
        )
    )
    await db_session.flush()
    await loyalty_repository.ensure_reward(BUYER, DISCOUNT_MILESTONE)
    reward = await loyalty_repository.lock_reward(BUYER, DISCOUNT_MILESTONE)
    assert reward is not None

    transition = {
        LoyaltyRewardStatus.APPLIED: loyalty_repository.mark_applied,
        LoyaltyRewardStatus.CONSUMED: loyalty_repository.mark_consumed,
    }[status]

    with pytest.raises(ConflictError) as caught:
        await transition(reward.id, first.id)
    assert caught.value.details["constraint"] == constraint

    # The savepoint took back only the refused change: the next query does not
    # retry it, and the transaction goes on to write something else.
    still = await loyalty_repository.lock_reward(BUYER, DISCOUNT_MILESTONE)
    assert still is not None
    assert still.status is LoyaltyRewardStatus.AVAILABLE
    moved = await transition(reward.id, second.id)
    assert moved.status is status
    await db_session.flush()
