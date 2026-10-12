"""The launch rehearsal: a shop already in business takes Daily Content on.

Staging-like, on the throwaway test PostgreSQL and Redis and nothing else. The
schema starts where production stands before the launch — 0005, with buyers
and their purchases in it — and climbs 0006 → 0007 → 0008 one revision at a
time. Then the launch is played the way docs/daily-content.md runs it; every
change of DAILY_* is a restart of every process with the new settings:

1. deployed and idle (DAILY_ENABLED=false): the owner fills the library, a
   buyer hears "soon", the worker sends nothing, no button anywhere;
2. the beta: the invited buyer completes purchases 14 and 15 through the
   shop's Stars checkout; the VIP notice and "🏆 My progress" offer
   "👑 Открыть VIP Daily", whose deep link opens the Daily bot — the welcome
   and the first item;
3. the next day: the worker's planner and sender bring the second item;
4. the full launch: the buyer the beta kept out comes in;
5. the switch off: nothing goes out, the button is gone, nothing is lost;
6. the switch on again: the next day's items go out.

Both bots are recording doubles: nothing reaches Telegram, and no credential
of a real bot is used anywhere.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qs, urlsplit

import pytest
from aiogram.methods import AnswerPreCheckoutQuery, SendInvoice, SendMessage, SendPhoto
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import NullPool, text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from app.bot.factory import create_checkout, create_dispatcher
from app.bot.texts import DAILY_ENTRY_BUTTON, LOYALTY_MILESTONE_NOTICES, PROGRESS_SECTION_BUTTON
from app.core.config import LoyaltySettings
from app.daily_bot import texts as daily_texts
from app.domain.commands import ProductDraft
from app.domain.daily import DeliveryStep
from app.domain.enums import DailySubscriptionStatus, PaymentProvider
from app.domain.loyalty import VIP_MILESTONE
from app.services.daily_planning import DailyPlanningService, PlanReport
from tests.api_harness import api_settings, build_api_harness
from tests.bot_harness import (
    RecordingBot,
    make_user,
    pay_button_update,
    pre_checkout_update,
    progress_command_update,
    start_update,
    successful_payment_update,
)
from tests.daily_bot_harness import (
    DAILY_BOT_USER,
    Clock,
    DailyHarness,
    build_daily_harness,
    buyer,
    owner_message,
    photo_sizes,
    update_of,
)
from tests.db import alembic_config

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import httpx
    from aiogram import Dispatcher
    from aiogram.types import Update
    from alembic.config import Config
    from redis.asyncio import Redis

    from app.core.container import Container
    from app.domain.entities import DailySubscription
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.engine import Database
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory
    from app.services.daily_delivery import DeliveryReport
    from tests.api_harness import ApiHarness

OWNER: Final = 9700
VIP: Final = 9701
"""Invited to the beta; 13 purchases before the launch, 14 and 15 during it."""

OUTSIDER: Final = 9702
"""Kept out by the beta; 15 purchases before the launch."""

PURCHASES_BEFORE_THE_LAUNCH: Final = {VIP: 13, OUTSIDER: 15}
DAILY_BOT: Final = "VipDailyBot"
LINK: Final = "https://t.me/VipDailyBot?start=vip"
DAY_ONE: Final = datetime(2026, 10, 12, 9, 35, tzinfo=UTC)  # 12:35 in Moscow
ANCHOR: Final = 12 * 60 + 35
LIBRARY: Final = ("dawn", "noon", "dusk", "night")
DAILY_TABLES: Final = {
    "0006": ("daily_subscriptions", "daily_content", "daily_deliveries", "daily_delivery_attempts"),
    "0007": ("daily_outbox", "daily_alerts"),
    "0008": ("daily_requests", "daily_request_items", "daily_request_events"),
}

_update_ids = itertools.count(70_000)


# --------------------------------------------------------------------------- #
# The schema, before and after                                                #
# --------------------------------------------------------------------------- #


async def _sql(dsn: str, statement: str, **params: Any) -> Any:
    """One statement in a transaction of its own; its first column, if any."""
    engine = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            result = await connection.execute(text(statement), params)
            return result.scalar() if result.returns_rows else None
    finally:
        await engine.dispose()


# Plain SQL naming only the columns 0005 has: the ORM models describe ``head``.
_PRODUCTS_BEFORE_THE_LAUNCH = (
    "INSERT INTO products (id, slug, title, delivery_url, price_stars) "
    "SELECT gen_random_uuid(), 'legacy-' || n, 'Pack ' || n, 'https://t.me/+legacy', 100 "
    "FROM generate_series(1, 15) AS n"
)
_PURCHASES_BEFORE_THE_LAUNCH = (
    "INSERT INTO purchases (id, user_id, product_id, provider, status, amount, currency, "
    "external_id, paid_at, delivered_at, delivered_url) "
    "SELECT gen_random_uuid(), CAST(:user_id AS bigint), id, 'stars', 'delivered', "
    "price_stars, 'XTR', gen_random_uuid()::text, now() - interval '30 days', "
    "now() - interval '30 days', delivery_url "
    "FROM products WHERE slug LIKE 'legacy-%' ORDER BY slug LIMIT CAST(:count AS bigint)"
)


async def _shop_in_business(dsn: str) -> None:
    """The shop as production has it at 0005: products, buyers, delivered purchases."""
    await _sql(dsn, _PRODUCTS_BEFORE_THE_LAUNCH)
    for user_id, count in PURCHASES_BEFORE_THE_LAUNCH.items():
        await _sql(dsn, "INSERT INTO users (telegram_id) VALUES (:user_id)", user_id=user_id)
        await _sql(dsn, _PURCHASES_BEFORE_THE_LAUNCH, user_id=user_id, count=count)


async def _delivered(dsn: str, user_id: int) -> int:
    statement = "SELECT count(*) FROM purchases WHERE user_id = :user_id AND status = 'delivered'"
    return int(await _sql(dsn, statement, user_id=user_id))


async def _climb(config: Config, dsn: str) -> None:
    """0006, 0007, 0008 one at a time, looking at the schema after every step."""
    every_table = [table for tables in DAILY_TABLES.values() for table in tables]
    created: list[str] = []
    for revision, tables in DAILY_TABLES.items():
        await asyncio.to_thread(command.upgrade, config, revision)
        created.extend(tables)
        assert await _sql(dsn, "SELECT version_num FROM alembic_version") == revision
        for table in every_table:
            exists = await _sql(dsn, "SELECT to_regclass(:name) IS NOT NULL", name=table)
            assert exists is (table in created), f"{table} at {revision}"


# --------------------------------------------------------------------------- #
# The site and its deployments                                                #
# --------------------------------------------------------------------------- #


@dataclass
class Deployment:
    """Every process of one deployment, started with one set of DAILY_*."""

    daily: DailyHarness
    shop: Dispatcher
    shop_bot: RecordingBot
    container: Container
    planning: DailyPlanningService

    async def to_shop(self, update: Update) -> None:
        await self.shop.feed_update(self.shop_bot, update)

    async def worker_turn(self) -> tuple[PlanReport, tuple[DeliveryReport, ...]]:
        """One turn of the worker's planner, then of its sender."""
        planned = await self.planning.plan()
        sent = await self.daily.services.delivery.process_due()
        return planned, sent


@dataclass
class Site:
    """The staging site: its stores, its clock, and every deployment made on it."""

    dsn: str
    database: Database
    uow_factory: SqlAlchemyUnitOfWorkFactory
    locks: RedisLockManager
    redis: Redis
    clock: Clock = field(default_factory=lambda: Clock(DAY_ONE))
    running: list[tuple[DailyHarness, ApiHarness, httpx.AsyncClient, RecordingBot]] = field(
        default_factory=list
    )

    def deploy(self, **daily: Any) -> Deployment:
        """Restart every process with these DAILY_* — all of them read one .env."""
        harness = build_daily_harness(
            self.uow_factory,
            self.locks,
            clock=self.clock,
            owner=OWNER,
            default_user=VIP,
            bot_username=DAILY_BOT,
            **daily,
        )
        settings = api_settings(
            self.dsn,
            loyalty=LoyaltySettings(enabled=True),
            daily=harness.settings,
        )
        api, client = build_api_harness(
            settings=settings,
            database=self.database,
            locks=self.locks,
            redis=self.redis,
        )
        shop_bot = RecordingBot()
        checkout = create_checkout(api.container, shop_bot)
        self.running.append((harness, api, client, shop_bot))
        return Deployment(
            daily=harness,
            shop=create_dispatcher(api.container, checkout),
            shop_bot=shop_bot,
            container=api.container,
            planning=DailyPlanningService(
                uow_factory=self.uow_factory,
                settings=harness.settings,
                clock=self.clock,
            ),
        )

    async def close(self) -> None:
        for harness, api, client, shop_bot in self.running:
            await harness.close()
            await client.aclose()
            await api.bot.session.close()
            await shop_bot.session.close()


@pytest.fixture
async def site(
    live_database: Database,
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
    redis_client: Redis,
    migrated_database: str,
) -> AsyncIterator[Site]:
    staging = Site(
        dsn=migrated_database,
        database=live_database,
        uow_factory=live_uow_factory,
        locks=live_locks,
        redis=redis_client,
    )
    try:
        yield staging
    finally:
        await staging.close()


# --------------------------------------------------------------------------- #
# What the buyer and the owner do, and what they see                          #
# --------------------------------------------------------------------------- #


async def _fill_the_library(deployment: Deployment) -> None:
    owner = buyer(OWNER, "owner")
    for tag in LIBRARY:
        await deployment.daily.feed(update_of(owner_message(user=owner, photo=photo_sizes(tag))))


async def _buy_with_stars(deployment: Deployment, user_id: int, number: int) -> list[SendMessage]:
    """One whole Stars purchase as Telegram drives it; what the buyer was sent."""
    product = await deployment.container.products.create(
        ProductDraft(
            slug=f"launch-{number}",
            title=f"Pack {number}",
            description="",
            delivery_url=f"https://t.me/+launch-{number}",
            price_stars=150,
        )
    )
    user = make_user(user_id)
    calls = deployment.shop_bot.calls
    first = len(calls)
    await deployment.to_shop(start_update(product.slug, user=user, update_id=next(_update_ids)))
    await deployment.to_shop(
        pay_button_update(
            provider=PaymentProvider.STARS,
            product_id=product.id,
            user=user,
            update_id=next(_update_ids),
        )
    )
    (invoice,) = [call for call in calls[first:] if isinstance(call, SendInvoice)]
    amount = sum(price.amount for price in invoice.prices)
    await deployment.to_shop(
        pre_checkout_update(
            payload=invoice.payload,
            amount=amount,
            user=user,
            update_id=next(_update_ids),
        )
    )
    (answer,) = [call for call in calls[first:] if isinstance(call, AnswerPreCheckoutQuery)]
    assert answer.ok is True
    await deployment.to_shop(
        successful_payment_update(
            payload=invoice.payload,
            amount=amount,
            charge_id=f"launch-charge-{number}",
            user=user,
            update_id=next(_update_ids),
        )
    )
    sent = [call for call in calls[first:] if isinstance(call, SendMessage)]
    assert any(product.delivery_url in str(message.text) for message in sent), "no pack"
    return sent


def _notices(sent: list[SendMessage]) -> list[SendMessage]:
    return [message for message in sent if message.text in LOYALTY_MILESTONE_NOTICES.values()]


async def _progress(deployment: Deployment, user_id: int) -> SendMessage:
    """The buyer's "🏆 My progress" screen as it is right now."""
    await deployment.to_shop(
        progress_command_update(user=make_user(user_id), update_id=next(_update_ids))
    )
    screen: SendMessage = deployment.shop_bot.methods(SendMessage)[-1]
    assert screen.chat_id == user_id
    return screen


def _buttons(message: SendMessage) -> list[tuple[str, str | None]]:
    if message.reply_markup is None:
        return []
    assert isinstance(message.reply_markup, InlineKeyboardMarkup)
    keyboard = message.reply_markup.inline_keyboard
    return [(button.text, button.url) for row in keyboard for button in row]


def _opened_from(link: str) -> str:
    """What the Daily bot receives when the buyer opens ``link`` in Telegram."""
    url = urlsplit(link)
    assert (url.scheme, url.netloc, url.path) == ("https", "t.me", f"/{DAILY_BOT_USER.username}")
    (payload,) = parse_qs(url.query)["start"]
    return f"/start {payload}"


def _photos(deployment: Deployment, user_id: int) -> list[str]:
    """The items one buyer received from this deployment's Daily bot, by file."""
    items = deployment.daily.bot.items_to(user_id)
    assert all(isinstance(item, SendPhoto) and item.protect_content is True for item in items)
    return [str(item.photo) for item in items if isinstance(item, SendPhoto)]


async def _subscription(deployment: Deployment, user_id: int) -> DailySubscription | None:
    async with deployment.daily.uow_factory() as uow:
        return await uow.daily_subscriptions.get(user_id)


def _sent(reports: tuple[DeliveryReport, ...]) -> list[tuple[int | None, DeliveryStep]]:
    return sorted(
        ((report.user_id, report.step) for report in reports),
        key=lambda pair: pair[0] or 0,
    )


# --------------------------------------------------------------------------- #
# The rehearsal, act by act                                                   #
# --------------------------------------------------------------------------- #

LIBRARY_FILES: Final = frozenset(f"{tag}-1280" for tag in LIBRARY)
"""What the owner's photos go out as: the largest size of each."""


async def _upgrade(site: Site) -> None:
    """A shop in business at 0005, then 0006 → 0007 → 0008: its history intact."""
    config = alembic_config(site.dsn)
    await asyncio.to_thread(command.downgrade, config, "0005")
    try:
        assert await _sql(site.dsn, "SELECT to_regclass('daily_subscriptions') IS NULL")
        await _shop_in_business(site.dsn)
        await _climb(config, site.dsn)
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
    await asyncio.to_thread(command.check, config)
    for user_id, count in PURCHASES_BEFORE_THE_LAUNCH.items():
        assert await _delivered(site.dsn, user_id) == count


async def _deployed_and_idle(idle: Deployment) -> None:
    """DAILY_ENABLED=false: the library fills; nobody else notices a thing."""
    await _fill_the_library(idle)
    assert idle.daily.bot.texts_to(OWNER) == [
        f"✅ Добавлено #{number} · фото · активных: {number}"
        for number in range(1, len(LIBRARY) + 1)
    ]
    await idle.daily.send("/start", OUTSIDER)
    assert idle.daily.bot.texts_to(OUTSIDER) == [daily_texts.DISABLED]
    assert await _subscription(idle, OUTSIDER) is None
    assert await idle.worker_turn() == (PlanReport(), ())
    screen = await _progress(idle, OUTSIDER)
    assert "Покупок: 15" in str(screen.text)
    assert _buttons(screen) == []
    assert idle.daily.bot.items() == []


async def _the_beta(beta: Deployment) -> str:
    """Purchases 14 and 15, the button, its deep link, the first item — returned."""
    assert _notices(await _buy_with_stars(beta, VIP, 14)) == []
    screen = await _progress(beta, VIP)
    assert "Покупок: 14" in str(screen.text)
    assert _buttons(screen) == []

    (notice,) = _notices(await _buy_with_stars(beta, VIP, 15))
    assert notice.text == LOYALTY_MILESTONE_NOTICES[VIP_MILESTONE]
    assert _buttons(notice) == [(PROGRESS_SECTION_BUTTON, None), (DAILY_ENTRY_BUTTON, LINK)]
    screen = await _progress(beta, VIP)
    assert "Покупок: 15" in str(screen.text)
    assert _buttons(screen) == [(DAILY_ENTRY_BUTTON, LINK)]

    # Fifteen purchases too, but outside the beta: no button, and no way in yet.
    assert _buttons(await _progress(beta, OUTSIDER)) == []
    await beta.daily.send(_opened_from(LINK), OUTSIDER)
    assert beta.daily.bot.texts_to(OUTSIDER) == [daily_texts.DISABLED]
    assert await _subscription(beta, OUTSIDER) is None

    await beta.daily.send(_opened_from(LINK), VIP)  # "👑 Открыть VIP Daily" pressed
    welcome, item = beta.daily.bot.sent_to(VIP)  # the welcome first, then the item
    assert isinstance(welcome, SendMessage)
    assert welcome.text == daily_texts.welcome(ANCHOR)
    assert isinstance(item, SendPhoto)
    (first,) = _photos(beta, VIP)
    subscription = await _subscription(beta, VIP)
    assert subscription is not None
    assert subscription.status is DailySubscriptionStatus.ACTIVE
    assert (subscription.anchor_minute, subscription.activated_at) == (ANCHOR, DAY_ONE)
    # Today's item is out: the worker opens nothing more for today.
    assert await beta.worker_turn() == (PlanReport(scanned=1, opened=0), ())
    return first


async def _the_next_day(beta: Deployment) -> str:
    """The worker alone brings the beta buyer's second item — returned."""
    planned, sent = await beta.worker_turn()
    assert planned == PlanReport(scanned=1, opened=1)
    assert _sent(sent) == [(VIP, DeliveryStep.SENT)]
    _, second = _photos(beta, VIP)
    return second


async def _the_full_launch(launch: Deployment) -> str:
    """The beta is over: the buyer it kept out comes in; their first item — returned."""
    assert _buttons(await _progress(launch, OUTSIDER)) == [(DAILY_ENTRY_BUTTON, LINK)]
    await launch.daily.send(_opened_from(LINK), OUTSIDER)
    assert launch.daily.bot.texts_to(OUTSIDER) == [daily_texts.welcome(ANCHOR)]
    (first,) = _photos(launch, OUTSIDER)
    return first


async def _switched_off(off: Deployment) -> None:
    """DAILY_ENABLED=false again: nothing goes out, the button is gone, nothing is lost."""
    assert await off.worker_turn() == (PlanReport(), ())
    await off.daily.send("/status", VIP)
    assert off.daily.bot.texts_to(VIP) == [daily_texts.DISABLED]
    assert off.daily.bot.items() == []
    assert _buttons(await _progress(off, VIP)) == []
    for user_id in (VIP, OUTSIDER):
        kept = await _subscription(off, user_id)
        assert kept is not None
        assert (kept.status, kept.anchor_minute) == (DailySubscriptionStatus.ACTIVE, ANCHOR)


async def _switched_on_again(again: Deployment) -> dict[int, str]:
    """The next day's items go out to both buyers — returned, by buyer."""
    planned, sent = await again.worker_turn()
    assert planned == PlanReport(scanned=2, opened=2)
    assert _sent(sent) == [(VIP, DeliveryStep.SENT), (OUTSIDER, DeliveryStep.SENT)]
    assert _buttons(await _progress(again, VIP)) == [(DAILY_ENTRY_BUTTON, LINK)]
    (to_vip,) = _photos(again, VIP)
    (to_outsider,) = _photos(again, OUTSIDER)
    return {VIP: to_vip, OUTSIDER: to_outsider}


async def test_the_launch_from_the_upgrade_to_the_switch_off_and_back(site: Site) -> None:
    await _upgrade(site)
    await _deployed_and_idle(site.deploy(enabled=False))

    beta = site.deploy(enabled=True, beta_user_ids=(VIP,))
    received: dict[int, list[str]] = {VIP: [await _the_beta(beta)]}
    site.clock.advance(days=1)
    received[VIP].append(await _the_next_day(beta))
    received[OUTSIDER] = [await _the_full_launch(site.deploy(enabled=True))]

    site.clock.advance(days=1)
    await _switched_off(site.deploy(enabled=False))

    site.clock.advance(days=1)
    for user_id, item in (await _switched_on_again(site.deploy(enabled=True))).items():
        received[user_id].append(item)

    # Every item is one of the owner's, and nobody ever got one twice.
    assert {user_id: len(set(items)) for user_id, items in received.items()} == {
        VIP: 3,
        OUTSIDER: 2,
    }
    assert {item for items in received.values() for item in items} <= LIBRARY_FILES
