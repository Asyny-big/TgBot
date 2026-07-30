"""Admin API: statistics, purchase search, manual payment verification, re-delivery.

The verification endpoint is the support tool for "I paid but got no link", so
its idempotency is tested from every starting state.
"""

from __future__ import annotations

import json
from decimal import Decimal
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import text

from app.domain.commands import ProductDraft, UserDraft
from app.domain.enums import PaymentProvider, PurchaseStatus
from tests.api_harness import api_settings, build_api_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from redis.asyncio import Redis

    from app.domain.entities import Product, Purchase
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.engine import Database
    from tests.api_harness import ApiHarness

BUYER = UserDraft(telegram_id=8001, username="buyer", first_name="Buyer", language_code="ru")
DELIVERY_URL = "https://t.me/+private-invite"
CRYPTO_INVOICE_ID = "551100"


class CryptoPayDouble:
    """A Crypto Pay stand-in whose invoice state the test controls."""

    def __init__(self) -> None:
        self.states: dict[str, str] = {}
        self.calls = 0
        self.unavailable = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.unavailable:
            return httpx.Response(503, json={"ok": False, "error": "maintenance"})
        payload = json.loads(request.content or b"{}")
        requested = str(payload.get("invoice_ids", "")).split(",")
        items = [
            {"invoice_id": int(invoice_id), "status": self.states.get(invoice_id, "active")}
            for invoice_id in requested
            if invoice_id
        ]
        return httpx.Response(200, json={"ok": True, "result": {"items": items}})


@pytest.fixture
def crypto_double() -> CryptoPayDouble:
    return CryptoPayDouble()


@pytest.fixture
async def verify_api(
    live_database: Database,
    live_locks: RedisLockManager,
    redis_client: Redis,
    migrated_database: str,
    crypto_double: CryptoPayDouble,
) -> AsyncIterator[ApiHarness]:
    """An authenticated admin client whose payment provider the test drives."""
    settings = api_settings(migrated_database)
    harness, client = build_api_harness(
        settings=settings,
        database=live_database,
        locks=live_locks,
        redis=redis_client,
        crypto_transport=httpx.MockTransport(crypto_double.handler),
    )
    try:
        await harness.authenticate()
        yield harness
    finally:
        await client.aclose()
        await harness.bot.session.close()


async def _product(api: ApiHarness, **overrides: Any) -> Product:
    values: dict[str, Any] = {
        "slug": f"vip{uuid4().hex[:6]}",
        "title": "VIP access",
        "description": "Lifetime access",
        "delivery_url": DELIVERY_URL,
        "price_stars": 150,
        "price_usdt": Decimal("5.00"),
    }
    values.update(overrides)
    return await api.container.products.create(ProductDraft(**values))


async def _pending(
    api: ApiHarness,
    product: Product,
    *,
    provider: PaymentProvider,
    external_id: str,
) -> Purchase:
    await api.container.purchases.remember_user(BUYER)
    return await api.container.purchases.start_purchase(
        user_id=BUYER.telegram_id,
        product_id=product.id,
        provider=provider,
        external_id=external_id,
    )


async def _verify(api: ApiHarness, purchase: Purchase) -> dict[str, Any]:
    response = await api.client.post(
        f"{api.prefix}/purchases/{purchase.id}/verify",
    )
    assert response.status_code == HTTPStatus.OK, response.text
    return dict(response.json())


# --------------------------------------------------------------------------- #
# Manual verification: CryptoBot
# --------------------------------------------------------------------------- #
async def test_verify_settles_a_crypto_payment_the_webhook_never_delivered(
    verify_api: ApiHarness,
    crypto_double: CryptoPayDouble,
) -> None:
    """The support case: buyer paid in CryptoBot, the webhook was lost."""
    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.CRYPTO,
        external_id=CRYPTO_INVOICE_ID,
    )
    crypto_double.states[CRYPTO_INVOICE_ID] = "paid"

    report = await _verify(verify_api, purchase)

    assert report["outcome"] == "settled_and_delivered"
    assert report["resolved"] is True
    assert report["status_before"] == "pending"
    assert report["status_after"] == "delivered"
    assert report["provider_state"] == "paid"
    assert report["delivery"]["status"] == "sent"
    assert DELIVERY_URL in verify_api.bot.last_text()

    stored = await verify_api.container.purchases.get(purchase.id)
    assert stored.status is PurchaseStatus.DELIVERED


async def test_verify_is_idempotent(
    verify_api: ApiHarness,
    crypto_double: CryptoPayDouble,
) -> None:
    """Pressing "check payment" repeatedly must never deliver or charge twice."""
    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.CRYPTO,
        external_id=CRYPTO_INVOICE_ID,
    )
    crypto_double.states[CRYPTO_INVOICE_ID] = "paid"

    first = await _verify(verify_api, purchase)
    second = await _verify(verify_api, purchase)
    third = await _verify(verify_api, purchase)

    assert first["outcome"] == "settled_and_delivered"
    assert second["outcome"] == "already_delivered"
    assert third["outcome"] == "already_delivered"
    assert all(report["resolved"] for report in (first, second, third))
    assert second["delivery"] is None

    # Exactly one link was sent and the sale is counted once.
    assert len([text for text in verify_api.bot.texts() if DELIVERY_URL in text]) == 1
    overview = await verify_api.container.stats.overview()
    assert overview.total.purchases_count == 1


async def test_verify_reports_an_unpaid_invoice_without_touching_it(
    verify_api: ApiHarness,
    crypto_double: CryptoPayDouble,
) -> None:
    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.CRYPTO,
        external_id=CRYPTO_INVOICE_ID,
    )

    report = await _verify(verify_api, purchase)

    assert report["outcome"] == "still_unpaid"
    assert report["resolved"] is False
    assert report["status_after"] == "pending"
    assert report["delivery"] is None
    assert verify_api.bot.texts() == []


async def test_verify_reports_an_expired_invoice(
    verify_api: ApiHarness,
    crypto_double: CryptoPayDouble,
) -> None:
    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.CRYPTO,
        external_id=CRYPTO_INVOICE_ID,
    )
    crypto_double.states[CRYPTO_INVOICE_ID] = "expired"

    report = await _verify(verify_api, purchase)

    assert report["outcome"] == "expired_unpaid"
    assert report["provider_state"] == "expired"
    assert report["status_after"] == "pending"


async def test_verify_survives_an_unavailable_provider(
    verify_api: ApiHarness,
    crypto_double: CryptoPayDouble,
) -> None:
    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.CRYPTO,
        external_id=CRYPTO_INVOICE_ID,
    )
    crypto_double.unavailable = True

    report = await _verify(verify_api, purchase)

    assert report["outcome"] == "provider_unavailable"
    assert report["resolved"] is False
    assert report["status_after"] == "pending"

    # Once the provider recovers, the same button finishes the job.
    crypto_double.unavailable = False
    crypto_double.states[CRYPTO_INVOICE_ID] = "paid"
    recovered = await _verify(verify_api, purchase)
    assert recovered["outcome"] == "settled_and_delivered"


# --------------------------------------------------------------------------- #
# Manual verification: Telegram Stars
# --------------------------------------------------------------------------- #
async def test_verify_uses_the_stored_stars_charge_id(verify_api: ApiHarness) -> None:
    """Stars has no lookup API; a recorded charge id is Telegram's own receipt."""
    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.STARS,
        external_id="stars-payload-1",
    )
    # Construct the torn state this branch exists for: Telegram's charge id was
    # recorded, but the status update never completed (crash between the two).
    async with verify_api.container.uow_factory() as uow:
        await uow.session.execute(
            text("UPDATE purchases SET telegram_charge_id = :charge WHERE id = :id"),
            {"charge": "charge-abc", "id": str(purchase.id)},
        )

    report = await _verify(verify_api, purchase)

    assert report["outcome"] == "settled_and_delivered"
    assert report["provider_state"] == "paid"
    assert report["status_after"] == "delivered"
    assert DELIVERY_URL in verify_api.bot.last_text()


async def test_verify_reports_no_evidence_for_an_unpaid_stars_invoice(
    verify_api: ApiHarness,
) -> None:
    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.STARS,
        external_id="stars-payload-2",
    )

    report = await _verify(verify_api, purchase)

    assert report["outcome"] == "no_provider_evidence"
    assert report["resolved"] is False
    assert report["detail"]
    assert verify_api.bot.texts() == []


# --------------------------------------------------------------------------- #
# Manual verification: other starting states
# --------------------------------------------------------------------------- #
async def test_verify_retries_a_failed_delivery(verify_api: ApiHarness) -> None:
    """Paid but undelivered: the check re-runs delivery and finishes the sale."""
    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.STARS,
        external_id="stars-payload-3",
    )
    await verify_api.container.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="stars-payload-3",
    )

    report = await _verify(verify_api, purchase)

    assert report["outcome"] == "delivered_now"
    assert report["status_before"] == "paid"
    assert report["status_after"] == "delivered"
    assert report["delivery"]["status"] == "sent"


async def test_verify_reports_a_dead_chat_without_losing_the_entitlement(
    verify_api: ApiHarness,
) -> None:
    from aiogram.exceptions import TelegramForbiddenError  # noqa: PLC0415
    from aiogram.methods import SendMessage  # noqa: PLC0415

    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.STARS,
        external_id="stars-payload-4",
    )
    await verify_api.container.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="stars-payload-4",
    )
    verify_api.bot.fail_send_message = [
        TelegramForbiddenError(
            method=SendMessage(chat_id=BUYER.telegram_id, text="x"),
            message="Forbidden: bot was blocked by the user",
        )
    ]

    report = await _verify(verify_api, purchase)

    assert report["outcome"] == "delivery_failed"
    assert report["resolved"] is False
    assert report["status_after"] == "paid"

    # The buyer unblocks the bot; the same button now completes the delivery.
    retried = await _verify(verify_api, purchase)
    assert retried["outcome"] == "delivered_now"


async def test_verify_reports_a_refund(verify_api: ApiHarness) -> None:
    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.STARS,
        external_id="stars-payload-5",
    )
    await verify_api.container.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="stars-payload-5",
    )
    await verify_api.container.purchases.refund(purchase.id)

    report = await _verify(verify_api, purchase)

    assert report["outcome"] == "refunded"
    assert report["resolved"] is False
    assert verify_api.bot.texts() == []


async def test_verify_of_an_unknown_purchase_is_a_404(verify_api: ApiHarness) -> None:
    response = await verify_api.client.post(f"{verify_api.prefix}/purchases/{uuid4()}/verify")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["error"]["code"] == "purchase_not_found"


async def test_verify_requires_authentication(api: ApiHarness) -> None:
    response = await api.client.post(f"{api.prefix}/purchases/{uuid4()}/verify")
    assert response.status_code == HTTPStatus.UNAUTHORIZED


# --------------------------------------------------------------------------- #
# Re-delivery, search and statistics
# --------------------------------------------------------------------------- #
async def test_resend_hands_the_link_over_again(verify_api: ApiHarness) -> None:
    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.STARS,
        external_id="stars-payload-6",
    )
    await verify_api.container.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="stars-payload-6",
    )
    await _verify(verify_api, purchase)

    response = await verify_api.client.post(f"{verify_api.prefix}/purchases/{purchase.id}/resend")

    assert response.status_code == HTTPStatus.OK
    assert response.json()["status"] == "sent"
    assert len([text for text in verify_api.bot.texts() if DELIVERY_URL in text]) == 2


async def test_resend_refuses_an_unpaid_purchase(verify_api: ApiHarness) -> None:
    product = await _product(verify_api)
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.STARS,
        external_id="stars-payload-7",
    )

    response = await verify_api.client.post(f"{verify_api.prefix}/purchases/{purchase.id}/resend")
    assert response.status_code == HTTPStatus.CONFLICT


async def test_purchase_search_matches_every_documented_field(
    verify_api: ApiHarness,
) -> None:
    product = await _product(verify_api, title="Python course")
    purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.STARS,
        external_id="INV-77",
    )
    await verify_api.container.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="INV-77",
        telegram_charge_id="charge-77",
    )

    for term in (str(BUYER.telegram_id), "buyer", "python", product.slug, "inv-77", "charge-77"):
        response = await verify_api.client.get(
            f"{verify_api.prefix}/purchases",
            params={"search": term},
        )
        assert response.status_code == HTTPStatus.OK, term
        items = response.json()["items"]
        assert [item["purchase"]["id"] for item in items] == [str(purchase.id)], term
        assert items[0]["buyer"]["display_name"] == "@buyer"
        assert items[0]["product"]["title"] == "Python course"

    empty = await verify_api.client.get(
        f"{verify_api.prefix}/purchases",
        params={"search": "nothing"},
    )
    assert empty.json()["meta"]["total"] == 0


async def test_purchase_search_filters_by_status(verify_api: ApiHarness) -> None:
    product = await _product(verify_api)
    pending = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.CRYPTO,
        external_id="inv-pending",
    )
    paid_purchase = await _pending(
        verify_api,
        product,
        provider=PaymentProvider.STARS,
        external_id="inv-paid",
    )
    await verify_api.container.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="inv-paid",
    )

    response = await verify_api.client.get(
        f"{verify_api.prefix}/purchases",
        params={"status": "paid"},
    )
    assert [item["purchase"]["id"] for item in response.json()["items"]] == [str(paid_purchase.id)]

    pending_response = await verify_api.client.get(
        f"{verify_api.prefix}/purchases",
        params={"status": "pending"},
    )
    assert [item["purchase"]["id"] for item in pending_response.json()["items"]] == [
        str(pending.id)
    ]


async def test_overview_reports_revenue_per_rail(verify_api: ApiHarness) -> None:
    product = await _product(verify_api, price_stars=150, price_usdt=Decimal("5.00"))
    await _pending(
        verify_api,
        product,
        provider=PaymentProvider.STARS,
        external_id="inv-stars",
    )
    await verify_api.container.purchases.confirm_payment(
        provider=PaymentProvider.STARS,
        external_id="inv-stars",
    )

    response = await verify_api.client.get(f"{verify_api.prefix}/stats/overview")

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["total"]["purchases_count"] == 1
    assert body["total"]["stars_amount"] == 150
    assert Decimal(body["total"]["usdt_amount"]) == Decimal(0)
    assert body["today"]["purchases_count"] == 1
    assert body["products_total"] == 1
    assert body["products_active"] == 1
    assert body["users_total"] == 1
    assert [item["slug"] for item in body["top_products"]] == [product.slug]
    assert body["recent_purchases"][0]["buyer"]["telegram_id"] == BUYER.telegram_id
