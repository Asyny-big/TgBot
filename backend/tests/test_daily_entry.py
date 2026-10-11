"""The way into VIP Daily from the shop bot: "👑 Открыть VIP Daily".

The button is offered under the VIP notice and on "🏆 My progress", and only
to a buyer the Daily bot would serve: Daily Content on, the Daily bot's
username configured, the buyer in the beta if one runs, and 15 completed
purchases by the loyalty programme's own count. It is a deep link that opens
the Daily bot with ``/start vip`` — which the Daily bot takes as any ``/start``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest
from aiogram.methods import SendMessage, SendVideo
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.factory import create_checkout, create_dispatcher
from app.bot.keyboards import ProgressCallback, daily_entry_keyboard, progress_keyboard
from app.bot.texts import DAILY_ENTRY_BUTTON, LOYALTY_MILESTONE_NOTICES, PROGRESS_SECTION_BUTTON
from app.core.config import DailySettings, LoyaltySettings
from app.daily_bot import texts as daily_texts
from app.domain.commands import DailyContentDraft
from app.domain.daily import DAILY_ACCESS_MILESTONE, daily_bot_link
from app.domain.enums import DailyContentKind
from app.domain.loyalty import DISCOUNT_MILESTONE, FREE_PACK_MILESTONE, VIP_MILESTONE
from app.infrastructure.telegram.notifications import TelegramLoyaltyNotifier
from app.services.daily_entry import DailyEntry
from tests.api_harness import api_settings, build_api_harness
from tests.bot_harness import (
    RecordingBot,
    make_user,
    progress_button_update,
    progress_command_update,
)
from tests.daily_bot_harness import DAILY_BOT_USER, Clock, build_daily_harness, seed_buyer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from redis.asyncio import Redis

    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.engine import Database
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

OWNER = 9990
BUYER = 9991
OUTSIDER = 9992
DAILY_BOT = "VipDailyBot"
LINK = "https://t.me/VipDailyBot?start=vip"
MIDDAY = datetime(2026, 10, 12, 9, 35, tzinfo=UTC)  # 12:35 in Moscow


def _daily(**overrides: Any) -> DailySettings:
    values: dict[str, Any] = {"enabled": True, "bot_username": DAILY_BOT}
    values.update(overrides)
    return DailySettings(**values)


def _buttons(markup: object) -> list[InlineKeyboardButton]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [button for row in markup.inline_keyboard for button in row]


# --------------------------------------------------------------------------- #
# Who is offered the button                                                   #
# --------------------------------------------------------------------------- #


def test_the_link_opens_the_daily_bot_with_start_vip() -> None:
    assert daily_bot_link(DAILY_BOT) == LINK


def test_a_vip_buyer_is_offered_the_daily_bot() -> None:
    entry = DailyEntry(_daily())

    assert entry.link_for(BUYER, completed_purchases=DAILY_ACCESS_MILESTONE) == LINK
    assert entry.link_for(BUYER, completed_purchases=40) == LINK


@pytest.mark.parametrize(
    ("settings", "completed", "user_id"),
    [
        (_daily(enabled=False), 15, BUYER),
        (_daily(bot_username=None), 15, BUYER),
        (_daily(), DAILY_ACCESS_MILESTONE - 1, BUYER),
        (_daily(), 0, BUYER),
        (_daily(beta_user_ids=(OUTSIDER,)), 15, BUYER),
    ],
    ids=["daily-off", "no-username", "fourteen", "none", "outside-the-beta"],
)
def test_no_button_unless_the_daily_bot_would_serve_the_buyer(
    settings: DailySettings,
    completed: int,
    user_id: int,
) -> None:
    assert DailyEntry(settings).link_for(user_id, completed_purchases=completed) is None


def test_a_beta_buyer_is_offered_the_daily_bot() -> None:
    entry = DailyEntry(_daily(beta_user_ids=(BUYER, OUTSIDER)))

    assert entry.link_for(BUYER, completed_purchases=15) == LINK


# --------------------------------------------------------------------------- #
# Where it is shown                                                           #
# --------------------------------------------------------------------------- #


def test_the_milestone_keyboard_carries_the_link_only_when_given() -> None:
    plain = _buttons(progress_keyboard())
    vip = _buttons(progress_keyboard(daily_link=LINK))

    assert [button.text for button in plain] == [PROGRESS_SECTION_BUTTON]
    assert [(button.text, button.url) for button in vip] == [
        (PROGRESS_SECTION_BUTTON, None),
        (DAILY_ENTRY_BUTTON, LINK),
    ]
    assert vip[0].callback_data == ProgressCallback().pack()
    assert daily_entry_keyboard(None) is None
    assert [(button.text, button.url) for button in _buttons(daily_entry_keyboard(LINK))] == [
        (DAILY_ENTRY_BUTTON, LINK),
    ]


@pytest.mark.parametrize(
    ("milestone", "daily", "expected"),
    [
        (VIP_MILESTONE, _daily(), [PROGRESS_SECTION_BUTTON, DAILY_ENTRY_BUTTON]),
        (VIP_MILESTONE, _daily(enabled=False), [PROGRESS_SECTION_BUTTON]),
        (VIP_MILESTONE, _daily(bot_username=None), [PROGRESS_SECTION_BUTTON]),
        (VIP_MILESTONE, None, [PROGRESS_SECTION_BUTTON]),
        (FREE_PACK_MILESTONE, _daily(), [PROGRESS_SECTION_BUTTON]),
        (DISCOUNT_MILESTONE, _daily(), [PROGRESS_SECTION_BUTTON]),
    ],
    ids=["vip", "vip-daily-off", "vip-no-username", "no-daily-wired", "ten", "five"],
)
async def test_the_vip_notice_offers_the_daily_bot(
    milestone: int,
    daily: DailySettings | None,
    expected: list[str],
) -> None:
    bot = RecordingBot()
    notifier = TelegramLoyaltyNotifier(
        bot,
        daily=DailyEntry(daily) if daily is not None else None,
    )
    try:
        await notifier.notify_milestone(BUYER, milestone)
    finally:
        await bot.session.close()

    (notice,) = bot.methods(SendMessage)
    assert notice.text == LOYALTY_MILESTONE_NOTICES[milestone]
    buttons = _buttons(notice.reply_markup)
    assert [button.text for button in buttons] == expected
    if DAILY_ENTRY_BUTTON in expected:
        assert buttons[-1].url == LINK


# --------------------------------------------------------------------------- #
# The "🏆 My progress" screen, through the shop bot's dispatcher              #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def shop_bot(
    live_database: Database,
    live_locks: RedisLockManager,
    redis_client: Redis,
    migrated_database: str,
) -> AsyncIterator[Callable[..., Any]]:
    """The shop bot over the live stores, built for given Daily settings."""
    built: list[Any] = []

    def build(daily: DailySettings) -> Any:
        settings = api_settings(
            migrated_database,
            loyalty=LoyaltySettings(enabled=True),
            daily=daily,
        )
        harness, client = build_api_harness(
            settings=settings,
            database=live_database,
            locks=live_locks,
            redis=redis_client,
        )
        bot = RecordingBot()
        checkout = create_checkout(harness.container, bot)
        dispatcher = create_dispatcher(harness.container, checkout)
        built.append((harness, client, bot))
        return harness.container, dispatcher, bot

    yield build
    for harness, client, bot in built:
        await client.aclose()
        await harness.bot.session.close()
        await bot.session.close()


@pytest.mark.parametrize(
    ("completed", "daily", "offered"),
    [
        (15, _daily(), True),
        (14, _daily(), False),
        (15, _daily(enabled=False), False),
        (15, _daily(bot_username=None), False),
        (15, _daily(beta_user_ids=(OUTSIDER,)), False),
    ],
    ids=["vip", "fourteen", "daily-off", "no-username", "outside-the-beta"],
)
async def test_the_progress_screen_offers_the_daily_bot_to_vip_buyers(
    shop_bot: Callable[..., Any],
    completed: int,
    daily: DailySettings,
    offered: bool,
) -> None:
    container, dispatcher, bot = shop_bot(daily)
    await seed_buyer(container.uow_factory, BUYER, completed=completed)
    user = make_user(BUYER)

    await dispatcher.feed_update(bot, progress_command_update(user=user))
    await dispatcher.feed_update(bot, progress_button_update(user=user))

    screens = bot.methods(SendMessage)
    assert len(screens) == 2
    for screen in screens:
        assert f"Покупок: {completed}" in screen.text
        if offered:
            assert [(button.text, button.url) for button in _buttons(screen.reply_markup)] == [
                (DAILY_ENTRY_BUTTON, LINK),
            ]
        else:
            assert screen.reply_markup is None


# --------------------------------------------------------------------------- #
# Where it leads                                                              #
# --------------------------------------------------------------------------- #


async def test_the_daily_bot_takes_start_vip_as_any_start(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    """The link opens the Daily bot itself, and its ``/start vip`` is a ``/start``:
    it activates a VIP buyer, answers a repeat with the status, and tells a
    buyer short of 15 how far they are."""
    assert urlsplit(LINK).path == f"/{DAILY_BOT_USER.username}"
    daily = build_daily_harness(
        live_uow_factory,
        live_locks,
        clock=Clock(MIDDAY),
        owner=OWNER,
        default_user=BUYER,
        bot_username=DAILY_BOT,
    )
    try:
        await seed_buyer(live_uow_factory, BUYER)
        await seed_buyer(live_uow_factory, OUTSIDER, completed=14)
        async with live_uow_factory() as uow:
            await uow.daily_content.add(
                DailyContentDraft(
                    kind=DailyContentKind.VIDEO,
                    source_chat_id=OWNER,
                    source_message_id=1,
                    added_by=OWNER,
                    file_id="video-file",
                    file_unique_id="video-unique",
                )
            )

        await daily.send("/start vip")
        await daily.send("/start vip")
        await daily.send("/start vip", OUTSIDER)
    finally:
        await daily.close()

    welcome, item, status = daily.bot.sent_to(BUYER)
    assert isinstance(welcome, SendMessage)
    assert welcome.text == daily_texts.welcome(12 * 60 + 35)
    assert isinstance(item, SendVideo)
    assert isinstance(status, SendMessage)
    assert str(status.text).startswith("📊 Получено: 1 ")
    assert daily.bot.texts_to(OUTSIDER) == [daily_texts.no_access(14)]
