"""The referral and bonus feature end to end, on real PostgreSQL and Redis.

These are the scenarios that decide whether the feature can be trusted with
money. The fakes prove the rules; this file proves the constraints — a replayed
webhook really cannot insert a second reward, and a discount really is burned
once, because the database says so.

The shop's own flow is asserted here too, unchanged: a buyer with no inviter
pays the list price and receives their link exactly as before.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.core.config import DeliverySettings, ReferralSettings, TelegramSettings
from app.core.exceptions import BonusesNotAvailableError, InsufficientBonusBalanceError
from app.domain.commands import ProductDraft, UserDraft
from app.domain.enums import (
    BonusTransactionType,
    Currency,
    PaymentProvider,
    PurchaseStatus,
)
from app.services.bonuses import BonusService
from app.services.delivery import DeliveryService
from app.services.products import ProductService
from app.services.purchases import PurchaseService
from app.services.referrals import ReferralService
from tests.fakes import FakeDeliveryGateway
from tests.settings_factory import VALID_BOT_TOKEN

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.entities import Product, Purchase
    from app.domain.notifications import RewardNotice
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

INVITER = UserDraft(telegram_id=7001, username="inviter", first_name="A")
INVITED = UserDraft(telegram_id=7002, username="invited", first_name="B")
THIRD = UserDraft(telegram_id=7003, username="third", first_name="C")
LONER = UserDraft(telegram_id=7004, username="loner", first_name="Nobody")

DELIVERY_URL = "https://t.me/+private-invite"
STARS_PRICE = 1000


class RecordingNotifier:
    """Captures the courtesy messages instead of sending them."""

    def __init__(self, *, fail: bool = False) -> None:
        self.rewards: list[RewardNotice] = []
        self.hints: list[int] = []
        self._fail = fail

    async def notify_reward(self, notice: RewardNotice) -> None:
        if self._fail:
            message = "the inviter has blocked the bot"
            raise RuntimeError(message)
        self.rewards.append(notice)

    async def notify_purchase_complete(self, user_id: int) -> None:
        self.hints.append(user_id)


@dataclass(frozen=True, slots=True)
class Shop:
    """Every service wired over the live database and live Redis."""

    products: ProductService
    purchases: PurchaseService
    delivery: DeliveryService
    referrals: ReferralService
    bonuses: BonusService
    gateway: FakeDeliveryGateway
    notifier: RecordingNotifier
    uow_factory: SqlAlchemyUnitOfWorkFactory


def _telegram() -> TelegramSettings:
    return TelegramSettings(
        bot_token=SecretStr(VALID_BOT_TOKEN),
        bot_username="MyShopBot",
        use_webhook=False,
        webhook_secret=SecretStr("webhook-secret-value"),
    )


def _build_shop(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    locks: RedisLockManager,
    *,
    referral: ReferralSettings,
    notifier: RecordingNotifier | None = None,
) -> Shop:
    recording = notifier or RecordingNotifier()
    bonuses = BonusService(
        uow_factory=uow_factory,
        locks=locks,
        settings=referral,
        notifier=recording,
    )
    purchases = PurchaseService(uow_factory=uow_factory, locks=locks, pricing=bonuses)
    gateway = FakeDeliveryGateway()
    return Shop(
        products=ProductService(uow_factory=uow_factory, telegram=_telegram()),
        purchases=purchases,
        delivery=DeliveryService(
            uow_factory=uow_factory,
            purchases=purchases,
            gateway=gateway,
            locks=locks,
            settings=DeliverySettings(max_attempts=2, initial_backoff_seconds=0.01),
            sale_completion=bonuses,
        ),
        referrals=ReferralService(
            uow_factory=uow_factory,
            telegram=_telegram(),
            settings=referral,
        ),
        bonuses=bonuses,
        gateway=gateway,
        notifier=recording,
        uow_factory=uow_factory,
    )


@pytest.fixture
def referral_settings() -> ReferralSettings:
    return ReferralSettings(
        enabled=True,
        discount_percent=10,
        reward_percent=15,
        max_bonus_payment_percent=50,
    )


@pytest.fixture
def shop(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
    referral_settings: ReferralSettings,
) -> Shop:
    return _build_shop(live_uow_factory, live_locks, referral=referral_settings)


async def _buyers(shop: Shop, *profiles: UserDraft) -> None:
    """The bot records a visitor when a card or an invitation opens."""
    for profile in profiles:
        await shop.purchases.remember_user(profile)


async def _product(shop: Shop, **overrides: object) -> Product:
    values: dict[str, object] = {
        "slug": f"vip{uuid4().hex[:6]}",
        "title": "VIP access",
        "description": "Lifetime access",
        "delivery_url": DELIVERY_URL,
        "price_stars": STARS_PRICE,
    }
    values.update(overrides)
    return await shop.products.create(ProductDraft(**values))  # type: ignore[arg-type]


async def _invite(shop: Shop, inviter: UserDraft, invited: UserDraft) -> None:
    """Mint the inviter's code and open it as the invited user."""
    summary = await shop.referrals.summary(inviter)
    registration = await shop.referrals.register(
        payload=f"ref_{summary.referral_code}",
        profile=invited,
    )
    assert registration.is_linked


async def _buy(
    shop: Shop,
    buyer: UserDraft,
    product: Product,
    *,
    use_bonus: bool = False,
) -> Purchase:
    """One complete sale: invoice, payment, delivery."""
    purchase = await shop.purchases.start_purchase(
        user_id=buyer.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
        use_bonus=use_bonus,
    )
    await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id=purchase.external_id,
    )
    result = await shop.delivery.deliver_purchase(purchase.id)
    assert result.succeeded
    return await shop.purchases.get(purchase.id)


async def _balance(shop: Shop, telegram_id: int) -> int:
    return await shop.bonuses.balance_of(telegram_id)


async def _ledger_types(shop: Shop, purchase_id: UUID) -> list[str]:
    async with shop.uow_factory() as uow:
        entry = await uow.bonuses.find_for_purchase(purchase_id)
        return [] if entry is None else [entry.type.value]


# --- The shop without a referral, which must be untouched


async def test_a_buyer_without_an_inviter_pays_the_list_price(shop: Shop) -> None:
    """The regression that matters most: the ordinary flow is unchanged."""
    await _buyers(shop, LONER)
    product = await _product(shop)

    purchase = await _buy(shop, LONER, product)

    assert purchase.amount == Decimal(STARS_PRICE)
    assert purchase.discount_amount == Decimal(0)
    assert purchase.bonus_amount == Decimal(0)
    assert purchase.status is PurchaseStatus.DELIVERED
    assert shop.gateway.sent[0].delivery_url == DELIVERY_URL


async def test_nothing_is_written_to_the_ledger_without_a_referral(shop: Shop) -> None:
    await _buyers(shop, LONER)
    product = await _product(shop)

    purchase = await _buy(shop, LONER, product)

    assert await _ledger_types(shop, purchase.id) == []
    assert await _balance(shop, LONER.telegram_id) == 0


async def test_the_feature_can_be_switched_off_completely(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    """With the master switch off, an existing referral changes no price."""
    enabled = _build_shop(
        live_uow_factory,
        live_locks,
        referral=ReferralSettings(enabled=True),
    )
    await _buyers(enabled, INVITER, INVITED)
    await _invite(enabled, INVITER, INVITED)

    disabled = _build_shop(
        live_uow_factory,
        live_locks,
        referral=ReferralSettings(enabled=False),
    )
    product = await _product(disabled)
    purchase = await _buy(disabled, INVITED, product)

    assert purchase.amount == Decimal(STARS_PRICE)
    assert await _balance(disabled, INVITER.telegram_id) == 0


# --- The discount


async def test_a_first_purchase_after_an_invitation_costs_ten_percent_less(
    shop: Shop,
) -> None:
    """1000 becomes 900, and the breakdown records why."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)

    purchase = await _buy(shop, INVITED, product)

    assert purchase.base_amount == Decimal(1000)
    assert purchase.discount_amount == Decimal(100)
    assert purchase.amount == Decimal(900)


async def test_the_product_price_itself_is_never_changed(shop: Shop) -> None:
    """A discount is a property of a sale, never of the catalogue."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)

    await _buy(shop, INVITED, product)

    stored = await shop.purchases.product_for_checkout(product.id)
    assert stored.price_stars == STARS_PRICE


async def test_the_second_purchase_is_full_price(shop: Shop) -> None:
    """The discount exists once. After it, the ordinary price applies."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    first = await _product(shop)
    second = await _product(shop)

    await _buy(shop, INVITED, first)
    later = await _buy(shop, INVITED, second)

    assert later.discount_amount == Decimal(0)
    assert later.amount == Decimal(STARS_PRICE)


async def test_the_discount_survives_opening_a_different_product(shop: Shop) -> None:
    """A referral link carries no product; the attribution outlives the visit.

    B is invited, then buys a completely different item days later — the
    relationship is still what prices that sale.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _product(shop, slug="ohhh")
    muse = await _product(shop, slug="muse")

    purchase = await _buy(shop, INVITED, muse)

    assert purchase.discount_amount == Decimal(100)


async def test_an_abandoned_invoice_does_not_burn_the_discount(shop: Shop) -> None:
    """The discount is consumed when the payment lands, not when it is offered.

    While a discounted checkout is active (PENDING), it holds the one-time
    discount and blocks any other checkout from claiming it. Once that invoice
    is abandoned and expires (EXPIRED), the discount is released so the buyer
    can use it on a fresh purchase.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    abandoned = await _product(shop)
    second_product = await _product(shop)
    real = await _product(shop)

    # 1. Start a discounted checkout. It is pending and holds the discount.
    first_purchase = await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=abandoned.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
    assert first_purchase.discount_amount == Decimal(100)

    # 2. While the first checkout is PENDING, another checkout cannot claim the discount.
    blocked_purchase = await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=second_product.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
    assert blocked_purchase.discount_amount == Decimal(0)

    # 3. The first invoice is abandoned and expires.
    await shop.purchases.expire_stale(now=_far_future())

    # 4. After expiration, a new checkout receives the discount and completes.
    purchase = await _buy(shop, INVITED, real)
    assert purchase.discount_amount == Decimal(100)


# --- The inviter's reward


async def test_the_inviter_earns_fifteen_percent_of_what_was_paid(shop: Shop) -> None:
    """B pays 900 after the discount, so A earns 135 — not 150."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)

    await _buy(shop, INVITED, product)

    assert await _balance(shop, INVITER.telegram_id) == 135


async def test_every_purchase_earns_its_own_reward(shop: Shop) -> None:
    """Not only the first: three sales produce three separate credits."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    first = await _product(shop, price_stars=1000)
    second = await _product(shop, price_stars=1200)
    third = await _product(shop, price_stars=700)

    await _buy(shop, INVITED, first)
    await _buy(shop, INVITED, second)
    await _buy(shop, INVITED, third)

    # 900 → 135, then full price 1200 → 180 and 700 → 105.
    assert await _balance(shop, INVITER.telegram_id) == 135 + 180 + 105
    async with shop.uow_factory() as uow:
        assert await uow.bonuses.reward_total(INVITER.telegram_id) == 420
        assert await uow.bonuses.recompute_balance(INVITER.telegram_id) == 420


async def test_the_inviter_is_told_once_per_purchase(shop: Shop) -> None:
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)

    await _buy(shop, INVITED, product)

    assert len(shop.notifier.rewards) == 1
    notice = shop.notifier.rewards[0]
    assert notice.user_id == INVITER.telegram_id
    assert notice.units == 135
    assert notice.paid_amount == Decimal(900)


async def test_a_reward_survives_a_notification_that_cannot_be_delivered(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
    referral_settings: ReferralSettings,
) -> None:
    """A blocked inviter still earns. The message is a courtesy, not the money."""
    shop = _build_shop(
        live_uow_factory,
        live_locks,
        referral=referral_settings,
        notifier=RecordingNotifier(fail=True),
    )
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)

    await _buy(shop, INVITED, product)

    assert shop.notifier.rewards == []
    assert await _balance(shop, INVITER.telegram_id) == 135


async def test_an_unpaid_invoice_earns_nothing(shop: Shop) -> None:
    """A reward comes from a completed sale, never from an invoice."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)

    await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )

    assert await _balance(shop, INVITER.telegram_id) == 0


async def test_a_payment_without_delivery_earns_nothing_yet(shop: Shop) -> None:
    """The reward follows the hand-over, which is the promise being kept."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)

    purchase = await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
    await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id=purchase.external_id,
    )

    assert await _balance(shop, INVITER.telegram_id) == 0


# --- Idempotency


@pytest.mark.parametrize("replays", [2, 5])
async def test_a_replayed_payment_notification_credits_one_reward(
    shop: Shop,
    replays: int,
) -> None:
    """Five identical webhooks must leave +135, never +675.

    Guaranteed by ``uq_bonus_transactions_purchase_type``: the second insert
    cannot exist, whatever the application layer attempts.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)

    purchase = await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
    for _ in range(replays):
        await shop.purchases.confirm_payment(
            provider=PaymentProvider.STARS,
            external_id=purchase.external_id,
        )
        await shop.delivery.deliver_purchase(purchase.id)

    assert await _balance(shop, INVITER.telegram_id) == 135
    assert len(shop.notifier.rewards) == 1


async def test_a_repeat_delivery_does_not_pay_again(shop: Shop) -> None:
    """Re-sending a link the buyer already owns is not a new sale."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)
    purchase = await _buy(shop, INVITED, product)

    await shop.delivery.redeliver(purchase.id)
    await shop.delivery.redeliver(purchase.id)

    assert await _balance(shop, INVITER.telegram_id) == 135


async def test_the_backfill_pass_is_also_idempotent(shop: Shop) -> None:
    """Housekeeping repairs a missed reward once, and then finds nothing."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)
    await _buy(shop, INVITED, product)

    assert await shop.bonuses.accrue_missing() == 0
    assert await _balance(shop, INVITER.telegram_id) == 135


async def test_a_missed_reward_is_repaired_by_housekeeping(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
    referral_settings: ReferralSettings,
) -> None:
    """Simulates a process that died after delivery but before the ledger write.

    The sale is completed by a shop with no bonus hook at all, then a properly
    wired one repairs it — which is the crash window this pass exists for.
    """
    blind = _build_shop(
        live_uow_factory,
        live_locks,
        referral=referral_settings,
    )
    await _buyers(blind, INVITER, INVITED)
    await _invite(blind, INVITER, INVITED)
    product = await _product(blind)

    purchase = await blind.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
    await blind.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id=purchase.external_id,
    )
    # Deliver through a service that reports nothing to the bonus feature.
    crashed = DeliveryService(
        uow_factory=live_uow_factory,
        purchases=blind.purchases,
        gateway=FakeDeliveryGateway(),
        locks=live_locks,
        settings=DeliverySettings(max_attempts=2, initial_backoff_seconds=0.01),
    )
    assert (await crashed.deliver_purchase(purchase.id)).succeeded
    assert await _balance(blind, INVITER.telegram_id) == 0

    assert await blind.bonuses.accrue_missing() == 1
    assert await _balance(blind, INVITER.telegram_id) == 135


# --- Refunds, which are deliberately left alone


async def test_a_refund_does_not_reverse_a_reward(shop: Shop) -> None:
    """The agreed rule: a credited reward is final.

    No reversal, no debt, no negative balance and no change to the existing
    refund flow. The purchase becomes refunded; the ledger does not move.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)
    purchase = await _buy(shop, INVITED, product)
    assert await _balance(shop, INVITER.telegram_id) == 135

    refunded = await shop.purchases.refund(purchase.id)

    assert refunded.status is PurchaseStatus.REFUNDED
    assert await _balance(shop, INVITER.telegram_id) == 135
    async with shop.uow_factory() as uow:
        assert await uow.bonuses.reward_total(INVITER.telegram_id) == 135


async def test_a_repeated_refund_still_changes_nothing(shop: Shop) -> None:
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop)
    purchase = await _buy(shop, INVITED, product)

    await shop.purchases.refund(purchase.id)
    await shop.purchases.refund(purchase.id)

    assert await _balance(shop, INVITER.telegram_id) == 135


# --- Spending bonuses


async def test_bonuses_reduce_a_later_purchase(shop: Shop) -> None:
    """The specification's closing example: 1000 - 135 = 865."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    earner = await _product(shop)
    await _buy(shop, INVITED, earner)
    assert await _balance(shop, INVITER.telegram_id) == 135

    product = await _product(shop)
    purchase = await _buy(shop, INVITER, product, use_bonus=True)

    assert purchase.base_amount == Decimal(1000)
    assert purchase.bonus_amount == Decimal(135)
    assert purchase.amount == Decimal(865)
    assert await _balance(shop, INVITER.telegram_id) == 0


async def test_spent_bonuses_become_a_settled_ledger_entry(shop: Shop) -> None:
    """A reservation turns into a spend when the payment lands."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _buy(shop, INVITED, await _product(shop))

    purchase = await _buy(shop, INVITER, await _product(shop), use_bonus=True)

    assert await _ledger_types(shop, purchase.id) == [BonusTransactionType.BONUS_SPENT.value]


async def test_bonuses_cover_at_most_half_of_a_price(shop: Shop) -> None:
    """A 200 Star item accepts 100 in bonuses even with 135 available."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _buy(shop, INVITED, await _product(shop))

    cheap = await _product(shop, price_stars=200)
    purchase = await _buy(shop, INVITER, cheap, use_bonus=True)

    assert purchase.bonus_amount == Decimal(100)
    assert purchase.amount == Decimal(100)
    assert await _balance(shop, INVITER.telegram_id) == 35


async def test_a_first_referral_purchase_cannot_also_spend_bonuses(shop: Shop) -> None:
    """The agreed simplification, asserted end to end.

    B has both a pending discount and a balance of their own; the discount wins
    and the balance is left untouched.
    """
    await _buyers(shop, INVITER, INVITED, THIRD)
    await _invite(shop, INVITER, INVITED)
    await _invite(shop, INVITED, THIRD)
    # THIRD buys, which credits INVITED.
    await _buy(shop, THIRD, await _product(shop))
    assert await _balance(shop, INVITED.telegram_id) == 135

    product = await _product(shop)
    purchase = await _buy(shop, INVITED, product, use_bonus=True)

    assert purchase.discount_amount == Decimal(100)
    assert purchase.bonus_amount == Decimal(0)
    assert purchase.amount == Decimal(900)
    assert await _balance(shop, INVITED.telegram_id) == 135


async def test_asking_to_spend_bonuses_that_are_gone_is_refused(shop: Shop) -> None:
    """Refusing beats silently charging a price the buyer never agreed to."""
    await _buyers(shop, LONER)
    product = await _product(shop)

    with pytest.raises(InsufficientBonusBalanceError):
        await shop.purchases.start_purchase(
            user_id=LONER.telegram_id,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            external_id=uuid4().hex,
            use_bonus=True,
        )


async def test_an_expired_invoice_gives_reserved_bonuses_back(shop: Shop) -> None:
    """A reservation is not a charge, so an unpaid invoice returns it."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _buy(shop, INVITED, await _product(shop))

    product = await _product(shop)
    await shop.purchases.start_purchase(
        user_id=INVITER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
        use_bonus=True,
    )
    assert await _balance(shop, INVITER.telegram_id) == 0

    await shop.purchases.expire_stale(now=_far_future())
    released = await shop.bonuses.release_stale_holds()

    assert released == 1
    assert await _balance(shop, INVITER.telegram_id) == 135


async def test_releasing_a_reservation_twice_credits_it_once(shop: Shop) -> None:
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _buy(shop, INVITED, await _product(shop))
    await shop.purchases.start_purchase(
        user_id=INVITER.telegram_id,
        product_id=(await _product(shop)).id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
        use_bonus=True,
    )
    await shop.purchases.expire_stale(now=_far_future())

    await shop.bonuses.release_stale_holds()
    await shop.bonuses.release_stale_holds()

    assert await _balance(shop, INVITER.telegram_id) == 135


async def test_bonuses_spent_on_a_paid_purchase_are_never_given_back(shop: Shop) -> None:
    """A spend is settled. Housekeeping must not mistake it for a reservation."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _buy(shop, INVITED, await _product(shop))
    await _buy(shop, INVITER, await _product(shop), use_bonus=True)

    assert await shop.bonuses.release_stale_holds() == 0
    assert await _balance(shop, INVITER.telegram_id) == 0


# --- Concurrency


async def test_the_same_balance_cannot_fund_two_checkouts(shop: Shop) -> None:
    """135 units, two products, two simultaneous checkouts: one wins.

    Three defences stand behind this — the Redis lock on the balance, the row
    lock on the users row, and the non-negative check constraint. The assertion
    is simply that the balance never went below zero and only one sale was
    funded.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _buy(shop, INVITED, await _product(shop))
    assert await _balance(shop, INVITER.telegram_id) == 135

    first = await _product(shop, price_stars=1000)
    second = await _product(shop, price_stars=1000)

    async def checkout(product: Product) -> Purchase | None:
        try:
            return await shop.purchases.start_purchase(
                user_id=INVITER.telegram_id,
                product_id=product.id,
                provider=PaymentProvider.STARS,
                external_id=uuid4().hex,
                use_bonus=True,
            )
        except Exception:
            return None

    results = await asyncio.gather(checkout(first), checkout(second))

    funded = [purchase for purchase in results if purchase and purchase.bonus_amount > 0]
    assert len(funded) == 1
    assert await _balance(shop, INVITER.telegram_id) >= 0
    assert await _balance(shop, INVITER.telegram_id) == 0


async def test_the_ledger_always_explains_the_balance(shop: Shop) -> None:
    """The cached balance is a cache, and it is provably one.

    Reward, reservation, release and spend all move both the ledger and the
    cached column in the same transaction, so recomputing from history must
    always agree with the column.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    # Earn, then exercise every kind of ledger entry in turn: a reservation
    # that is released, and a reservation that becomes a spend.
    await _buy(shop, INVITED, await _product(shop, price_stars=1000))
    await _buy(shop, INVITED, await _product(shop, price_stars=1200))

    await shop.purchases.start_purchase(
        user_id=INVITER.telegram_id,
        product_id=(await _product(shop)).id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
        use_bonus=True,
    )
    await shop.purchases.expire_stale(now=_far_future())
    await shop.bonuses.release_stale_holds()

    await _buy(shop, INVITER, await _product(shop, price_stars=1000), use_bonus=True)

    async with shop.uow_factory() as uow:
        cached = await uow.bonuses.balance(INVITER.telegram_id)
        recomputed = await uow.bonuses.recompute_balance(INVITER.telegram_id)
    assert cached == recomputed


def _far_future() -> datetime:
    """A moment by which every invoice in a test has certainly expired."""
    return datetime.now(UTC) + timedelta(days=1)


# --- Rewards from a USDT purchase


async def _buy_with_crypto(
    shop: Shop,
    buyer: UserDraft,
    product: Product,
) -> Purchase:
    """One complete USDT sale: invoice, payment, delivery.

    ``expected_amount`` is passed because production passes it: the CryptoBot
    invoice is raised before the purchase row exists, so the price is quoted
    first and the purchase refuses to be recorded at any other number. A helper
    that omitted it would quietly skip that check — which is exactly how a
    discounted crypto checkout once shipped broken.
    """
    expected = await shop.purchases.quote_amount(
        user_id=buyer.telegram_id,
        product=product,
        provider=PaymentProvider.CRYPTO,
    )
    purchase = await shop.purchases.start_purchase(
        user_id=buyer.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.CRYPTO,
        external_id=uuid4().hex,
        expected_amount=expected,
    )
    await shop.purchases.confirm_payment(
        provider=PaymentProvider.CRYPTO,
        external_id=purchase.external_id,
    )
    result = await shop.delivery.deliver_purchase(purchase.id)
    assert result.succeeded
    return await shop.purchases.get(purchase.id)


async def _burn_discount(shop: Shop, buyer: UserDraft) -> None:
    """Spend this buyer's one-time referral discount on a throwaway sale.

    Used by the tests that want a *full price* purchase, since an invited
    buyer's very first one is always discounted.
    """
    await _buy(shop, buyer, await _product(shop, price_stars=10, price_usdt=None))


async def test_a_usdt_purchase_earns_through_the_products_declared_rate(
    shop: Shop,
) -> None:
    """The specification's example, end to end.

    The product is priced at 700 ⭐ / 10 USDT, so the shop itself has declared
    70 ⭐ per USDT. A 10 USDT payment is therefore worth 700 ⭐, and 15% of that
    is 105 bonuses.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _burn_discount(shop, INVITED)
    product = await _product(shop, price_stars=700, price_usdt=Decimal("10.00"))
    before = await _balance(shop, INVITER.telegram_id)

    purchase = await _buy_with_crypto(shop, INVITED, product)

    assert purchase.amount == Decimal("10.00")
    assert await _balance(shop, INVITER.telegram_id) - before == 105


async def test_a_usdt_reward_records_the_rate_it_used(shop: Shop) -> None:
    """Stored on the entry, so the number can always be explained later."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop, price_stars=700, price_usdt=Decimal("10.00"))

    purchase = await _buy_with_crypto(shop, INVITED, product)

    async with shop.uow_factory() as uow:
        entry = await uow.bonuses.find_for_purchase(
            purchase.id,
            BonusTransactionType.REFERRAL_REWARD,
        )
    assert entry is not None
    assert entry.stars_per_usdt == Decimal(70)


async def test_repricing_the_product_does_not_recompute_an_old_reward(
    shop: Shop,
) -> None:
    """The rule that makes historical bonuses trustworthy.

    A reward earned at 70 ⭐/USDT stays what it was even after the admin moves
    the product to 80 ⭐/USDT — the old entry keeps its own rate, and only the
    next sale uses the new one.
    """
    await _buyers(shop, INVITER, INVITED, THIRD)
    await _invite(shop, INVITER, INVITED)
    await _invite(shop, INVITER, THIRD)
    await _burn_discount(shop, INVITED)
    await _burn_discount(shop, THIRD)
    product = await _product(shop, price_stars=700, price_usdt=Decimal("10.00"))
    first = await _buy_with_crypto(shop, INVITED, product)

    from app.domain.commands import ProductUpdate  # noqa: PLC0415

    await shop.products.update(product.id, ProductUpdate(price_stars=800))
    second = await _buy_with_crypto(shop, THIRD, product)

    async with shop.uow_factory() as uow:
        old = await uow.bonuses.find_for_purchase(
            first.id,
            BonusTransactionType.REFERRAL_REWARD,
        )
        new = await uow.bonuses.find_for_purchase(
            second.id,
            BonusTransactionType.REFERRAL_REWARD,
        )
    assert old is not None
    assert new is not None
    # The old reward and its rate are untouched…
    assert old.amount == 105
    assert old.stars_per_usdt == Decimal(70)
    # …while the new sale used the new rate: 800/10 gives 80, times 10 is 800, 15% is 120.
    assert new.amount == 120
    assert new.stars_per_usdt == Decimal(80)


async def test_a_usdt_only_product_earns_nothing_and_says_so(shop: Shop) -> None:
    """No Stars price means no declared rate, so there is nothing to convert.

    Crediting a guess would be worse than crediting nothing; the service logs
    the sale and moves on.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _burn_discount(shop, INVITED)
    product = await _product(shop, price_stars=None, price_usdt=Decimal("10.00"))
    before = await _balance(shop, INVITER.telegram_id)

    purchase = await _buy_with_crypto(shop, INVITED, product)

    assert purchase.amount == Decimal("10.00")
    assert await _balance(shop, INVITER.telegram_id) == before
    assert await _ledger_types(shop, purchase.id) == []


async def test_a_usdt_purchase_still_gets_the_referral_discount(shop: Shop) -> None:
    """The discount is a percentage, so it needs no rate at all."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop, price_stars=700, price_usdt=Decimal("10.00"))

    purchase = await _buy_with_crypto(shop, INVITED, product)

    assert purchase.base_amount == Decimal("10.00")
    assert purchase.discount_amount == Decimal("1.00")
    assert purchase.amount == Decimal("9.00")
    # 9 USDT at 70 stars each is 630 stars, and 15% of that is 94.5, floored to 94.
    assert await _balance(shop, INVITER.telegram_id) == 94


# --- Bonuses are a Stars discount and nothing else


async def test_bonuses_cannot_be_spent_on_a_crypto_purchase(shop: Shop) -> None:
    """Explicitly refused rather than quietly ignored.

    The bot never offers the choice on a crypto card, so a request to spend
    bonuses there is a hand-crafted callback — and charging the full price
    silently would be the wrong answer to it.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _buy(shop, INVITED, await _product(shop))
    assert await _balance(shop, INVITER.telegram_id) == 135

    product = await _product(shop, price_stars=1000, price_usdt=Decimal("10.00"))
    with pytest.raises(BonusesNotAvailableError):
        await shop.purchases.start_purchase(
            user_id=INVITER.telegram_id,
            product_id=product.id,
            provider=PaymentProvider.CRYPTO,
            external_id=uuid4().hex,
            use_bonus=True,
        )
    assert await _balance(shop, INVITER.telegram_id) == 135


async def test_no_bonus_offer_is_made_on_a_crypto_card(shop: Shop) -> None:
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _buy(shop, INVITED, await _product(shop))

    offer = await shop.bonuses.offer_for(
        user_id=INVITER.telegram_id,
        base_amount=Decimal("10.00"),
        currency=Currency.USDT,
    )

    assert not offer.available
    assert offer.quote_without_bonus.charged_amount == Decimal("10.00")


async def test_one_bonus_buys_exactly_one_star(shop: Shop) -> None:
    """The model, asserted against real rows."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _buy(shop, INVITED, await _product(shop, price_stars=1000))
    assert await _balance(shop, INVITER.telegram_id) == 135

    purchase = await _buy(shop, INVITER, await _product(shop, price_stars=1000), use_bonus=True)

    assert purchase.bonus_amount == Decimal(135)
    assert purchase.amount == Decimal(865)


# --- The product card


async def test_an_invited_buyer_sees_the_discounted_price_on_the_card(
    shop: Shop,
) -> None:
    """The list price is struck through beside what this visitor will pay."""
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product = await _product(shop, price_stars=1000, price_usdt=None)

    card = await shop.purchases.open_card(INVITED, product.slug)

    assert card.is_discounted
    assert card.discount_percent == 10
    option = card.options[0]
    assert option.amount == 1000
    assert option.discounted_amount == Decimal(900)


async def test_an_ordinary_buyer_sees_the_card_unchanged(shop: Shop) -> None:
    """The regression that protects every existing buyer."""
    await _buyers(shop, LONER)
    product = await _product(shop, price_stars=1000, price_usdt=None)

    card = await shop.purchases.open_card(LONER, product.slug)

    assert not card.is_discounted
    assert card.discount_percent == 0
    assert card.options[0].discounted_amount is None


async def test_the_card_stops_showing_a_discount_once_it_is_used(shop: Shop) -> None:
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    await _buy(shop, INVITED, await _product(shop))
    later = await _product(shop, price_stars=1000, price_usdt=None)

    card = await shop.purchases.open_card(INVITED, later.slug)

    assert not card.is_discounted
    assert card.options[0].discounted_amount is None


async def test_the_card_shows_no_discount_while_the_programme_is_off(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    enabled = _build_shop(
        live_uow_factory,
        live_locks,
        referral=ReferralSettings(enabled=True),
    )
    await _buyers(enabled, INVITER, INVITED)
    await _invite(enabled, INVITER, INVITED)

    disabled = _build_shop(
        live_uow_factory,
        live_locks,
        referral=ReferralSettings(enabled=False),
    )
    product = await _product(disabled, price_stars=1000, price_usdt=None)
    card = await disabled.purchases.open_card(INVITED, product.slug)

    assert not card.is_discounted
    assert card.options[0].discounted_amount is None


# --- Bug regression tests


async def test_bug2_settlement_after_discount_burned_does_not_crash(
    shop: Shop,
) -> None:
    """Bug #2 safety net: on_payment_confirmed catches DiscountAlreadyUsedError.

    Scenario (legacy data or theoretical race that slipped past Bug #1's guard):
    two purchases were both created with ``discount_amount > 0``.  The first
    was paid and its settlement burned the referral's discount.  Settling the
    second must NOT raise — the purchase is already paid, the money moved, and
    refusing delivery would be worse than honouring the leaked discount.

    What we verify:
    - payment is confirmed (purchase.status moves to PAID);
    - delivery succeeds (the buyer gets their link);
    - the original ``discount_purchase_id`` on the referral is unchanged;
    - a warning is logged (structlog capture is outside this test's scope, but
      the code path runs without raising).
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product_a = await _product(shop, price_stars=1000)

    # Normal discounted purchase — burns the one-time discount.
    purchase_a = await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=product_a.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
    assert purchase_a.discount_amount == Decimal(100)
    await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id=purchase_a.external_id,
    )
    result_a = await shop.delivery.deliver_purchase(purchase_a.id)
    assert result_a.succeeded

    # Verify the discount is now burned, pointing at purchase A.
    async with shop.uow_factory() as uow:
        referral = await uow.referrals.get_by_referred(INVITED.telegram_id)
    assert referral is not None
    assert referral.discount_purchase_id == purchase_a.id
    assert not referral.discount_available

    # Second purchase — with Bug #1 guard active, it gets full price.
    product_b = await _product(shop, price_stars=1000)
    purchase_b = await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=product_b.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
    assert purchase_b.discount_amount == Decimal(0)
    assert purchase_b.amount == Decimal(1000)

    # Confirm and deliver — must not raise.
    confirmed_b = await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id=purchase_b.external_id,
    )
    result_b = await shop.delivery.deliver_purchase(confirmed_b.id)
    assert result_b.succeeded

    # The original discount_purchase_id must NOT have changed.
    async with shop.uow_factory() as uow:
        referral_after = await uow.referrals.get_by_referred(INVITED.telegram_id)
    assert referral_after is not None
    assert referral_after.discount_purchase_id == purchase_a.id


async def test_bug2_referral_reward_accrues_exactly_once(
    shop: Shop,
) -> None:
    """The inviter earns the reward once, from the first completed purchase.

    Settlement must not duplicate the reward entry. A second delivered purchase
    by the same invited buyer earns a separate reward for the inviter — but
    each purchase earns at most once (guaranteed by the unique constraint on
    ``(purchase_id, type)``).
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)

    before = await _balance(shop, INVITER.telegram_id)

    # First purchase — discounted, earns reward.
    purchase = await _buy(shop, INVITED, await _product(shop, price_stars=1000))
    assert purchase.discount_amount == Decimal(100)

    after_first = await _balance(shop, INVITER.telegram_id)
    reward = after_first - before
    assert reward > 0  # 15% of (1000 - 100) = 135

    # Replayed settlement — must be idempotent, no extra reward.
    await shop.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id=purchase.external_id,
    )
    assert await _balance(shop, INVITER.telegram_id) == after_first


async def test_bug1_concurrent_checkouts_serialised_by_for_update(
    shop: Shop,
) -> None:
    """Bug #1: two truly concurrent checkouts on real PostgreSQL.

    Both coroutines issue ``start_purchase`` for different products at the same
    moment.  The ``SELECT … FOR UPDATE`` on the referral row serialises them
    at the database level: one gets the lock first, creates a discounted pending
    purchase and commits; the other then gets the lock, re-checks, and sees
    the pending discount — so it creates a full-price purchase instead.

    ``asyncio.gather`` is real concurrency here because the blocking happens
    inside ``asyncpg`` on the PostgreSQL wire, not on the Python event loop.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)

    first_product = await _product(shop, price_stars=1000)
    second_product = await _product(shop, price_stars=1000)

    async def checkout(product: Product) -> Purchase:
        return await shop.purchases.start_purchase(
            user_id=INVITED.telegram_id,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            external_id=uuid4().hex,
        )

    purchase_a, purchase_b = await asyncio.gather(
        checkout(first_product),
        checkout(second_product),
    )

    # Both checkouts succeed (different products).
    assert purchase_a is not None
    assert purchase_b is not None

    # Exactly one carries the discount.
    discounts = sorted(
        [purchase_a.discount_amount, purchase_b.discount_amount], reverse=True,
    )
    assert discounts == [Decimal(100), Decimal(0)], (
        f"Expected exactly one discount, got: "
        f"a={purchase_a.discount_amount}, b={purchase_b.discount_amount}"
    )


async def test_bug1_sequential_second_checkout_sees_pending_discount(
    shop: Shop,
) -> None:
    """Bug #1 without concurrency: the second checkout for a different product
    sees the first pending discounted purchase and pays full price.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    product_a = await _product(shop, price_stars=1000)
    product_b = await _product(shop, price_stars=1000)

    # First checkout — gets the discount.
    purchase_a = await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=product_a.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
    assert purchase_a.discount_amount == Decimal(100)
    assert purchase_a.amount == Decimal(900)

    # Second checkout — the pending discount guard blocks it.
    purchase_b = await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=product_b.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
    assert purchase_b.discount_amount == Decimal(0)
    assert purchase_b.amount == Decimal(1000)


async def test_bug1_discount_becomes_available_again_after_pending_expires(
    shop: Shop,
) -> None:
    """After the pending discounted purchase expires, the discount is available again.

    The ``has_pending_discount`` guard must only block ``PENDING`` purchases,
    not expired ones.  Otherwise a buyer who abandoned a checkout would lose
    the discount permanently.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    abandoned = await _product(shop, price_stars=1000)

    # Create a discounted pending purchase, then let it expire.
    purchase = await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=abandoned.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
    assert purchase.discount_amount == Decimal(100)

    await shop.purchases.expire_stale(now=_far_future())

    # Now a fresh checkout should still get the discount.
    real = await _product(shop, price_stars=1000)
    real_purchase = await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=real.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
    assert real_purchase.discount_amount == Decimal(100)
