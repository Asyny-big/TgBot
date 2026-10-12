"""Loyalty progress arithmetic and the "🏆 My progress" screen, without a database.

A buyer's progress is a count of packs they received plus whatever rewards were
recorded. A reward that was never recorded is available exactly when its
milestone is reached — which is what makes historical buyers work without a
backfill.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from app.bot.keyboards import (
    BonusCallback,
    ProgressCallback,
    bonus_hint_keyboard,
    progress_keyboard,
)
from app.bot.texts import (
    BONUS_SECTION_BUTTON,
    FREE_PACK_PROGRESS_HINT,
    PROGRESS_SECTION_BUTTON,
    loyalty_milestone_notice,
    loyalty_progress,
    progress_button_label,
)
from app.domain.entities import LoyaltyReward, Product
from app.domain.enums import Currency, LoyaltyRewardKind, LoyaltyRewardStatus
from app.domain.loyalty import (
    DISCOUNT_MILESTONE,
    FREE_PACK_MILESTONE,
    REWARD_MILESTONES,
    VIP_MILESTONE,
    MilestoneRewardState,
    free_pack_external_id,
    free_pack_value,
    milestone_of,
    progress_of,
    reward_state,
)

AVAILABLE = MilestoneRewardState.AVAILABLE
LOCKED = MilestoneRewardState.LOCKED


def _reward(milestone: int, status: LoyaltyRewardStatus) -> LoyaltyReward:
    return LoyaltyReward(
        id=uuid4(),
        user_id=1,
        milestone=milestone,
        kind=REWARD_MILESTONES[milestone],
        status=status,
        earned_at=datetime.now(UTC),
    )


def test_the_milestones_are_five_ten_and_fifteen() -> None:
    assert (DISCOUNT_MILESTONE, FREE_PACK_MILESTONE, VIP_MILESTONE) == (5, 10, 15)
    assert REWARD_MILESTONES == {
        5: LoyaltyRewardKind.DISCOUNT_50_PERCENT,
        10: LoyaltyRewardKind.FREE_PACK,
    }
    # VIP is a status, never a reward.
    assert VIP_MILESTONE not in REWARD_MILESTONES


# --------------------------------------------------------------------------- #
# Progress without any recorded reward: every historical buyer                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("completed", "expected"),
    [
        # completed: (50% discount, free pack, VIP, to free, to VIP)
        (0, (LOCKED, LOCKED, False, 10, 15)),
        (1, (LOCKED, LOCKED, False, 9, 14)),
        (4, (LOCKED, LOCKED, False, 6, 11)),
        (5, (AVAILABLE, LOCKED, False, 5, 10)),
        (6, (AVAILABLE, LOCKED, False, 4, 9)),
        (9, (AVAILABLE, LOCKED, False, 1, 6)),
        (10, (AVAILABLE, AVAILABLE, False, 0, 5)),
        (11, (AVAILABLE, AVAILABLE, False, 0, 4)),
        (13, (AVAILABLE, AVAILABLE, False, 0, 2)),
        (14, (AVAILABLE, AVAILABLE, False, 0, 1)),
        (15, (AVAILABLE, AVAILABLE, True, 0, 0)),
        (18, (AVAILABLE, AVAILABLE, True, 0, 0)),
        (20, (AVAILABLE, AVAILABLE, True, 0, 0)),
    ],
)
def test_progress_from_the_count_alone(
    completed: int,
    expected: tuple[MilestoneRewardState, MilestoneRewardState, bool, int, int],
) -> None:
    discount, free, vip, to_free, to_vip = expected
    progress = progress_of(completed, ())

    assert progress.completed_count == completed
    assert progress.discount_50 is discount
    assert progress.free_pack is free
    assert progress.discount_50_available is (discount is AVAILABLE)
    assert progress.free_pack_available is (free is AVAILABLE)
    assert progress.vip is vip
    assert progress.to_free == to_free
    assert progress.to_vip == to_vip
    assert progress.to_discount == max(5 - completed, 0)


# --------------------------------------------------------------------------- #
# Once a reward is recorded, its row is the truth                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (LoyaltyRewardStatus.AVAILABLE, MilestoneRewardState.AVAILABLE),
        (LoyaltyRewardStatus.APPLIED, MilestoneRewardState.IN_USE),
        (LoyaltyRewardStatus.CONSUMED, MilestoneRewardState.USED),
    ],
)
def test_a_recorded_reward_decides_its_own_state(
    status: LoyaltyRewardStatus,
    expected: MilestoneRewardState,
) -> None:
    assert reward_state(7, DISCOUNT_MILESTONE, status) is expected


def test_a_used_discount_is_no_longer_available_however_far_the_buyer_got() -> None:
    progress = progress_of(
        18,
        (
            _reward(DISCOUNT_MILESTONE, LoyaltyRewardStatus.CONSUMED),
            _reward(FREE_PACK_MILESTONE, LoyaltyRewardStatus.CONSUMED),
        ),
    )

    assert not progress.discount_50_available
    assert not progress.free_pack_available
    assert progress.discount_50 is MilestoneRewardState.USED
    assert progress.free_pack is MilestoneRewardState.USED
    # VIP is the count, not a reward: using rewards never takes it away.
    assert progress.vip


def test_a_discount_waiting_on_an_invoice_is_not_offered_twice() -> None:
    progress = progress_of(7, (_reward(DISCOUNT_MILESTONE, LoyaltyRewardStatus.APPLIED),))

    assert progress.discount_50 is MilestoneRewardState.IN_USE
    assert not progress.discount_50_available


def test_one_recorded_reward_does_not_affect_the_other() -> None:
    progress = progress_of(12, (_reward(DISCOUNT_MILESTONE, LoyaltyRewardStatus.CONSUMED),))

    assert progress.discount_50 is MilestoneRewardState.USED
    assert progress.free_pack is MilestoneRewardState.AVAILABLE


# --------------------------------------------------------------------------- #
# The screen                                                                  #
# --------------------------------------------------------------------------- #


def _screen(completed: int, *rewards: LoyaltyReward) -> list[str]:
    return [line for line in loyalty_progress(progress_of(completed, rewards)).split("\n") if line]


def test_the_screen_for_seven_purchases() -> None:
    assert _screen(7) == [
        "🏆 <b>Мой прогресс</b>",
        "Покупок: 7",
        "🎉 Скидка 50% доступна",
        "До бесплатного пака: 3 покупки",
        "До VIP: 8 покупок",
    ]


def test_the_screen_for_thirteen_purchases() -> None:
    assert _screen(13) == [
        "🏆 <b>Мой прогресс</b>",
        "Покупок: 13",
        "🎉 Скидка 50% доступна",
        "🎁 Бесплатный пак доступен",
        FREE_PACK_PROGRESS_HINT,
        "До VIP: 2 покупки",
    ]


def test_the_screen_for_eighteen_purchases() -> None:
    assert _screen(18) == [
        "🏆 <b>Мой прогресс</b>",
        "Покупок: 18",
        "🎉 Скидка 50% доступна",
        "🎁 Бесплатный пак доступен",
        FREE_PACK_PROGRESS_HINT,
        "👑 VIP",
    ]


def test_the_screen_for_a_new_buyer() -> None:
    assert _screen(0) == [
        "🏆 <b>Мой прогресс</b>",
        "Покупок: 0",
        "До скидки 50%: 5 покупок",
        "До бесплатного пака: 10 покупок",
        "До VIP: 15 покупок",
    ]


@pytest.mark.parametrize(
    ("completed", "free_pack", "hinted"),
    [
        (0, None, False),
        (5, None, False),
        (9, None, False),
        (10, None, True),
        (18, None, True),
        (12, LoyaltyRewardStatus.AVAILABLE, True),
        (12, LoyaltyRewardStatus.CONSUMED, False),
    ],
)
def test_the_screen_points_to_the_free_pack_only_while_it_waits(
    completed: int,
    free_pack: LoyaltyRewardStatus | None,
    hinted: bool,
) -> None:
    """The screen itself has no button: it says where the pack is claimed."""
    rewards = () if free_pack is None else (_reward(FREE_PACK_MILESTONE, free_pack),)
    text = loyalty_progress(progress_of(completed, rewards))
    assert (FREE_PACK_PROGRESS_HINT in text) is hinted
    assert ("забрать" in text.lower()) is hinted


def test_the_screen_for_rewards_in_use_and_used() -> None:
    lines = _screen(
        12,
        _reward(DISCOUNT_MILESTONE, LoyaltyRewardStatus.APPLIED),
        _reward(FREE_PACK_MILESTONE, LoyaltyRewardStatus.CONSUMED),
    )
    assert "⏳ Скидка 50% ждёт оплаты счёта" in lines
    assert "✅ Бесплатный пак получен" in lines


@pytest.mark.parametrize(
    ("missing", "phrase"),
    [
        (1, "1 покупка"),
        (2, "2 покупки"),
        (4, "4 покупки"),
        (5, "5 покупок"),
        (11, "11 покупок"),
        (12, "12 покупок"),
        (14, "14 покупок"),
    ],
)
def test_purchases_are_counted_in_proper_russian(missing: int, phrase: str) -> None:
    completed = VIP_MILESTONE - missing
    assert f"До VIP: {phrase}" in _screen(completed)


# --------------------------------------------------------------------------- #
# "🏆 Мой прогресс" under the post-purchase hint                              #
# --------------------------------------------------------------------------- #

USED_DISCOUNT = _reward(DISCOUNT_MILESTONE, LoyaltyRewardStatus.CONSUMED)
USED_FREE_PACK = _reward(FREE_PACK_MILESTONE, LoyaltyRewardStatus.CONSUMED)


@pytest.mark.parametrize(
    ("completed", "rewards", "label"),
    [
        (0, (), "🏆 Мой прогресс — ещё 5 до скидки 50%"),
        (4, (), "🏆 Мой прогресс — ещё 1 до скидки 50%"),
        (5, (), "🏆 Мой прогресс — 5/10 · 🎉 скидка 50%"),
        (7, (), "🏆 Мой прогресс — 7/10 · 🎉 скидка 50%"),
        (10, (USED_DISCOUNT,), "🏆 Мой прогресс — 10/15 · 🎁 бесплатный пак"),
        (15, (USED_DISCOUNT, USED_FREE_PACK), "🏆 Мой прогресс — 15/20 · 👑 VIP"),
        (20, (USED_DISCOUNT, USED_FREE_PACK), "🏆 Мой прогресс — 20 · ✅ все цели"),
    ],
    ids=["0", "4", "5", "7", "10", "15", "20"],
)
def test_the_progress_button_shows_the_next_goal_and_what_was_reached(
    completed: int,
    rewards: tuple[LoyaltyReward, ...],
    label: str,
) -> None:
    assert progress_button_label(progress_of(completed, rewards)) == label


@pytest.mark.parametrize(
    ("completed", "rewards", "label"),
    [
        (7, (USED_DISCOUNT,), "🏆 Мой прогресс — 7/10"),
        (
            7,
            (_reward(DISCOUNT_MILESTONE, LoyaltyRewardStatus.APPLIED),),
            "🏆 Мой прогресс — 7/10",
        ),
        (11, (USED_DISCOUNT, USED_FREE_PACK), "🏆 Мой прогресс — 11/15"),
        (11, (USED_FREE_PACK,), "🏆 Мой прогресс — 11/15 · 🎉 скидка 50%"),
        (17, (USED_DISCOUNT,), "🏆 Мой прогресс — 17/20 · 🎁 бесплатный пак"),
        (23, (USED_DISCOUNT, USED_FREE_PACK), "🏆 Мой прогресс — 23 · ✅ все цели"),
    ],
    ids=[
        "discount-used",
        "discount-in-an-invoice",
        "both-used",
        "discount-waits",
        "pack-waits",
        "twenty-three",
    ],
)
def test_the_progress_button_names_a_reward_only_while_it_is_unused(
    completed: int,
    rewards: tuple[LoyaltyReward, ...],
    label: str,
) -> None:
    """A used reward is not advertised again; one still waiting is, whatever the count."""
    assert progress_button_label(progress_of(completed, rewards)) == label


def test_the_purchase_hint_offers_progress_only_when_given_a_label() -> None:
    plain = bonus_hint_keyboard()
    with_progress = bonus_hint_keyboard(progress="🏆 Мой прогресс — 7/10")

    assert [[button.text for button in row] for row in plain.inline_keyboard] == [
        [BONUS_SECTION_BUTTON]
    ]
    assert [
        [(button.text, button.callback_data) for button in row]
        for row in with_progress.inline_keyboard
    ] == [
        [(BONUS_SECTION_BUTTON, BonusCallback().pack())],
        [("🏆 Мой прогресс — 7/10", ProgressCallback().pack())],
    ]


# --------------------------------------------------------------------------- #
# The free pack on paper                                                      #
# --------------------------------------------------------------------------- #


def _product(*, stars: int | None, usdt: str | None) -> Product:
    now = datetime.now(UTC)
    return Product(
        id=uuid4(),
        slug="pack",
        title="Pack",
        description="",
        photo_file_id=None,
        delivery_url="https://t.me/+pack",
        price_stars=stars,
        price_usdt=Decimal(usdt) if usdt is not None else None,
        is_active=True,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    ("stars", "usdt", "expected"),
    [
        (1000, "10.00", (Decimal(1000), Currency.XTR)),
        (1000, None, (Decimal(1000), Currency.XTR)),
        (None, "7.50", (Decimal("7.50"), Currency.USDT)),
    ],
)
def test_a_free_pack_is_valued_in_stars_first(
    stars: int | None,
    usdt: str | None,
    expected: tuple[Decimal, Currency],
) -> None:
    assert free_pack_value(_product(stars=stars, usdt=usdt)) == expected


def test_a_product_without_a_price_cannot_be_valued() -> None:
    with pytest.raises(ValueError, match="no price"):
        free_pack_value(_product(stars=None, usdt=None))


def test_one_reward_names_one_free_pack() -> None:
    reward_id = uuid4()
    assert free_pack_external_id(reward_id) == f"loyalty:{reward_id}"
    assert free_pack_external_id(reward_id) != free_pack_external_id(uuid4())


# --------------------------------------------------------------------------- #
# Milestone notices                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rank", range(1, 31))
def test_only_the_fifth_tenth_and_fifteenth_delivery_reach_a_milestone(rank: int) -> None:
    assert milestone_of(rank) == (rank if rank in (5, 10, 15) else None)


def test_the_milestone_notices_read_exactly_as_agreed() -> None:
    assert loyalty_milestone_notice(5) == (
        "🎉 Поздравляем! Это ваша 5-я покупка. Вам доступна скидка 50% на следующий пак "
        "— она применится автоматически при оплате."
    )
    assert loyalty_milestone_notice(10) == (
        "🎁 10 покупок! Вам доступен бесплатный пак. "
        "Откройте любой пак и нажмите «🎁 Забрать бесплатно»."
    )
    assert loyalty_milestone_notice(15) == "👑 15 покупок — поздравляем! Вы достигли VIP-статуса."
    with pytest.raises(KeyError):
        loyalty_milestone_notice(6)


def test_a_notice_carries_the_progress_button_alone() -> None:
    ((button,),) = progress_keyboard().inline_keyboard
    assert button.text == PROGRESS_SECTION_BUTTON
    assert button.callback_data == ProgressCallback().pack()
