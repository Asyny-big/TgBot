"""The arithmetic that decides what a buyer is charged and what an inviter earns.

These are the rules the shop's economics rest on, and they are pure functions,
so they are tested here with literals rather than through a database. Every case
below is a rule somebody could get wrong in a way that costs real money.

The model in one line: **one bonus is one Telegram Star.**
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.bonuses import (
    BonusPolicy,
    discount_for,
    max_bonus_payment,
    plain_quote,
    quote,
    reward_bonuses,
    spendable_bonuses,
    stars_equivalent,
)
from app.domain.enums import Currency

POLICY = BonusPolicy(
    discount_percent=10,
    reward_percent=15,
    max_bonus_payment_percent=50,
)

STARS = Currency.XTR
USDT = Currency.USDT


# --- The referral discount


def test_the_discount_is_ten_percent_of_the_list_price() -> None:
    """The worked example from the specification: 1000 Stars becomes 900."""
    assert discount_for(Decimal(1000), STARS, POLICY) == Decimal(100)


def test_a_stars_discount_is_always_a_whole_number_of_stars() -> None:
    """Telegram Stars are integers, so a fractional discount cannot exist.

    Rounding down is what keeps ``int(Decimal(amount))`` in the Stars gateway
    honest: 999 minus a floored 10% is 900, and no fraction disappears silently.
    """
    assert discount_for(Decimal(999), STARS, POLICY) == Decimal(99)
    assert Decimal(999) - discount_for(Decimal(999), STARS, POLICY) == Decimal(900)


def test_a_usdt_discount_is_rounded_down_to_a_cent() -> None:
    """USDT is charged in hundredths; a third of a cent is not chargeable."""
    assert discount_for(Decimal("12.35"), USDT, POLICY) == Decimal("1.23")


def test_a_tiny_price_simply_gets_no_discount() -> None:
    """One Star cannot be discounted by a tenth of a Star, so it is not."""
    assert discount_for(Decimal(1), STARS, POLICY) == Decimal(0)


def test_a_zero_percent_discount_is_no_discount() -> None:
    """Configuring the programme off must not produce rounding artefacts."""
    policy = BonusPolicy(discount_percent=0)
    assert discount_for(Decimal(1000), STARS, policy) == Decimal(0)


# --- The inviter's reward, from a Stars purchase


def test_a_full_price_stars_purchase_earns_fifteen_percent() -> None:
    """The specification's headline case: 1000 ⭐ paid earns 150 bonuses."""
    assert reward_bonuses(Decimal(1000), STARS, policy=POLICY) == 150


def test_the_reward_is_taken_from_what_was_actually_paid() -> None:
    """A buyer who paid 900 after a discount earns the inviter 135, not 150.

    Paying 15% of the list price would quietly overpay every inviter on every
    discounted first purchase.
    """
    assert reward_bonuses(Decimal(900), STARS, policy=POLICY) == 135


def test_every_later_purchase_earns_its_own_reward() -> None:
    """The inviter earns from each settled purchase, not just the first."""
    assert reward_bonuses(Decimal(1200), STARS, policy=POLICY) == 180
    assert reward_bonuses(Decimal(700), STARS, policy=POLICY) == 105


def test_a_stars_reward_is_floored() -> None:
    """101 ⭐ at 15% is 15.15 bonuses, which is 15 bonuses."""
    assert reward_bonuses(Decimal(101), STARS, policy=POLICY) == 15


def test_a_reward_too_small_to_express_is_zero() -> None:
    """Six Stars at 15% is 0.9 of a bonus, which is not a bonus."""
    assert reward_bonuses(Decimal(6), STARS, policy=POLICY) == 0


# --- The inviter's reward, from a USDT purchase


def test_a_usdt_purchase_is_converted_through_the_products_own_rate() -> None:
    """The specification's example: 10 USDT at 70 ⭐/USDT earns 105 bonuses.

    The rate is not fetched from anywhere — it is what the shop itself declared
    by pricing the product in both currencies.
    """
    assert stars_equivalent(Decimal(10), USDT, stars_per_usdt=Decimal(70)) == Decimal(700)
    assert reward_bonuses(Decimal(10), USDT, policy=POLICY, stars_per_usdt=Decimal(70)) == 105


def test_a_stars_purchase_needs_no_rate_at_all() -> None:
    """It is already denominated in Stars."""
    assert stars_equivalent(Decimal(900), STARS, stars_per_usdt=None) == Decimal(900)


def test_a_usdt_reward_is_floored_to_a_whole_bonus() -> None:
    """12.34 USDT at 70 ⭐/USDT is 863.8 ⭐; 15% of that is 129.57 → 129."""
    earned = reward_bonuses(
        Decimal("12.34"),
        USDT,
        policy=POLICY,
        stars_per_usdt=Decimal(70),
    )
    assert earned == 129


def test_a_usdt_purchase_without_a_declared_rate_earns_nothing() -> None:
    """No rate means no conversion, and inventing one is not an option.

    The service logs this and credits nothing; a product priced in USDT only
    declares no equivalence for the shop to use.
    """
    assert stars_equivalent(Decimal(10), USDT, stars_per_usdt=None) is None
    assert reward_bonuses(Decimal(10), USDT, policy=POLICY, stars_per_usdt=None) == 0


@pytest.mark.parametrize("rate", [Decimal(0), Decimal(-1)])
def test_a_nonsensical_rate_earns_nothing(rate: Decimal) -> None:
    assert reward_bonuses(Decimal(10), USDT, policy=POLICY, stars_per_usdt=rate) == 0


def test_a_zero_percent_reward_earns_nothing() -> None:
    policy = BonusPolicy(reward_percent=0)
    assert reward_bonuses(Decimal(1000), STARS, policy=policy) == 0


# --- How much of a price bonuses may cover


def test_bonuses_cover_at_most_half_the_price() -> None:
    """The specification's caps, read straight off the price."""
    assert max_bonus_payment(Decimal(1000), POLICY) == 500
    assert max_bonus_payment(Decimal(600), POLICY) == 300
    assert max_bonus_payment(Decimal(200), POLICY) == 100


def test_a_huge_balance_still_cannot_pay_more_than_the_cap() -> None:
    """A buyer with 5000 bonuses still pays 500 ⭐ of a 1000 ⭐ price."""
    assert spendable_bonuses(Decimal(1000), STARS, balance=5000, policy=POLICY) == 500


def test_a_small_balance_is_spent_in_full() -> None:
    """Below the cap the balance itself is the limit: 1000 - 135 = 865."""
    assert spendable_bonuses(Decimal(1000), STARS, balance=135, policy=POLICY) == 135


def test_an_empty_balance_spends_nothing() -> None:
    assert spendable_bonuses(Decimal(1000), STARS, balance=0, policy=POLICY) == 0


def test_bonuses_cannot_be_spent_on_a_usdt_purchase() -> None:
    """A bonus is a Star, so it reduces a Stars invoice and nothing else."""
    assert spendable_bonuses(Decimal("10.00"), USDT, balance=5000, policy=POLICY) == 0


def test_something_is_always_left_to_charge() -> None:
    """A price of 1 ⭐ cannot be paid entirely with bonuses.

    The purchases table refuses a zero amount, and an invoice for nothing is
    not a sale — so the maths must never produce one.
    """
    assert spendable_bonuses(Decimal(1), STARS, balance=5000, policy=POLICY) == 0


@pytest.mark.parametrize("base", [Decimal(2), Decimal(3), Decimal(10), Decimal(1000)])
def test_a_bonus_payment_never_consumes_the_whole_price(base: Decimal) -> None:
    """Whatever the price and however large the balance, something is charged."""
    spent = spendable_bonuses(base, STARS, balance=10**6, policy=POLICY)
    assert base - Decimal(spent) > 0


# --- Pricing one checkout


def test_a_plain_checkout_is_the_list_price() -> None:
    priced = plain_quote(Decimal(1000), STARS)
    assert priced.charged_amount == Decimal(1000)
    assert priced.is_plain


def test_an_eligible_first_purchase_gets_the_discount() -> None:
    priced = quote(Decimal(1000), STARS, policy=POLICY, discount_eligible=True)
    assert priced.discount_amount == Decimal(100)
    assert priced.charged_amount == Decimal(900)
    assert priced.uses_discount


def test_the_discount_and_bonuses_are_never_combined() -> None:
    """The agreed simplification: on a first referral purchase, no bonuses.

    The discount exists only on that one purchase, so allowing both would make
    the economics of the very first sale the hardest case in the system. After
    it, the discount is gone and bonuses apply from then on.
    """
    priced = quote(
        Decimal(1000),
        STARS,
        policy=POLICY,
        discount_eligible=True,
        balance=500,
        use_bonus=True,
    )
    assert priced.discount_amount == Decimal(100)
    assert priced.bonus_units == 0
    assert priced.charged_amount == Decimal(900)


def test_bonuses_apply_when_there_is_no_discount() -> None:
    """The specification's closing example: 1000 - 135 = 865."""
    priced = quote(Decimal(1000), STARS, policy=POLICY, balance=135, use_bonus=True)
    assert priced.bonus_units == 135
    assert priced.charged_amount == Decimal(865)
    assert not priced.uses_discount


def test_one_bonus_is_one_star() -> None:
    """The whole model, asserted structurally.

    ``bonus_amount`` is derived from the unit count rather than stored beside
    it, so an inconsistent quote cannot be constructed in the first place.
    """
    priced = quote(Decimal(1000), STARS, policy=POLICY, balance=135, use_bonus=True)
    assert priced.bonus_amount == Decimal(priced.bonus_units)


def test_asking_for_bonuses_without_a_balance_changes_nothing() -> None:
    priced = quote(Decimal(1000), STARS, policy=POLICY, balance=0, use_bonus=True)
    assert priced.is_plain
    assert priced.charged_amount == Decimal(1000)


def test_asking_for_bonuses_on_a_usdt_price_changes_nothing() -> None:
    priced = quote(Decimal("10.00"), USDT, policy=POLICY, balance=500, use_bonus=True)
    assert priced.is_plain
    assert priced.charged_amount == Decimal("10.00")


def test_the_breakdown_always_explains_the_charge() -> None:
    """base - discount - bonus must equal what is billed.

    The same identity is a check constraint on the purchases table, so a quote
    that broke it could not even be stored.
    """
    for eligible, balance, use_bonus in ((True, 0, False), (False, 300, True), (False, 0, False)):
        priced = quote(
            Decimal(1000),
            STARS,
            policy=POLICY,
            discount_eligible=eligible,
            balance=balance,
            use_bonus=use_bonus,
        )
        expected = priced.base_amount - priced.discount_amount - priced.bonus_amount
        assert priced.charged_amount == expected
