"""One pricing path for card, preview, invoice and purchase (Stage 3, step 3a).

``PricingService`` orchestrates what ``BonusService`` already prices and puts the
loyalty programme's 50% discount between them:

    Referral > Loyalty > Bonus > list price — never two at once.

The real loyalty side is wired in a later step. Here it is a scripted double, so
these tests pin down the orchestration itself: the precedence, the "bonuses are
silently dropped when loyalty applies" rule, that a zero discount never spends
the reward, and — with loyalty off — that nothing at all changed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from app.bot.texts import product_card
from app.core.config import ReferralSettings
from app.core.exceptions import BonusesNotAvailableError
from app.domain.bonuses import PriceQuote
from app.domain.commands import (
    BonusTransactionDraft,
    ProductDraft,
    ReferralDraft,
    UserDraft,
)
from app.domain.enums import BonusTransactionType, Currency, PaymentProvider
from app.domain.loyalty import LOYALTY_DISCOUNT_PERCENT, loyalty_discount_for, loyalty_quote
from app.services.bonuses import BonusService
from app.services.pricing import PricingService
from app.services.purchases import PurchaseService
from tests.fakes import FakeLockManager, FakeUnitOfWork, FakeUnitOfWorkFactory

if TYPE_CHECKING:
    from app.domain.entities import Product, Purchase
    from app.domain.uow import UnitOfWork

BUYER = 9401
INVITER = 9402
REFERRAL = ReferralSettings(
    enabled=True,
    discount_percent=10,
    reward_percent=15,
    max_bonus_payment_percent=50,
)
STARS = Currency.XTR
USDT = Currency.USDT


# --------------------------------------------------------------------------- #
# The 50% arithmetic                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("base", "currency", "discount"),
    [
        (Decimal(1000), STARS, Decimal(500)),
        (Decimal(999), STARS, Decimal(499)),  # 499.5 floors to a whole Star
        (Decimal(3), STARS, Decimal(1)),
        (Decimal(2), STARS, Decimal(1)),
        (Decimal(1), STARS, Decimal(0)),  # half a Star cannot be charged
        (Decimal("10.00"), USDT, Decimal("5.00")),
        (Decimal("9.99"), USDT, Decimal("4.99")),  # 4.995 floors to a cent
        (Decimal("0.03"), USDT, Decimal("0.01")),
        (Decimal("0.01"), USDT, Decimal("0.00")),  # half a cent cannot be charged
    ],
)
def test_the_loyalty_discount_is_half_rounded_down_to_the_rails_unit(
    base: Decimal,
    currency: Currency,
    discount: Decimal,
) -> None:
    assert LOYALTY_DISCOUNT_PERCENT == 50
    assert loyalty_discount_for(base, currency) == discount


def test_a_loyalty_quote_explains_its_own_charge() -> None:
    reward = uuid4()
    priced = loyalty_quote(Decimal(999), STARS, reward_id=reward)

    assert priced is not None
    assert priced.loyalty_amount == Decimal(499)
    assert priced.charged_amount == Decimal(500)
    assert priced.uses_loyalty
    assert not priced.uses_discount
    assert not priced.uses_bonus
    assert not priced.is_plain
    assert priced.loyalty_reward_id == reward
    assert priced.charged_amount == (
        priced.base_amount - priced.discount_amount - priced.loyalty_amount - priced.bonus_amount
    )


@pytest.mark.parametrize(("base", "currency"), [(Decimal(1), STARS), (Decimal("0.01"), USDT)])
def test_a_zero_loyalty_discount_is_no_quote_at_all(base: Decimal, currency: Currency) -> None:
    """``None`` tells the caller not to spend the reward on nothing."""
    assert loyalty_quote(base, currency) is None


def test_existing_quotes_carry_no_loyalty() -> None:
    plain = PriceQuote(base_amount=Decimal(100))
    assert plain.loyalty_amount == 0
    assert not plain.uses_loyalty
    assert plain.is_plain
    assert plain.charged_amount == Decimal(100)


# --------------------------------------------------------------------------- #
# A scripted loyalty side                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class ScriptedLoyalty:
    """Answers like the loyalty programme would, and records what it was asked."""

    enabled: bool = True
    available: bool = True
    reward_id: UUID = field(default_factory=uuid4)
    locked: list[int] = field(default_factory=list)
    applied: list[tuple[UUID, Decimal]] = field(default_factory=list)
    consumed: list[UUID] = field(default_factory=list)
    released: list[UUID] = field(default_factory=list)

    async def discount_available(self, uow: UnitOfWork, user_id: int) -> bool:
        del uow, user_id
        return self.available

    async def lock_discount(self, uow: UnitOfWork, user_id: int) -> UUID | None:
        del uow
        self.locked.append(user_id)
        return self.reward_id if self.available else None

    async def apply_discount(self, uow: UnitOfWork, purchase: Purchase, priced: PriceQuote) -> None:
        del uow
        assert priced.loyalty_reward_id == self.reward_id
        self.applied.append((purchase.id, priced.loyalty_amount))

    async def consume_discount(self, uow: UnitOfWork, purchase: Purchase) -> None:
        del uow
        self.consumed.append(purchase.id)

    async def release_discount(self, uow: UnitOfWork, purchase: Purchase) -> None:
        del uow
        self.released.append(purchase.id)


@dataclass(frozen=True, slots=True)
class Shop:
    unit: FakeUnitOfWork
    bonuses: BonusService
    pricing: PricingService
    purchases: PurchaseService


def _shop(loyalty: ScriptedLoyalty | None = None) -> Shop:
    unit = FakeUnitOfWork()
    factory = FakeUnitOfWorkFactory(unit)
    locks = FakeLockManager()
    bonuses = BonusService(uow_factory=factory, locks=locks, settings=REFERRAL)
    pricing = PricingService(uow_factory=factory, bonuses=bonuses, loyalty=loyalty)
    purchases = PurchaseService(uow_factory=factory, locks=locks, pricing=pricing)
    return Shop(unit=unit, bonuses=bonuses, pricing=pricing, purchases=purchases)


async def _buyer(
    shop: Shop,
    *,
    invited: bool = False,
    bonuses: int = 0,
    stars: int | None = 1000,
    usdt: str | None = "10.00",
) -> Product:
    await shop.unit.users.upsert(UserDraft(telegram_id=BUYER))
    if invited:
        await shop.unit.users.upsert(UserDraft(telegram_id=INVITER))
        await shop.unit.referrals.create(
            ReferralDraft(referrer_user_id=INVITER, referred_user_id=BUYER)
        )
    if bonuses:
        await shop.unit.bonuses.add(
            BonusTransactionDraft(
                user_id=BUYER,
                amount=bonuses,
                type=BonusTransactionType.REFERRAL_REWARD,
            )
        )
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


async def _all_paths(
    shop: Shop,
    product: Product,
    provider: PaymentProvider,
    *,
    use_bonus: bool = False,
) -> dict[str, Decimal]:
    """The same checkout priced by every path a buyer meets."""
    base = Decimal(product.price_for(provider) or 0)
    currency = provider.currency
    async with FakeUnitOfWorkFactory(shop.unit)() as uow:
        card = await shop.pricing.card_quote(
            uow,
            user_id=BUYER,
            base_amount=base,
            currency=currency,
            stars_per_usdt=product.stars_per_usdt,
        )
        resolved = await shop.pricing.resolve(
            uow,
            user_id=BUYER,
            base_amount=base,
            currency=currency,
            use_bonus=use_bonus,
            stars_per_usdt=product.stars_per_usdt,
        )
    preview = await shop.pricing.preview(
        user_id=BUYER,
        base_amount=base,
        currency=currency,
        use_bonus=use_bonus,
        stars_per_usdt=product.stars_per_usdt,
    )
    return {
        "card": card.charged_amount,
        "preview": preview.charged_amount,
        "resolve": resolved.charged_amount,
    }


# --------------------------------------------------------------------------- #
# Loyalty off (or absent): nothing changed                                    #
# --------------------------------------------------------------------------- #


_SCENARIOS = [
    pytest.param({}, PaymentProvider.STARS, False, id="plain-stars"),
    pytest.param({}, PaymentProvider.CRYPTO, False, id="plain-usdt"),
    pytest.param({"invited": True}, PaymentProvider.STARS, False, id="referral-stars"),
    pytest.param({"invited": True}, PaymentProvider.CRYPTO, False, id="referral-usdt"),
    pytest.param({"bonuses": 300}, PaymentProvider.STARS, True, id="bonus-stars"),
    pytest.param({"bonuses": 300}, PaymentProvider.CRYPTO, True, id="bonus-usdt"),
    pytest.param({"bonuses": 300}, PaymentProvider.STARS, False, id="bonus-declined"),
]


@pytest.mark.parametrize("loyalty", [None, ScriptedLoyalty(enabled=False)], ids=["absent", "off"])
@pytest.mark.parametrize(("setup", "provider", "use_bonus"), _SCENARIOS)
async def test_without_loyalty_pricing_is_exactly_the_bonus_service(
    loyalty: ScriptedLoyalty | None,
    setup: dict[str, object],
    provider: PaymentProvider,
    use_bonus: bool,
) -> None:
    shop = _shop(loyalty)
    product = await _buyer(
        shop,
        invited=bool(setup.get("invited", False)),
        bonuses=int(str(setup.get("bonuses", 0))),
    )
    base = Decimal(product.price_for(provider) or 0)
    currency = provider.currency
    rate = product.stars_per_usdt
    async with FakeUnitOfWorkFactory(shop.unit)() as uow:
        assert await shop.pricing.resolve(
            uow,
            user_id=BUYER,
            base_amount=base,
            currency=currency,
            use_bonus=use_bonus,
            stars_per_usdt=rate,
        ) == await shop.bonuses.resolve(
            uow,
            user_id=BUYER,
            base_amount=base,
            currency=currency,
            use_bonus=use_bonus,
            stars_per_usdt=rate,
        )
        assert await shop.pricing.card_quote(
            uow, user_id=BUYER, base_amount=base, currency=currency, stars_per_usdt=rate
        ) == await shop.bonuses.card_quote(
            uow, user_id=BUYER, base_amount=base, currency=currency, stars_per_usdt=rate
        )
    assert await shop.pricing.preview(
        user_id=BUYER, base_amount=base, currency=currency, use_bonus=use_bonus, stars_per_usdt=rate
    ) == await shop.bonuses.preview(
        user_id=BUYER, base_amount=base, currency=currency, use_bonus=use_bonus, stars_per_usdt=rate
    )
    assert await shop.pricing.offer_for(
        user_id=BUYER, base_amount=base, currency=currency, stars_per_usdt=rate
    ) == await shop.bonuses.offer_for(
        user_id=BUYER, base_amount=base, currency=currency, stars_per_usdt=rate
    )
    if loyalty is not None:
        assert loyalty.locked == []


async def test_without_loyalty_errors_are_the_bonus_service_errors() -> None:
    """Bonuses on a USDT-only product are still refused exactly as before."""
    shop = _shop(ScriptedLoyalty(enabled=False))
    product = await _buyer(shop, bonuses=300, stars=None)

    async with FakeUnitOfWorkFactory(shop.unit)() as uow:
        with pytest.raises(BonusesNotAvailableError):
            await shop.pricing.resolve(
                uow,
                user_id=BUYER,
                base_amount=Decimal("10.00"),
                currency=USDT,
                use_bonus=True,
                stars_per_usdt=product.stars_per_usdt,
            )


# --------------------------------------------------------------------------- #
# Loyalty on: the precedence                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("provider", "expected"),
    [(PaymentProvider.STARS, Decimal(500)), (PaymentProvider.CRYPTO, Decimal("5.00"))],
)
async def test_loyalty_is_one_price_on_every_path(
    provider: PaymentProvider,
    expected: Decimal,
) -> None:
    """Card == preview == the amount the purchase is recorded at."""
    shop = _shop(ScriptedLoyalty())
    product = await _buyer(shop)

    prices = await _all_paths(shop, product, provider)

    assert prices == {"card": expected, "preview": expected, "resolve": expected}


async def test_the_referral_discount_wins_over_loyalty() -> None:
    loyalty = ScriptedLoyalty()
    shop = _shop(loyalty)
    product = await _buyer(shop, invited=True)

    prices = await _all_paths(shop, product, PaymentProvider.STARS)

    # 10% referral discount: 1000 → 900, not the loyalty 500.
    assert prices == {"card": Decimal(900), "preview": Decimal(900), "resolve": Decimal(900)}
    assert loyalty.locked == []


async def test_loyalty_wins_over_bonuses_and_never_combines_with_them() -> None:
    """Decision C: bonuses asked for anyway are silently dropped, not combined."""
    shop = _shop(ScriptedLoyalty())
    product = await _buyer(shop, bonuses=300)

    prices = await _all_paths(shop, product, PaymentProvider.STARS, use_bonus=True)

    assert prices == {"card": Decimal(500), "preview": Decimal(500), "resolve": Decimal(500)}


async def test_no_bonus_question_is_asked_while_loyalty_applies() -> None:
    shop = _shop(ScriptedLoyalty())
    await _buyer(shop, bonuses=300)

    offer = await shop.pricing.offer_for(
        user_id=BUYER,
        base_amount=Decimal(1000),
        currency=STARS,
    )

    assert not offer.available
    assert offer.balance == 300
    assert offer.quote_without_bonus.charged_amount == Decimal(500)
    assert offer.quote_without_bonus.uses_loyalty


async def test_a_zero_loyalty_discount_does_not_spend_the_reward() -> None:
    """A 1 Star pack cannot be halved: the list price is charged, nothing applied."""
    loyalty = ScriptedLoyalty()
    shop = _shop(loyalty)
    product = await _buyer(shop, stars=1, usdt=None)

    async with FakeUnitOfWorkFactory(shop.unit)() as uow:
        priced = await shop.pricing.resolve(
            uow,
            user_id=BUYER,
            base_amount=Decimal(1),
            currency=STARS,
            use_bonus=False,
            stars_per_usdt=product.stars_per_usdt,
        )
    assert priced.is_plain
    assert priced.charged_amount == Decimal(1)
    assert loyalty.applied == []


async def test_an_unavailable_reward_leaves_bonuses_as_they_were() -> None:
    shop = _shop(ScriptedLoyalty(available=False))
    product = await _buyer(shop, bonuses=300)

    prices = await _all_paths(shop, product, PaymentProvider.STARS, use_bonus=True)

    assert prices == {"card": Decimal(1000), "preview": Decimal(700), "resolve": Decimal(700)}


# --------------------------------------------------------------------------- #
# The holds follow the purchase                                               #
# --------------------------------------------------------------------------- #


async def test_a_loyalty_checkout_records_its_discount_and_reserves_no_bonuses() -> None:
    loyalty = ScriptedLoyalty()
    shop = _shop(loyalty)
    product = await _buyer(shop, bonuses=300)

    purchase = await shop.purchases.start_purchase(
        user_id=BUYER,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-1",
        use_bonus=True,
    )

    assert purchase.amount == Decimal(500)
    assert purchase.base_amount == Decimal(1000)
    assert purchase.loyalty_amount == Decimal(500)
    assert purchase.discount_amount == 0
    assert purchase.bonus_amount == 0
    assert loyalty.applied == [(purchase.id, Decimal(500))]
    # Not a single bonus moved.
    assert shop.unit.bonuses.entries[-1].type is BonusTransactionType.REFERRAL_REWARD
    assert shop.unit.users.items[BUYER].bonus_balance == 300


async def test_payment_and_expiry_reach_the_loyalty_side() -> None:
    loyalty = ScriptedLoyalty()
    shop = _shop(loyalty)
    product = await _buyer(shop)
    paid = await shop.purchases.start_purchase(
        user_id=BUYER,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-paid",
    )
    await shop.purchases.confirm_payment(provider=PaymentProvider.STARS, external_id="stars-paid")

    async with FakeUnitOfWorkFactory(shop.unit)() as uow:
        await shop.pricing.on_checkout_expired(uow, paid)

    assert loyalty.consumed == [paid.id]
    assert loyalty.released == [paid.id]


async def test_a_reward_already_priced_in_is_settled_even_after_switching_off() -> None:
    """A buyer billed at half price used their reward, whatever the switch says now."""
    loyalty = ScriptedLoyalty()
    shop = _shop(loyalty)
    product = await _buyer(shop)
    purchase = await shop.purchases.start_purchase(
        user_id=BUYER,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-switch",
    )

    loyalty.enabled = False
    await shop.purchases.confirm_payment(provider=PaymentProvider.STARS, external_id="stars-switch")

    assert loyalty.consumed == [purchase.id]


async def test_a_plain_purchase_never_touches_the_loyalty_side() -> None:
    loyalty = ScriptedLoyalty(available=False)
    shop = _shop(loyalty)
    product = await _buyer(shop)
    purchase = await shop.purchases.start_purchase(
        user_id=BUYER,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-plain",
    )
    await shop.purchases.confirm_payment(provider=PaymentProvider.STARS, external_id="stars-plain")

    assert purchase.loyalty_amount == 0
    assert loyalty.applied == []
    assert loyalty.consumed == []


# --------------------------------------------------------------------------- #
# The card                                                                    #
# --------------------------------------------------------------------------- #


async def test_the_card_shows_the_loyalty_price_and_says_why() -> None:
    shop = _shop(ScriptedLoyalty())
    product = await _buyer(shop)

    card = await shop.purchases.open_card(UserDraft(telegram_id=BUYER), product.slug)

    assert [option.discounted_amount for option in card.options] == [
        Decimal(500),
        Decimal("5.00"),
    ]
    assert card.loyalty_percent == 50
    assert card.discount_percent == 0
    assert card.is_loyalty_discounted
    text = product_card(card)
    assert "<s>1000</s> 500 XTR" in text
    assert "<s>10</s> 5 USDT" in text
    assert "🏆 Скидка 50% по программе лояльности" in text
    assert "по приглашению" not in text


async def test_the_card_keeps_the_referral_wording_when_the_referral_wins() -> None:
    shop = _shop(ScriptedLoyalty())
    product = await _buyer(shop, invited=True)

    card = await shop.purchases.open_card(UserDraft(telegram_id=BUYER), product.slug)

    assert card.discount_percent == 10
    assert card.loyalty_percent == 0
    text = product_card(card)
    assert "🎁 Скидка 10% по приглашению" in text
    assert "лояльности" not in text


async def test_the_card_is_unchanged_without_loyalty() -> None:
    shop = _shop(None)
    product = await _buyer(shop)

    card = await shop.purchases.open_card(UserDraft(telegram_id=BUYER), product.slug)

    assert [option.discounted_amount for option in card.options] == [None, None]
    assert card.loyalty_percent == 0
    assert not card.is_loyalty_discounted
    assert "Скидка" not in product_card(card)


async def test_an_unavailable_reward_without_bonuses_is_the_list_price_everywhere() -> None:
    shop = _shop(ScriptedLoyalty(available=False))
    product = await _buyer(shop)

    prices = await _all_paths(shop, product, PaymentProvider.STARS)

    assert prices == {"card": Decimal(1000), "preview": Decimal(1000), "resolve": Decimal(1000)}


async def test_a_card_never_shows_a_zero_loyalty_discount() -> None:
    shop = _shop(ScriptedLoyalty())
    product = await _buyer(shop, stars=1, usdt=None)

    card = await shop.purchases.open_card(UserDraft(telegram_id=BUYER), product.slug)

    assert [option.discounted_amount for option in card.options] == [None]
    assert card.loyalty_percent == 0
    assert not card.is_loyalty_discounted


async def test_the_bonus_question_is_unchanged_when_no_reward_is_due() -> None:
    shop = _shop(ScriptedLoyalty(available=False))
    await _buyer(shop, bonuses=300)

    offer = await shop.pricing.offer_for(user_id=BUYER, base_amount=Decimal(1000), currency=STARS)

    assert offer == await shop.bonuses.offer_for(
        user_id=BUYER, base_amount=Decimal(1000), currency=STARS
    )
    assert offer.available


async def test_an_expiring_plain_invoice_never_touches_the_loyalty_side() -> None:
    loyalty = ScriptedLoyalty(available=False)
    shop = _shop(loyalty)
    product = await _buyer(shop)
    purchase = await shop.purchases.start_purchase(
        user_id=BUYER,
        product_id=product.id,
        provider=PaymentProvider.STARS,
        external_id="stars-expiring",
    )

    async with FakeUnitOfWorkFactory(shop.unit)() as uow:
        await shop.pricing.on_checkout_expired(uow, purchase)

    assert loyalty.released == []
