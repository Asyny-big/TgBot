"""The admin panel's read-only view of referrals and bonuses.

Two endpoints and one rule: nothing here can change anything. The referral
percentages are process configuration, so the panel reports them; there is no
PATCH to reach for, by design.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import uuid4

from app.domain.commands import ProductDraft, ReferralDraft, UserDraft

if TYPE_CHECKING:
    from tests.api_harness import ApiHarness

INVITER = 9101
INVITED = 9102


async def _relationship(api: ApiHarness) -> None:
    """Record one referral relationship through the live container."""
    async with api.container.uow_factory() as uow:
        await uow.users.upsert(UserDraft(telegram_id=INVITER, username="inviter"))
        await uow.users.upsert(UserDraft(telegram_id=INVITED, username="invited"))
        await uow.referrals.create(
            ReferralDraft(referrer_user_id=INVITER, referred_user_id=INVITED)
        )


async def test_the_settings_endpoint_reports_the_running_configuration(
    admin_api: ApiHarness,
) -> None:
    response = await admin_api.client.get("/api/v1/referrals/settings")

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["discount_percent"] == admin_api.settings.referral.discount_percent
    assert body["reward_percent"] == admin_api.settings.referral.reward_percent
    assert body["max_bonus_payment_percent"] == (
        admin_api.settings.referral.max_bonus_payment_percent
    )
    assert body["enabled"] is admin_api.settings.referral.enabled


async def test_the_settings_endpoint_offers_no_way_to_change_anything(
    admin_api: ApiHarness,
) -> None:
    """Referral configuration is a deploy, not a form."""
    response = await admin_api.client.patch(
        "/api/v1/referrals/settings",
        json={"discount_percent": 90},
    )

    assert response.status_code == HTTPStatus.METHOD_NOT_ALLOWED


async def test_relationships_are_listed_with_both_parties(admin_api: ApiHarness) -> None:
    await _relationship(admin_api)

    response = await admin_api.client.get("/api/v1/referrals")

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["meta"]["total"] == 1
    row = body["items"][0]
    assert row["referrer"]["buyer"]["telegram_id"] == INVITER
    assert row["referred"]["buyer"]["telegram_id"] == INVITED
    assert row["referrer"]["bonus_balance"] == 0
    assert row["referred_purchase_count"] == 0
    assert row["reward_total"] == 0
    assert row["discount_used_at"] is None


async def test_relationships_can_be_searched_by_either_party(
    admin_api: ApiHarness,
) -> None:
    await _relationship(admin_api)

    by_inviter = await admin_api.client.get("/api/v1/referrals", params={"search": "inviter"})
    by_invited = await admin_api.client.get("/api/v1/referrals", params={"search": str(INVITED)})
    by_nobody = await admin_api.client.get("/api/v1/referrals", params={"search": "nobody"})

    assert by_inviter.json()["meta"]["total"] == 1
    assert by_invited.json()["meta"]["total"] == 1
    assert by_nobody.json()["meta"]["total"] == 0


async def test_a_bonus_balance_is_reported_next_to_its_owner(
    admin_api: ApiHarness,
) -> None:
    """The panel's whole bonus surface: a number beside a name."""
    await _relationship(admin_api)
    async with admin_api.container.uow_factory() as uow:
        from app.domain.commands import BonusTransactionDraft  # noqa: PLC0415
        from app.domain.enums import BonusTransactionType  # noqa: PLC0415

        product = await uow.products.create(
            ProductDraft(
                slug=f"item{uuid4().hex[:6]}",
                title="Item",
                description="",
                delivery_url="https://t.me/+x",
                price_stars=1000,
            )
        )
        del product
        await uow.bonuses.add(
            BonusTransactionDraft(
                user_id=INVITER,
                amount=135,
                type=BonusTransactionType.REFERRAL_REWARD,
            )
        )

    response = await admin_api.client.get("/api/v1/referrals")

    assert response.json()["items"][0]["referrer"]["bonus_balance"] == 135


async def test_both_endpoints_require_an_administrator(api: ApiHarness) -> None:
    listing = await api.client.get("/api/v1/referrals")
    settings = await api.client.get("/api/v1/referrals/settings")

    assert listing.status_code == HTTPStatus.UNAUTHORIZED
    assert settings.status_code == HTTPStatus.UNAUTHORIZED
