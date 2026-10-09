"""Loyalty milestone notices (5, 10, 15), end to end (PostgreSQL + Redis).

The delivery's own transaction locks the purchase row, takes the buyer's
progress lock and — for a genuine ``paid → delivered`` only — counts the buyer's
completed purchases. With every earlier delivery committed, that count is the
delivery's exact rank, so each milestone is reached by exactly one delivery.
The notice goes out after the commit, best effort.

These tests drive the real services and the admin API against the real
database: the thresholds, historical buyers, replays, failed deliveries, the
free pack, a failing notifier, concurrency, and the switch.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from decimal import Decimal
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup

from app.bot.keyboards import ProgressCallback
from app.bot.texts import LOYALTY_MILESTONE_NOTICES, PROGRESS_SECTION_BUTTON
from app.core.config import DeliverySettings, LoyaltySettings, ReferralSettings
from app.core.exceptions import DeliveryPermanentError, LockBusyError, PurchaseNotFoundError
from app.domain.commands import ProductDraft, PurchaseDraft, UserDraft
from app.domain.delivery import DeliveryStatus
from app.domain.enums import Currency, PaymentProvider, PurchaseStatus
from app.domain.loyalty import DISCOUNT_MILESTONE, FREE_PACK_MILESTONE, VIP_MILESTONE
from app.domain.verification import VerificationOutcome
from app.infrastructure.db.repositories.loyalty import SqlAlchemyLoyaltyRepository
from app.services import delivery as delivery_module
from app.services.bonuses import BonusService
from app.services.checkout import CheckoutService
from app.services.delivery import DeliveryService
from app.services.loyalty import LoyaltyService
from app.services.pricing import PricingService
from app.services.purchases import PurchaseService
from tests.api_harness import api_settings, build_api_harness
from tests.fakes import (
    FakeDeliveryGateway,
    FakeLockManager,
    FakeUnitOfWork,
    FakeUnitOfWorkFactory,
)
from tests.test_loyalty_discount import RecordingStars, ScriptedCryptoPay

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from redis.asyncio import Redis

    from app.domain.entities import Product, Purchase
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.engine import Database
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory
    from tests.api_harness import ApiHarness

BUYER = 9801
DELIVERY_URL = "https://t.me/+milestone"
REFERRAL = ReferralSettings(
    enabled=True,
    discount_percent=10,
    reward_percent=15,
    max_bonus_payment_percent=50,
)


@dataclass
class RecordingNotifier:
    """Milestone notices, as the buyer would receive them; optionally failing."""

    sent: list[tuple[int, int]] = field(default_factory=list)
    attempts: int = 0
    fail: Exception | None = None

    async def notify_milestone(self, user_id: int, milestone: int) -> None:
        self.attempts += 1
        if self.fail is not None:
            raise self.fail
        self.sent.append((user_id, milestone))


@dataclass(frozen=True, slots=True)
class Shop:
    uow_factory: SqlAlchemyUnitOfWorkFactory
    loyalty: LoyaltyService
    purchases: PurchaseService
    checkout: CheckoutService
    delivery: DeliveryService
    gateway: FakeDeliveryGateway
    notifier: RecordingNotifier
    stars: RecordingStars


def _shop(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    locks: RedisLockManager,
    *,
    loyalty_on: bool = True,
) -> Shop:
    bonuses = BonusService(uow_factory=uow_factory, locks=locks, settings=REFERRAL)
    loyalty = LoyaltyService(uow_factory=uow_factory, settings=LoyaltySettings(enabled=loyalty_on))
    pricing = PricingService(uow_factory=uow_factory, bonuses=bonuses, loyalty=loyalty)
    purchases = PurchaseService(
        uow_factory=uow_factory,
        locks=locks,
        pricing=pricing,
        loyalty=loyalty,
    )
    gateway = FakeDeliveryGateway()
    notifier = RecordingNotifier()
    delivery = DeliveryService(
        uow_factory=uow_factory,
        purchases=purchases,
        gateway=gateway,
        locks=locks,
        settings=DeliverySettings(max_attempts=1, initial_backoff_seconds=0.01),
        sale_completion=bonuses,
        milestones=notifier,
    )
    stars = RecordingStars()
    checkout = CheckoutService(
        purchases=purchases,
        delivery=delivery,
        stars=stars,
        crypto=ScriptedCryptoPay(),
    )
    return Shop(
        uow_factory=uow_factory,
        loyalty=loyalty,
        purchases=purchases,
        checkout=checkout,
        delivery=delivery,
        gateway=gateway,
        notifier=notifier,
        stars=stars,
    )


@pytest.fixture
def shop(live_uow_factory: SqlAlchemyUnitOfWorkFactory, live_locks: RedisLockManager) -> Shop:
    return _shop(live_uow_factory, live_locks)


async def _product(shop: Shop) -> Product:
    async with shop.uow_factory() as uow:
        return await uow.products.create(
            ProductDraft(
                slug=f"ms{uuid4().hex[:8]}",
                title="Pack",
                description="",
                delivery_url=DELIVERY_URL,
                price_stars=100,
                price_usdt=Decimal("2.00"),
            )
        )


async def _buyer(shop: Shop, *, user_id: int = BUYER, received: int = 0) -> None:
    """A buyer who already received ``received`` packs, recorded straight in the
    database — the historical kind, delivered before any notice existed."""
    async with shop.uow_factory() as uow:
        await uow.users.upsert(UserDraft(telegram_id=user_id))
    for _ in range(received):
        paid = await _paid(shop, user_id=user_id)
        async with shop.uow_factory() as uow:
            await uow.purchases.mark_delivered(paid.id, delivered_url=DELIVERY_URL)


async def _paid(shop: Shop, *, user_id: int = BUYER) -> Purchase:
    """A paid purchase of a fresh product, waiting for its delivery."""
    product = await _product(shop)
    async with shop.uow_factory() as uow:
        return await uow.purchases.create(
            PurchaseDraft(
                user_id=user_id,
                product_id=product.id,
                provider=PaymentProvider.STARS,
                amount=Decimal(100),
                currency=Currency.XTR,
                external_id=uuid4().hex,
                status=PurchaseStatus.PAID,
            )
        )


async def _status(shop: Shop, purchase: Purchase) -> PurchaseStatus:
    async with shop.uow_factory() as uow:
        found = await uow.purchases.get(purchase.id)
    assert found is not None
    return found.status


# --------------------------------------------------------------------------- #
# Thresholds                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("received", "milestone"),
    [
        (DISCOUNT_MILESTONE - 1, DISCOUNT_MILESTONE),
        (FREE_PACK_MILESTONE - 1, FREE_PACK_MILESTONE),
        (VIP_MILESTONE - 1, VIP_MILESTONE),
    ],
)
async def test_the_delivery_that_reaches_a_milestone_announces_it(
    shop: Shop,
    received: int,
    milestone: int,
) -> None:
    await _buyer(shop, received=received)
    purchase = await _paid(shop)

    result = await shop.delivery.deliver_purchase(purchase.id)

    assert result.status is DeliveryStatus.SENT
    assert await _status(shop, purchase) is PurchaseStatus.DELIVERED
    assert shop.notifier.sent == [(BUYER, milestone)]


@pytest.mark.parametrize("received", [0, 3, 5, 8, 10, 13, 15, 18])
async def test_every_other_delivery_announces_nothing(shop: Shop, received: int) -> None:
    """Ranks 1, 4, 6, 9, 11, 14, 16 and 19 reach nothing."""
    await _buyer(shop, received=received)

    await shop.delivery.deliver_purchase((await _paid(shop)).id)

    assert shop.notifier.sent == []


async def test_each_milestone_is_announced_exactly_once_on_the_way_up(shop: Shop) -> None:
    await _buyer(shop)

    for _ in range(VIP_MILESTONE + 2):
        await shop.delivery.deliver_purchase((await _paid(shop)).id)

    assert shop.notifier.sent == [
        (BUYER, DISCOUNT_MILESTONE),
        (BUYER, FREE_PACK_MILESTONE),
        (BUYER, VIP_MILESTONE),
    ]


@pytest.mark.parametrize("received", [7, 18])
async def test_a_historical_buyer_gets_no_retroactive_notice(shop: Shop, received: int) -> None:
    """Milestones crossed before the notices existed stay silent — reading
    progress included; the next milestone ahead is announced as usual."""
    await _buyer(shop, received=received)

    await shop.loyalty.get_progress(BUYER)
    await shop.delivery.deliver_purchase((await _paid(shop)).id)

    assert shop.notifier.sent == []
    if received < FREE_PACK_MILESTONE:
        await shop.delivery.deliver_purchase((await _paid(shop)).id)  # 9th
        await shop.delivery.deliver_purchase((await _paid(shop)).id)  # 10th
        assert shop.notifier.sent == [(BUYER, FREE_PACK_MILESTONE)]


# --------------------------------------------------------------------------- #
# Only a genuine paid → delivered                                             #
# --------------------------------------------------------------------------- #


async def test_handling_a_delivered_purchase_again_announces_nothing(shop: Shop) -> None:
    await _buyer(shop, received=DISCOUNT_MILESTONE - 1)
    purchase = await _paid(shop)
    await shop.delivery.deliver_purchase(purchase.id)
    assert shop.notifier.sent == [(BUYER, DISCOUNT_MILESTONE)]

    again = await shop.delivery.deliver_purchase(purchase.id)
    resent = await shop.delivery.redeliver(purchase.id)
    recorded = await shop.purchases.record_delivery(purchase.id, delivered_url=DELIVERY_URL)

    assert again.status is DeliveryStatus.ALREADY_DELIVERED
    assert resent.status is DeliveryStatus.SENT
    assert recorded.milestone is None
    assert shop.notifier.sent == [(BUYER, DISCOUNT_MILESTONE)]


async def test_a_replayed_stars_payment_announces_once(shop: Shop) -> None:
    await _buyer(shop, received=DISCOUNT_MILESTONE - 1)
    product = await _product(shop)
    purchase = await shop.checkout.start_stars_checkout(user_id=BUYER, product=product)

    for _ in range(3):
        await shop.checkout.settle_payment(
            provider=PaymentProvider.STARS,
            external_id=purchase.external_id,
            telegram_charge_id="charge-milestone",
        )

    assert shop.notifier.sent == [(BUYER, DISCOUNT_MILESTONE)]
    assert len(shop.gateway.sent) == 1


async def test_a_failed_delivery_is_announced_only_when_it_lands(shop: Shop) -> None:
    await _buyer(shop, received=DISCOUNT_MILESTONE - 1)
    purchase = await _paid(shop)
    shop.gateway.failures = [DeliveryPermanentError("bot was blocked")]

    failed = await shop.delivery.deliver_purchase(purchase.id)

    assert failed.status is DeliveryStatus.FAILED
    assert await _status(shop, purchase) is PurchaseStatus.PAID
    assert shop.notifier.sent == []

    landed = await shop.delivery.deliver_purchase(purchase.id)

    assert landed.status is DeliveryStatus.SENT
    assert shop.notifier.sent == [(BUYER, DISCOUNT_MILESTONE)]


async def test_a_free_pack_as_the_fifteenth_purchase_reaches_vip(shop: Shop) -> None:
    await _buyer(shop, received=VIP_MILESTONE - 1)
    product = await _product(shop)

    claim = await shop.checkout.claim_free_pack(user_id=BUYER, product=product)

    assert claim.delivery.status is DeliveryStatus.SENT
    assert claim.purchase.provider is PaymentProvider.INTERNAL
    assert shop.notifier.sent == [(BUYER, VIP_MILESTONE)]


# --------------------------------------------------------------------------- #
# A notice is a courtesy                                                      #
# --------------------------------------------------------------------------- #


@dataclass
class RecordingLogger:
    events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def _record(self, level: str, event: str, **fields: Any) -> None:
        self.events.append((level, event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self._record("info", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._record("warning", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._record("error", event, **fields)


async def test_a_failing_notifier_never_undoes_the_delivery(
    shop: Shop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = RecordingLogger()
    monkeypatch.setattr(delivery_module, "logger", log)
    await _buyer(shop, received=DISCOUNT_MILESTONE - 1)
    purchase = await _paid(shop)
    shop.notifier.fail = RuntimeError("Forbidden: bot was blocked by the user")

    result = await shop.delivery.deliver_purchase(purchase.id)

    assert result.status is DeliveryStatus.SENT
    assert await _status(shop, purchase) is PurchaseStatus.DELIVERED
    assert shop.notifier.attempts == 1
    (failure,) = [entry for entry in log.events if entry[1] == "loyalty_milestone_not_announced"]
    assert failure[0] == "error"
    assert failure[2]["user_id"] == BUYER
    assert failure[2]["milestone"] == DISCOUNT_MILESTONE


async def test_a_delivery_without_a_notifier_still_ranks_and_delivers(shop: Shop) -> None:
    quiet = replace(shop.delivery, milestones=None)
    await _buyer(shop, received=DISCOUNT_MILESTONE - 1)
    purchase = await _paid(shop)

    result = await quiet.deliver_purchase(purchase.id)

    assert result.status is DeliveryStatus.SENT
    assert await _status(shop, purchase) is PurchaseStatus.DELIVERED
    assert shop.notifier.sent == []


# --------------------------------------------------------------------------- #
# The switch                                                                  #
# --------------------------------------------------------------------------- #


async def test_with_the_programme_off_nothing_is_locked_counted_or_announced(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shop = _shop(live_uow_factory, live_locks, loyalty_on=False)
    await _buyer(shop, received=DISCOUNT_MILESTONE - 1)
    purchase = await _paid(shop)

    async def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        message = "touched while LOYALTY_ENABLED=false"
        raise AssertionError(message)

    monkeypatch.setattr(SqlAlchemyLoyaltyRepository, "lock_progress", forbidden)
    monkeypatch.setattr(SqlAlchemyLoyaltyRepository, "count_completed", forbidden)

    result = await shop.delivery.deliver_purchase(purchase.id)

    assert result.status is DeliveryStatus.SENT
    assert await _status(shop, purchase) is PurchaseStatus.DELIVERED
    assert shop.notifier.attempts == 0


# --------------------------------------------------------------------------- #
# Concurrency                                                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("received", [3, 4], ids=["3+2", "4+2"])
async def test_two_deliveries_at_once_announce_the_milestone_exactly_once(
    shop: Shop,
    received: int,
) -> None:
    """3+2: ranks 4 and 5. 4+2: ranks 5 and 6. Either way exactly one 5."""
    for round_ in range(5):
        user_id = BUYER + 10 + round_
        await _buyer(shop, user_id=user_id, received=received)
        first, second = await _paid(shop, user_id=user_id), await _paid(shop, user_id=user_id)

        results = await asyncio.gather(
            shop.delivery.deliver_purchase(first.id),
            shop.delivery.deliver_purchase(second.id),
        )

        assert all(result.status is DeliveryStatus.SENT for result in results)
        assert [sent for sent in shop.notifier.sent if sent[0] == user_id] == [
            (user_id, DISCOUNT_MILESTONE)
        ]


async def test_two_deliveries_of_one_purchase_announce_once(shop: Shop) -> None:
    await _buyer(shop, received=DISCOUNT_MILESTONE - 1)
    purchase = await _paid(shop)

    # Straight to the record, past the delivery lock: the database decides.
    recorded = await asyncio.gather(
        shop.purchases.record_delivery(purchase.id, delivered_url=DELIVERY_URL),
        shop.purchases.record_delivery(purchase.id, delivered_url=DELIVERY_URL),
    )

    assert sorted((r.milestone for r in recorded), key=lambda m: m or 0) == [
        None,
        DISCOUNT_MILESTONE,
    ]

    # And through the delivery service: one sends, the other is refused or
    # finds it delivered; no second notice either way.
    other = await _paid(shop)
    outcomes = await asyncio.gather(
        shop.delivery.deliver_purchase(other.id),
        shop.delivery.deliver_purchase(other.id),
        return_exceptions=True,
    )
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            assert isinstance(outcome, LockBusyError), outcome
        else:
            assert outcome.succeeded
    assert shop.notifier.sent == []  # rank 6


async def test_a_delivery_waits_for_the_buyers_delivery_in_flight(shop: Shop) -> None:
    """The progress lock on its own, deterministically.

    Another delivery of this buyer is recorded but not committed. Ranking now
    would count 4 and miss the milestone; the second delivery must wait, then
    count both and reach 5.
    """
    await _buyer(shop, received=DISCOUNT_MILESTONE - 2)
    first, second = await _paid(shop), await _paid(shop)

    async with shop.uow_factory() as holder:
        await holder.loyalty.lock_progress(BUYER)
        await holder.purchases.mark_delivered(first.id, delivered_url=DELIVERY_URL)
        contender = asyncio.create_task(
            shop.purchases.record_delivery(second.id, delivered_url=DELIVERY_URL)
        )
        await asyncio.sleep(0.3)
        assert not contender.done()  # parked on the buyer's progress lock

    recorded = await contender
    assert recorded.milestone == DISCOUNT_MILESTONE


# --------------------------------------------------------------------------- #
# The admin API delivers and announces too                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def loyal_api(
    live_database: Database,
    live_locks: RedisLockManager,
    redis_client: Redis,
    migrated_database: str,
) -> AsyncIterator[ApiHarness]:
    settings = api_settings(migrated_database, loyalty=LoyaltySettings(enabled=True))
    harness, client = build_api_harness(
        settings=settings,
        database=live_database,
        locks=live_locks,
        redis=redis_client,
    )
    try:
        await harness.authenticate()
        yield harness
    finally:
        await client.aclose()
        await harness.bot.session.close()


async def test_an_admin_verify_that_delivers_the_fifth_pack_announces_it(
    loyal_api: ApiHarness,
) -> None:
    container = loyal_api.container
    shop = _shop(container.uow_factory, container.locks)
    await _buyer(shop, received=DISCOUNT_MILESTONE - 1)
    purchase = await _paid(shop)

    response = await loyal_api.client.post(f"{loyal_api.prefix}/purchases/{purchase.id}/verify")

    assert response.status_code == HTTPStatus.OK, response.text
    assert response.json()["outcome"] == VerificationOutcome.DELIVERED_NOW.value
    sent = loyal_api.bot.methods(SendMessage)
    assert [message.chat_id for message in sent] == [BUYER, BUYER]
    link, notice = sent
    assert DELIVERY_URL in link.text
    assert notice.text == LOYALTY_MILESTONE_NOTICES[DISCOUNT_MILESTONE]
    markup = notice.reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    (button,) = [button for row in markup.inline_keyboard for button in row]
    assert button.text == PROGRESS_SECTION_BUTTON
    assert button.callback_data == ProgressCallback().pack()

    # Verifying again: already delivered — no second notice.
    await loyal_api.client.post(f"{loyal_api.prefix}/purchases/{purchase.id}/verify")
    assert len(loyal_api.bot.methods(SendMessage)) == len(sent)


# --------------------------------------------------------------------------- #
# The same rules without a database                                           #
# --------------------------------------------------------------------------- #


async def _fake_paid(unit: FakeUnitOfWork, *, received: int) -> Purchase:
    """``received`` delivered packs and one more paid, each its own product."""
    await unit.users.upsert(UserDraft(telegram_id=BUYER))
    purchase: Purchase | None = None
    for index in range(received + 1):
        if purchase is not None:
            await unit.purchases.mark_delivered(purchase.id, delivered_url=DELIVERY_URL)
        product = await unit.products.create(
            ProductDraft(
                slug=f"fake{index}",
                title="Pack",
                description="",
                delivery_url=DELIVERY_URL,
                price_stars=100,
            )
        )
        purchase = await unit.purchases.create(
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
    assert purchase is not None
    return purchase


@pytest.mark.parametrize("loyalty_on", [True, False], ids=["on", "off"])
async def test_the_progress_lock_follows_the_switch(loyalty_on: bool) -> None:
    unit = FakeUnitOfWork()
    factory = FakeUnitOfWorkFactory(unit)
    loyalty = LoyaltyService(uow_factory=factory, settings=LoyaltySettings(enabled=loyalty_on))
    service = PurchaseService(uow_factory=factory, locks=FakeLockManager(), loyalty=loyalty)
    purchase = await _fake_paid(unit, received=DISCOUNT_MILESTONE - 1)

    recorded = await service.record_delivery(purchase.id, delivered_url=DELIVERY_URL)
    again = await service.record_delivery(purchase.id, delivered_url=DELIVERY_URL)

    assert recorded.purchase.status is PurchaseStatus.DELIVERED
    assert recorded.milestone == (DISCOUNT_MILESTONE if loyalty_on else None)
    assert again.milestone is None
    assert unit.loyalty.progress_locks == ([BUYER, BUYER] if loyalty_on else [])


async def test_mark_delivered_is_record_delivery_without_the_milestone() -> None:
    unit = FakeUnitOfWork()
    factory = FakeUnitOfWorkFactory(unit)
    loyalty = LoyaltyService(uow_factory=factory, settings=LoyaltySettings(enabled=True))
    service = PurchaseService(uow_factory=factory, locks=FakeLockManager(), loyalty=loyalty)
    purchase = await _fake_paid(unit, received=0)

    delivered = await service.mark_delivered(purchase.id, delivered_url=DELIVERY_URL)

    assert delivered.status is PurchaseStatus.DELIVERED
    assert unit.loyalty.progress_locks == [BUYER]


async def test_recording_an_unknown_purchase_is_refused() -> None:
    unit = FakeUnitOfWork()
    factory = FakeUnitOfWorkFactory(unit)
    loyalty = LoyaltyService(uow_factory=factory, settings=LoyaltySettings(enabled=True))
    service = PurchaseService(uow_factory=factory, locks=FakeLockManager(), loyalty=loyalty)

    with pytest.raises(PurchaseNotFoundError):
        await service.record_delivery(uuid4(), delivered_url=DELIVERY_URL)
    assert unit.loyalty.progress_locks == []
