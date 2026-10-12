"""Bug #3: a buyer holds at most one live CryptoBot invoice per product.

In production one buyer pressed the USDT button fifteen times and got fifteen
invoices, twelve of them within two and a half minutes. Two holes let that
happen, and each is pinned down here:

* the purchase lock was taken only *after* Crypto Pay had already issued the
  invoice, so overlapping presses each reached the provider;
* nothing looked for the invoice the buyer already had, so even presses seconds
  apart each made a new one.

Most tests run on the in-memory fakes; the ones that only mean something
against the real lock and the real SQL run on PostgreSQL and Redis.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from decimal import Decimal
from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.core.config import DeliverySettings, ReferralSettings
from app.core.exceptions import (
    CheckoutAlreadyPendingError,
    ConflictError,
    DuplicatePurchaseError,
    LockBusyError,
    PaymentAlreadyReceivedError,
    PaymentGatewayError,
    PendingInvoiceExistsError,
)
from app.domain.commands import BonusTransactionDraft, ProductDraft, ReferralDraft, UserDraft
from app.domain.enums import BonusTransactionType, PaymentProvider, PurchaseStatus
from app.domain.locks import purchase_lock_key
from app.domain.payments import Invoice, InvoiceSnapshot, PaymentState
from app.services.bonuses import BonusService
from app.services.checkout import CheckoutService, CryptoCheckout
from app.services.delivery import DeliveryService
from app.services.purchases import PurchaseService
from tests.fakes import (
    FakeDeliveryGateway,
    FakeLockManager,
    FakeUnitOfWork,
    FakeUnitOfWorkFactory,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from app.domain.entities import Product, Purchase
    from app.domain.locks import LockManager
    from app.domain.payments import InvoiceRequest
    from app.domain.uow import UnitOfWorkFactory
    from app.infrastructure.cache.locks import RedisLockManager
    from app.infrastructure.db.uow import SqlAlchemyUnitOfWorkFactory

BUYER = 6856730130
INVITER = 7010001
USDT_PRICE = Decimal("10.00")
STARS_PRICE = 1000
"""Both prices declared, so the product's own rate is 100 Stars per USDT."""

REFERRAL = ReferralSettings(
    enabled=True,
    discount_percent=10,
    reward_percent=15,
    max_bonus_payment_percent=50,
)


class ScriptedCryptoPay:
    """A Crypto Pay double whose invoice states the test drives.

    Counts every invoice it is asked to create — the number this whole file is
    about — and can stall, fail, or go dark on lookups.
    """

    def __init__(self, *, stall_seconds: float = 0.0) -> None:
        self.created: list[InvoiceRequest] = []
        self.states: dict[str, PaymentState] = {}
        self.lookups = 0
        self.stall_seconds = stall_seconds
        self.fail_create = False
        self.unavailable = False
        self.forgotten: set[str] = set()

    async def create_invoice(self, request: InvoiceRequest) -> Invoice:
        if self.fail_create:
            message = "Crypto Pay is unreachable"
            raise PaymentGatewayError(message)
        self.created.append(request)
        # Numbered before the stall, so overlapping calls get distinct ids.
        external_id = f"{9000 + len(self.created)}"
        if self.stall_seconds:
            await asyncio.sleep(self.stall_seconds)
        self.states[external_id] = PaymentState.PENDING
        return Invoice(external_id=external_id, pay_url=self.url(external_id))

    async def fetch_states(self, external_ids: Sequence[str]) -> Mapping[str, PaymentState]:
        invoices = await self.fetch_invoices(external_ids)
        return {invoice_id: snapshot.state for invoice_id, snapshot in invoices.items()}

    async def fetch_invoices(self, external_ids: Sequence[str]) -> Mapping[str, InvoiceSnapshot]:
        self.lookups += 1
        if self.unavailable:
            message = "Crypto Pay is unreachable"
            raise PaymentGatewayError(message)
        return {
            invoice_id: InvoiceSnapshot(
                external_id=invoice_id,
                state=self.states[invoice_id],
                pay_url=self.url(invoice_id),
            )
            for invoice_id in external_ids
            if invoice_id in self.states and invoice_id not in self.forgotten
        }

    @staticmethod
    def url(external_id: str) -> str:
        return f"https://t.me/CryptoBot?start=IV{external_id}"


class NullStars:
    async def send_invoice(self, request: InvoiceRequest) -> None:
        del request


@dataclass(frozen=True, slots=True)
class Shop:
    checkout: CheckoutService
    purchases: PurchaseService
    crypto: ScriptedCryptoPay


def _shop(
    uow_factory: UnitOfWorkFactory,
    locks: LockManager,
    *,
    referral: ReferralSettings | None = None,
    crypto: ScriptedCryptoPay | None = None,
) -> Shop:
    pricing = (
        BonusService(uow_factory=uow_factory, locks=locks, settings=referral)
        if referral is not None
        else None
    )
    purchases = PurchaseService(uow_factory=uow_factory, locks=locks, pricing=pricing)
    gateway = crypto or ScriptedCryptoPay()
    checkout = CheckoutService(
        purchases=purchases,
        delivery=DeliveryService(
            uow_factory=uow_factory,
            purchases=purchases,
            gateway=FakeDeliveryGateway(),
            locks=locks,
            settings=DeliverySettings(max_attempts=1, initial_backoff_seconds=0.01),
        ),
        stars=NullStars(),
        crypto=gateway,
    )
    return Shop(checkout=checkout, purchases=purchases, crypto=gateway)


def _draft(
    *, price_usdt: Decimal = USDT_PRICE, price_stars: int | None = STARS_PRICE
) -> ProductDraft:
    return ProductDraft(
        slug=f"pack{uuid4().hex[:8]}",
        title="Big Pack",
        description="",
        delivery_url="https://t.me/+pack",
        price_usdt=price_usdt,
        price_stars=price_stars,
    )


@pytest.fixture
def unit() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def locks() -> FakeLockManager:
    return FakeLockManager()


@pytest.fixture
async def product(unit: FakeUnitOfWork) -> Product:
    await unit.users.upsert(UserDraft(telegram_id=BUYER, username="buyer"))
    return await unit.products.create(_draft())


def _fake_shop(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    *,
    referral: ReferralSettings | None = None,
    crypto: ScriptedCryptoPay | None = None,
) -> Shop:
    return _shop(FakeUnitOfWorkFactory(unit), locks, referral=referral, crypto=crypto)


async def _invite(unit: FakeUnitOfWork) -> None:
    await unit.users.upsert(UserDraft(telegram_id=INVITER, username="inviter"))
    await unit.referrals.create(ReferralDraft(referrer_user_id=INVITER, referred_user_id=BUYER))


async def _grant_bonuses(unit: FakeUnitOfWork, units: int) -> None:
    await unit.bonuses.add(
        BonusTransactionDraft(
            user_id=BUYER,
            amount=units,
            type=BonusTransactionType.REFERRAL_REWARD,
        )
    )


def _reserves(unit: FakeUnitOfWork) -> list[int]:
    return [
        entry.amount
        for entry in unit.bonuses.entries
        if entry.type is BonusTransactionType.BONUS_RESERVED
    ]


def _pending(unit: FakeUnitOfWork) -> list[Purchase]:
    return [
        purchase
        for purchase in unit.purchases.items.values()
        if purchase.status is PurchaseStatus.PENDING
    ]


# --------------------------------------------------------------------------- #
# The two holes                                                               #
# --------------------------------------------------------------------------- #


async def test_a_second_press_seconds_later_creates_no_second_invoice(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    """The production pattern: presses that never overlap."""
    shop = _fake_shop(unit, locks)

    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    second = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    third = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert len(shop.crypto.created) == 1
    assert not first.resumed
    assert second.resumed
    assert third.resumed
    assert second.purchase.id == third.purchase.id == first.purchase.id
    assert second.pay_url == first.pay_url
    assert len(_pending(unit)) == 1


async def test_overlapping_presses_reach_crypto_pay_once(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    """The lock is now taken before the provider is called, not after."""
    shop = _fake_shop(unit, locks, crypto=ScriptedCryptoPay(stall_seconds=0.05))

    results = await asyncio.gather(
        shop.checkout.start_crypto_checkout(user_id=BUYER, product=product),
        shop.checkout.start_crypto_checkout(user_id=BUYER, product=product),
        return_exceptions=True,
    )

    assert len(shop.crypto.created) == 1
    assert sum(isinstance(result, CryptoCheckout) for result in results) == 1
    assert sum(isinstance(result, LockBusyError) for result in results) == 1
    assert len(_pending(unit)) == 1


async def test_a_held_lock_stops_the_checkout_before_crypto_pay(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    shop = _fake_shop(unit, locks)
    locks.busy_keys.add(purchase_lock_key(BUYER, product.id))

    with pytest.raises(LockBusyError):
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert shop.crypto.created == []
    assert shop.crypto.lookups == 0


# --------------------------------------------------------------------------- #
# What happens to the invoice the buyer already has                           #
# --------------------------------------------------------------------------- #


async def test_an_existing_invoice_on_the_same_terms_is_handed_back(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    shop = _fake_shop(unit, locks)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    again = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert again.resumed
    assert again.purchase == first.purchase
    assert again.pay_url == ScriptedCryptoPay.url(first.purchase.external_id)
    assert shop.crypto.lookups == 1


async def test_an_existing_invoice_at_another_price_is_neither_reused_nor_doubled(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    shop = _fake_shop(unit, locks)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    repriced = replace(product, price_usdt=Decimal("12.00"))
    unit.products.items[product.id] = repriced

    with pytest.raises(PendingInvoiceExistsError) as caught:
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=repriced)

    assert len(shop.crypto.created) == 1
    assert caught.value.amount == USDT_PRICE
    assert caught.value.currency == "USDT"
    assert caught.value.pay_url == ScriptedCryptoPay.url(first.purchase.external_id)
    # The old invoice is left exactly as it was: still pending, still payable.
    assert unit.purchases.items[first.purchase.id].status is PurchaseStatus.PENDING


async def test_an_invoice_expired_at_crypto_pay_is_expired_here_then_reissued(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    shop = _fake_shop(unit, locks)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    shop.crypto.states[first.purchase.external_id] = PaymentState.EXPIRED

    second = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert unit.purchases.items[first.purchase.id].status is PurchaseStatus.EXPIRED
    assert not second.resumed
    assert second.purchase.id != first.purchase.id
    assert second.purchase.status is PurchaseStatus.PENDING
    assert len(shop.crypto.created) == 2
    assert len(_pending(unit)) == 1


async def test_an_unreachable_crypto_pay_issues_nothing_new(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    shop = _fake_shop(unit, locks)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    shop.crypto.unavailable = True

    with pytest.raises(PaymentGatewayError):
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert len(shop.crypto.created) == 1
    assert unit.purchases.items[first.purchase.id].status is PurchaseStatus.PENDING


async def test_an_invoice_crypto_pay_does_not_report_is_not_treated_as_expired(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    """Silence is not "expired": only the provider's own word frees the slot."""
    shop = _fake_shop(unit, locks)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    shop.crypto.forgotten.add(first.purchase.external_id)

    with pytest.raises(PaymentGatewayError):
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert len(shop.crypto.created) == 1
    assert unit.purchases.items[first.purchase.id].status is PurchaseStatus.PENDING


async def test_an_invoice_already_paid_is_not_followed_by_another(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    """Paid but not yet settled: the webhook or reconciliation will deliver."""
    shop = _fake_shop(unit, locks)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    shop.crypto.states[first.purchase.external_id] = PaymentState.PAID

    with pytest.raises(PaymentAlreadyReceivedError):
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert len(shop.crypto.created) == 1
    assert unit.purchases.items[first.purchase.id].status is PurchaseStatus.PENDING


async def test_duplicates_left_over_from_before_the_fix_are_worked_through(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    """Expired ones are closed, and the one still payable is handed back.

    Migration 0004 refuses to run while such duplicates exist, so this is
    defence in depth: the rows are placed directly, as if written before it.
    """
    shop = _fake_shop(unit, locks)
    older = await shop.purchases.start_purchase(
        user_id=BUYER,
        product_id=product.id,
        provider=PaymentProvider.CRYPTO,
        external_id="5001",
    )
    newer = replace(older, id=uuid4(), external_id="5002")
    unit.purchases.items[newer.id] = newer
    shop.crypto.states["5001"] = PaymentState.PENDING
    shop.crypto.states["5002"] = PaymentState.EXPIRED

    resumed = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert resumed.resumed
    assert resumed.purchase.id == older.id
    assert unit.purchases.items[newer.id].status is PurchaseStatus.EXPIRED
    assert shop.crypto.created == []


async def test_a_buyer_who_owns_the_product_never_reaches_crypto_pay(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    shop = _fake_shop(unit, locks)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    await shop.purchases.confirm_payment(
        provider=PaymentProvider.CRYPTO,
        external_id=first.purchase.external_id,
    )

    with pytest.raises(DuplicatePurchaseError):
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert len(shop.crypto.created) == 1
    assert shop.crypto.lookups == 0


async def test_a_payable_invoice_without_a_payment_url_issues_nothing_new(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shop = _fake_shop(unit, locks)
    await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    monkeypatch.setattr(ScriptedCryptoPay, "url", staticmethod(lambda _external_id: ""))

    with pytest.raises(PaymentGatewayError):
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert len(shop.crypto.created) == 1


# --------------------------------------------------------------------------- #
# The lock itself                                                             #
# --------------------------------------------------------------------------- #


async def test_the_lock_is_released_when_crypto_pay_refuses_the_invoice(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    shop = _fake_shop(unit, locks)
    shop.crypto.fail_create = True

    with pytest.raises(PaymentGatewayError):
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert locks.held == {}
    assert _pending(unit) == []

    shop.crypto.fail_create = False
    retried = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    assert retried.purchase.status is PurchaseStatus.PENDING
    assert len(shop.crypto.created) == 1


async def test_the_lock_is_released_when_the_price_moved_mid_checkout(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    """``expected_amount`` still guards the insert, and still lets go of the lock."""
    shop = _fake_shop(unit, locks)
    unit.products.items[product.id] = replace(product, price_usdt=Decimal("11.00"))

    with pytest.raises(ConflictError):
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert locks.held == {}
    assert _pending(unit) == []


async def test_the_stars_path_still_takes_the_lock_itself(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    shop = _fake_shop(unit, locks)
    locks.busy_keys.add(purchase_lock_key(BUYER, product.id))

    with pytest.raises(LockBusyError):
        await shop.purchases.start_purchase(
            user_id=BUYER,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            external_id="stars-1",
        )


async def test_a_checkout_session_cannot_outlive_its_lock(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    shop = _fake_shop(unit, locks)
    async with shop.purchases.checkout_session(BUYER, product.id) as session:
        assert locks.held  # the lock is genuinely held while the session is open

    with pytest.raises(RuntimeError):
        await session.record(provider=PaymentProvider.CRYPTO, external_id="late")
    assert _pending(unit) == []


async def test_the_lock_is_not_reentrant_so_the_session_never_retakes_it(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    """Documents why the session exists: calling start_purchase inside would fail."""
    shop = _fake_shop(unit, locks)
    async with shop.purchases.checkout_session(BUYER, product.id):
        with pytest.raises(LockBusyError):
            await shop.purchases.start_purchase(
                user_id=BUYER,
                product_id=product.id,
                provider=PaymentProvider.CRYPTO,
                external_id="nested",
            )


async def test_a_checkout_session_refuses_another_buyers_purchase(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    shop = _fake_shop(unit, locks)
    other = await unit.products.create(_draft())

    async with shop.purchases.checkout_session(BUYER, product.id) as session:
        with pytest.raises(ValueError, match="different buyer or product"):
            await session.quote(other, PaymentProvider.CRYPTO)


# --------------------------------------------------------------------------- #
# Referral discount and bonuses                                               #
# --------------------------------------------------------------------------- #


async def test_a_second_press_keeps_the_referral_discount(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    """Before the fix the second invoice was billed at full price.

    The pending-discount guard saw the first invoice holding the discount and
    priced the second one without it.
    """
    await _invite(unit)
    shop = _fake_shop(unit, locks, referral=REFERRAL)

    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    second = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert first.purchase.discount_amount == Decimal("1.00")
    assert first.purchase.amount == Decimal("9.00")
    assert second.resumed
    assert second.purchase.amount == Decimal("9.00")
    assert [request.amount for request in shop.crypto.created] == [Decimal("9.00")]


async def test_the_discount_carries_over_to_the_invoice_that_replaces_an_expired_one(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    await _invite(unit)
    shop = _fake_shop(unit, locks, referral=REFERRAL)
    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    shop.crypto.states[first.purchase.external_id] = PaymentState.EXPIRED

    second = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert second.purchase.discount_amount == Decimal("1.00")
    assert second.purchase.amount == Decimal("9.00")
    # One discount in flight at a time, never two.
    assert sum(1 for purchase in _pending(unit) if purchase.discount_amount > 0) == 1


async def test_a_second_press_reserves_no_more_bonuses(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    await _grant_bonuses(unit, 300)
    shop = _fake_shop(unit, locks, referral=REFERRAL)

    first = await shop.checkout.start_crypto_checkout(
        user_id=BUYER, product=product, use_bonus=True
    )
    second = await shop.checkout.start_crypto_checkout(
        user_id=BUYER, product=product, use_bonus=True
    )

    # 300 bonuses at the product's 100 Stars per USDT are worth 3.00.
    assert first.purchase.bonus_amount == Decimal("3.00")
    assert first.purchase.amount == Decimal("7.00")
    assert second.resumed
    assert second.purchase.id == first.purchase.id
    assert _reserves(unit) == [-300]
    assert unit.users.items[BUYER].bonus_balance == 0


async def test_a_different_bonus_choice_is_reported_not_silently_reused(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    await _grant_bonuses(unit, 300)
    shop = _fake_shop(unit, locks, referral=REFERRAL)
    await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product, use_bonus=True)

    with pytest.raises(PendingInvoiceExistsError) as caught:
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert caught.value.amount == Decimal("7.00")
    assert len(shop.crypto.created) == 1
    assert _reserves(unit) == [-300]


async def test_an_expired_bonus_invoice_gives_its_bonuses_back_before_the_new_one(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    await _grant_bonuses(unit, 300)
    shop = _fake_shop(unit, locks, referral=REFERRAL)
    first = await shop.checkout.start_crypto_checkout(
        user_id=BUYER, product=product, use_bonus=True
    )
    shop.crypto.states[first.purchase.external_id] = PaymentState.EXPIRED

    second = await shop.checkout.start_crypto_checkout(
        user_id=BUYER, product=product, use_bonus=True
    )

    # Released, then reserved again by the new invoice — never held twice.
    assert second.purchase.bonus_amount == Decimal("3.00")
    assert _reserves(unit) == [-300, -300]
    released = [
        entry
        for entry in unit.bonuses.entries
        if entry.type is BonusTransactionType.BONUS_RESERVE_RELEASED
    ]
    assert [entry.purchase_id for entry in released] == [first.purchase.id]
    assert unit.users.items[BUYER].bonus_balance == 0
    assert await unit.bonuses.recompute_balance(BUYER) == 0


async def test_a_plain_invoice_is_not_reused_once_a_discount_is_owed(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    """Handing back a full-price invoice would overcharge a newly invited buyer."""
    shop = _fake_shop(unit, locks, referral=REFERRAL)
    await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    await _invite(unit)

    with pytest.raises(PendingInvoiceExistsError) as caught:
        await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)

    assert caught.value.amount == USDT_PRICE
    assert len(shop.crypto.created) == 1


async def test_stars_pricing_is_untouched(
    unit: FakeUnitOfWork,
    locks: FakeLockManager,
    product: Product,
) -> None:
    await _invite(unit)
    shop = _fake_shop(unit, locks, referral=REFERRAL)

    stars = (await shop.checkout.start_stars_checkout(user_id=BUYER, product=product)).purchase

    assert stars.amount == Decimal(900)
    assert stars.discount_amount == Decimal(100)
    assert shop.crypto.created == []
    assert shop.crypto.lookups == 0


# --------------------------------------------------------------------------- #
# Against the real lock and the real SQL                                      #
# --------------------------------------------------------------------------- #


async def _live_product(
    uow_factory: SqlAlchemyUnitOfWorkFactory,
    *,
    invited: bool = False,
    bonuses: int = 0,
) -> Product:
    async with uow_factory() as uow:
        await uow.users.upsert(UserDraft(telegram_id=BUYER, username="buyer"))
        if invited:
            await uow.users.upsert(UserDraft(telegram_id=INVITER, username="inviter"))
            await uow.referrals.create(
                ReferralDraft(referrer_user_id=INVITER, referred_user_id=BUYER)
            )
        if bonuses:
            await uow.bonuses.add(
                BonusTransactionDraft(
                    user_id=BUYER,
                    amount=bonuses,
                    type=BonusTransactionType.REFERRAL_REWARD,
                )
            )
        return await uow.products.create(_draft())


async def test_live_overlapping_presses_reach_crypto_pay_once(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    """Five presses at once on Redis and PostgreSQL: one invoice, one purchase."""
    product = await _live_product(live_uow_factory)
    shop = _shop(live_uow_factory, live_locks, crypto=ScriptedCryptoPay(stall_seconds=0.05))

    results = await asyncio.gather(
        *(shop.checkout.start_crypto_checkout(user_id=BUYER, product=product) for _ in range(5)),
        return_exceptions=True,
    )

    assert len(shop.crypto.created) == 1
    assert sum(isinstance(result, CryptoCheckout) for result in results) == 1
    assert all(isinstance(result, (CryptoCheckout, LockBusyError)) for result in results), results
    async with live_uow_factory() as uow:
        pending = await uow.purchases.pending_checkouts(BUYER, product.id, PaymentProvider.CRYPTO)
    assert len(pending) == 1


class ExpiredLocks:
    """A lock that has always already expired: every caller gets straight in.

    Stands in for the one case the database index exists for — a checkout that
    outlived its Redis lock — so the test exercises PostgreSQL alone.
    """

    @asynccontextmanager
    async def lock(
        self,
        key: str,
        *,
        ttl_seconds: float | None = None,
        wait_seconds: float = 0.0,
    ) -> AsyncIterator[None]:
        del key, ttl_seconds, wait_seconds
        yield


async def test_live_the_database_alone_keeps_one_pending_invoice(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """With the lock out of the picture, the partial unique index decides."""
    product = await _live_product(live_uow_factory)
    shop = _shop(live_uow_factory, ExpiredLocks(), crypto=ScriptedCryptoPay(stall_seconds=0.05))

    results = await asyncio.gather(
        *(shop.checkout.start_crypto_checkout(user_id=BUYER, product=product) for _ in range(5)),
        return_exceptions=True,
    )

    # Every press reached Crypto Pay: the lock really was bypassed…
    assert len(shop.crypto.created) == 5
    # …and still exactly one invoice was recorded, the rest refused cleanly.
    won = [result for result in results if isinstance(result, CryptoCheckout)]
    refused = [result for result in results if isinstance(result, CheckoutAlreadyPendingError)]
    assert len(won) == 1, results
    assert len(refused) == 4, results
    assert all(error.status_code == HTTPStatus.CONFLICT for error in refused)
    async with live_uow_factory() as uow:
        pending = await uow.purchases.pending_checkouts(BUYER, product.id, PaymentProvider.CRYPTO)
    assert [purchase.id for purchase in pending] == [won[0].purchase.id]

    # The buyer's next press gets the invoice that won, not a sixth one.
    again = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    assert again.resumed
    assert again.purchase.id == won[0].purchase.id
    assert len(shop.crypto.created) == 5


async def test_live_a_losing_insert_releases_nothing_it_did_not_hold(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
) -> None:
    """The refused insert rolls back whole: no stray bonus reserve is left."""
    product = await _live_product(live_uow_factory, bonuses=300)
    shop = _shop(
        live_uow_factory,
        ExpiredLocks(),
        referral=REFERRAL,
        crypto=ScriptedCryptoPay(stall_seconds=0.05),
    )

    results = await asyncio.gather(
        *(
            shop.checkout.start_crypto_checkout(user_id=BUYER, product=product, use_bonus=True)
            for _ in range(2)
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, CryptoCheckout) for result in results) == 1, results
    async with live_uow_factory() as uow:
        balance = await uow.bonuses.balance(BUYER)
        ledger = await uow.bonuses.recompute_balance(BUYER)
    # One reservation of 300, from the invoice that was recorded.
    assert balance == ledger == 0


async def test_live_sequential_presses_keep_one_discounted_invoice(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    product = await _live_product(live_uow_factory, invited=True)
    shop = _shop(live_uow_factory, live_locks, referral=REFERRAL)

    first = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
    for _ in range(5):
        again = await shop.checkout.start_crypto_checkout(user_id=BUYER, product=product)
        assert again.resumed
        assert again.purchase.id == first.purchase.id

    assert first.purchase.amount == Decimal("9.00")
    assert len(shop.crypto.created) == 1


async def test_live_expired_invoice_is_replaced_and_its_bonuses_come_back(
    live_uow_factory: SqlAlchemyUnitOfWorkFactory,
    live_locks: RedisLockManager,
) -> None:
    product = await _live_product(live_uow_factory, bonuses=300)
    shop = _shop(live_uow_factory, live_locks, referral=REFERRAL)
    first = await shop.checkout.start_crypto_checkout(
        user_id=BUYER, product=product, use_bonus=True
    )
    shop.crypto.states[first.purchase.external_id] = PaymentState.EXPIRED

    second = await shop.checkout.start_crypto_checkout(
        user_id=BUYER, product=product, use_bonus=True
    )

    assert second.purchase.amount == Decimal("7.00")
    assert len(shop.crypto.created) == 2
    async with live_uow_factory() as uow:
        old = await uow.purchases.get(first.purchase.id)
        balance = await uow.bonuses.balance(BUYER)
        ledger = await uow.bonuses.recompute_balance(BUYER)
        released = await uow.bonuses.find_for_purchase(
            first.purchase.id, BonusTransactionType.BONUS_RESERVE_RELEASED
        )
    assert old is not None
    assert old.status is PurchaseStatus.EXPIRED
    assert released is not None
    assert released.amount == 300
    assert balance == ledger == 0
