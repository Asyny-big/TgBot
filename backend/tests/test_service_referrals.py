"""The rules that decide who is attributed to whom.

Every case here is an abuse vector or a user mistake that the shop has to answer
without breaking: inviting yourself, being invited twice, arriving as an
existing buyer, or opening a link that no longer belongs to anybody.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from app.core.config import ReferralSettings
from app.domain.commands import ProductDraft, PurchaseDraft, UserDraft
from app.domain.enums import Currency, PaymentProvider, PurchaseStatus
from app.domain.referrals import (
    REFERRAL_PAYLOAD_PREFIX,
    ReferralOutcome,
    generate_referral_code,
    is_referral_payload,
    parse_referral_payload,
    referral_payload,
)
from app.services.referrals import ReferralService
from tests.fakes import FakeUnitOfWork, FakeUnitOfWorkFactory
from tests.settings_factory import build_settings

if TYPE_CHECKING:
    from uuid import UUID

INVITER = 1001
INVITED = 2002
STRANGER = 3003


@pytest.fixture
def unit() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def uow_factory(unit: FakeUnitOfWork) -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory(unit)


@pytest.fixture
def service(uow_factory: FakeUnitOfWorkFactory) -> ReferralService:
    settings = build_settings()
    return ReferralService(
        uow_factory=uow_factory,
        telegram=settings.telegram,
        settings=settings.referral,
    )


def _profile(telegram_id: int) -> UserDraft:
    return UserDraft(telegram_id=telegram_id, username=f"user{telegram_id}")


async def _code_of(service: ReferralService, telegram_id: int) -> str:
    """Open the bonus screen, which is what mints a code."""
    summary = await service.summary(_profile(telegram_id))
    return summary.referral_code


async def _record_purchase(
    unit: FakeUnitOfWork,
    telegram_id: int,
    *,
    status: PurchaseStatus = PurchaseStatus.DELIVERED,
) -> UUID:
    product = await unit.products.create(
        _product_draft(),
    )
    purchase = await unit.purchases.create(
        PurchaseDraft(
            user_id=telegram_id,
            product_id=product.id,
            provider=PaymentProvider.STARS,
            amount=Decimal(1000),
            currency=Currency.XTR,
            external_id=f"invoice-{telegram_id}-{status.value}",
            status=status,
        )
    )
    return purchase.id


def _product_draft() -> ProductDraft:
    return ProductDraft(
        slug=f"item{generate_referral_code()[:6]}",
        title="VIP",
        description="Access",
        delivery_url="https://t.me/+private",
        price_stars=1000,
    )


# --- The referral link itself


def test_a_referral_payload_is_recognised_by_its_prefix() -> None:
    assert is_referral_payload(f"{REFERRAL_PAYLOAD_PREFIX}abcdef")
    assert not is_referral_payload("muse")


def test_a_code_round_trips_through_its_payload() -> None:
    code = generate_referral_code()
    assert parse_referral_payload(referral_payload(code)) == code


def test_a_malformed_code_is_not_a_referral_payload() -> None:
    """``None`` is what lets ``/start`` fall through to the product lookup.

    A product whose slug happens to begin with "ref_" therefore keeps working,
    which is why this returns ``None`` instead of raising.
    """
    assert parse_referral_payload("ref_") is None
    assert parse_referral_payload("ref_UPPERCASE") is None
    assert parse_referral_payload("ref_" + "a" * 40) is None
    assert parse_referral_payload("muse") is None


def test_a_generated_code_leaks_no_account_identifier() -> None:
    """The code travels in a public link, so it must not encode the user.

    Two codes minted in a row must differ, and neither may contain characters
    that are routinely misread off a screen.
    """
    codes = {generate_referral_code() for _ in range(50)}
    assert len(codes) == 50
    assert all(not set(code) & set("ilo01") for code in codes)


# --- Accepting an invitation


async def test_a_first_invitation_is_recorded(
    service: ReferralService,
    unit: FakeUnitOfWork,
) -> None:
    code = await _code_of(service, INVITER)

    registration = await service.register(
        payload=referral_payload(code),
        profile=_profile(INVITED),
    )

    assert registration.outcome is ReferralOutcome.LINKED
    assert registration.referrer_user_id == INVITER
    referral = await unit.referrals.get_by_referred(INVITED)
    assert referral is not None
    assert referral.referrer_user_id == INVITER
    assert referral.discount_available


async def test_a_buyer_does_not_have_to_purchase_to_invite(
    service: ReferralService,
    unit: FakeUnitOfWork,
) -> None:
    """Anyone may invite. The inviter here has never bought anything."""
    code = await _code_of(service, INVITER)
    assert not await unit.purchases.has_history(
        INVITER,
        statuses=(PurchaseStatus.PAID, PurchaseStatus.DELIVERED),
    )

    registration = await service.register(
        payload=referral_payload(code),
        profile=_profile(INVITED),
    )

    assert registration.outcome is ReferralOutcome.LINKED


async def test_a_second_invitation_cannot_take_the_buyer_over(
    service: ReferralService,
    unit: FakeUnitOfWork,
) -> None:
    """A→B is permanent: C→B is refused and B stays with A."""
    first = await _code_of(service, INVITER)
    second = await _code_of(service, STRANGER)
    await service.register(payload=referral_payload(first), profile=_profile(INVITED))

    registration = await service.register(
        payload=referral_payload(second),
        profile=_profile(INVITED),
    )

    assert registration.outcome is ReferralOutcome.ALREADY_LINKED
    assert registration.referrer_user_id == INVITER
    referral = await unit.referrals.get_by_referred(INVITED)
    assert referral is not None
    assert referral.referrer_user_id == INVITER


async def test_opening_your_own_link_is_refused(service: ReferralService) -> None:
    """Checked by Telegram id, never by username."""
    code = await _code_of(service, INVITER)

    registration = await service.register(
        payload=referral_payload(code),
        profile=_profile(INVITER),
    )

    assert registration.outcome is ReferralOutcome.SELF_REFERRAL


async def test_an_existing_buyer_is_not_eligible(
    service: ReferralService,
    unit: FakeUnitOfWork,
) -> None:
    """Eligibility is purchase history, not the age of the Telegram account."""
    code = await _code_of(service, INVITER)
    await _record_purchase(unit, INVITED)

    registration = await service.register(
        payload=referral_payload(code),
        profile=_profile(INVITED),
    )

    assert registration.outcome is ReferralOutcome.NOT_ELIGIBLE
    assert await unit.referrals.get_by_referred(INVITED) is None


async def test_a_refunded_purchase_still_counts_as_history(
    service: ReferralService,
    unit: FakeUnitOfWork,
) -> None:
    """Otherwise a buyer could refund their way back to being "new".

    That would turn the first-purchase discount into something harvestable, so
    a refund does not restore eligibility.
    """
    code = await _code_of(service, INVITER)
    await _record_purchase(unit, INVITED, status=PurchaseStatus.REFUNDED)

    registration = await service.register(
        payload=referral_payload(code),
        profile=_profile(INVITED),
    )

    assert registration.outcome is ReferralOutcome.NOT_ELIGIBLE


async def test_an_abandoned_invoice_does_not_make_a_buyer_ineligible(
    service: ReferralService,
    unit: FakeUnitOfWork,
) -> None:
    """A pending or expired invoice is not a purchase."""
    code = await _code_of(service, INVITER)
    await _record_purchase(unit, INVITED, status=PurchaseStatus.PENDING)

    registration = await service.register(
        payload=referral_payload(code),
        profile=_profile(INVITED),
    )

    assert registration.outcome is ReferralOutcome.LINKED


async def test_an_unknown_code_is_reported_rather_than_stored(
    service: ReferralService,
    unit: FakeUnitOfWork,
) -> None:
    registration = await service.register(
        payload=referral_payload("nosuchcode"),
        profile=_profile(INVITED),
    )

    assert registration.outcome is ReferralOutcome.UNKNOWN_CODE
    assert await unit.referrals.get_by_referred(INVITED) is None


async def test_the_programme_can_be_switched_off_entirely(
    uow_factory: FakeUnitOfWorkFactory,
) -> None:
    """With the master switch off, no invitation is ever accepted."""
    settings = build_settings(referral=ReferralSettings(enabled=False))
    service = ReferralService(
        uow_factory=uow_factory,
        telegram=settings.telegram,
        settings=settings.referral,
    )

    registration = await service.register(
        payload=referral_payload("abcdefghjk"),
        profile=_profile(INVITED),
    )

    assert registration.outcome is ReferralOutcome.DISABLED


# --- A referred buyer becomes an inviter


async def test_an_invited_buyer_can_invite_others(
    service: ReferralService,
    unit: FakeUnitOfWork,
) -> None:
    """A→B and then B→C. Both relationships exist and are independent."""
    inviter_code = await _code_of(service, INVITER)
    await service.register(payload=referral_payload(inviter_code), profile=_profile(INVITED))

    invited_code = await _code_of(service, INVITED)
    registration = await service.register(
        payload=referral_payload(invited_code),
        profile=_profile(STRANGER),
    )

    assert registration.outcome is ReferralOutcome.LINKED
    assert registration.referrer_user_id == INVITED


async def test_there_is_no_second_level(
    service: ReferralService,
    unit: FakeUnitOfWork,
) -> None:
    """A earns from B only. C's inviter is B, and A has no claim on C.

    Asserted structurally: the relationship table is the only thing that decides
    who earns, and it holds exactly one referrer per buyer.
    """
    inviter_code = await _code_of(service, INVITER)
    await service.register(payload=referral_payload(inviter_code), profile=_profile(INVITED))
    invited_code = await _code_of(service, INVITED)
    await service.register(payload=referral_payload(invited_code), profile=_profile(STRANGER))

    stranger_referral = await unit.referrals.get_by_referred(STRANGER)
    assert stranger_referral is not None
    assert stranger_referral.referrer_user_id == INVITED
    assert await unit.referrals.count_invited(INVITER) == 1


# --- The bonus screen


async def test_the_same_user_always_gets_the_same_link(service: ReferralService) -> None:
    """One permanent code per user, minted once and reused for life."""
    first = await _code_of(service, INVITER)
    second = await _code_of(service, INVITER)
    assert first == second


async def test_the_summary_counts_invitees_and_their_purchases(
    service: ReferralService,
    unit: FakeUnitOfWork,
) -> None:
    code = await _code_of(service, INVITER)
    await service.register(payload=referral_payload(code), profile=_profile(INVITED))
    await service.register(payload=referral_payload(code), profile=_profile(STRANGER))
    await _record_purchase(unit, INVITED)

    summary = await service.summary(_profile(INVITER))

    assert summary.invited_count == 2
    assert summary.referral_purchase_count == 1
    assert summary.balance == 0


async def test_the_link_points_at_this_bot(service: ReferralService) -> None:
    code = await _code_of(service, INVITER)
    link = service.link_for(code)
    assert link == f"https://t.me/MyShopBot?start={REFERRAL_PAYLOAD_PREFIX}{code}"
