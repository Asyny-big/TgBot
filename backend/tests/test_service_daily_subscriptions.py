"""DailySubscriptionService on the in-memory fakes: who is let in, and when.

The fakes keep the database's rules (see ``test_repository_daily``), so these
tests are about the service's decisions; the bot's end-to-end path over
PostgreSQL and Redis lives in ``test_daily_bot_flow``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.core.config import DailySettings
from app.domain.commands import ProductDraft, PurchaseDraft, UserDraft
from app.domain.daily import activation_window
from app.domain.enums import (
    Currency,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    DailySubscriptionStatus,
    PaymentProvider,
    PurchaseStatus,
)
from app.services.daily_subscriptions import ContactOutcome, DailySubscriptionService
from tests.fakes import FakeUnitOfWorkFactory

BUYER = 9701
OWNER = 9700
T0 = datetime(2026, 10, 10, 9, 35, tzinfo=UTC)  # 12:35 in Moscow
TODAY = date(2026, 10, 10)


@dataclass
class Clock:
    now: datetime = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Daily:
    service: DailySubscriptionService
    store: FakeUnitOfWorkFactory
    clock: Clock


def _daily(**settings: Any) -> Daily:
    values: dict[str, Any] = {"enabled": True, "admin_telegram_ids": (OWNER,)}
    values.update(settings)
    store, clock = FakeUnitOfWorkFactory(), Clock()
    service = DailySubscriptionService(
        uow_factory=store, settings=DailySettings(**values), clock=clock
    )
    return Daily(service=service, store=store, clock=clock)


@pytest.fixture
def daily() -> Daily:
    return _daily()


async def _buyer(daily: Daily, user_id: int = BUYER, *, completed: int = 15) -> None:
    async with daily.store() as uow:
        await uow.users.upsert(UserDraft(telegram_id=user_id))
    for _ in range(completed):
        await _complete_one(daily, user_id)


async def _complete_one(daily: Daily, user_id: int) -> None:
    async with daily.store() as uow:
        product = await uow.products.create(
            ProductDraft(
                slug=f"s{uuid4().hex[:10]}",
                title="Pack",
                description="",
                delivery_url="https://t.me/+pack",
                price_stars=100,
            )
        )
        paid = await uow.purchases.create(
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
        await uow.purchases.mark_delivered(paid.id, delivered_url="https://t.me/+pack")


async def _recorded(daily: Daily, user_id: int = BUYER) -> Any:
    async with daily.store() as uow:
        return await uow.daily_subscriptions.get(user_id)


async def _delivery_on(daily: Daily, day: date, user_id: int = BUYER) -> Any:
    async with daily.store() as uow:
        return await uow.daily_deliveries.for_date(user_id, day)


# --------------------------------------------------------------------------- #
# Who gets nothing recorded                                                   #
# --------------------------------------------------------------------------- #


async def test_with_daily_content_off_nothing_is_written() -> None:
    daily = _daily(enabled=False)
    await _buyer(daily)

    assert (await daily.service.first_contact(BUYER)).outcome is ContactOutcome.DISABLED
    assert await daily.service.pause(BUYER) is None
    assert await daily.service.resume(BUYER) is None
    assert await daily.service.status(BUYER) is None
    await daily.service.blocked(BUYER)
    await daily.service.unblocked(BUYER)

    assert await _recorded(daily) is None


async def test_the_owner_is_turned_away_whatever_they_bought(daily: Daily) -> None:
    await _buyer(daily, OWNER, completed=20)

    result = await daily.service.first_contact(OWNER)

    assert result.outcome is ContactOutcome.OWNER
    assert daily.service.is_owner(OWNER)
    assert not daily.service.is_owner(BUYER)
    assert await _recorded(daily, OWNER) is None
    assert await _delivery_on(daily, TODAY, OWNER) is None


async def test_a_beta_turns_away_everybody_else() -> None:
    daily = _daily(beta_user_ids=(BUYER + 1,))
    await _buyer(daily)
    await _buyer(daily, BUYER + 1)

    outsider = await daily.service.first_contact(BUYER)
    invited = await daily.service.first_contact(BUYER + 1)

    assert outsider.outcome is ContactOutcome.NOT_INVITED
    assert await _recorded(daily) is None
    assert invited.outcome is ContactOutcome.ACTIVATED


async def test_somebody_who_never_used_the_shop_is_not_recorded(daily: Daily) -> None:
    result = await daily.service.first_contact(BUYER)

    assert result.outcome is ContactOutcome.UNKNOWN_BUYER
    assert result.completed_purchases == 0
    assert await _recorded(daily) is None


# --------------------------------------------------------------------------- #
# Access and the anchor                                                       #
# --------------------------------------------------------------------------- #


async def test_below_fifteen_the_contact_is_recorded_without_an_anchor(daily: Daily) -> None:
    await _buyer(daily, completed=14)

    result = await daily.service.first_contact(BUYER)

    assert result.outcome is ContactOutcome.NO_ACCESS
    assert result.completed_purchases == 14
    recorded = await _recorded(daily)
    assert recorded.status is DailySubscriptionStatus.INACTIVE
    assert recorded.first_interaction_at == T0
    assert recorded.anchor_minute is None
    assert await _delivery_on(daily, TODAY) is None
    assert await daily.service.status(BUYER) is None


async def test_the_fifteenth_purchase_activates_at_the_next_contact(daily: Daily) -> None:
    await _buyer(daily, completed=14)
    await daily.service.first_contact(BUYER)
    await _complete_one(daily, BUYER)
    daily.clock.now = T0 + timedelta(hours=3, minutes=7)  # 15:42 in Moscow

    result = await daily.service.first_contact(BUYER)

    assert result.outcome is ContactOutcome.ACTIVATED
    assert result.completed_purchases == 15
    assert result.subscription is not None
    assert result.subscription.anchor_minute == 15 * 60 + 42
    assert result.subscription.first_interaction_at == T0


async def test_activation_anchors_at_this_contact_and_opens_todays_delivery(
    daily: Daily,
) -> None:
    await _buyer(daily)

    result = await daily.service.first_contact(BUYER)

    assert result.outcome is ContactOutcome.ACTIVATED
    subscription, delivery = result.subscription, result.delivery
    assert subscription is not None
    assert subscription.status is DailySubscriptionStatus.ACTIVE
    assert subscription.anchor_minute == 12 * 60 + 35
    assert subscription.activated_at == T0
    assert delivery is not None
    window = activation_window(T0, recovery=daily.service.settings.recovery)
    assert delivery.trigger is DailyDeliveryTrigger.ACTIVATION
    assert delivery.status is DailyDeliveryStatus.PENDING
    assert delivery.delivery_date == window.delivery_date == TODAY
    assert delivery.next_attempt_at == T0
    assert delivery.deadline_at == window.deadline_at


async def test_once_active_a_contact_changes_nothing(daily: Daily) -> None:
    await _buyer(daily)
    first = await daily.service.first_contact(BUYER)
    assert first.delivery is not None

    daily.clock.now = T0 + timedelta(hours=5)
    again = await daily.service.first_contact(BUYER)

    assert again.outcome is ContactOutcome.ACTIVE
    assert again.delivery is None
    assert again.subscription is not None
    assert again.subscription.anchor_minute == 12 * 60 + 35
    today = await _delivery_on(daily, TODAY)
    assert today.id == first.delivery.id


# --------------------------------------------------------------------------- #
# Pause, block, status                                                        #
# --------------------------------------------------------------------------- #


async def test_pause_and_resume_need_an_activated_buyer(daily: Daily) -> None:
    await _buyer(daily, completed=3)
    await daily.service.first_contact(BUYER)

    assert await daily.service.pause(BUYER) is None
    assert await daily.service.resume(BUYER) is None
    assert await daily.service.pause(424242) is None


async def test_a_paused_buyer_stays_paused_when_writing(daily: Daily) -> None:
    await _buyer(daily)
    await daily.service.first_contact(BUYER)

    paused = await daily.service.pause(BUYER)
    assert paused is not None
    assert paused.status is DailySubscriptionStatus.PAUSED
    assert (await daily.service.first_contact(BUYER)).outcome is ContactOutcome.PAUSED

    resumed = await daily.service.resume(BUYER)
    assert resumed is not None
    assert resumed.status is DailySubscriptionStatus.ACTIVE
    assert (await daily.service.first_contact(BUYER)).outcome is ContactOutcome.ACTIVE


async def test_writing_to_the_bot_clears_a_block(daily: Daily) -> None:
    await _buyer(daily)
    await daily.service.first_contact(BUYER)
    await daily.service.blocked(BUYER)
    assert (await _recorded(daily)).is_blocked

    await daily.service.first_contact(BUYER)
    assert not (await _recorded(daily)).is_blocked

    await daily.service.blocked(BUYER)
    await daily.service.unblocked(BUYER)
    assert not (await _recorded(daily)).is_blocked


async def test_a_block_from_somebody_never_recorded_is_ignored(daily: Daily) -> None:
    await daily.service.blocked(BUYER)
    await daily.service.unblocked(BUYER)

    assert await _recorded(daily) is None


async def test_the_status_counts_come_from_the_store(daily: Daily) -> None:
    await _buyer(daily)
    await daily.service.first_contact(BUYER)

    status = await daily.service.status(BUYER)

    assert status is not None
    assert status.received == 0
    assert status.available == 0
    assert status.subscription.anchor_minute == 12 * 60 + 35
