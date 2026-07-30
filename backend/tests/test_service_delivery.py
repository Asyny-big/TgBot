"""Unit tests for DeliveryService — transport, retries and confirmation."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.core.config import DeliverySettings
from app.core.exceptions import (
    ConflictError,
    DeliveryPermanentError,
    DeliveryTransientError,
    LockBusyError,
    PurchaseNotFoundError,
)
from app.domain.commands import ProductDraft, UserDraft
from app.domain.delivery import DeliveryStatus
from app.domain.enums import PaymentProvider, PurchaseStatus
from app.domain.locks import delivery_lock_key
from app.services.delivery import DeliveryService
from app.services.purchases import PurchaseService
from tests.fakes import (
    FakeDeliveryGateway,
    FakeLockManager,
    FakeUnitOfWorkFactory,
    RecordingSleeper,
)

if TYPE_CHECKING:
    from app.domain.entities import Purchase

BUYER = UserDraft(telegram_id=777001, username="buyer", first_name="Buyer", language_code="en")
DELIVERY_URL = "https://t.me/+private-invite"


def _settings(**overrides: object) -> DeliverySettings:
    values: dict[str, object] = {
        "max_attempts": 3,
        "initial_backoff_seconds": 1.0,
        "max_backoff_seconds": 10.0,
        "backoff_multiplier": 2.0,
        "jitter_ratio": 0.0,
    }
    values.update(overrides)
    return DeliverySettings(**values)  # type: ignore[arg-type]


class Harness:
    """A wired PurchaseService + DeliveryService over in-memory doubles."""

    def __init__(
        self,
        *,
        failures: list[Exception] | None = None,
        settings: DeliverySettings | None = None,
    ) -> None:
        self.uow_factory = FakeUnitOfWorkFactory()
        self.locks = FakeLockManager()
        self.gateway = FakeDeliveryGateway(failures)
        self.sleeper = RecordingSleeper()
        self.purchases = PurchaseService(uow_factory=self.uow_factory, locks=self.locks)
        self.delivery = DeliveryService(
            uow_factory=self.uow_factory,
            purchases=self.purchases,
            gateway=self.gateway,
            locks=self.locks,
            settings=settings or _settings(),
            sleep=self.sleeper,
            jitter=lambda: 0.5,
        )

    async def paid_purchase(self, *, provider: PaymentProvider = PaymentProvider.STARS) -> Purchase:
        product = await self.uow_factory.unit.products.create(
            ProductDraft(
                slug="vip1",
                title="VIP access",
                description="Access",
                delivery_url=DELIVERY_URL,
                price_stars=100,
                price_usdt=Decimal("4.99"),
            )
        )
        await self.uow_factory.unit.users.upsert(BUYER)
        await self.purchases.start_purchase(
            user_id=BUYER.telegram_id,
            product_id=product.id,
            provider=provider,
            external_id="inv-1",
        )
        return await self.purchases.confirm_payment(provider=provider, external_id="inv-1")


async def test_successful_delivery_sends_once_and_confirms() -> None:
    harness = Harness()
    purchase = await harness.paid_purchase()

    result = await harness.delivery.deliver_purchase(purchase.id)

    assert result.status is DeliveryStatus.SENT
    assert result.attempts == 1
    assert len(harness.gateway.sent) == 1
    message = harness.gateway.sent[0]
    assert message.chat_id == BUYER.telegram_id
    assert message.delivery_url == DELIVERY_URL
    assert message.product_title == "VIP access"
    assert not message.is_repeat
    # Only DeliveryService moved the purchase to delivered.
    stored = harness.uow_factory.unit.purchases.items[purchase.id]
    assert stored.status is PurchaseStatus.DELIVERED
    assert stored.delivered_url == DELIVERY_URL
    assert harness.sleeper.delays == []


async def test_delivery_holds_a_ttl_bounded_lock() -> None:
    harness = Harness()
    purchase = await harness.paid_purchase()

    await harness.delivery.deliver_purchase(purchase.id)

    assert delivery_lock_key(purchase.id) in [key for key, _ in harness.locks.acquired]
    assert harness.locks.held == {}


async def test_a_busy_delivery_lock_stops_a_second_worker() -> None:
    harness = Harness()
    purchase = await harness.paid_purchase()
    harness.locks.busy_keys.add(delivery_lock_key(purchase.id))

    with pytest.raises(LockBusyError):
        await harness.delivery.deliver_purchase(purchase.id)
    assert harness.gateway.sent == []


async def test_transient_failures_are_retried_with_growing_backoff() -> None:
    harness = Harness(
        failures=[
            DeliveryTransientError("network glitch"),
            DeliveryTransientError("still down"),
        ]
    )
    purchase = await harness.paid_purchase()

    result = await harness.delivery.deliver_purchase(purchase.id)

    assert result.status is DeliveryStatus.SENT
    assert result.attempts == 3
    assert harness.sleeper.delays == [1.0, 2.0]
    assert harness.uow_factory.unit.purchases.items[purchase.id].status is PurchaseStatus.DELIVERED


async def test_flood_control_hint_wins_over_the_computed_backoff() -> None:
    harness = Harness(failures=[DeliveryTransientError("flood wait", retry_after=7.5)])
    purchase = await harness.paid_purchase()

    result = await harness.delivery.deliver_purchase(purchase.id)

    assert result.status is DeliveryStatus.SENT
    assert harness.sleeper.delays == [7.5]


async def test_backoff_is_capped() -> None:
    harness = Harness(
        failures=[DeliveryTransientError("1"), DeliveryTransientError("2")],
        settings=_settings(max_attempts=3, initial_backoff_seconds=8.0, max_backoff_seconds=10.0),
    )
    purchase = await harness.paid_purchase()

    await harness.delivery.deliver_purchase(purchase.id)
    assert harness.sleeper.delays == [8.0, 10.0]


async def test_jitter_spreads_the_delay_around_the_nominal_value() -> None:
    harness = Harness(
        failures=[DeliveryTransientError("boom")],
        settings=_settings(max_attempts=2, jitter_ratio=0.5),
    )
    harness.delivery = DeliveryService(
        uow_factory=harness.uow_factory,
        purchases=harness.purchases,
        gateway=harness.gateway,
        locks=harness.locks,
        settings=_settings(max_attempts=2, jitter_ratio=0.5),
        sleep=harness.sleeper,
        jitter=lambda: 1.0,
    )
    purchase = await harness.paid_purchase()

    await harness.delivery.deliver_purchase(purchase.id)
    # 1.0 nominal + 50% jitter at the top of the range.
    assert harness.sleeper.delays == [1.5]


async def test_exhausted_retries_leave_the_purchase_paid_for_a_later_attempt() -> None:
    harness = Harness(
        failures=[
            DeliveryTransientError("1"),
            DeliveryTransientError("2"),
            DeliveryTransientError("3"),
        ]
    )
    purchase = await harness.paid_purchase()

    result = await harness.delivery.deliver_purchase(purchase.id)

    assert result.status is DeliveryStatus.FAILED
    assert result.attempts == 3
    assert result.error == "3"
    assert not result.succeeded
    # The buyer keeps the right to the link: the purchase is still payable-delivered later.
    assert harness.uow_factory.unit.purchases.items[purchase.id].status is PurchaseStatus.PAID


async def test_a_permanent_failure_is_not_retried() -> None:
    harness = Harness(failures=[DeliveryPermanentError("bot was blocked by the user")])
    purchase = await harness.paid_purchase()

    result = await harness.delivery.deliver_purchase(purchase.id)

    assert result.status is DeliveryStatus.FAILED
    assert result.attempts == 1
    assert harness.sleeper.delays == []
    assert harness.uow_factory.unit.purchases.items[purchase.id].status is PurchaseStatus.PAID


async def test_second_delivery_of_the_same_purchase_does_not_resend() -> None:
    harness = Harness()
    purchase = await harness.paid_purchase()
    await harness.delivery.deliver_purchase(purchase.id)

    result = await harness.delivery.deliver_purchase(purchase.id)

    assert result.status is DeliveryStatus.ALREADY_DELIVERED
    assert result.succeeded
    assert len(harness.gateway.sent) == 1


async def test_redelivery_sends_the_link_again_marked_as_repeat() -> None:
    harness = Harness()
    purchase = await harness.paid_purchase()
    await harness.delivery.deliver_purchase(purchase.id)
    delivered_at = harness.uow_factory.unit.purchases.items[purchase.id].delivered_at

    result = await harness.delivery.redeliver(purchase.id)

    assert result.status is DeliveryStatus.SENT
    assert len(harness.gateway.sent) == 2
    assert harness.gateway.sent[1].is_repeat
    # Re-sending does not rewrite the original delivery timestamp.
    assert harness.uow_factory.unit.purchases.items[purchase.id].delivered_at == delivered_at


async def test_redelivery_hands_over_the_current_link_after_an_admin_edit() -> None:
    harness = Harness()
    purchase = await harness.paid_purchase()
    await harness.delivery.deliver_purchase(purchase.id)

    product = harness.uow_factory.unit.products.items[purchase.product_id]
    harness.uow_factory.unit.products.items[product.id] = replace(
        product,
        delivery_url="https://t.me/+rotated-invite",
    )

    await harness.delivery.redeliver(purchase.id)
    assert harness.gateway.sent[-1].delivery_url == "https://t.me/+rotated-invite"


async def test_an_unpaid_purchase_is_never_delivered() -> None:
    harness = Harness()
    product = await harness.uow_factory.unit.products.create(
        ProductDraft(
            slug="vip1",
            title="VIP",
            description="",
            delivery_url=DELIVERY_URL,
            price_stars=100,
        )
    )
    await harness.uow_factory.unit.users.upsert(BUYER)
    pending = await harness.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="inv-1",
    )

    with pytest.raises(ConflictError, match="Only a paid purchase"):
        await harness.delivery.deliver_purchase(pending.id)
    assert harness.gateway.sent == []


async def test_delivering_an_unknown_purchase_raises() -> None:
    harness = Harness()
    with pytest.raises(PurchaseNotFoundError):
        await harness.delivery.deliver_purchase(uuid4())
