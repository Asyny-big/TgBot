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
from contextlib import suppress
from dataclasses import dataclass
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
from aiohttp import web
from pydantic import SecretStr

from app.bot.factory import create_checkout, create_dispatcher
from app.bot.texts import (
    ALREADY_PURCHASED,
    CARD_NOT_FOUND,
    CARD_UNAVAILABLE,
    CRYPTO_INVOICE_CREATED,
    DELIVERY_FAILED,
    NO_DEEP_LINK,
    PAYMENT_IN_PROGRESS,
    PAYMENT_UNAVAILABLE,
    PRE_CHECKOUT_ALREADY_OWNED,
    PRE_CHECKOUT_UNKNOWN,
    REFUND_NOTICE,
)
from app.bot.webhooks import register_cryptobot_webhook
from app.bot.workers import HousekeepingWorker, ReconciliationWorker
from app.core.config import (
    BotSettings,
    CryptoBotSettings,
    DeliverySettings,
    PostgresSettings,
    RedisSettings,
    SecuritySettings,
    Settings,
    TelegramSettings,
)
from app.core.container import Container
from app.core.exceptions import PaymentGatewayError
from app.domain.commands import ProductDraft
from app.domain.enums import PaymentProvider, PurchaseStatus
from app.infrastructure.cache.rate_limit import RedisRateLimiter
from app.infrastructure.payments.cryptobot import SIGNATURE_HEADER, CryptoBotClient
from app.infrastructure.telegram.gateways import TelegramDeliveryGateway
from tests.bot_harness import (
    RecordingBot,
    make_user,
    pay_button_update,
    pre_checkout_update,
    refunded_payment_update,
    start_update,
)
from tests.settings_factory import build_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from aiogram import Dispatcher
    from redis.asyncio import Redis

    from app.domain.entities import Product
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.engine import Database
    from app.services.checkout import CheckoutService

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
            items = [
                {"invoice_id": int(invoice_id), "status": self.states.get(invoice_id, "active")}
                for invoice_id in requested
                if invoice_id
            ]
            return httpx.Response(200, json={"ok": True, "result": {"items": items}})
        message = f"unexpected Crypto Pay call: {request.url.path}"
        raise AssertionError(message)

    def mark_paid(self, invoice_id: str) -> None:
        self.states[invoice_id] = "paid"

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


def _settings(dsn: str, *, throttle: float = 0.0) -> Settings:
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
    from app.services.products import ProductService  # noqa: PLC0415
    from app.services.purchases import PurchaseService  # noqa: PLC0415
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
    return Container(
        settings=settings,
        uow_factory=uow_factory,
        locks=locks,
        rate_limiter=RedisRateLimiter(redis),
        crypto_payments=crypto_client,
        products=ProductService(uow_factory=uow_factory, telegram=settings.telegram),
        purchases=PurchaseService(uow_factory=uow_factory, locks=locks),
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
    assert DELIVERY_URL in harness.bot.last_text()

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
    assert DELIVERY_URL in harness.bot.last_text()


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
    assert DELIVERY_URL in harness.bot.last_text()

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
