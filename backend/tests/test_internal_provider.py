"""``PaymentProvider.INTERNAL`` is not a payment rail, and nothing treats it as one.

It marks a loyalty free pack the shop hands over itself. These tests pin down
that adding it changed nothing a buyer can see or do: no button, no invoice, no
provider call, no "not Stars, so crypto" fall-through, and no way to confirm a
"payment" for something nobody paid for.

The database's own rules for internal purchases are tested against PostgreSQL in
``test_repository_loyalty_schema``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.bot.keyboards import PayCallback, payment_keyboard
from app.core.config import DeliverySettings
from app.core.exceptions import ProviderNotSupportedError
from app.domain.commands import ProductDraft, PurchaseDraft, UserDraft
from app.domain.enums import PAYABLE_PROVIDERS, Currency, PaymentProvider, PurchaseStatus
from app.domain.payments import Invoice, InvoiceSnapshot, PaymentState
from app.domain.verification import VerificationOutcome
from app.services.checkout import CheckoutService
from app.services.delivery import DeliveryService
from app.services.purchases import PurchaseService
from tests.fakes import (
    FakeDeliveryGateway,
    FakeLockManager,
    FakeUnitOfWork,
    FakeUnitOfWorkFactory,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from app.domain.entities import Product, Purchase
    from app.domain.payments import InvoiceRequest

BUYER = 5101


class RecordingCryptoPay:
    """Fails the test the moment anything reaches Crypto Pay."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        self.calls.append(f"create_invoice:{request.payload}")
        return Invoice(external_id="never", pay_url="https://t.me/never")

    async def fetch_states(self, external_ids: Sequence[str]) -> Mapping[str, PaymentState]:
        self.calls.append(f"fetch_states:{list(external_ids)}")
        return {}

    async def fetch_invoices(self, external_ids: Sequence[str]) -> Mapping[str, InvoiceSnapshot]:
        self.calls.append(f"fetch_invoices:{list(external_ids)}")
        return {}


class RecordingStars:
    def __init__(self) -> None:
        self.calls: list[InvoiceRequest] = []

    async def send_invoice(self, request: InvoiceRequest) -> None:
        self.calls.append(request)


@dataclass(frozen=True, slots=True)
class Shop:
    unit: FakeUnitOfWork
    purchases: PurchaseService
    checkout: CheckoutService
    crypto: RecordingCryptoPay
    stars: RecordingStars
    delivery: FakeDeliveryGateway


def _shop() -> Shop:
    unit = FakeUnitOfWork()
    factory = FakeUnitOfWorkFactory(unit)
    locks = FakeLockManager()
    purchases = PurchaseService(uow_factory=factory, locks=locks)
    gateway = FakeDeliveryGateway()
    crypto = RecordingCryptoPay()
    stars = RecordingStars()
    checkout = CheckoutService(
        purchases=purchases,
        delivery=DeliveryService(
            uow_factory=factory,
            purchases=purchases,
            gateway=gateway,
            locks=locks,
            settings=DeliverySettings(max_attempts=1, initial_backoff_seconds=0.01),
        ),
        stars=stars,
        crypto=crypto,
    )
    return Shop(
        unit=unit,
        purchases=purchases,
        checkout=checkout,
        crypto=crypto,
        stars=stars,
        delivery=gateway,
    )


async def _product(shop: Shop, *, stars: int | None = 100, usdt: str | None = "5.00") -> Product:
    await shop.unit.users.upsert(UserDraft(telegram_id=BUYER))
    return await shop.unit.products.create(
        ProductDraft(
            slug=f"pack{uuid4().hex[:8]}",
            title="Pack",
            description="",
            delivery_url="https://t.me/+pack",
            price_stars=stars,
            price_usdt=Decimal(usdt) if usdt is not None else None,
        )
    )


async def _free_pack(shop: Shop, product: Product) -> Purchase:
    """An internal grant shaped exactly as the database requires."""
    return await shop.unit.purchases.create(
        PurchaseDraft(
            user_id=BUYER,
            product_id=product.id,
            provider=PaymentProvider.INTERNAL,
            amount=Decimal(0),
            base_amount=Decimal(100),
            loyalty_amount=Decimal(100),
            currency=Currency.XTR,
            external_id=f"loyalty:{uuid4()}",
            status=PurchaseStatus.PAID,
        )
    )


# --------------------------------------------------------------------------- #
# The enum and the product                                                    #
# --------------------------------------------------------------------------- #


def test_internal_is_not_a_payment_rail() -> None:
    assert PAYABLE_PROVIDERS == (PaymentProvider.STARS, PaymentProvider.CRYPTO)
    assert PaymentProvider.STARS.is_payable
    assert PaymentProvider.CRYPTO.is_payable
    assert not PaymentProvider.INTERNAL.is_payable


def test_internal_charges_in_no_currency() -> None:
    assert PaymentProvider.STARS.currency is Currency.XTR
    assert PaymentProvider.CRYPTO.currency is Currency.USDT
    with pytest.raises(ValueError, match="not a payment rail"):
        _ = PaymentProvider.INTERNAL.currency


@pytest.mark.parametrize(
    ("stars", "usdt", "expected"),
    [
        (100, "5.00", (PaymentProvider.STARS, PaymentProvider.CRYPTO)),
        (100, None, (PaymentProvider.STARS,)),
        (None, "5.00", (PaymentProvider.CRYPTO,)),
    ],
)
async def test_a_product_never_offers_internal(
    stars: int | None,
    usdt: str | None,
    expected: tuple[PaymentProvider, ...],
) -> None:
    product = await _product(_shop(), stars=stars, usdt=usdt)

    assert product.available_providers == expected
    assert not product.supports(PaymentProvider.INTERNAL)
    assert product.price_for(PaymentProvider.INTERNAL) is None


async def test_the_card_keyboard_has_no_internal_button() -> None:
    shop = _shop()
    product = await _product(shop)

    card = await shop.purchases.open_card(UserDraft(telegram_id=BUYER), product.slug)
    keyboard = payment_keyboard(card)

    providers = [
        PayCallback.unpack(button.callback_data or "").provider
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert providers == [PaymentProvider.STARS, PaymentProvider.CRYPTO]
    assert PaymentProvider.INTERNAL not in [option.provider for option in card.options]


# --------------------------------------------------------------------------- #
# Nothing can buy, quote or confirm through INTERNAL                          #
# --------------------------------------------------------------------------- #


async def test_a_purchase_cannot_be_started_through_internal() -> None:
    shop = _shop()
    product = await _product(shop)

    with pytest.raises(ProviderNotSupportedError):
        await shop.purchases.start_purchase(
            user_id=BUYER,
            product_id=product.id,
            provider=PaymentProvider.INTERNAL,
            external_id="internal-attempt",
        )

    assert shop.unit.purchases.items == {}


async def test_nothing_can_be_quoted_through_internal() -> None:
    shop = _shop()
    product = await _product(shop)

    with pytest.raises(ProviderNotSupportedError):
        await shop.purchases.quote_amount(
            user_id=BUYER,
            product=product,
            provider=PaymentProvider.INTERNAL,
        )


async def test_an_internal_grant_can_never_be_confirmed_as_a_payment() -> None:
    """Neither directly nor through settlement — and no provider is touched."""
    shop = _shop()
    product = await _product(shop)
    grant = await _free_pack(shop, product)

    with pytest.raises(ProviderNotSupportedError):
        await shop.purchases.confirm_payment(
            provider=PaymentProvider.INTERNAL,
            external_id=grant.external_id,
        )
    with pytest.raises(ProviderNotSupportedError):
        await shop.checkout.settle_payment(
            provider=PaymentProvider.INTERNAL,
            external_id=grant.external_id,
        )

    assert shop.unit.purchases.items[grant.id].status is PurchaseStatus.PAID
    assert shop.delivery.sent == []
    assert shop.crypto.calls == []
    assert shop.stars.calls == []


# --------------------------------------------------------------------------- #
# Admin verification never asks a provider about an internal grant           #
# --------------------------------------------------------------------------- #


async def test_verifying_an_undelivered_free_pack_delivers_it_without_a_provider() -> None:
    shop = _shop()
    product = await _product(shop)
    grant = await _free_pack(shop, product)

    report = await shop.checkout.verify_payment(grant.id)

    assert report.outcome is VerificationOutcome.DELIVERED_NOW
    assert report.status_after is PurchaseStatus.DELIVERED
    assert len(shop.delivery.sent) == 1

    again = await shop.checkout.verify_payment(grant.id)
    assert again.outcome is VerificationOutcome.ALREADY_DELIVERED
    assert len(shop.delivery.sent) == 1
    assert shop.crypto.calls == []
    assert shop.stars.calls == []


async def test_a_pending_internal_grant_is_never_sent_to_crypto_pay() -> None:
    """Defence in depth: the database forbids this state; if it ever existed,
    verification would still not fall through to the CryptoBot lookup."""
    shop = _shop()
    product = await _product(shop)
    grant = await _free_pack(shop, product)
    impossible = replace(grant, status=PurchaseStatus.PENDING, paid_at=None)
    shop.unit.purchases.items[grant.id] = impossible

    report = await shop.checkout.verify_payment(grant.id)

    assert report.outcome is VerificationOutcome.NO_PROVIDER_EVIDENCE
    assert report.provider is PaymentProvider.INTERNAL
    assert shop.crypto.calls == []
    assert shop.stars.calls == []
    assert shop.delivery.sent == []


# --------------------------------------------------------------------------- #
# The in-memory doubles keep the database's rules                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("provider", "amount"),
    [
        (PaymentProvider.STARS, Decimal(0)),
        (PaymentProvider.CRYPTO, Decimal(0)),
        (PaymentProvider.INTERNAL, Decimal(1)),
    ],
)
async def test_the_fakes_refuse_an_amount_that_does_not_match_the_provider(
    provider: PaymentProvider,
    amount: Decimal,
) -> None:
    shop = _shop()
    product = await _product(shop)

    with pytest.raises(ValueError, match="does not match its provider"):
        await shop.unit.purchases.create(
            PurchaseDraft(
                user_id=BUYER,
                product_id=product.id,
                provider=provider,
                amount=amount,
                base_amount=Decimal(100),
                loyalty_amount=Decimal(100) if provider is PaymentProvider.INTERNAL else Decimal(0),
                currency=Currency.XTR,
                external_id=uuid4().hex,
                status=PurchaseStatus.PAID,
            )
        )
