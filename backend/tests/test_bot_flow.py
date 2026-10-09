"""Bot level tests on real PostgreSQL and real Redis.

Updates are fed through the real dispatcher; only Telegram's network seam and
Crypto Pay's HTTP API are replaced. Everything below — middlewares, filters,
handlers, services, locks, database — is the production code path.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    SendInvoice,
    SendMessage,
    SendPhoto,
)
from aiogram.types import InlineKeyboardMarkup
from aiohttp import web
from pydantic import SecretStr
from sqlalchemy import text as sql_text

from app.bot.factory import create_checkout, create_dispatcher
from app.bot.profile import profile_of
from app.bot.texts import (
    ALREADY_PURCHASED,
    BONUS_SECTION_BUTTON,
    CARD_NOT_FOUND,
    CARD_UNAVAILABLE,
    CRYPTO_INVOICE_CREATED,
    CRYPTO_INVOICE_RESUMED,
    DELIVERY_FAILED,
    FREE_PACK_ALREADY_USED,
    FREE_PACK_BUTTON,
    FREE_PACK_CARD_LINE,
    FREE_PACK_DELIVERY_FAILED,
    FREE_PACK_FAILED,
    FREE_PACK_GRANTED,
    FREE_PACK_NOT_AVAILABLE,
    FREE_PACK_PENDING_INVOICE,
    FREE_PACK_PROGRESS_HINT,
    FREE_PACK_RESENT,
    LOYALTY_MILESTONE_NOTICES,
    NO_DEEP_LINK,
    PAYMENT_IN_PROGRESS,
    PAYMENT_UNAVAILABLE,
    PRE_CHECKOUT_ALREADY_OWNED,
    PRE_CHECKOUT_UNKNOWN,
    PROGRESS_SECTION_BUTTON,
    PROGRESS_UNAVAILABLE,
    REFUND_NOTICE,
    SECTION_UNAVAILABLE,
    STARS_BUTTON,
    bonus_section,
    referral_welcome,
)
from app.bot.webhooks import register_cryptobot_webhook
from app.bot.workers import HousekeepingWorker, ReconciliationWorker
from app.core.config import (
    BotSettings,
    CryptoBotSettings,
    DeliverySettings,
    LoyaltySettings,
    PostgresSettings,
    RedisSettings,
    SecuritySettings,
    Settings,
    TelegramSettings,
)
from app.core.container import Container
from app.core.exceptions import (
    CheckoutAlreadyPendingError,
    InvalidSlugError,
    PaymentGatewayError,
    ServiceUnavailableError,
)
from app.domain.commands import (
    BonusTransactionDraft,
    ProductDraft,
    ProductUpdate,
    PurchaseDraft,
    UserDraft,
)
from app.domain.enums import BonusTransactionType, Currency, PaymentProvider, PurchaseStatus
from app.infrastructure.cache.rate_limit import RedisRateLimiter
from app.infrastructure.payments.cryptobot import SIGNATURE_HEADER, CryptoBotClient
from app.infrastructure.telegram.gateways import TelegramDeliveryGateway
from app.services.checkout import CheckoutService
from app.services.loyalty import LoyaltyService
from tests.bot_harness import (
    RecordingBot,
    bonus_button_update,
    bonus_choice_update,
    bonus_command_update,
    free_pack_button_update,
    make_user,
    pay_button_update,
    pre_checkout_update,
    progress_button_update,
    progress_command_update,
    refunded_payment_update,
    start_update,
    successful_payment_update,
)
from tests.settings_factory import build_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from aiogram import Dispatcher
    from aiogram.types import User as TelegramUser
    from redis.asyncio import Redis

    from app.domain.entities import Product
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.engine import Database

CRYPTO_TOKEN = "12345:cryptobot-test-token"  # noqa: S105
CRYPTO_INVOICE_ID = "770001"
CRYPTO_PAY_URL = "https://t.me/CryptoBot?start=IV0001"
DELIVERY_URL = "https://t.me/+private-invite"
STARS_PRICE = 150
USDT_PRICE = Decimal("5.00")


# --------------------------------------------------------------------------- #
# Fake Crypto Pay
# --------------------------------------------------------------------------- #
class FakeCryptoPay:
    """In-process Crypto Pay: records requests, serves scripted responses."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.states: dict[str, str] = {}
        self.lookups: list[list[str]] = []
        self.next_invoice_id = int(CRYPTO_INVOICE_ID)
        self.fail_create = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        if request.url.path.endswith("/createInvoice"):
            if self.fail_create:
                return httpx.Response(500, json={"ok": False, "error": "server error"})
            invoice_id = str(self.next_invoice_id)
            self.next_invoice_id += 1
            self.created.append(payload)
            self.states[invoice_id] = "active"
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "invoice_id": int(invoice_id),
                        "status": "active",
                        "bot_invoice_url": CRYPTO_PAY_URL,
                    },
                },
            )
        if request.url.path.endswith("/getInvoices"):
            requested = str(payload.get("invoice_ids", "")).split(",")
            self.lookups.append(requested)
            items = [
                {
                    "invoice_id": int(invoice_id),
                    "status": self.states[invoice_id],
                    "bot_invoice_url": CRYPTO_PAY_URL,
                }
                for invoice_id in requested
                if invoice_id in self.states
            ]
            return httpx.Response(200, json={"ok": True, "result": {"items": items}})
        message = f"unexpected Crypto Pay call: {request.url.path}"
        raise AssertionError(message)

    def mark_paid(self, invoice_id: str) -> None:
        self.states[invoice_id] = "paid"

    def mark_expired(self, invoice_id: str) -> None:
        self.states[invoice_id] = "expired"

    def webhook_body(self, invoice_id: str) -> bytes:
        return json.dumps(
            {
                "update_id": 1,
                "update_type": "invoice_paid",
                "request_date": "2026-07-30T10:00:00Z",
                "payload": {"invoice_id": int(invoice_id), "status": "paid"},
            }
        ).encode()

    @staticmethod
    def sign(body: bytes) -> str:
        secret = hashlib.sha256(CRYPTO_TOKEN.encode()).digest()
        return hmac.new(secret, body, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class BotHarness:
    """Everything a bot level test needs."""

    bot: RecordingBot
    dispatcher: Dispatcher
    container: Container
    checkout: CheckoutService
    crypto: FakeCryptoPay
    settings: Settings

    async def feed(self, update: Any) -> None:
        await self.dispatcher.feed_update(self.bot, update)


def _settings(dsn: str, *, throttle: float = 0.0, loyalty: bool = False) -> Settings:
    url = httpx.URL(dsn.replace("postgresql+asyncpg", "postgresql"))
    return build_settings(
        postgres=PostgresSettings(
            host=url.host,
            port=url.port or 5432,
            user=url.username,
            password=SecretStr(url.password),
            db=url.path.lstrip("/"),
        ),
        redis=RedisSettings(host="127.0.0.1", lock_ttl_seconds=10.0),
        telegram=TelegramSettings(
            bot_token=SecretStr("123456789:AAHfake-Test-Token_for_unit_tests_only01"),
            bot_username="MyShopBot",
            use_webhook=False,
            webhook_secret=SecretStr("webhook-secret-value"),
        ),
        cryptobot=CryptoBotSettings(api_token=SecretStr(CRYPTO_TOKEN), network="testnet"),
        bot=BotSettings(throttle_seconds=throttle, reconciliation_batch_size=10),
        delivery=DeliverySettings(max_attempts=3, initial_backoff_seconds=0.01),
        loyalty=LoyaltySettings(enabled=loyalty),
        security=SecuritySettings(
            jwt_secret=SecretStr("a" * 48),
            admin_username="administrator",
            admin_password=SecretStr("super-secret-password"),
        ),
    )


@pytest.fixture
def crypto_pay() -> FakeCryptoPay:
    return FakeCryptoPay()


@pytest.fixture
async def harness(
    live_database: Database,
    live_locks: RedisLockManager,
    redis_client: Redis,
    migrated_database: str,
    crypto_pay: FakeCryptoPay,
) -> AsyncIterator[BotHarness]:
    """A bot wired to the live database, live Redis and a fake Crypto Pay."""
    settings = _settings(migrated_database)
    async with _running_bot(settings, live_database, live_locks, redis_client, crypto_pay) as bot:
        yield bot


@pytest.fixture
async def loyalty_harness(
    live_database: Database,
    live_locks: RedisLockManager,
    redis_client: Redis,
    migrated_database: str,
    crypto_pay: FakeCryptoPay,
) -> AsyncIterator[BotHarness]:
    """The same bot with the loyalty programme switched on."""
    settings = _settings(migrated_database, loyalty=True)
    async with _running_bot(settings, live_database, live_locks, redis_client, crypto_pay) as bot:
        yield bot


@asynccontextmanager
async def _running_bot(
    settings: Settings,
    live_database: Database,
    live_locks: RedisLockManager,
    redis_client: Redis,
    crypto_pay: FakeCryptoPay,
) -> AsyncIterator[BotHarness]:
    container = _container(settings, live_database, live_locks, redis_client, crypto_pay)
    bot = RecordingBot()
    checkout = create_checkout(container, bot)
    dispatcher = create_dispatcher(container, checkout)
    try:
        yield BotHarness(
            bot=bot,
            dispatcher=dispatcher,
            container=container,
            checkout=checkout,
            crypto=crypto_pay,
            settings=settings,
        )
    finally:
        await bot.session.close()


def _container(
    settings: Settings,
    database: Database,
    locks: RedisLockManager,
    redis: Redis,
    crypto_pay: FakeCryptoPay,
) -> Container:
    """A container using the live infrastructure and the fake payment provider."""
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory  # noqa: PLC0415
    from app.services.auth import AuthService  # noqa: PLC0415
    from app.services.bonuses import BonusService  # noqa: PLC0415
    from app.services.loyalty import LoyaltyService  # noqa: PLC0415
    from app.services.pricing import PricingService  # noqa: PLC0415
    from app.services.products import ProductService  # noqa: PLC0415
    from app.services.purchases import PurchaseService  # noqa: PLC0415
    from app.services.referrals import ReferralService  # noqa: PLC0415
    from app.services.stats import StatsService  # noqa: PLC0415
    from tests.fakes import FakeRevocationStore  # noqa: PLC0415

    uow_factory = SqlAlchemyUnitOfWorkFactory(database)
    crypto_client = CryptoBotClient(
        settings.cryptobot,
        client=httpx.AsyncClient(
            base_url=settings.cryptobot.api_base_url,
            transport=httpx.MockTransport(crypto_pay.handler),
        ),
    )
    bonuses = BonusService(
        uow_factory=uow_factory,
        locks=locks,
        settings=settings.referral,
    )
    loyalty = LoyaltyService(uow_factory=uow_factory, settings=settings.loyalty)
    pricing = PricingService(uow_factory=uow_factory, bonuses=bonuses, loyalty=loyalty)
    return Container(
        settings=settings,
        uow_factory=uow_factory,
        locks=locks,
        rate_limiter=RedisRateLimiter(redis),
        crypto_payments=crypto_client,
        products=ProductService(uow_factory=uow_factory, telegram=settings.telegram),
        purchases=PurchaseService(
            uow_factory=uow_factory,
            locks=locks,
            pricing=pricing,
            loyalty=loyalty,
        ),
        referrals=ReferralService(
            uow_factory=uow_factory,
            telegram=settings.telegram,
            settings=settings.referral,
        ),
        bonuses=bonuses,
        pricing=pricing,
        loyalty=loyalty,
        stats=StatsService(uow_factory=uow_factory),
        auth=AuthService(settings.security, FakeRevocationStore()),
    )


async def _product(harness: BotHarness, **overrides: object) -> Product:
    values: dict[str, object] = {
        "slug": f"vip{uuid4().hex[:6]}",
        "title": "VIP access",
        "description": "Lifetime access",
        "delivery_url": DELIVERY_URL,
        "price_stars": STARS_PRICE,
        "price_usdt": USDT_PRICE,
    }
    values.update(overrides)
    return await harness.container.products.create(ProductDraft(**values))  # type: ignore[arg-type]


async def _stars_payload(harness: BotHarness) -> str:
    """The payload of the Stars invoice the bot just sent."""
    invoices = harness.bot.methods(SendInvoice)
    assert invoices, "no Stars invoice was sent"
    return str(invoices[-1].payload)


# --------------------------------------------------------------------------- #
# Opening a card creates nothing
# --------------------------------------------------------------------------- #
async def test_deep_link_shows_the_card_and_creates_no_purchase(harness: BotHarness) -> None:
    """The mandated rule: viewing a product bills nothing and records no purchase."""
    product = await _product(harness)

    await harness.feed(start_update(product.slug))

    text = harness.bot.last_text()
    assert "VIP access" in text
    assert "150 XTR" in text
    assert "5 USDT" in text
    # Buttons are offered, but nothing was created.
    keyboard = harness.bot.methods(SendMessage)[-1].reply_markup
    assert keyboard is not None
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert labels == ["⭐ Telegram Stars", "💎 CryptoBot (USDT)"]

    assert harness.bot.methods(SendInvoice) == []
    assert harness.crypto.created == []
    assert await harness.container.purchases.list_pending(PaymentProvider.STARS) == ()
    assert await harness.container.purchases.list_pending(PaymentProvider.CRYPTO) == ()
    overview = await harness.container.stats.overview()
    assert overview.total.purchases_count == 0
    # The visitor is remembered — that is a profile, not a payment record.
    assert overview.users_total == 1


async def test_a_card_with_a_photo_is_sent_as_a_photo(harness: BotHarness) -> None:
    product = await _product(harness, photo_file_id="photo-file-id")

    await harness.feed(start_update(product.slug))

    photos = harness.bot.methods(SendPhoto)
    assert len(photos) == 1
    assert photos[0].photo == "photo-file-id"
    assert "VIP access" in str(photos[0].caption)


async def test_start_without_a_deep_link_shows_no_catalog(harness: BotHarness) -> None:
    await _product(harness)

    await harness.feed(start_update(None))

    assert harness.bot.last_text() == NO_DEEP_LINK


@pytest.mark.parametrize("payload", ["unknown-slug", "not a slug", "x" * 80])
async def test_unknown_or_malformed_payloads_are_rejected(
    harness: BotHarness,
    payload: str,
) -> None:
    await harness.feed(start_update(payload))
    assert harness.bot.last_text() == CARD_NOT_FOUND


async def test_an_inactive_product_cannot_be_opened(harness: BotHarness) -> None:
    product = await _product(harness, is_active=False)

    await harness.feed(start_update(product.slug))
    assert harness.bot.last_text() == CARD_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Telegram Stars
# --------------------------------------------------------------------------- #
async def test_stars_purchase_end_to_end(harness: BotHarness) -> None:
    product = await _product(harness)
    user = make_user()

    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.STARS, product_id=product.id, user=user)
    )

    invoices = harness.bot.methods(SendInvoice)
    assert len(invoices) == 1
    invoice = invoices[0]
    assert invoice.currency == "XTR"
    assert invoice.prices[0].amount == STARS_PRICE
    payload = str(invoice.payload)

    pending = await harness.container.purchases.list_pending(PaymentProvider.STARS)
    assert len(pending) == 1
    assert pending[0].external_id == payload
    assert pending[0].status is PurchaseStatus.PENDING

    # Telegram asks whether it may charge.
    await harness.feed(pre_checkout_update(payload=payload, amount=STARS_PRICE, user=user))
    answers = harness.bot.methods(AnswerPreCheckoutQuery)
    assert [answer.ok for answer in answers] == [True]

    # The payment lands: confirm, then deliver.
    from tests.bot_harness import successful_payment_update  # noqa: PLC0415

    await harness.feed(successful_payment_update(payload=payload, amount=STARS_PRICE, user=user))

    settled = await harness.container.purchases.find_by_invoice(
        provider=PaymentProvider.STARS,
        external_id=payload,
    )
    assert settled is not None
    assert settled.status is PurchaseStatus.DELIVERED
    assert settled.telegram_charge_id == "charge-1"
    # The delivery is followed by a one-line referral invitation, so assert the
    # link was handed over rather than that it was the very last message. This
    # is the same style the repeat-delivery assertions in this file already use.
    assert [text for text in harness.bot.texts() if DELIVERY_URL in text]

    overview = await harness.container.stats.overview()
    assert overview.total.purchases_count == 1
    assert overview.total.stars_amount == STARS_PRICE


async def test_pre_checkout_rejects_an_unknown_or_already_paid_invoice(
    harness: BotHarness,
) -> None:
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))

    await harness.feed(pre_checkout_update(payload="ghost-payload", amount=STARS_PRICE, user=user))
    answers = harness.bot.methods(AnswerPreCheckoutQuery)
    assert answers[-1].ok is False
    assert answers[-1].error_message == PRE_CHECKOUT_UNKNOWN

    await harness.feed(
        pay_button_update(provider=PaymentProvider.STARS, product_id=product.id, user=user)
    )
    payload = await _stars_payload(harness)
    await harness.container.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id=payload,
    )

    await harness.feed(pre_checkout_update(payload=payload, amount=STARS_PRICE, user=user))
    answers = harness.bot.methods(AnswerPreCheckoutQuery)
    assert answers[-1].ok is False
    assert answers[-1].error_message == PRE_CHECKOUT_ALREADY_OWNED


async def test_a_replayed_stars_payment_delivers_once(harness: BotHarness) -> None:
    from tests.bot_harness import successful_payment_update  # noqa: PLC0415

    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.STARS, product_id=product.id, user=user)
    )
    payload = await _stars_payload(harness)

    for update_id in (10, 11, 12):
        await harness.feed(
            successful_payment_update(
                payload=payload,
                amount=STARS_PRICE,
                user=user,
                update_id=update_id,
            )
        )

    deliveries = [text for text in harness.bot.texts() if DELIVERY_URL in text]
    assert len(deliveries) == 1
    overview = await harness.container.stats.overview()
    assert overview.total.purchases_count == 1


async def test_a_stars_refund_revokes_access(harness: BotHarness) -> None:
    from tests.bot_harness import successful_payment_update  # noqa: PLC0415

    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.STARS, product_id=product.id, user=user)
    )
    payload = await _stars_payload(harness)
    await harness.feed(successful_payment_update(payload=payload, amount=STARS_PRICE, user=user))

    await harness.feed(refunded_payment_update(payload=payload, amount=STARS_PRICE, user=user))

    assert harness.bot.last_text() == REFUND_NOTICE
    purchase = await harness.container.purchases.find_by_invoice(
        provider=PaymentProvider.STARS,
        external_id=payload,
    )
    assert purchase is not None
    assert purchase.status is PurchaseStatus.REFUNDED
    assert await harness.container.purchases.find_owned(user.id, product.id) is None


# --------------------------------------------------------------------------- #
# CryptoBot
# --------------------------------------------------------------------------- #
async def test_crypto_purchase_end_to_end_via_webhook(harness: BotHarness) -> None:
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.CRYPTO, product_id=product.id, user=user)
    )

    assert len(harness.crypto.created) == 1
    assert harness.crypto.created[0]["amount"] == "5.00"
    assert harness.crypto.created[0]["asset"] == "USDT"
    assert CRYPTO_INVOICE_CREATED in harness.bot.texts()
    keyboard = harness.bot.methods(SendMessage)[-1].reply_markup
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].url == CRYPTO_PAY_URL

    pending = await harness.container.purchases.list_pending(PaymentProvider.CRYPTO)
    assert len(pending) == 1
    invoice_id = pending[0].external_id

    # Crypto Pay notifies the shop.
    client = await _webhook_client(harness)
    body = harness.crypto.webhook_body(invoice_id)
    response = await client.post(
        harness.settings.cryptobot.webhook_path,
        content=body,
        headers={SIGNATURE_HEADER: FakeCryptoPay.sign(body)},
    )
    assert response.status_code == HTTPStatus.OK

    purchase = await harness.container.purchases.find_by_invoice(
        provider=PaymentProvider.CRYPTO,
        external_id=invoice_id,
    )
    assert purchase is not None
    assert purchase.status is PurchaseStatus.DELIVERED
    # The delivery is followed by a one-line referral invitation, so assert the
    # link was handed over rather than that it was the very last message. This
    # is the same style the repeat-delivery assertions in this file already use.
    assert [text for text in harness.bot.texts() if DELIVERY_URL in text]


async def test_a_webhook_with_a_bad_signature_is_rejected(harness: BotHarness) -> None:
    client = await _webhook_client(harness)
    body = harness.crypto.webhook_body("999")

    response = await client.post(
        harness.settings.cryptobot.webhook_path,
        content=body,
        headers={SIGNATURE_HEADER: "deadbeef"},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED

    missing = await client.post(harness.settings.cryptobot.webhook_path, content=body)
    assert missing.status_code == HTTPStatus.UNAUTHORIZED


async def test_replayed_crypto_webhooks_deliver_once(harness: BotHarness) -> None:
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.CRYPTO, product_id=product.id, user=user)
    )
    invoice_id = (await harness.container.purchases.list_pending(PaymentProvider.CRYPTO))[
        0
    ].external_id

    client = await _webhook_client(harness)
    body = harness.crypto.webhook_body(invoice_id)
    signature = FakeCryptoPay.sign(body)
    for _ in range(4):
        response = await client.post(
            harness.settings.cryptobot.webhook_path,
            content=body,
            headers={SIGNATURE_HEADER: signature},
        )
        assert response.status_code == HTTPStatus.OK

    deliveries = [text for text in harness.bot.texts() if DELIVERY_URL in text]
    assert len(deliveries) == 1


async def test_a_lost_webhook_is_recovered_by_reconciliation(harness: BotHarness) -> None:
    """No notification ever arrives: the poller must still deliver the purchase."""
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.CRYPTO, product_id=product.id, user=user)
    )
    invoice_id = (await harness.container.purchases.list_pending(PaymentProvider.CRYPTO))[
        0
    ].external_id

    worker = ReconciliationWorker(
        purchases=harness.container.purchases,
        checkout=harness.checkout,
        crypto=harness.container.crypto_payments,
        settings=harness.settings.bot,
    )
    # Still unpaid at the provider: nothing happens.
    assert await worker.run_once() == 0

    harness.crypto.mark_paid(invoice_id)
    assert await worker.run_once() == 1

    purchase = await harness.container.purchases.find_by_invoice(
        provider=PaymentProvider.CRYPTO,
        external_id=invoice_id,
    )
    assert purchase is not None
    assert purchase.status is PurchaseStatus.DELIVERED
    # The delivery is followed by a one-line referral invitation, so assert the
    # link was handed over rather than that it was the very last message. This
    # is the same style the repeat-delivery assertions in this file already use.
    assert [text for text in harness.bot.texts() if DELIVERY_URL in text]

    # A second sweep must not deliver again.
    assert await worker.run_once() == 0
    assert len([text for text in harness.bot.texts() if DELIVERY_URL in text]) == 1


async def test_a_failing_provider_is_reported_to_the_buyer(harness: BotHarness) -> None:
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    harness.crypto.fail_create = True

    await harness.feed(
        pay_button_update(provider=PaymentProvider.CRYPTO, product_id=product.id, user=user)
    )

    answers = harness.bot.methods(AnswerCallbackQuery)
    assert answers[-1].text == PAYMENT_UNAVAILABLE
    assert await harness.container.purchases.list_pending(PaymentProvider.CRYPTO) == ()


async def test_a_hand_crafted_internal_pay_button_is_refused(harness: BotHarness) -> None:
    """``pay:internal:…`` must never fall through to "not Stars, so crypto"."""
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))

    await harness.feed(
        pay_button_update(provider=PaymentProvider.INTERNAL, product_id=product.id, user=user)
    )

    answer = harness.bot.methods(AnswerCallbackQuery)[-1]
    assert answer.text == CARD_UNAVAILABLE
    assert answer.show_alert is True
    assert harness.crypto.created == []
    assert harness.crypto.lookups == []
    assert harness.bot.methods(SendInvoice) == []
    for provider in PaymentProvider:
        if provider.is_payable:
            assert await harness.container.purchases.list_pending(provider) == ()


async def test_a_hand_crafted_internal_bonus_choice_is_refused(harness: BotHarness) -> None:
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))

    await harness.feed(
        bonus_choice_update(
            provider=PaymentProvider.INTERNAL,
            product_id=product.id,
            use_bonus=False,
            user=user,
        )
    )

    assert harness.bot.methods(AnswerCallbackQuery)[-1].text == CARD_UNAVAILABLE
    assert harness.crypto.created == []
    assert harness.bot.methods(SendInvoice) == []


async def test_pressing_crypto_again_hands_back_the_same_invoice(harness: BotHarness) -> None:
    """Bug #3: fifteen presses used to make fifteen invoices."""
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))

    for update_id in (31, 32, 33):
        await harness.feed(
            pay_button_update(
                provider=PaymentProvider.CRYPTO,
                product_id=product.id,
                user=user,
                update_id=update_id,
            )
        )

    assert len(harness.crypto.created) == 1
    assert len(await harness.container.purchases.list_pending(PaymentProvider.CRYPTO)) == 1
    assert harness.bot.texts().count(CRYPTO_INVOICE_CREATED) == 1
    assert harness.bot.texts().count(CRYPTO_INVOICE_RESUMED) == 2
    for sent in harness.bot.methods(SendMessage)[-3:]:
        assert sent.reply_markup.inline_keyboard[0][0].url == CRYPTO_PAY_URL


async def test_an_invoice_at_an_old_price_is_explained_not_doubled(harness: BotHarness) -> None:
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.CRYPTO, product_id=product.id, user=user)
    )
    await harness.container.products.update(
        product.id,
        ProductUpdate(price_usdt=Decimal("7.00")),
    )

    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.CRYPTO,
            product_id=product.id,
            user=user,
            update_id=41,
        )
    )

    assert len(harness.crypto.created) == 1
    notice = harness.bot.methods(SendMessage)[-1]
    assert "уже есть неоплаченный счёт" in notice.text
    assert "5 USDT" in notice.text
    assert notice.reply_markup.inline_keyboard[0][0].url == CRYPTO_PAY_URL


async def test_a_paid_but_unsettled_invoice_is_not_followed_by_another(
    harness: BotHarness,
) -> None:
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.CRYPTO, product_id=product.id, user=user)
    )
    (pending,) = await harness.container.purchases.list_pending(PaymentProvider.CRYPTO)
    harness.crypto.mark_paid(pending.external_id)

    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.CRYPTO,
            product_id=product.id,
            user=user,
            update_id=61,
        )
    )

    assert len(harness.crypto.created) == 1
    assert harness.bot.methods(AnswerCallbackQuery)[-1].text == PAYMENT_IN_PROGRESS


async def test_a_checkout_refused_by_the_database_is_answered_not_crashed(
    harness: BotHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uq_purchases_user_product_pending_crypto surfaces as "in progress", never a 500."""
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))

    async def lost_the_race(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise CheckoutAlreadyPendingError

    monkeypatch.setattr(CheckoutService, "start_crypto_checkout", lost_the_race)
    sent_before = len(harness.bot.methods(SendMessage))

    await harness.feed(
        pay_button_update(provider=PaymentProvider.CRYPTO, product_id=product.id, user=user)
    )

    assert harness.bot.methods(AnswerCallbackQuery)[-1].text == PAYMENT_IN_PROGRESS
    # No invoice link, no error text: just the short "in progress" answer.
    assert len(harness.bot.methods(SendMessage)) == sent_before


async def test_an_invoice_expired_at_crypto_pay_is_replaced(harness: BotHarness) -> None:
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.CRYPTO, product_id=product.id, user=user)
    )
    (first,) = await harness.container.purchases.list_pending(PaymentProvider.CRYPTO)
    harness.crypto.mark_expired(first.external_id)

    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.CRYPTO,
            product_id=product.id,
            user=user,
            update_id=51,
        )
    )

    assert len(harness.crypto.created) == 2
    assert (await harness.container.purchases.get(first.id)).status is PurchaseStatus.EXPIRED
    (second,) = await harness.container.purchases.list_pending(PaymentProvider.CRYPTO)
    assert second.id != first.id
    assert harness.bot.texts().count(CRYPTO_INVOICE_CREATED) == 2


async def test_housekeeping_expires_unpaid_invoices(harness: BotHarness) -> None:
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.CRYPTO, product_id=product.id, user=user)
    )

    worker = HousekeepingWorker(
        purchases=harness.container.purchases,
        settings=harness.settings.bot,
    )
    # The invoice is fresh, so nothing expires yet.
    assert await worker.run_once() == 0


# --------------------------------------------------------------------------- #
# Repeat visits, double presses, parallel buyers
# --------------------------------------------------------------------------- #
async def test_a_returning_buyer_gets_the_link_without_paying_again(
    harness: BotHarness,
) -> None:
    from tests.bot_harness import successful_payment_update  # noqa: PLC0415

    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.STARS, product_id=product.id, user=user)
    )
    payload = await _stars_payload(harness)
    await harness.feed(successful_payment_update(payload=payload, amount=STARS_PRICE, user=user))
    invoices_before = len(harness.bot.methods(SendInvoice))

    await harness.feed(start_update(product.slug, user=user, update_id=99))

    texts = harness.bot.texts()
    assert ALREADY_PURCHASED in texts
    assert len([text for text in texts if DELIVERY_URL in text]) == 2
    # No new invoice, no new purchase.
    assert len(harness.bot.methods(SendInvoice)) == invoices_before
    overview = await harness.container.stats.overview()
    assert overview.total.purchases_count == 1


async def test_double_press_of_the_pay_button_creates_one_invoice(harness: BotHarness) -> None:
    """Two simultaneous presses: the TTL bounded lock lets exactly one through."""
    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))

    await asyncio.gather(
        harness.feed(
            pay_button_update(
                provider=PaymentProvider.STARS,
                product_id=product.id,
                user=user,
                update_id=21,
            )
        ),
        harness.feed(
            pay_button_update(
                provider=PaymentProvider.STARS,
                product_id=product.id,
                user=user,
                update_id=22,
            )
        ),
    )

    assert len(harness.bot.methods(SendInvoice)) == 1
    assert len(await harness.container.purchases.list_pending(PaymentProvider.STARS)) == 1
    busy = [
        answer
        for answer in harness.bot.methods(AnswerCallbackQuery)
        if answer.text == PAYMENT_IN_PROGRESS
    ]
    assert len(busy) == 1


async def test_pressing_stars_then_crypto_pays_only_once(harness: BotHarness) -> None:
    """Both rails may be started, but only one payment can ever complete."""
    from tests.bot_harness import successful_payment_update  # noqa: PLC0415

    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.STARS, product_id=product.id, user=user)
    )
    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.CRYPTO,
            product_id=product.id,
            user=user,
            update_id=23,
        )
    )
    payload = await _stars_payload(harness)
    invoice_id = (await harness.container.purchases.list_pending(PaymentProvider.CRYPTO))[
        0
    ].external_id

    await harness.feed(successful_payment_update(payload=payload, amount=STARS_PRICE, user=user))

    # The crypto invoice is paid too, late: the shop must not sell the product twice.
    client = await _webhook_client(harness)
    body = harness.crypto.webhook_body(invoice_id)
    response = await client.post(
        harness.settings.cryptobot.webhook_path,
        content=body,
        headers={SIGNATURE_HEADER: FakeCryptoPay.sign(body)},
    )
    assert response.status_code == HTTPStatus.OK

    overview = await harness.container.stats.overview()
    assert overview.total.purchases_count == 1
    assert len([text for text in harness.bot.texts() if DELIVERY_URL in text]) == 1


async def test_many_buyers_purchase_in_parallel(harness: BotHarness) -> None:
    """Twenty buyers, one product, no interference."""
    from tests.bot_harness import successful_payment_update  # noqa: PLC0415

    product = await _product(harness)
    buyers = [make_user(telegram_id=5000 + index, username=f"buyer{index}") for index in range(20)]

    await asyncio.gather(
        *(
            harness.feed(start_update(product.slug, user=buyer, update_id=100 + index))
            for index, buyer in enumerate(buyers)
        )
    )
    await asyncio.gather(
        *(
            harness.feed(
                pay_button_update(
                    provider=PaymentProvider.STARS,
                    product_id=product.id,
                    user=buyer,
                    update_id=200 + index,
                )
            )
            for index, buyer in enumerate(buyers)
        )
    )

    invoices = harness.bot.methods(SendInvoice)
    assert len(invoices) == len(buyers)

    payload_by_chat = {int(invoice.chat_id): str(invoice.payload) for invoice in invoices}
    await asyncio.gather(
        *(
            harness.feed(
                successful_payment_update(
                    payload=payload_by_chat[buyer.id],
                    amount=STARS_PRICE,
                    charge_id=f"charge-{buyer.id}",
                    user=buyer,
                    update_id=300 + index,
                )
            )
            for index, buyer in enumerate(buyers)
        )
    )

    overview = await harness.container.stats.overview()
    assert overview.total.purchases_count == len(buyers)
    assert overview.total.stars_amount == STARS_PRICE * len(buyers)
    assert len([text for text in harness.bot.texts() if DELIVERY_URL in text]) == len(buyers)


# --------------------------------------------------------------------------- #
# Delivery retries at the Telegram seam
# --------------------------------------------------------------------------- #
async def test_delivery_retries_a_flood_wait_and_then_succeeds(harness: BotHarness) -> None:
    from tests.bot_harness import successful_payment_update  # noqa: PLC0415

    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.STARS, product_id=product.id, user=user)
    )
    payload = await _stars_payload(harness)

    harness.bot.fail_send_message = [
        TelegramRetryAfter(
            method=SendMessage(chat_id=user.id, text="x"),
            message="flood",
            retry_after=0,
        ),
    ]
    await harness.feed(successful_payment_update(payload=payload, amount=STARS_PRICE, user=user))

    purchase = await harness.container.purchases.find_by_invoice(
        provider=PaymentProvider.STARS,
        external_id=payload,
    )
    assert purchase is not None
    assert purchase.status is PurchaseStatus.DELIVERED
    # One attempt was throttled away, the retry landed: exactly one link reached
    # the buyer, and the purchase is marked delivered only once.
    assert len(harness.bot.failed_calls) == 1
    assert len([text for text in harness.bot.texts() if DELIVERY_URL in text]) == 1


async def test_a_blocked_bot_keeps_the_purchase_recoverable(harness: BotHarness) -> None:
    from tests.bot_harness import successful_payment_update  # noqa: PLC0415

    product = await _product(harness)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.STARS, product_id=product.id, user=user)
    )
    payload = await _stars_payload(harness)

    harness.bot.fail_send_message = [
        TelegramForbiddenError(
            method=SendMessage(chat_id=user.id, text="x"),
            message="Forbidden: bot was blocked by the user",
        ),
    ]
    await harness.feed(successful_payment_update(payload=payload, amount=STARS_PRICE, user=user))

    purchase = await harness.container.purchases.find_by_invoice(
        provider=PaymentProvider.STARS,
        external_id=payload,
    )
    assert purchase is not None
    assert purchase.status is PurchaseStatus.PAID
    assert DELIVERY_FAILED in harness.bot.texts()

    # The buyer unblocks the bot and opens the link again: delivery completes.
    await harness.feed(start_update(product.slug, user=user, update_id=98))
    settled = await harness.container.purchases.find_by_invoice(
        provider=PaymentProvider.STARS,
        external_id=payload,
    )
    assert settled is not None
    assert settled.status is PurchaseStatus.DELIVERED


async def test_a_permanent_telegram_error_is_not_retried(harness: BotHarness) -> None:
    """The delivery gateway classifies, the service obeys: one attempt only."""
    gateway = TelegramDeliveryGateway(harness.bot)
    harness.bot.fail_send_message = [
        TelegramForbiddenError(
            method=SendMessage(chat_id=1, text="x"),
            message="Forbidden: bot was blocked by the user",
        ),
    ]
    from app.core.exceptions import DeliveryPermanentError  # noqa: PLC0415
    from app.domain.delivery import DeliveryMessage  # noqa: PLC0415

    with pytest.raises(DeliveryPermanentError):
        await gateway.send(
            DeliveryMessage(
                chat_id=1,
                purchase_id=uuid4(),
                product_title="VIP",
                delivery_url=DELIVERY_URL,
            )
        )


async def test_the_stars_gateway_reports_a_refusal_as_a_gateway_error(
    harness: BotHarness,
) -> None:
    from aiogram.exceptions import TelegramBadRequest  # noqa: PLC0415

    from app.domain.payments import InvoiceRequest  # noqa: PLC0415
    from app.infrastructure.telegram.gateways import TelegramStarsInvoiceSender  # noqa: PLC0415

    class RefusingBot(RecordingBot):
        async def __call__(self, method: Any, request_timeout: int | None = None) -> Any:
            del request_timeout
            raise TelegramBadRequest(method=method, message="Bad Request: CURRENCY_INVALID")

    bot = RefusingBot()
    try:
        sender = TelegramStarsInvoiceSender(bot)
        with pytest.raises(PaymentGatewayError):
            await sender.send_invoice(
                InvoiceRequest(
                    user_id=1,
                    product_id=uuid4(),
                    title="VIP",
                    description="d",
                    amount=100,
                    currency=PaymentProvider.STARS.currency,
                    payload="payload",
                )
            )
    finally:
        await bot.session.close()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _webhook_client(harness: BotHarness) -> httpx.AsyncClient:
    """An HTTP client wired straight into the aiohttp webhook application."""
    app = web.Application()
    register_cryptobot_webhook(
        app,
        path=harness.settings.cryptobot.webhook_path,
        client=harness.container.crypto_payments,
        checkout=harness.checkout,
    )
    transport = _AiohttpTransport(app)
    return httpx.AsyncClient(transport=transport, base_url="http://bot")


class _AiohttpTransport(httpx.AsyncBaseTransport):
    """Routes httpx requests into an aiohttp application without a socket."""

    def __init__(self, app: web.Application) -> None:
        self._app = app
        self._handler_ready = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from aiohttp.test_utils import TestClient, TestServer  # noqa: PLC0415

        server = TestServer(self._app)
        client = TestClient(server)
        await client.start_server()
        try:
            response = await client.request(
                request.method,
                request.url.path,
                data=request.content,
                headers=dict(request.headers),
            )
            body = await response.read()
            return httpx.Response(response.status, content=body)
        finally:
            await client.close()


@pytest.fixture(autouse=True)
def _isolate_structlog_context() -> Iterator[None]:
    """Bot middlewares bind and clear context; keep tests independent."""
    import structlog  # noqa: PLC0415

    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


async def test_the_throttle_middleware_lets_one_action_through_at_a_time(
    live_locks: RedisLockManager,
) -> None:
    """Anti-flood is the same TTL bounded lock, so it always self-heals."""
    from app.bot.middlewares import ThrottleMiddleware  # noqa: PLC0415
    from tests.bot_harness import RecordingBot, make_message  # noqa: PLC0415

    middleware = ThrottleMiddleware(live_locks, window_seconds=5.0)
    bot = RecordingBot()
    user = make_user(telegram_id=6001)
    started = asyncio.Event()
    release = asyncio.Event()
    handled = 0

    async def handler(event: Any, data: dict[str, Any]) -> str:
        nonlocal handled
        del event, data
        handled += 1
        started.set()
        await release.wait()
        return "done"

    message = make_message(text="hi", user=user).as_(bot)
    data = {"event_from_user": user}

    try:
        first = asyncio.create_task(middleware(handler, message, data))
        await started.wait()
        # While the first action is in flight, the second is refused.
        second = await middleware(handler, message, data)
        assert second is None
        assert handled == 1

        release.set()
        assert await first == "done"

        # Once the first finished, the lock is free again.
        assert await middleware(handler, message, data) == "done"
        assert handled == 2
    finally:
        await bot.session.close()


async def test_a_throttled_buyer_is_told_to_slow_down(live_locks: RedisLockManager) -> None:
    from app.bot.middlewares import ThrottleMiddleware  # noqa: PLC0415
    from app.bot.texts import TOO_FAST  # noqa: PLC0415
    from tests.bot_harness import RecordingBot, make_message  # noqa: PLC0415

    middleware = ThrottleMiddleware(live_locks, window_seconds=5.0)
    user = make_user(telegram_id=6002)
    bot = RecordingBot()
    data = {"event_from_user": user}

    async def slow(event: Any, inner: dict[str, Any]) -> None:
        del event, inner
        await asyncio.sleep(0.2)

    message = make_message(text="hi", user=user).as_(bot)
    try:
        task = asyncio.create_task(middleware(slow, message, data))
        await asyncio.sleep(0.05)
        await middleware(slow, message, data)
        await task
        assert TOO_FAST in bot.texts()
    finally:
        await bot.session.close()


# --------------------------------------------------------------------------- #
# The webhook server as the container runs it
# --------------------------------------------------------------------------- #
async def test_the_webhook_server_serves_every_route_and_shuts_down_on_request(
    harness: BotHarness,
) -> None:
    """The production transport, end to end: bind, serve, stop, release the port.

    Covers what the container depends on and nothing else can prove: the liveness
    route answers, both provider routes exist and reject unauthenticated calls,
    and a shutdown request actually ends the process instead of leaving the port
    held.
    """
    from app.bot.runner import BotRuntime, run_webhook  # noqa: PLC0415
    from app.bot.webhooks import HEALTH_PATH  # noqa: PLC0415

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    settings = harness.settings.model_copy(
        update={
            "telegram": TelegramSettings(
                bot_token=SecretStr("123456789:AAHfake-Test-Token_for_unit_tests_only01"),
                bot_username="MyShopBot",
                use_webhook=True,
                webhook_base_url="https://shop.example.com",
                webhook_secret=SecretStr("webhook-secret-value"),
                drop_pending_updates=True,
            ),
            "bot": BotSettings(throttle_seconds=0.0, webhook_port=port),
        },
    )

    runtime = BotRuntime(
        settings=settings,
        container=harness.container,
        bot=harness.bot,
        dispatcher=harness.dispatcher,
        checkout=harness.checkout,
    )
    stop = asyncio.Event()
    server = asyncio.create_task(run_webhook(runtime, stop))
    base = f"http://127.0.0.1:{port}"
    try:
        async with asyncio.timeout(10):
            await _wait_until_listening(port)

        async with httpx.AsyncClient(base_url=base, timeout=5.0) as client:
            liveness = await client.get(HEALTH_PATH)
            assert liveness.status_code == 200
            assert liveness.json()["status"] == "alive"

            # Registered, and closed to anyone without the secret token.
            unauthorised = await client.post(
                settings.telegram.webhook_path,
                json={"update_id": 1},
            )
            assert unauthorised.status_code == 401

            # Registered, and closed to anyone without a valid signature.
            unsigned = await client.post(
                settings.cryptobot.webhook_path,
                json={"update_type": "invoice_paid"},
            )
            assert unsigned.status_code == 401

        stop.set()
        await asyncio.wait_for(server, timeout=10)
    finally:
        if not server.done():
            server.cancel()
            with suppress(asyncio.CancelledError):
                await server

    # The port is free again, so a restart cannot fail on "address in use".
    with socket.socket() as rebind:
        rebind.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        rebind.bind(("127.0.0.1", port))


async def _wait_until_listening(port: int) -> None:
    """Wait for the webhook server to accept connections."""
    while True:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            await asyncio.sleep(0.05)
            continue
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()
        return


async def test_long_polling_stops_when_the_process_is_asked_to(harness: BotHarness) -> None:
    """Ctrl+C locally, SIGTERM in a container: both must end the polling loop.

    Without this, `stop_polling` would never be called and the process would hang
    until it was killed, skipping every cleanup path.
    """
    from app.bot.runner import BotRuntime, run_polling  # noqa: PLC0415

    runtime = BotRuntime(
        settings=harness.settings,
        container=harness.container,
        bot=harness.bot,
        dispatcher=harness.dispatcher,
        checkout=harness.checkout,
    )
    stop = asyncio.Event()
    polling = asyncio.create_task(run_polling(runtime, stop))
    try:
        # Give the loop time to reach getUpdates before asking it to stop.
        await asyncio.sleep(0.1)
        assert not polling.done()

        stop.set()
        async with asyncio.timeout(10):
            await polling
    finally:
        if not polling.done():
            polling.cancel()
            with suppress(asyncio.CancelledError):
                await polling


# --------------------------------------------------------------------------- #
# A link that turned out to be broken
# --------------------------------------------------------------------------- #
async def test_a_rotated_link_is_what_a_re_delivery_sends(harness: BotHarness) -> None:
    """The buyer always receives the link the product carries *now*.

    The support case behind this: the buyer paid with Stars, the link was dead,
    and the next day the administrator fixed the product and pressed «Отправить
    ссылку». Sending the snapshot taken at the moment of the sale would hand the
    dead link over a second time, so delivery reads the product instead.
    """
    from app.domain.commands import ProductUpdate  # noqa: PLC0415
    from tests.bot_harness import successful_payment_update  # noqa: PLC0415

    broken = "https://example.com/broken-invite"
    working = "https://example.com/working-invite"

    product = await _product(harness, delivery_url=broken)
    user = make_user()
    await harness.feed(start_update(product.slug, user=user))
    await harness.feed(
        pay_button_update(provider=PaymentProvider.STARS, product_id=product.id, user=user)
    )
    payload = await _stars_payload(harness)
    await harness.feed(successful_payment_update(payload=payload, amount=STARS_PRICE, user=user))

    assert any(broken in text for text in harness.bot.texts())

    # The administrator fixes the product the next day.
    await harness.container.products.update(
        product.id,
        ProductUpdate(delivery_url=working),
    )

    # …and presses «Отправить ссылку» in the panel.
    owned = await harness.container.purchases.find_owned(user.id, product.id)
    assert owned is not None
    result = await harness.checkout.redeliver(owned.id)

    assert result.succeeded
    assert working in harness.bot.last_text()
    # The dead link is never sent again, by any path.
    assert sum(broken in text for text in harness.bot.texts()) == 1

    # The buyer opening the deep link again also gets the working one.
    await harness.feed(start_update(product.slug, user=user, update_id=77))
    assert working in harness.bot.last_text()
    assert sum(working in text for text in harness.bot.texts()) == 2

    # No second charge for any of it.
    overview = await harness.container.stats.overview()
    assert overview.total.purchases_count == 1


# --------------------------- Referral and bonuses --------------------------- #
#
# The bot's side of the feature: an invitation link never opens a product, the
# bonus section is reachable without one, and the shop's own deep-link path is
# untouched by any of it.


async def _referral_link_payload(harness: BotHarness, user: TelegramUser) -> str:
    """Open the bonus section as this user and take their invitation payload."""
    summary = await harness.container.referrals.summary(
        UserDraft(telegram_id=user.id, username=user.username),
    )
    return f"ref_{summary.referral_code}"


async def test_an_invitation_link_does_not_open_a_product(harness: BotHarness) -> None:
    """The core concept: a referral link records a relationship, nothing more.

    No card, no price, no payment button — and no product list either.
    """
    inviter = make_user(telegram_id=5101, username="inviter")
    invited = make_user(telegram_id=5102, username="invited")
    payload = await _referral_link_payload(harness, inviter)

    await harness.feed(start_update(payload, user=invited, update_id=90))

    assert harness.bot.methods(SendInvoice) == []
    assert harness.bot.last_text() == referral_welcome(
        discount_percent=harness.settings.referral.discount_percent,
        preview_available=False,
    )
    referral = await harness.container.referrals.referral_of(invited.id)
    assert referral is not None
    assert referral.referrer_user_id == inviter.id


async def test_an_invitation_offers_the_preview_directory_only(
    harness: BotHarness,
) -> None:
    """One configured URL, and no channel list anywhere in the bot."""
    inviter = make_user(telegram_id=5103, username="inviter3")
    invited = make_user(telegram_id=5104, username="invited4")
    payload = await _referral_link_payload(harness, inviter)

    await harness.feed(start_update(payload, user=invited, update_id=91))

    sent = harness.bot.methods(SendMessage)[-1]
    buttons = [
        button
        for row in (sent.reply_markup.inline_keyboard if sent.reply_markup else [])
        for button in row
    ]
    # No preview URL is configured in the test settings, so the button is absent
    # rather than broken — exactly the documented fallback.
    assert [button.url for button in buttons if button.url] == []
    assert [button.text for button in buttons] == [BONUS_SECTION_BUTTON]


async def test_an_unknown_referral_code_falls_through_to_the_product_lookup(
    harness: BotHarness,
) -> None:
    """A slug that happens to start with "ref_" must keep working.

    This is why an unresolvable code is not an error: the payload continues down
    the ordinary path, and a legacy product stays reachable. The row is written
    through the repository rather than the service, because that is the real
    situation being covered — a slug that predates the reservation rule.
    """
    async with harness.container.uow_factory() as uow:
        product = await uow.products.create(
            ProductDraft(
                slug="ref_legacy",
                title="Legacy item",
                description="Predates the referral feature",
                delivery_url=DELIVERY_URL,
                price_stars=STARS_PRICE,
            )
        )
    user = make_user(telegram_id=5105, username="legacy")

    await harness.feed(start_update("ref_legacy", user=user, update_id=92))

    assert product.title in harness.bot.last_text()


async def test_a_product_can_no_longer_be_created_in_the_referral_namespace(
    harness: BotHarness,
) -> None:
    """New slugs may not collide with an invitation payload."""
    with pytest.raises(InvalidSlugError):
        await _product(harness, slug="ref_newthing")


async def test_the_bonus_section_is_reachable_without_a_product_link(
    harness: BotHarness,
) -> None:
    """``/bonus`` works for somebody who has never bought anything.

    Anyone may invite, so the link must not be gated behind a purchase.
    """
    user = make_user(telegram_id=5106, username="curious")

    await harness.feed(bonus_command_update(user=user, update_id=93))

    summary = await harness.container.referrals.summary(
        UserDraft(telegram_id=user.id, username=user.username),
    )
    assert summary.balance == 0
    assert harness.bot.last_text() == bonus_section(
        balance=0,
        invited_count=0,
        referral_purchase_count=0,
        referral_link=harness.container.referrals.link_for(summary.referral_code),
        reward_percent=harness.settings.referral.reward_percent,
    )


async def test_the_bonus_section_never_calls_a_balance_telegram_stars(
    harness: BotHarness,
) -> None:
    """Bonuses are an internal balance, and the wording must not promise Stars."""
    user = make_user(telegram_id=5107, username="wording")

    await harness.feed(bonus_command_update(user=user, update_id=94))

    assert "Telegram Stars" not in harness.bot.last_text()


async def test_the_bonus_button_opens_the_section(harness: BotHarness) -> None:
    user = make_user(telegram_id=5108, username="presser")

    await harness.feed(bonus_button_update(user=user, update_id=95))

    assert "?start=ref_" in harness.bot.last_text()


async def test_a_bare_start_still_refuses_to_be_a_catalogue(harness: BotHarness) -> None:
    """The no-deep-link answer is unchanged; only a bonus button is offered."""
    user = make_user(telegram_id=5109, username="wanderer")

    await harness.feed(start_update(None, user=user, update_id=96))

    sent = harness.bot.methods(SendMessage)[-1]
    assert sent.text == NO_DEEP_LINK
    buttons = [
        button
        for row in (sent.reply_markup.inline_keyboard if sent.reply_markup else [])
        for button in row
    ]
    assert len(buttons) == 1


async def test_an_invited_buyer_pays_the_discounted_price_through_the_bot(
    harness: BotHarness,
) -> None:
    """The whole invited-buyer journey, driven through real updates."""
    inviter = make_user(telegram_id=5110, username="inviter10")
    invited = make_user(telegram_id=5111, username="invited11")
    payload = await _referral_link_payload(harness, inviter)
    await harness.feed(start_update(payload, user=invited, update_id=97))

    product = await _product(harness, price_stars=1000, price_usdt=None)
    await harness.feed(start_update(product.slug, user=invited, update_id=98))
    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.STARS,
            product_id=product.id,
            user=invited,
            update_id=99,
        )
    )

    invoice = harness.bot.methods(SendInvoice)[-1]
    assert invoice.prices[0].amount == 900


async def test_an_invited_buyer_can_actually_pay_the_discounted_price_with_crypto(
    harness: BotHarness,
) -> None:
    """The regression that reached production, driven the way a buyer hits it.

    The card offered 4.50, but the CryptoBot invoice was raised at the list
    5.00, and ``start_purchase`` then refused to record a purchase at a price
    other than the invoiced one. The buyer saw only "не удалось создать счёт на
    оплату" and could not buy anything at all with crypto while their discount
    was unspent.

    The Stars rail was never affected, which is why this went unnoticed: the
    Stars invoice is created *after* the purchase, so there are never two
    numbers to disagree.
    """
    inviter = make_user(telegram_id=5130, username="inviter30")
    invited = make_user(telegram_id=5131, username="invited31")
    payload = await _referral_link_payload(harness, inviter)
    await harness.feed(start_update(payload, user=invited, update_id=140))

    product = await _product(harness, price_stars=500, price_usdt=Decimal("5.00"))
    await harness.feed(start_update(product.slug, user=invited, update_id=141))
    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.CRYPTO,
            product_id=product.id,
            user=invited,
            update_id=142,
        )
    )

    # The buyer is not told to come back in a minute.
    alerts = [answer.text for answer in harness.bot.methods(AnswerCallbackQuery)]
    assert PAYMENT_UNAVAILABLE not in alerts
    assert CRYPTO_INVOICE_CREATED in harness.bot.texts()

    # The provider was billed the discounted amount, once.
    assert len(harness.crypto.created) == 1
    assert harness.crypto.created[0]["amount"] == "4.50"

    # And the purchase was recorded at exactly that, with the breakdown intact.
    pending = await harness.container.purchases.list_pending(PaymentProvider.CRYPTO)
    assert len(pending) == 1
    assert pending[0].amount == Decimal("4.50")
    assert pending[0].base_amount == Decimal("5.00")
    assert pending[0].discount_amount == Decimal("0.50")


async def test_an_invited_buyer_sees_the_same_crypto_price_on_the_card_and_the_invoice(
    harness: BotHarness,
) -> None:
    """Shown, invoiced, recorded — three numbers that must be one number.

    Each is computed by a different collaborator (the card by
    ``PurchaseService``, the invoice by ``preview``, the row by ``resolve``),
    so this is the seam where a discount can go missing.
    """
    inviter = make_user(telegram_id=5132, username="inviter32")
    invited = make_user(telegram_id=5133, username="invited33")
    payload = await _referral_link_payload(harness, inviter)
    await harness.feed(start_update(payload, user=invited, update_id=150))

    product = await _product(harness, price_stars=500, price_usdt=Decimal("12.35"))
    card = await harness.container.purchases.open_card(
        profile_of(invited),
        product.slug,
    )
    shown = next(
        option.discounted_amount
        for option in card.options
        if option.provider is PaymentProvider.CRYPTO
    )

    await harness.feed(start_update(product.slug, user=invited, update_id=151))
    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.CRYPTO,
            product_id=product.id,
            user=invited,
            update_id=152,
        )
    )

    pending = await harness.container.purchases.list_pending(PaymentProvider.CRYPTO)
    invoiced = Decimal(harness.crypto.created[0]["amount"])

    # 12.35 less 10%, floored to a chargeable cent.
    assert shown == Decimal("11.12")
    assert invoiced == shown
    assert pending[0].amount == shown


async def test_a_buyer_with_bonuses_is_asked_before_an_invoice_exists(
    harness: BotHarness,
) -> None:
    """The question comes first, and pressing ⭐ alone bills nothing.

    The invariant the shop already had — an invoice exists only because a button
    was pressed — is preserved: the bonus prompt is a message, not an invoice.
    """
    inviter = make_user(telegram_id=5112, username="inviter12")
    invited = make_user(telegram_id=5113, username="invited13")
    payload = await _referral_link_payload(harness, inviter)
    await harness.feed(start_update(payload, user=invited, update_id=100))

    # The invited buyer buys once, which credits the inviter.
    earner = await _product(harness, price_stars=1000, price_usdt=None)
    await harness.feed(start_update(earner.slug, user=invited, update_id=101))
    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.STARS,
            product_id=earner.id,
            user=invited,
            update_id=102,
        )
    )
    payload_id = harness.bot.methods(SendInvoice)[-1].payload
    await harness.feed(
        successful_payment_update(
            payload=payload_id,
            amount=900,
            charge_id="charge-referral-1",
            user=invited,
            update_id=103,
        )
    )
    assert await harness.container.bonuses.balance_of(inviter.id) == 135

    # Now the inviter buys, and is asked about their balance first.
    product = await _product(harness, price_stars=1000, price_usdt=None)
    await harness.feed(start_update(product.slug, user=inviter, update_id=104))
    invoices_before = len(harness.bot.methods(SendInvoice))
    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.STARS,
            product_id=product.id,
            user=inviter,
            update_id=105,
        )
    )

    assert len(harness.bot.methods(SendInvoice)) == invoices_before
    prompt = harness.bot.last_text()
    assert "135" in prompt
    assert "865" in prompt

    # Answering "use them" issues an invoice for the reduced amount.
    await harness.feed(
        bonus_choice_update(
            provider=PaymentProvider.STARS,
            product_id=product.id,
            use_bonus=True,
            user=inviter,
            update_id=106,
        )
    )
    assert harness.bot.methods(SendInvoice)[-1].prices[0].amount == 865


async def test_declining_bonuses_bills_the_full_price(harness: BotHarness) -> None:
    inviter = make_user(telegram_id=5114, username="inviter14")
    invited = make_user(telegram_id=5115, username="invited15")
    payload = await _referral_link_payload(harness, inviter)
    await harness.feed(start_update(payload, user=invited, update_id=110))
    earner = await _product(harness, price_stars=1000, price_usdt=None)
    await harness.feed(start_update(earner.slug, user=invited, update_id=111))
    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.STARS,
            product_id=earner.id,
            user=invited,
            update_id=112,
        )
    )
    await harness.feed(
        successful_payment_update(
            payload=harness.bot.methods(SendInvoice)[-1].payload,
            amount=900,
            charge_id="charge-referral-2",
            user=invited,
            update_id=113,
        )
    )

    product = await _product(harness, price_stars=1000, price_usdt=None)
    await harness.feed(start_update(product.slug, user=inviter, update_id=114))
    await harness.feed(
        bonus_choice_update(
            provider=PaymentProvider.STARS,
            product_id=product.id,
            use_bonus=False,
            user=inviter,
            update_id=115,
        )
    )

    assert harness.bot.methods(SendInvoice)[-1].prices[0].amount == 1000
    assert await harness.container.bonuses.balance_of(inviter.id) == 135


async def test_a_buyer_without_bonuses_is_never_asked(harness: BotHarness) -> None:
    """No balance, no extra question: the original two-tap flow is preserved."""
    user = make_user(telegram_id=5116, username="plain")
    product = await _product(harness, price_stars=1000, price_usdt=None)
    await harness.feed(start_update(product.slug, user=user, update_id=120))

    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.STARS,
            product_id=product.id,
            user=user,
            update_id=121,
        )
    )

    assert harness.bot.methods(SendInvoice)[-1].prices[0].amount == 1000


# --------------------------------------------------------------------------- #
# Loyalty: "🏆 My progress" (read only)
# --------------------------------------------------------------------------- #
async def _seed_received_packs(harness: BotHarness, user_id: int, count: int) -> None:
    """``count`` delivered packs, one product each, as a historical buyer has."""
    async with harness.container.uow_factory() as uow:
        await uow.users.upsert(UserDraft(telegram_id=user_id))
        for _ in range(count):
            product = await uow.products.create(
                ProductDraft(
                    slug=f"pack{uuid4().hex[:8]}",
                    title="Pack",
                    description="",
                    delivery_url=DELIVERY_URL,
                    price_stars=STARS_PRICE,
                )
            )
            paid = await uow.purchases.create(
                PurchaseDraft(
                    user_id=user_id,
                    product_id=product.id,
                    provider=PaymentProvider.STARS,
                    amount=Decimal(STARS_PRICE),
                    currency=Currency.XTR,
                    external_id=uuid4().hex,
                    status=PurchaseStatus.PAID,
                )
            )
            await uow.purchases.mark_delivered(paid.id, delivered_url=DELIVERY_URL)


async def _database_snapshot(harness: BotHarness) -> tuple[Any, ...]:
    async with harness.container.uow_factory() as uow:
        row = (
            await uow.session.execute(
                sql_text(
                    "SELECT"
                    " (SELECT count(*) FROM loyalty_rewards),"
                    " (SELECT count(*) FROM purchases),"
                    " (SELECT md5(coalesce(string_agg(id::text || status::text,"
                    "   ',' ORDER BY id), '')) FROM purchases),"
                    " (SELECT md5(coalesce(string_agg(telegram_id::text || last_seen_at::text,"
                    "   ',' ORDER BY telegram_id), '')) FROM users)"
                )
            )
        ).one()
    return tuple(row)


def _labels(message: SendMessage) -> list[str]:
    markup = message.reply_markup
    rows = markup.inline_keyboard if isinstance(markup, InlineKeyboardMarkup) else []
    return [button.text for row in rows for button in row]


async def test_progress_is_invisible_while_the_programme_is_off(harness: BotHarness) -> None:
    """LOYALTY_ENABLED=false: no button, no command, and a pressed button reveals nothing."""
    user = make_user(telegram_id=5201, username="offbuyer")
    await _seed_received_packs(harness, user.id, 7)

    await harness.feed(start_update(None, user=user, update_id=201))
    assert _labels(harness.bot.methods(SendMessage)[-1]) == [BONUS_SECTION_BUTTON]

    sent_before = len(harness.bot.methods(SendMessage))
    await harness.feed(progress_command_update(user=user, update_id=202))
    # Not handled at all — exactly what the bot did before the section existed.
    assert len(harness.bot.methods(SendMessage)) == sent_before

    await harness.feed(progress_button_update(user=user, update_id=203))
    assert harness.bot.methods(AnswerCallbackQuery)[-1].text == SECTION_UNAVAILABLE
    assert len(harness.bot.methods(SendMessage)) == sent_before
    assert not [text for text in harness.bot.texts() if "Покупок" in text]


async def test_the_start_menu_offers_progress_when_the_programme_is_on(
    loyalty_harness: BotHarness,
) -> None:
    user = make_user(telegram_id=5202, username="onbuyer")

    await loyalty_harness.feed(start_update(None, user=user, update_id=211))

    sent = loyalty_harness.bot.methods(SendMessage)[-1]
    assert sent.text == NO_DEEP_LINK
    assert _labels(sent) == [BONUS_SECTION_BUTTON, PROGRESS_SECTION_BUTTON]


async def test_the_progress_button_shows_the_buyers_own_progress(
    loyalty_harness: BotHarness,
) -> None:
    user = make_user(telegram_id=5203, username="seven")
    await _seed_received_packs(loyalty_harness, user.id, 7)

    await loyalty_harness.feed(progress_button_update(user=user, update_id=221))

    assert loyalty_harness.bot.methods(AnswerCallbackQuery)[-1].text is None
    lines = [line for line in loyalty_harness.bot.last_text().split("\n") if line]
    assert lines == [
        "🏆 <b>Мой прогресс</b>",
        "Покупок: 7",
        "🎉 Скидка 50% доступна",
        "До бесплатного пака: 3 покупки",
        "До VIP: 8 покупок",
    ]


async def test_the_progress_command_shows_a_vip(loyalty_harness: BotHarness) -> None:
    user = make_user(telegram_id=5204, username="vip")
    await _seed_received_packs(loyalty_harness, user.id, 18)

    await loyalty_harness.feed(progress_command_update(user=user, update_id=231))

    text = loyalty_harness.bot.last_text()
    assert "Покупок: 18" in text
    assert "🎉 Скидка 50% доступна" in text
    assert "🎁 Бесплатный пак доступен" in text
    assert "👑 VIP" in text
    # Nothing to press: using a reward is not part of this screen.
    assert loyalty_harness.bot.methods(SendMessage)[-1].reply_markup is None


async def test_the_bonus_section_is_unaffected_by_the_programme(
    loyalty_harness: BotHarness,
) -> None:
    user = make_user(telegram_id=5205, username="bonuses")

    await loyalty_harness.feed(bonus_command_update(user=user, update_id=241))

    text = loyalty_harness.bot.last_text()
    assert "?start=ref_" in text
    assert "Мой прогресс" not in text


async def test_a_hand_crafted_progress_callback_reveals_nobody_elses_progress(
    loyalty_harness: BotHarness,
) -> None:
    """The callback carries no user id; a forged one with a payload is not even matched."""
    owner = make_user(telegram_id=5206, username="owner")
    snoop = make_user(telegram_id=5207, username="snoop")
    await _seed_received_packs(loyalty_harness, owner.id, 7)
    sent_before = len(loyalty_harness.bot.methods(SendMessage))

    await loyalty_harness.feed(
        progress_button_update(user=snoop, update_id=251, data=f"progress:{owner.id}")
    )
    assert len(loyalty_harness.bot.methods(SendMessage)) == sent_before

    await loyalty_harness.feed(progress_button_update(user=snoop, update_id=252))
    sent = loyalty_harness.bot.methods(SendMessage)[-1]
    assert sent.chat_id == snoop.id
    assert "Покупок: 0" in sent.text
    assert not [text for text in loyalty_harness.bot.texts() if "Покупок: 7" in text]


async def test_opening_progress_repeatedly_changes_nothing(loyalty_harness: BotHarness) -> None:
    user = make_user(telegram_id=5208, username="repeat")
    await _seed_received_packs(loyalty_harness, user.id, 12)
    before = await _database_snapshot(loyalty_harness)

    for update_id in range(261, 266):
        await loyalty_harness.feed(progress_button_update(user=user, update_id=update_id))
    await loyalty_harness.feed(progress_command_update(user=user, update_id=266))

    assert await _database_snapshot(loyalty_harness) == before
    assert before[0] == 0


async def test_a_failing_progress_read_is_a_soft_error(
    loyalty_harness: BotHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The section is an extra: a failure reads as "try later", never a broken bot."""
    user = make_user(telegram_id=5209, username="unlucky")

    async def unavailable(*args: object, **kwargs: object) -> object:
        del args, kwargs
        message = "the database is resting"
        raise ServiceUnavailableError(message)

    monkeypatch.setattr(LoyaltyService, "get_progress", unavailable)

    await loyalty_harness.feed(progress_button_update(user=user, update_id=271))

    assert loyalty_harness.bot.last_text() == PROGRESS_UNAVAILABLE


# --------------------------------------------------------------------------- #
# Loyalty: the 50% discount through the bot
# --------------------------------------------------------------------------- #
async def _seed_bonus_balance(harness: BotHarness, user_id: int, units: int) -> None:
    async with harness.container.uow_factory() as uow:
        await uow.bonuses.add(
            BonusTransactionDraft(
                user_id=user_id,
                amount=units,
                type=BonusTransactionType.REFERRAL_REWARD,
            )
        )


async def test_the_card_shows_the_loyalty_price(loyalty_harness: BotHarness) -> None:
    user = make_user(telegram_id=5301, username="loyal")
    await _seed_received_packs(loyalty_harness, user.id, 5)
    product = await _product(loyalty_harness)

    await loyalty_harness.feed(start_update(product.slug, user=user, update_id=301))

    text = loyalty_harness.bot.last_text()
    assert "<s>150</s> 75 XTR" in text
    assert "<s>5</s> 2.5 USDT" in text
    assert "🏆 Скидка 50% по программе лояльности" in text


async def test_no_bonus_question_while_the_loyalty_discount_applies(
    loyalty_harness: BotHarness,
) -> None:
    user = make_user(telegram_id=5302, username="loyalbonus")
    await _seed_received_packs(loyalty_harness, user.id, 5)
    await _seed_bonus_balance(loyalty_harness, user.id, 300)
    product = await _product(loyalty_harness)
    await loyalty_harness.feed(start_update(product.slug, user=user, update_id=311))

    await loyalty_harness.feed(
        pay_button_update(
            provider=PaymentProvider.STARS,
            product_id=product.id,
            user=user,
            update_id=312,
        )
    )

    invoices = loyalty_harness.bot.methods(SendInvoice)
    assert len(invoices) == 1
    assert invoices[0].prices[0].amount == 75
    assert not [text for text in loyalty_harness.bot.texts() if "бонусов" in text]


async def test_a_replaced_discounted_stars_invoice_is_refused_at_pre_checkout(
    loyalty_harness: BotHarness,
) -> None:
    """Decision B through the bot: the old invoice cannot be paid, the new one can."""
    user = make_user(telegram_id=5303, username="repress")
    await _seed_received_packs(loyalty_harness, user.id, 5)
    product = await _product(loyalty_harness)
    await loyalty_harness.feed(start_update(product.slug, user=user, update_id=321))

    for update_id in (322, 323):
        await loyalty_harness.feed(
            pay_button_update(
                provider=PaymentProvider.STARS,
                product_id=product.id,
                user=user,
                update_id=update_id,
            )
        )
    old, new = loyalty_harness.bot.methods(SendInvoice)
    assert old.prices[0].amount == new.prices[0].amount == 75

    await loyalty_harness.feed(
        pre_checkout_update(payload=old.payload, amount=75, user=user, update_id=324)
    )
    refused = loyalty_harness.bot.methods(AnswerPreCheckoutQuery)[-1]
    assert refused.ok is False
    assert refused.error_message == PRE_CHECKOUT_UNKNOWN

    await loyalty_harness.feed(
        pre_checkout_update(payload=new.payload, amount=75, user=user, update_id=325)
    )
    assert loyalty_harness.bot.methods(AnswerPreCheckoutQuery)[-1].ok is True
    await loyalty_harness.feed(
        successful_payment_update(
            payload=new.payload,
            amount=75,
            charge_id="charge-loyal",
            user=user,
            update_id=326,
        )
    )

    assert len([text for text in loyalty_harness.bot.texts() if DELIVERY_URL in text]) == 1
    async with loyalty_harness.container.uow_factory() as uow:
        rewards = await uow.loyalty.rewards_of(user.id)
    assert [(reward.milestone, reward.status.value) for reward in rewards] == [(5, "consumed")]


async def test_an_expired_plain_stars_invoice_still_passes_pre_checkout(
    loyalty_harness: BotHarness,
) -> None:
    """Only discounted invoices are refused once expired; nothing else changed."""
    user = make_user(telegram_id=5304, username="plainlate")
    product = await _product(loyalty_harness)
    await loyalty_harness.feed(start_update(product.slug, user=user, update_id=331))
    await loyalty_harness.feed(
        pay_button_update(
            provider=PaymentProvider.STARS,
            product_id=product.id,
            user=user,
            update_id=332,
        )
    )
    (invoice,) = loyalty_harness.bot.methods(SendInvoice)
    await loyalty_harness.container.purchases.expire_stale(
        now=datetime.now(UTC) + timedelta(hours=2)
    )

    await loyalty_harness.feed(
        pre_checkout_update(payload=invoice.payload, amount=150, user=user, update_id=333)
    )

    assert loyalty_harness.bot.methods(AnswerPreCheckoutQuery)[-1].ok is True


# --------------------------------------------------------------------------- #
# Loyalty: the free pack through the bot
# --------------------------------------------------------------------------- #
async def _internal_purchase_count(harness: BotHarness, user_id: int) -> int:
    async with harness.container.uow_factory() as uow:
        count = await uow.session.scalar(
            sql_text(
                "SELECT count(*) FROM purchases WHERE user_id = :user AND provider = 'internal'"
            ),
            {"user": user_id},
        )
    return int(count or 0)


async def test_the_card_offers_the_free_pack_above_the_payment_buttons(
    loyalty_harness: BotHarness,
) -> None:
    user = make_user(telegram_id=5401, username="tenpacks")
    await _seed_received_packs(loyalty_harness, user.id, 10)
    product = await _product(loyalty_harness)

    await loyalty_harness.feed(start_update(product.slug, user=user, update_id=401))

    card = loyalty_harness.bot.methods(SendMessage)[-1]
    assert FREE_PACK_CARD_LINE in card.text
    labels = _labels(card)
    assert labels[0] == FREE_PACK_BUTTON
    assert STARS_BUTTON in labels[1:]
    assert len(labels) == 3  # free pack, Stars, CryptoBot


async def test_pressing_take_it_for_free_delivers_the_pack(loyalty_harness: BotHarness) -> None:
    user = make_user(telegram_id=5402, username="claimer")
    await _seed_received_packs(loyalty_harness, user.id, 10)
    product = await _product(loyalty_harness)
    await loyalty_harness.feed(start_update(product.slug, user=user, update_id=411))

    await loyalty_harness.feed(
        free_pack_button_update(product_id=product.id, user=user, update_id=412)
    )

    assert loyalty_harness.bot.methods(AnswerCallbackQuery)[-1].text == FREE_PACK_GRANTED
    assert len([text for text in loyalty_harness.bot.texts() if DELIVERY_URL in text]) == 1
    assert loyalty_harness.bot.methods(SendInvoice) == []
    assert await _internal_purchase_count(loyalty_harness, user.id) == 1

    # Pressing the old button again sends the link again — no second pack.
    await loyalty_harness.feed(
        free_pack_button_update(product_id=product.id, user=user, update_id=413)
    )
    assert loyalty_harness.bot.methods(AnswerCallbackQuery)[-1].text == FREE_PACK_RESENT
    assert len([text for text in loyalty_harness.bot.texts() if DELIVERY_URL in text]) == 2
    assert await _internal_purchase_count(loyalty_harness, user.id) == 1

    await loyalty_harness.feed(progress_command_update(user=user, update_id=414))
    progress = loyalty_harness.bot.last_text()
    assert "Покупок: 11" in progress
    assert "✅ Бесплатный пак получен" in progress
    assert FREE_PACK_PROGRESS_HINT not in progress


async def test_the_progress_screen_says_where_to_claim_the_pack(
    loyalty_harness: BotHarness,
) -> None:
    user = make_user(telegram_id=5403, username="hinted")
    await _seed_received_packs(loyalty_harness, user.id, 10)

    await loyalty_harness.feed(progress_command_update(user=user, update_id=421))

    assert FREE_PACK_PROGRESS_HINT in loyalty_harness.bot.last_text()
    assert loyalty_harness.bot.methods(SendMessage)[-1].reply_markup is None


async def test_the_free_pack_is_invisible_while_the_programme_is_off(harness: BotHarness) -> None:
    user = make_user(telegram_id=5404, username="offclaimer")
    await _seed_received_packs(harness, user.id, 18)
    product = await _product(harness)

    await harness.feed(start_update(product.slug, user=user, update_id=431))
    card = harness.bot.methods(SendMessage)[-1]
    assert FREE_PACK_BUTTON not in _labels(card)
    assert FREE_PACK_CARD_LINE not in card.text

    await harness.feed(free_pack_button_update(product_id=product.id, user=user, update_id=432))
    assert harness.bot.methods(AnswerCallbackQuery)[-1].text == SECTION_UNAVAILABLE
    assert await _internal_purchase_count(harness, user.id) == 0
    assert not [text for text in harness.bot.texts() if DELIVERY_URL in text]


async def test_a_hand_crafted_claim_below_the_milestone_is_refused(
    loyalty_harness: BotHarness,
) -> None:
    user = make_user(telegram_id=5405, username="tooearly")
    await _seed_received_packs(loyalty_harness, user.id, 3)
    product = await _product(loyalty_harness)

    await loyalty_harness.feed(
        free_pack_button_update(product_id=product.id, user=user, update_id=441)
    )

    answer = loyalty_harness.bot.methods(AnswerCallbackQuery)[-1]
    assert answer.text == FREE_PACK_NOT_AVAILABLE
    assert answer.show_alert is True
    assert await _internal_purchase_count(loyalty_harness, user.id) == 0


async def test_a_free_pack_that_could_not_be_sent_is_handed_over_later(
    loyalty_harness: BotHarness,
) -> None:
    user = make_user(telegram_id=5406, username="blocked")
    await _seed_received_packs(loyalty_harness, user.id, 10)
    product = await _product(loyalty_harness)
    await loyalty_harness.feed(start_update(product.slug, user=user, update_id=451))

    loyalty_harness.bot.fail_send_message = [
        TelegramForbiddenError(
            method=SendMessage(chat_id=user.id, text="x"),
            message="Forbidden: bot was blocked by the user",
        ),
    ]
    await loyalty_harness.feed(
        free_pack_button_update(product_id=product.id, user=user, update_id=452)
    )
    assert FREE_PACK_DELIVERY_FAILED in loyalty_harness.bot.texts()
    assert not [text for text in loyalty_harness.bot.texts() if DELIVERY_URL in text]

    # The deep link again: the pack is owned, so it is simply handed over.
    await loyalty_harness.feed(start_update(product.slug, user=user, update_id=453))
    assert ALREADY_PURCHASED in loyalty_harness.bot.texts()
    assert len([text for text in loyalty_harness.bot.texts() if DELIVERY_URL in text]) == 1
    assert await _internal_purchase_count(loyalty_harness, user.id) == 1


async def test_an_old_stars_invoice_cannot_buy_a_pack_taken_for_free(
    loyalty_harness: BotHarness,
) -> None:
    """A Telegram invoice never expires: pre-checkout refuses what the buyer now owns."""
    user = make_user(telegram_id=5407, username="latepayer")
    await _seed_received_packs(loyalty_harness, user.id, 10)
    # One Star: too little to halve, so this invoice carries no loyalty discount
    # and the refusal below is the ownership check, not the discount guard.
    product = await _product(loyalty_harness, price_stars=1, price_usdt=None)
    await loyalty_harness.feed(start_update(product.slug, user=user, update_id=461))
    await loyalty_harness.feed(
        pay_button_update(
            provider=PaymentProvider.STARS,
            product_id=product.id,
            user=user,
            update_id=462,
        )
    )
    (invoice,) = loyalty_harness.bot.methods(SendInvoice)
    assert invoice.prices[0].amount == 1
    await loyalty_harness.container.purchases.expire_stale(
        now=datetime.now(UTC) + timedelta(hours=2)
    )
    await loyalty_harness.feed(
        free_pack_button_update(product_id=product.id, user=user, update_id=463)
    )
    assert loyalty_harness.bot.methods(AnswerCallbackQuery)[-1].text == FREE_PACK_GRANTED

    await loyalty_harness.feed(
        pre_checkout_update(payload=invoice.payload, amount=1, user=user, update_id=464)
    )

    refused = loyalty_harness.bot.methods(AnswerPreCheckoutQuery)[-1]
    assert refused.ok is False
    assert refused.error_message == PRE_CHECKOUT_ALREADY_OWNED


async def test_the_free_pack_goes_to_one_product_only(loyalty_harness: BotHarness) -> None:
    user = make_user(telegram_id=5408, username="onlyone")
    await _seed_received_packs(loyalty_harness, user.id, 10)
    first, second = await _product(loyalty_harness), await _product(loyalty_harness)
    await loyalty_harness.feed(
        free_pack_button_update(product_id=first.id, user=user, update_id=471)
    )

    await loyalty_harness.feed(
        free_pack_button_update(product_id=second.id, user=user, update_id=472)
    )

    answer = loyalty_harness.bot.methods(AnswerCallbackQuery)[-1]
    assert answer.text == FREE_PACK_ALREADY_USED
    assert answer.show_alert is True
    assert await _internal_purchase_count(loyalty_harness, user.id) == 1


async def test_an_unpaid_invoice_holds_the_free_pack_back(loyalty_harness: BotHarness) -> None:
    user = make_user(telegram_id=5409, username="pending")
    await _seed_received_packs(loyalty_harness, user.id, 10)
    product = await _product(loyalty_harness)
    await loyalty_harness.feed(start_update(product.slug, user=user, update_id=481))
    await loyalty_harness.feed(
        pay_button_update(
            provider=PaymentProvider.STARS,
            product_id=product.id,
            user=user,
            update_id=482,
        )
    )

    await loyalty_harness.feed(
        free_pack_button_update(product_id=product.id, user=user, update_id=483)
    )

    assert loyalty_harness.bot.methods(AnswerCallbackQuery)[-1].text == FREE_PACK_PENDING_INVOICE
    assert await _internal_purchase_count(loyalty_harness, user.id) == 0


async def test_a_stale_free_pack_button_for_a_bought_pack_resends_the_link(
    loyalty_harness: BotHarness,
) -> None:
    """Bought after the card was shown: no pack is spent, the link comes again."""
    user = make_user(telegram_id=5410, username="boughtit")
    await _seed_received_packs(loyalty_harness, user.id, 10)
    product = await _product(loyalty_harness)
    async with loyalty_harness.container.uow_factory() as uow:
        paid = await uow.purchases.create(
            PurchaseDraft(
                user_id=user.id,
                product_id=product.id,
                provider=PaymentProvider.STARS,
                amount=Decimal(STARS_PRICE),
                currency=Currency.XTR,
                external_id=uuid4().hex,
                status=PurchaseStatus.PAID,
            )
        )
        await uow.purchases.mark_delivered(paid.id, delivered_url=DELIVERY_URL)

    await loyalty_harness.feed(
        free_pack_button_update(product_id=product.id, user=user, update_id=491)
    )

    assert loyalty_harness.bot.methods(AnswerCallbackQuery)[-1].text == ALREADY_PURCHASED
    assert len([text for text in loyalty_harness.bot.texts() if DELIVERY_URL in text]) == 1
    assert await _internal_purchase_count(loyalty_harness, user.id) == 0


async def test_a_free_pack_for_a_withdrawn_product_is_refused(
    loyalty_harness: BotHarness,
) -> None:
    user = make_user(telegram_id=5411, username="withdrawn")
    await _seed_received_packs(loyalty_harness, user.id, 10)
    product = await _product(loyalty_harness)
    await loyalty_harness.container.products.update(product.id, ProductUpdate(is_active=False))

    await loyalty_harness.feed(
        free_pack_button_update(product_id=product.id, user=user, update_id=501)
    )

    assert loyalty_harness.bot.methods(AnswerCallbackQuery)[-1].text == CARD_UNAVAILABLE
    assert await _internal_purchase_count(loyalty_harness, user.id) == 0


async def test_a_failing_claim_is_a_soft_error(
    loyalty_harness: BotHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = make_user(telegram_id=5412, username="unluckyclaim")
    await _seed_received_packs(loyalty_harness, user.id, 10)
    product = await _product(loyalty_harness)

    async def unavailable(*args: object, **kwargs: object) -> object:
        del args, kwargs
        message = "the database is resting"
        raise ServiceUnavailableError(message)

    monkeypatch.setattr(CheckoutService, "claim_free_pack", unavailable)

    await loyalty_harness.feed(
        free_pack_button_update(product_id=product.id, user=user, update_id=511)
    )

    assert loyalty_harness.bot.methods(AnswerCallbackQuery)[-1].text == FREE_PACK_FAILED
    assert await _internal_purchase_count(loyalty_harness, user.id) == 0


# --------------------------------------------------------------------------- #
# Loyalty: milestone notices through the bot
# --------------------------------------------------------------------------- #
async def _pay_with_stars(
    harness: BotHarness,
    *,
    user: TelegramUser,
    product: Product,
    first_update: int,
) -> None:
    await harness.feed(start_update(product.slug, user=user, update_id=first_update))
    await harness.feed(
        pay_button_update(
            provider=PaymentProvider.STARS,
            product_id=product.id,
            user=user,
            update_id=first_update + 1,
        )
    )
    payload = await _stars_payload(harness)
    await harness.feed(
        successful_payment_update(
            payload=payload,
            amount=STARS_PRICE,
            charge_id=f"charge-{first_update}",
            user=user,
            update_id=first_update + 2,
        )
    )


async def test_the_fifth_purchase_announces_the_discount_after_the_link(
    loyalty_harness: BotHarness,
) -> None:
    user = make_user(telegram_id=5501, username="fifth")
    await _seed_received_packs(loyalty_harness, user.id, 4)
    product = await _product(loyalty_harness)

    await _pay_with_stars(loyalty_harness, user=user, product=product, first_update=521)

    texts = loyalty_harness.bot.texts()
    notice = LOYALTY_MILESTONE_NOTICES[5]
    assert texts.count(notice) == 1
    link = next(index for index, text in enumerate(texts) if DELIVERY_URL in text)
    assert link < texts.index(notice)
    sent = next(m for m in loyalty_harness.bot.methods(SendMessage) if m.text == notice)
    assert sent.chat_id == user.id
    assert _labels(sent) == [PROGRESS_SECTION_BUTTON]

    # The button under it opens the buyer's own progress.
    await loyalty_harness.feed(progress_button_update(user=user, update_id=531))
    assert "Покупок: 5" in loyalty_harness.bot.last_text()


async def test_no_milestone_notice_while_the_programme_is_off(harness: BotHarness) -> None:
    user = make_user(telegram_id=5502, username="fifthoff")
    await _seed_received_packs(harness, user.id, 4)
    product = await _product(harness)

    await _pay_with_stars(harness, user=user, product=product, first_update=541)

    texts = harness.bot.texts()
    assert any(DELIVERY_URL in text for text in texts)
    assert not [text for text in texts if text in LOYALTY_MILESTONE_NOTICES.values()]
