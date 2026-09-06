"""The bonus service's guarantees about failing safely.

The end-to-end file proves the feature works. This one proves the promises made
about what happens when it does not: the master switch really disables
everything, a courtesy message that cannot be delivered really is a non-event,
and a ledger problem really cannot take a delivery down with it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from app.core.config import ReferralSettings
from app.core.exceptions import ServiceUnavailableError
from app.domain.commands import (
    BonusTransactionDraft,
    ProductDraft,
    PurchaseDraft,
    ReferralDraft,
    UserDraft,
)
from app.domain.enums import BonusTransactionType, Currency, PaymentProvider, PurchaseStatus
from app.services.bonuses import BonusService
from tests.fakes import FakeLockManager, FakeUnitOfWork, FakeUnitOfWorkFactory

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.notifications import RewardNotice

INVITER = 4001
INVITED = 4002


class BrokenNotifier:
    """A transport that refuses both messages."""

    async def notify_reward(self, notice: RewardNotice) -> None:
        del notice
        message = "chat unavailable"
        raise RuntimeError(message)

    async def notify_purchase_complete(self, user_id: int) -> None:
        del user_id
        message = "chat unavailable"
        raise RuntimeError(message)


class RecordingNotifier:
    """Captures what would have been sent."""

    def __init__(self) -> None:
        self.rewards: list[RewardNotice] = []
        self.hints: list[int] = []

    async def notify_reward(self, notice: RewardNotice) -> None:
        self.rewards.append(notice)

    async def notify_purchase_complete(self, user_id: int) -> None:
        self.hints.append(user_id)


@pytest.fixture
def unit() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def uow_factory(unit: FakeUnitOfWork) -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory(unit)


def _service(
    uow_factory: FakeUnitOfWorkFactory,
    *,
    enabled: bool = True,
    notifier: object | None = None,
) -> BonusService:
    return BonusService(
        uow_factory=uow_factory,
        locks=FakeLockManager(),
        settings=ReferralSettings(enabled=enabled),
        notifier=notifier,  # type: ignore[arg-type]
    )


async def _delivered_purchase(unit: FakeUnitOfWork, *, referred: bool = True) -> UUID:
    """A settled sale by an invited buyer, ready to earn a reward."""
    await unit.users.upsert(UserDraft(telegram_id=INVITER))
    await unit.users.upsert(UserDraft(telegram_id=INVITED))
    if referred:
        await unit.referrals.create(
            ReferralDraft(referrer_user_id=INVITER, referred_user_id=INVITED)
        )
    product = await unit.products.create(
        ProductDraft(
            slug=f"item{uuid4().hex[:6]}",
            title="Item",
            description="",
            delivery_url="https://t.me/+x",
            price_stars=1000,
        )
    )
    purchase = await unit.purchases.create(
        PurchaseDraft(
            user_id=INVITED,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            amount=Decimal(900),
            base_amount=Decimal(1000),
            discount_amount=Decimal(100),
            currency=Currency.XTR,
            external_id=uuid4().hex,
            status=PurchaseStatus.PAID,
        )
    )
    await unit.purchases.mark_delivered(purchase.id, delivered_url="https://t.me/+x")
    return purchase.id


# --- The master switch


async def test_nothing_is_accrued_while_the_programme_is_off(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
) -> None:
    purchase_id = await _delivered_purchase(unit)
    service = _service(uow_factory, enabled=False, notifier=RecordingNotifier())

    await service.accrue(purchase_id)

    assert await service.balance_of(INVITER) == 0


async def test_no_housekeeping_runs_while_the_programme_is_off(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
) -> None:
    await _delivered_purchase(unit)
    service = _service(uow_factory, enabled=False)

    assert await service.accrue_missing() == 0
    assert await service.release_stale_holds() == 0


async def test_no_buyer_is_pestered_while_the_programme_is_off(
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    notifier = RecordingNotifier()
    service = _service(uow_factory, enabled=False, notifier=notifier)

    await service.invite_buyer(INVITED)

    assert notifier.hints == []


async def test_a_switched_off_programme_prices_at_the_list_price(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
) -> None:
    """Even an existing relationship changes nothing while the switch is off."""
    await _delivered_purchase(unit)
    service = _service(uow_factory, enabled=False)

    priced = await service.preview(
        user_id=INVITED,
        base_amount=Decimal(1000),
        currency=Currency.XTR,
        use_bonus=True,
    )

    assert priced.charged_amount == Decimal(1000)
    assert priced.is_plain


# --- Failing safely


async def test_a_reward_survives_a_notifier_that_raises(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
) -> None:
    """The message is a courtesy; the money is already in the ledger."""
    purchase_id = await _delivered_purchase(unit)
    service = _service(uow_factory, notifier=BrokenNotifier())

    await service.accrue(purchase_id)

    assert await service.balance_of(INVITER) == 135


async def test_a_broken_notifier_does_not_break_the_buyer_invitation(
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    service = _service(uow_factory, notifier=BrokenNotifier())

    await service.invite_buyer(INVITED)  # must simply return


async def test_a_reward_for_an_unknown_purchase_is_reported_not_raised(
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    service = _service(uow_factory)

    await service.accrue(uuid4())  # must simply return


async def test_a_ledger_failure_never_reaches_the_caller(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
) -> None:
    """``accrue`` is called straight after a delivery, so it cannot raise.

    A buyer who paid must keep their link whatever the ledger thinks.
    """
    purchase_id = await _delivered_purchase(unit)
    service = _service(uow_factory)

    async def explode(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ServiceUnavailableError

    unit.bonuses.add = explode  # type: ignore[method-assign]

    await service.accrue(purchase_id)

    assert await service.balance_of(INVITER) == 0


async def test_a_purchase_with_no_inviter_earns_nothing_quietly(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
) -> None:
    purchase_id = await _delivered_purchase(unit, referred=False)
    service = _service(uow_factory, notifier=RecordingNotifier())

    assert await service.accrue_once(purchase_id) is False


async def test_a_reward_too_small_to_express_is_not_written(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
) -> None:
    """A ledger entry of zero units would be noise, and the schema forbids it."""
    from app.domain.commands import ReferralDraft  # noqa: PLC0415

    await unit.users.upsert(UserDraft(telegram_id=INVITER))
    await unit.users.upsert(UserDraft(telegram_id=INVITED))
    await unit.referrals.create(ReferralDraft(referrer_user_id=INVITER, referred_user_id=INVITED))
    product = await unit.products.create(
        ProductDraft(
            slug=f"tiny{uuid4().hex[:6]}",
            title="Tiny",
            description="",
            delivery_url="https://t.me/+x",
            price_stars=6,
        )
    )
    purchase = await unit.purchases.create(
        PurchaseDraft(
            user_id=INVITED,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            amount=Decimal(6),
            base_amount=Decimal(6),
            currency=Currency.XTR,
            external_id=uuid4().hex,
            status=PurchaseStatus.PAID,
        )
    )
    await unit.purchases.mark_delivered(purchase.id, delivered_url="https://t.me/+x")
    service = _service(uow_factory)

    assert await service.accrue_once(purchase.id) is False
    assert await service.balance_of(INVITER) == 0


# --- Offers


async def test_no_offer_is_made_without_a_balance(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
) -> None:
    await unit.users.upsert(UserDraft(telegram_id=INVITER))
    service = _service(uow_factory)

    offer = await service.offer_for(
        user_id=INVITER,
        base_amount=Decimal(1000),
        currency=Currency.XTR,
    )

    assert not offer.available
    assert offer.quote_without_bonus.charged_amount == Decimal(1000)


async def test_no_offer_is_made_when_a_discount_applies(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
) -> None:
    """A first referral purchase takes the discount, not the balance."""
    purchase_id = await _delivered_purchase(unit)
    service = _service(uow_factory)
    await service.accrue(purchase_id)
    # The inviter now has a balance, and is themselves invited by nobody, so
    # give them a referrer to make the discount applicable.
    from app.domain.commands import ReferralDraft  # noqa: PLC0415

    third = 4003
    await unit.users.upsert(UserDraft(telegram_id=third))
    await unit.referrals.create(ReferralDraft(referrer_user_id=third, referred_user_id=INVITER))

    offer = await service.offer_for(
        user_id=INVITER,
        base_amount=Decimal(1000),
        currency=Currency.XTR,
    )

    assert not offer.available
    assert offer.quote_without_bonus.discount_amount == Decimal(100)


async def test_an_offer_prices_both_branches(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
) -> None:
    purchase_id = await _delivered_purchase(unit)
    service = _service(uow_factory)
    await service.accrue(purchase_id)

    offer = await service.offer_for(
        user_id=INVITER,
        base_amount=Decimal(1000),
        currency=Currency.XTR,
    )

    assert offer.available
    assert offer.balance == 135
    assert offer.units == 135
    assert offer.quote_with_bonus is not None
    assert offer.quote_with_bonus.charged_amount == Decimal(865)
    assert offer.quote_without_bonus.charged_amount == Decimal(1000)


# --- The two pricing paths must agree
#
# There are two of them, and they run at different moments. ``quote_amount`` ->
# ``preview`` is read-only and runs *before* the purchase exists, because
# CryptoBot assigns the invoice id that later webhooks are matched on, so the
# invoice has to be raised first. ``start_purchase`` -> ``resolve`` then prices
# the sale authoritatively inside the transaction and refuses to record it at a
# number other than the one already invoiced.
#
# That refusal is a safety net, not a feature: whenever the two disagree the
# buyer simply cannot pay. So the agreement between them is the real contract,
# and this is the only place that holds them to it.

STARS_PRICE = Decimal(1000)
CRYPTO_PRICE = Decimal("1.50")

RAILS = pytest.mark.parametrize(
    ("currency", "price"),
    [(Currency.XTR, STARS_PRICE), (Currency.USDT, CRYPTO_PRICE)],
    ids=["stars", "crypto"],
)


async def _link(unit: FakeUnitOfWork, *, referrer: int, referred: int) -> None:
    """An invited buyer who has not bought anything yet."""
    await unit.users.upsert(UserDraft(telegram_id=referrer))
    await unit.users.upsert(UserDraft(telegram_id=referred))
    await unit.referrals.create(ReferralDraft(referrer_user_id=referrer, referred_user_id=referred))


async def _grant(unit: FakeUnitOfWork, user_id: int, units: int) -> None:
    await unit.users.upsert(UserDraft(telegram_id=user_id))
    if units:
        await unit.bonuses.add(
            BonusTransactionDraft(
                user_id=user_id,
                amount=units,
                type=BonusTransactionType.REFERRAL_REWARD,
            )
        )


@RAILS
async def test_an_invited_buyers_first_purchase_previews_at_the_discounted_price(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
    currency: Currency,
    price: Decimal,
) -> None:
    """The bug this test exists for, on the rail that had it.

    ``offer_for`` used to return the *list* price for anything that was not
    Stars, on the grounds that bonuses cannot be spent there. But the referral
    discount is not a bonus — it is a percentage of the price, and it applies
    to crypto too. So the card offered 1.35, the CryptoBot invoice was raised
    for 1.50, and ``start_purchase`` refused the mismatch: every discounted
    crypto checkout died with "не удалось создать счёт на оплату".
    """
    await _link(unit, referrer=INVITER, referred=INVITED)
    service = _service(uow_factory)

    previewed = await service.preview(
        user_id=INVITED,
        base_amount=price,
        currency=currency,
        use_bonus=False,
    )

    expected = Decimal(900) if currency is Currency.XTR else Decimal("1.35")
    assert previewed.charged_amount == expected


@dataclass(frozen=True, slots=True)
class Checkout:
    """One situation a buyer can be in when they press a payment button."""

    currency: Currency
    price: Decimal
    invited: bool
    balance: int


@pytest.mark.parametrize(
    "case",
    [
        Checkout(currency=currency, price=price, invited=invited, balance=balance)
        for currency, price in ((Currency.XTR, STARS_PRICE), (Currency.USDT, CRYPTO_PRICE))
        for invited in (True, False)
        for balance in (0, 300)
    ],
    ids=lambda case: (
        f"{'stars' if case.currency is Currency.XTR else 'crypto'}"
        f"-{'invited' if case.invited else 'ordinary'}"
        f"-{'funded' if case.balance else 'broke'}"
    ),
)
async def test_the_previewed_price_is_what_the_purchase_is_recorded_at(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
    case: Checkout,
) -> None:
    """Every situation the bot can actually reach, priced both ways.

    ``use_bonus`` is derived the way the bot derives it — from the offer — so
    this covers exactly the inputs production produces, and nothing that only a
    hand-crafted callback could send.
    """
    if case.invited:
        await _link(unit, referrer=INVITER, referred=INVITED)
    await _grant(unit, INVITED, case.balance)
    service = _service(uow_factory)

    offer = await service.offer_for(
        user_id=INVITED,
        base_amount=case.price,
        currency=case.currency,
    )
    use_bonus = offer.available

    previewed = await service.preview(
        user_id=INVITED,
        base_amount=case.price,
        currency=case.currency,
        use_bonus=use_bonus,
    )
    resolved = await service.resolve(
        unit,
        user_id=INVITED,
        base_amount=case.price,
        currency=case.currency,
        use_bonus=use_bonus,
    )

    assert previewed.charged_amount == resolved.charged_amount
    assert previewed.discount_amount == resolved.discount_amount
    assert previewed.bonus_units == resolved.bonus_units


@RAILS
async def test_the_offer_reports_the_price_the_card_shows(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
    currency: Currency,
    price: Decimal,
) -> None:
    """``quote_without_bonus`` is a charge, not a list price.

    The product card computes the discount independently, so if the offer
    disagreed with it the buyer would be shown one number and billed another.
    """
    await _link(unit, referrer=INVITER, referred=INVITED)
    service = _service(uow_factory)

    offer = await service.offer_for(user_id=INVITED, base_amount=price, currency=currency)

    async with uow_factory() as uow:
        eligible = await service.discount_eligible(uow, INVITED)

    assert eligible
    assert offer.quote_without_bonus.charged_amount < price
    assert offer.quote_without_bonus.discount_amount > 0


async def test_bonuses_are_still_never_offered_on_a_crypto_card(
    uow_factory: FakeUnitOfWorkFactory,
    unit: FakeUnitOfWork,
) -> None:
    """A bonus is a Star. Fixing the discount must not have loosened that."""
    await _grant(unit, INVITED, 5000)
    service = _service(uow_factory)

    offer = await service.offer_for(
        user_id=INVITED,
        base_amount=CRYPTO_PRICE,
        currency=Currency.USDT,
    )

    assert not offer.available
    assert offer.units == 0
    assert offer.quote_with_bonus is None
    assert offer.balance == 5000, "the balance is reported honestly, it just cannot be spent here"
