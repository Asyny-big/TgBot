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
from app.core.exceptions import InsufficientBonusBalanceError
from app.domain.commands import ProductDraft, UserDraft
from app.domain.enums import BonusTransactionType, PaymentProvider, PurchaseStatus
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
        bonus_units_per_usdt=Decimal(500),
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

    Otherwise a buyer who opened a card and walked away would silently lose the
    only discount they had.
    """
    await _buyers(shop, INVITER, INVITED)
    await _invite(shop, INVITER, INVITED)
    abandoned = await _product(shop)
    real = await _product(shop)

    await shop.purchases.start_purchase(
        user_id=INVITED.telegram_id,
        product_id=abandoned.id,
        provider=PaymentProvider.STARS,
        external_id=uuid4().hex,
    )
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
