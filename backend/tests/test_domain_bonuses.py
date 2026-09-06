"""The arithmetic that decides what a buyer is charged.

These are the rules the shop's economics rest on, and they are pure functions,
so they are tested here with literals rather than through a database. Every case
below is a rule somebody could get wrong in a way that costs real money.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.bonuses import (
    BonusPolicy,
    discount_for,
    max_bonus_payment,
    money_for_units,
    plain_quote,
    quote,
    reward_units,
    spendable_units,
    units_for_money,
    units_to_cover,
)
from app.domain.enums import Currency

POLICY = BonusPolicy(
    discount_percent=10,
    reward_percent=15,
    max_bonus_payment_percent=50,
    units_per_usdt=Decimal(500),
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


# --- The referrer's reward


def test_the_reward_is_fifteen_percent_of_what_was_actually_paid() -> None:
    """The specification's example: a buyer who paid 900 earns the inviter 135.

    Crucially the base is the *paid* amount, not the list price — 15% of 1000
    would be 150, and paying that would quietly overpay every inviter.
    """
    assert reward_units(Decimal(900), STARS, POLICY) == 135


def test_every_later_purchase_earns_its_own_reward() -> None:
    """The inviter earns from each settled purchase, not just the first."""
    assert reward_units(Decimal(1200), STARS, POLICY) == 180
    assert reward_units(Decimal(800), STARS, POLICY) == 120
    assert reward_units(Decimal(700), STARS, POLICY) == 105


def test_a_usdt_purchase_earns_units_through_the_configured_rate() -> None:
    """12.34 USDT is worth 6170 units, and 15% of that is 925."""
    assert reward_units(Decimal("12.34"), USDT, POLICY) == 925


def test_a_reward_too_small_to_express_is_zero() -> None:
    """Six Stars at 15% is 0.9 of a unit, which is not a unit."""
    assert reward_units(Decimal(6), STARS, POLICY) == 0


# --- Converting between money and bonus units


def test_one_star_is_one_bonus_unit() -> None:
    """The pool is denominated in Stars, so Stars need no conversion."""
    assert units_for_money(Decimal(135), STARS, POLICY) == 135
    assert money_for_units(135, STARS, POLICY) == Decimal(135)


def test_usdt_joins_the_same_pool_through_the_rate() -> None:
    assert units_for_money(Decimal(1), USDT, POLICY) == 500
    assert money_for_units(500, USDT, POLICY) == Decimal("1.00")


def test_units_needed_to_cover_an_amount_are_rounded_up() -> None:
    """Rounded up on purpose: a buyer must not get a cent for free.

    One cent is worth five units exactly here, but 1.005 would need 5.025, and
    handing over five would give a fraction of a cent away.
    """
    assert units_to_cover(Decimal("0.01"), USDT, POLICY) == 5
    assert units_to_cover(Decimal("0.011"), USDT, POLICY) == 6


def test_converting_units_to_money_never_rounds_in_the_buyers_favour() -> None:
    """7 units is 1.4 cents, which buys one cent, not two."""
    assert money_for_units(7, USDT, POLICY) == Decimal("0.01")


# --- How much of a price bonuses may cover


def test_bonuses_cover_at_most_half_the_price() -> None:
    """The specification's cap: 1000 accepts 500 in bonuses and no more."""
    assert max_bonus_payment(Decimal(1000), STARS, POLICY) == Decimal(500)


def test_a_huge_balance_still_cannot_pay_more_than_the_cap() -> None:
    """A buyer with 5000 units still pays 500 Stars of a 1000 Star price."""
    units, worth = spendable_units(Decimal(1000), STARS, balance=5000, policy=POLICY)
    assert units == 500
    assert worth == Decimal(500)


def test_a_small_balance_is_spent_in_full() -> None:
    """Below the cap the balance itself is the limit."""
    units, worth = spendable_units(Decimal(1000), STARS, balance=135, policy=POLICY)
    assert units == 135
    assert worth == Decimal(135)


def test_an_empty_balance_spends_nothing() -> None:
    assert spendable_units(Decimal(1000), STARS, balance=0, policy=POLICY) == (0, Decimal(0))


def test_something_is_always_left_to_charge() -> None:
    """A price of 1 Star cannot be paid entirely with bonuses.

    The purchases table refuses a zero amount, and an invoice for nothing is
    not a sale — so the maths must never produce one.
    """
    units, worth = spendable_units(Decimal(1), STARS, balance=5000, policy=POLICY)
    assert units == 0
    assert worth == Decimal(0)


@pytest.mark.parametrize("base", [Decimal(2), Decimal(3), Decimal(10), Decimal(1000)])
def test_a_bonus_payment_never_consumes_the_whole_price(base: Decimal) -> None:
    """Whatever the price and however large the balance, something is charged."""
    _, worth = spendable_units(base, STARS, balance=10**6, policy=POLICY)
    assert base - worth > 0


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
    priced = quote(Decimal(1000), STARS, policy=POLICY, balance=135, use_bonus=True)
    assert priced.bonus_units == 135
    assert priced.charged_amount == Decimal(865)
    assert not priced.uses_discount


def test_asking_for_bonuses_without_a_balance_changes_nothing() -> None:
    priced = quote(Decimal(1000), STARS, policy=POLICY, balance=0, use_bonus=True)
    assert priced.is_plain
    assert priced.charged_amount == Decimal(1000)


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
