"""The Daily Content bot from a buyer's side, end to end (PostgreSQL + Redis).

Updates go through the real dispatcher, the subscription service, the delivery
engine and the Telegram sender; only Telegram itself is a recording double.
The clock is pinned, because the minute of the first eligible contact *is* the
buyer's anchor.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import LeaveChat, SendMessage, SendVideo
from aiogram.types import ReplyKeyboardMarkup
from sqlalchemy import text

from app.daily_bot import texts
from app.domain.commands import DailyContentDraft
from app.domain.daily import DeliveryStep
from app.domain.enums import (
    DailyContentKind,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    DailySubscriptionStatus,
)
from tests.concurrency import Gate, all_at_once, gated, warm_pool
from tests.daily_bot_harness import (
    Clock,
    DailyHarness,
    build_daily_harness,
    buyer,
    completed_purchase,
    membership_update,
    message_update,
    seed_buyer,
)

if TYPE_CHECKING:
    from app.domain.entities import DailyDelivery, DailySubscription
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

BUYER = 9601
OWNER = 9600
T0 = datetime(2026, 10, 10, 9, 35, tzinfo=UTC)  # 12:35 in Moscow
WELCOME_AT_1235 = (
    "👑 Добро пожаловать в VIP Daily! Каждый день около 12:35 (МСК) "
    "здесь будет новый материал. Вот первый:"
)

Factory = Callable[..., DailyHarness]


@pytest.fixture
async def make_daily(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> AsyncIterator[Factory]:
    """Build a Daily bot over the live stores; ``settings`` override DAILY_*.

    ``gate`` holds every first contact right after it read the subscription.
    """
    built: list[DailyHarness] = []

    def build(*, gate: Gate | None = None, **settings: Any) -> DailyHarness:
        subscriptions_uow = (
            None
            if gate is None
            else gated(
                live_uow_factory,
                gate,
                repository="daily_subscriptions",
                method="record_first_interaction",
            )
        )
        harness = build_daily_harness(
            live_uow_factory,
            live_locks,
            clock=Clock(T0),
            owner=OWNER,
            default_user=BUYER,
            subscriptions_uow=subscriptions_uow,
            **settings,
        )
        built.append(harness)
        return harness

    yield build
    for harness in built:
        await harness.close()


@pytest.fixture
def daily(make_daily: Factory) -> DailyHarness:
    return make_daily()


# --------------------------------------------------------------------------- #
# Seeding and reading                                                         #
# --------------------------------------------------------------------------- #


async def _buyer(daily: DailyHarness, user_id: int = BUYER, *, completed: int = 15) -> None:
    await seed_buyer(daily.uow_factory, user_id, completed=completed)


async def _library(daily: DailyHarness, count: int) -> None:
    async with daily.uow_factory() as uow:
        for _ in range(count):
            await uow.daily_content.add(
                DailyContentDraft(
                    kind=DailyContentKind.VIDEO,
                    source_chat_id=OWNER,
                    source_message_id=int(uuid4().int % 10_000_000),
                    added_by=OWNER,
                    file_id=f"video-{uuid4().hex}",
                    file_unique_id=f"unique-{uuid4().hex}",
                )
            )


async def _subscription(daily: DailyHarness, user_id: int = BUYER) -> DailySubscription | None:
    async with daily.uow_factory() as uow:
        return await uow.daily_subscriptions.get(user_id)


async def _deliveries(daily: DailyHarness, user_id: int = BUYER) -> list[DailyDelivery]:
    async with daily.uow_factory() as uow:
        rows = await uow.session.execute(
            text("SELECT id FROM daily_deliveries WHERE user_id = :user ORDER BY id"),
            {"user": user_id},
        )
        found = [await uow.daily_deliveries.get(row[0]) for row in rows]
    return [delivery for delivery in found if delivery is not None]


async def _all_at_once(daily: DailyHarness, text: str, *, times: int) -> None:
    """Send the same message ``times`` times at once; every one must be handled."""
    results = await all_at_once(*(daily.send(text) for _ in range(times)))
    assert [result for result in results if isinstance(result, BaseException)] == []


def _keyboard_labels(call: Any) -> list[str]:
    markup = getattr(call, "reply_markup", None)
    assert isinstance(markup, ReplyKeyboardMarkup)
    return [button.text for row in markup.keyboard for button in row]


# --------------------------------------------------------------------------- #
# Activation                                                                  #
# --------------------------------------------------------------------------- #


async def test_an_eligible_first_contact_gets_the_welcome_then_the_first_item(
    daily: DailyHarness,
) -> None:
    await _buyer(daily)
    await _library(daily, 3)

    await daily.send("/start")

    welcome, item = daily.bot.sent_to(BUYER)
    assert isinstance(welcome, SendMessage)
    assert welcome.text == WELCOME_AT_1235
    assert _keyboard_labels(welcome) == [texts.STATUS_BUTTON, texts.PAUSE_BUTTON]
    assert isinstance(item, SendVideo)
    assert item.protect_content is True

    subscription = await _subscription(daily)
    assert subscription is not None
    assert subscription.status is DailySubscriptionStatus.ACTIVE
    assert subscription.anchor_minute == 12 * 60 + 35
    assert subscription.activated_at == T0
    (delivery,) = await _deliveries(daily)
    assert delivery.trigger is DailyDeliveryTrigger.ACTIVATION
    assert delivery.status is DailyDeliveryStatus.SENT


@pytest.mark.parametrize(
    ("utc", "moscow", "window"),
    [
        ((9, 35), (12, 35), "11:35–13:35"),
        ((6, 10), (9, 10), "08:10–10:10"),
        ((18, 40), (21, 40), "20:40–22:40"),
    ],
)
async def test_the_anchor_is_the_minute_of_the_first_eligible_contact(
    daily: DailyHarness,
    utc: tuple[int, int],
    moscow: tuple[int, int],
    window: str,
) -> None:
    await _buyer(daily)
    await _library(daily, 2)
    daily.clock.now = datetime(2026, 10, 10, *utc, tzinfo=UTC)

    await daily.send("hello")
    await daily.send("/status")

    subscription = await _subscription(daily)
    assert subscription is not None
    assert subscription.anchor_minute == moscow[0] * 60 + moscow[1]
    first, *_, status = daily.bot.texts_to(BUYER)
    assert f"около {moscow[0]:02d}:{moscow[1]:02d} (МСК)" in first
    assert f"Окно: {window} МСК" in status


async def test_an_early_contact_never_becomes_the_anchor(daily: DailyHarness) -> None:
    await _buyer(daily, completed=14)
    await _library(daily, 2)

    await daily.send("/start")

    assert daily.bot.texts_to(BUYER) == [
        "Daily-контент открывается с 15-й покупки. Сейчас у вас 14 из 15."
    ]
    early = await _subscription(daily)
    assert early is not None
    assert early.status is DailySubscriptionStatus.INACTIVE
    assert early.first_interaction_at == T0
    assert early.anchor_minute is None
    assert await _deliveries(daily) == []
    assert daily.bot.items_to(BUYER) == []

    # The 15th purchase arrives; the buyer writes again six hours later.
    async with daily.uow_factory() as uow:
        await completed_purchase(uow, BUYER)
    daily.clock.advance(hours=6)  # 18:35 in Moscow
    await daily.send("/start")

    activated = await _subscription(daily)
    assert activated is not None
    assert activated.status is DailySubscriptionStatus.ACTIVE
    assert activated.anchor_minute == 18 * 60 + 35  # this contact, not the early one
    assert activated.first_interaction_at == T0
    assert "около 18:35 (МСК)" in daily.bot.texts_to(BUYER)[-1]
    assert len(daily.bot.items_to(BUYER)) == 1


# --------------------------------------------------------------------------- #
# One item a day                                                              #
# --------------------------------------------------------------------------- #


async def test_a_double_start_sends_one_item(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _library(daily, 3)

    await daily.send("/start")
    await daily.send("/start")

    assert len(daily.bot.items_to(BUYER)) == 1
    welcome, status = daily.bot.texts_to(BUYER)
    assert welcome == WELCOME_AT_1235
    assert status.startswith("📊 Получено: 1 ")


@pytest.mark.parametrize("wrote_before", [False, True], ids=["new", "recorded-earlier"])
async def test_parallel_updates_send_one_item(daily: DailyHarness, wrote_before: bool) -> None:
    """Five updates at once, for a buyer the bot has never seen — the insert
    serialises them — and for one recorded earlier with 14 purchases, where
    only the row lock does."""
    await _buyer(daily, completed=14 if wrote_before else 15)
    await _library(daily, 3)
    if wrote_before:
        await daily.send("/start")
        async with daily.uow_factory() as uow:
            await completed_purchase(uow, BUYER)
    before = len(daily.bot.texts_to(BUYER))
    await warm_pool(daily.uow_factory, 6)

    await _all_at_once(daily, "/start", times=5)

    replies = daily.bot.texts_to(BUYER)[before:]
    assert len(daily.bot.items_to(BUYER)) == 1
    assert replies.count(WELCOME_AT_1235) == 1
    assert sum(reply.startswith("📊 Получено:") for reply in replies) == 4  # none throttled
    assert len(await _deliveries(daily)) == 1


async def test_contacts_that_read_together_still_activate_once(make_daily: Factory) -> None:
    """The race the row lock is for, forced: five contacts that would all read
    the inactive subscription before any of them activates it."""
    daily = make_daily(gate=Gate(5))
    await _buyer(daily)
    await _library(daily, 3)
    async with daily.uow_factory() as uow:  # wrote earlier, with fewer purchases
        await uow.daily_subscriptions.record_first_interaction(BUYER, at=T0 - timedelta(days=3))
    await warm_pool(daily.uow_factory, 6)

    await _all_at_once(daily, "/start", times=5)

    replies = daily.bot.texts_to(BUYER)
    assert len(daily.bot.items_to(BUYER)) == 1
    assert replies.count(WELCOME_AT_1235) == 1
    assert sum(reply.startswith("📊 Получено:") for reply in replies) == 4
    assert len(await _deliveries(daily)) == 1


async def test_a_repeated_start_never_sends_another_item(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _library(daily, 5)
    await daily.send("/start")

    daily.clock.advance(hours=1)
    await daily.send("/start")
    daily.clock.advance(days=1)
    await daily.send("/start")
    await daily.send("anything at all")

    assert len(daily.bot.items_to(BUYER)) == 1
    assert len(await _deliveries(daily)) == 1


# --------------------------------------------------------------------------- #
# Status, pause, resume, help                                                 #
# --------------------------------------------------------------------------- #


async def test_the_status_says_what_was_received_and_what_is_left(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _library(daily, 4)
    await daily.send("/start")

    await daily.send(texts.STATUS_BUTTON)

    status = daily.bot.texts_to(BUYER)[-1]
    assert status == (
        "📊 Получено: 1 · Доступно новых: 3 · Окно: 11:35–13:35 МСК · Статус: активна"
    )


async def test_pause_and_resume(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _library(daily, 2)
    await daily.send("/start")

    await daily.send("/pause")
    paused_reply = [call for call in daily.bot.sent_to(BUYER) if isinstance(call, SendMessage)][-1]
    subscription = await _subscription(daily)
    assert subscription is not None
    assert subscription.status is DailySubscriptionStatus.PAUSED
    assert paused_reply.text == texts.PAUSED
    assert _keyboard_labels(paused_reply) == [texts.STATUS_BUTTON, texts.RESUME_BUTTON]

    await daily.send("/status")
    assert daily.bot.texts_to(BUYER)[-1].endswith("Статус: на паузе")

    await daily.send(texts.RESUME_BUTTON)
    resumed = await _subscription(daily)
    assert resumed is not None
    assert resumed.status is DailySubscriptionStatus.ACTIVE
    assert daily.bot.texts_to(BUYER)[-1] == texts.RESUMED

    await daily.send(texts.PAUSE_BUTTON)
    paused_again = await _subscription(daily)
    assert paused_again is not None
    assert paused_again.status is DailySubscriptionStatus.PAUSED


async def test_a_pause_leaves_the_open_delivery_to_the_engine(daily: DailyHarness) -> None:
    """Resumed before the window: today's item goes. Still paused: it is cancelled."""
    await _buyer(daily)
    await _buyer(daily, BUYER + 1)
    await _library(daily, 4)
    await daily.send("/start")
    await daily.send("/start", BUYER + 1)

    daily.clock.advance(days=1)  # tomorrow, at the centre of both windows
    engine = daily.services.delivery
    for user_id in (BUYER, BUYER + 1):
        subscription = await _subscription(daily, user_id)
        assert subscription is not None
        assert await engine.open_due(subscription, now=daily.clock.now) is not None
    await daily.send("/pause")
    await daily.send("/resume")
    await daily.send("/pause", BUYER + 1)

    reports = {report.user_id: report.step for report in await engine.process_due()}

    assert reports == {BUYER: DeliveryStep.SENT, BUYER + 1: DeliveryStep.CANCELLED}
    assert len(daily.bot.items_to(BUYER)) == 2
    assert len(daily.bot.items_to(BUYER + 1)) == 1


async def test_help_lists_the_commands(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _library(daily, 1)
    await daily.send("/start")

    await daily.send("/help")

    assert daily.bot.texts_to(BUYER)[-1] == texts.HELP


# --------------------------------------------------------------------------- #
# Blocked, unblocked, groups                                                  #
# --------------------------------------------------------------------------- #


async def test_blocking_and_unblocking_the_bot_follow_the_subscription(
    daily: DailyHarness,
) -> None:
    await _buyer(daily)
    await _library(daily, 1)
    await daily.send("/start")
    sent_before = len(daily.bot.calls)

    await daily.feed(membership_update(user=buyer(BUYER), status="kicked"))
    blocked = await _subscription(daily)
    assert blocked is not None
    assert blocked.is_blocked

    await daily.feed(membership_update(user=buyer(BUYER), status="member"))
    unblocked = await _subscription(daily)
    assert unblocked is not None
    assert not unblocked.is_blocked
    assert len(daily.bot.calls) == sent_before  # quietly


async def test_writing_again_after_a_block_unblocks(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _library(daily, 1)
    await daily.send("/start")
    await daily.feed(membership_update(user=buyer(BUYER), status="kicked"))

    await daily.send("/status")

    subscription = await _subscription(daily)
    assert subscription is not None
    assert not subscription.is_blocked


async def test_groups_are_ignored_and_left(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _library(daily, 1)
    group = -100500

    await daily.feed(
        message_update("/start", user=buyer(BUYER), chat_type="supergroup", chat_id=group)
    )
    await daily.feed(
        membership_update(user=buyer(BUYER), status="member", chat_type="group", chat_id=group)
    )

    assert await _subscription(daily) is None
    assert daily.bot.sent_to(group) == []
    (leave,) = [call for call in daily.bot.calls if isinstance(call, LeaveChat)]
    assert leave.chat_id == group


# --------------------------------------------------------------------------- #
# Who gets nothing                                                            #
# --------------------------------------------------------------------------- #


async def test_with_daily_content_off_nothing_is_recorded(make_daily: Factory) -> None:
    daily = make_daily(enabled=False)
    await _buyer(daily)
    await _library(daily, 1)

    await daily.send("/start")
    await daily.feed(membership_update(user=buyer(BUYER), status="kicked"))

    assert daily.bot.texts_to(BUYER) == ["Раздел скоро откроется."]
    assert await _subscription(daily) is None
    assert await _deliveries(daily) == []


async def test_a_beta_lets_in_only_its_buyers(make_daily: Factory) -> None:
    daily = make_daily(beta_user_ids=(BUYER + 1,))
    await _buyer(daily)
    await _buyer(daily, BUYER + 1)
    await _library(daily, 2)

    await daily.send("/start")
    await daily.send("/start", BUYER + 1)

    assert daily.bot.texts_to(BUYER) == ["Раздел скоро откроется."]
    assert await _subscription(daily) is None
    invited = await _subscription(daily, BUYER + 1)
    assert invited is not None
    assert invited.status is DailySubscriptionStatus.ACTIVE
    assert len(daily.bot.items_to(BUYER + 1)) == 1


async def test_the_owner_is_never_a_subscriber(daily: DailyHarness) -> None:
    await _buyer(daily, OWNER, completed=20)
    await _library(daily, 1)

    await daily.send("/start", OWNER)
    await daily.send("/status", OWNER)

    assert daily.bot.texts_to(OWNER) == [texts.OWNER_HELP, texts.OWNER_HELP]
    assert await _subscription(daily, OWNER) is None
    assert daily.bot.items_to(OWNER) == []


async def test_the_owner_gets_the_owner_note_even_while_daily_content_is_off(
    make_daily: Factory,
) -> None:
    daily = make_daily(enabled=False)

    await daily.send("/start", OWNER)

    assert daily.bot.texts_to(OWNER) == [texts.OWNER_HELP]


async def test_somebody_who_never_used_the_shop_is_told_and_not_recorded(
    daily: DailyHarness,
) -> None:
    await daily.send("/start", 424242)

    assert daily.bot.texts_to(424242) == [
        "Daily-контент открывается с 15-й покупки. Сейчас у вас 0 из 15."
    ]
    assert await _subscription(daily, 424242) is None
    async with daily.uow_factory() as uow:
        assert await uow.users.get(424242) is None  # the Daily bot never writes profiles


# --------------------------------------------------------------------------- #
# When the first item cannot go                                               #
# --------------------------------------------------------------------------- #


async def test_an_empty_library_says_items_will_come(daily: DailyHarness) -> None:
    await _buyer(daily)

    await daily.send("/start")

    assert daily.bot.texts_to(BUYER) == [
        WELCOME_AT_1235,
        "Материалы скоро появятся — пришлём автоматически.",
    ]
    (delivery,) = await _deliveries(daily)
    assert delivery.status is DailyDeliveryStatus.NO_CONTENT  # Phase E picks it up
    assert delivery.content_id is None
    assert delivery.deadline_at > daily.clock.now


async def test_a_blocked_bot_on_the_first_item_marks_the_buyer(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _library(daily, 2)
    daily.bot.failures[SendVideo] = [
        TelegramForbiddenError(
            method=SendVideo(chat_id=BUYER, video="x"),
            message="Forbidden: bot was blocked by the user",
        )
    ]

    await daily.send("/start")

    subscription = await _subscription(daily)
    assert subscription is not None
    assert subscription.is_blocked
    (delivery,) = await _deliveries(daily)
    assert delivery.status is DailyDeliveryStatus.FAILED
    assert daily.bot.items_to(BUYER) == []


async def test_a_blocked_bot_on_the_welcome_sends_nothing_more(daily: DailyHarness) -> None:
    await _buyer(daily)
    await _library(daily, 2)
    daily.bot.failures[SendMessage] = [
        TelegramForbiddenError(
            method=SendMessage(chat_id=BUYER, text="x"),
            message="Forbidden: bot was blocked by the user",
        )
    ]

    await daily.send("/start")

    subscription = await _subscription(daily)
    assert subscription is not None
    assert subscription.status is DailySubscriptionStatus.ACTIVE
    assert subscription.is_blocked
    assert daily.bot.items_to(BUYER) == []
