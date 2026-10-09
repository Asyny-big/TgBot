"""Loyalty progress arithmetic and the "🏆 My progress" screen, without a database.

A buyer's progress is a count of packs they received plus whatever rewards were
recorded. A reward that was never recorded is available exactly when its
milestone is reached — which is what makes historical buyers work without a
backfill.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.bot.texts import loyalty_progress
from app.domain.entities import LoyaltyReward
from app.domain.enums import LoyaltyRewardKind, LoyaltyRewardStatus
from app.domain.loyalty import (
    DISCOUNT_MILESTONE,
    FREE_PACK_MILESTONE,
    REWARD_MILESTONES,
    VIP_MILESTONE,
    MilestoneRewardState,
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
        "До VIP: 2 покупки",
    ]


def test_the_screen_for_eighteen_purchases() -> None:
    assert _screen(18) == [
        "🏆 <b>Мой прогресс</b>",
        "Покупок: 18",
        "🎉 Скидка 50% доступна",
        "🎁 Бесплатный пак доступен",
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


def test_the_screen_never_offers_to_claim_anything() -> None:
    """Using a reward is not implemented yet, so nothing here may promise it."""
    for completed in (0, 5, 10, 15, 18):
        text = loyalty_progress(progress_of(completed, ())).lower()
        assert "забрать" not in text
        assert "получить" not in text


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
