"""Referral and bonus schemas for the admin panel.

Read only by design. The percentages are process configuration, validated on
start-up like every other setting, so the panel reports them rather than
offering to change them — a mistyped reward percent must not be applicable
through a web form while money is moving.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID

from app.api.schemas.common import ApiModel
from app.api.schemas.purchases import BuyerResponse

if TYPE_CHECKING:
    from app.core.config import ReferralSettings
    from app.domain.entities import ReferralRecord


class ReferralSettingsResponse(ApiModel):
    """The referral programme's configured numbers, as the shop is running them."""

    enabled: bool
    discount_percent: int
    reward_percent: int
    max_bonus_payment_percent: int
    bonus_units_per_usdt: float
    preview_directory_url: str | None

    @classmethod
    def from_settings(cls, settings: ReferralSettings) -> ReferralSettingsResponse:
        return cls(
            enabled=settings.enabled,
            discount_percent=settings.discount_percent,
            reward_percent=settings.reward_percent,
            max_bonus_payment_percent=settings.max_bonus_payment_percent,
            bonus_units_per_usdt=float(settings.bonus_units_per_usdt),
            preview_directory_url=settings.preview_directory_url,
        )


class ReferralPartyResponse(ApiModel):
    """One side of a referral relationship, with their bonus balance."""

    buyer: BuyerResponse
    bonus_balance: int


class ReferralRecordResponse(ApiModel):
    """One relationship: who invited whom, and what it has produced."""

    id: UUID
    referrer: ReferralPartyResponse
    referred: ReferralPartyResponse
    created_at: datetime
    discount_used_at: datetime | None
    discount_purchase_id: UUID | None
    referred_purchase_count: int
    reward_total: int
    """Bonus units this relationship has earned the inviter, all time."""

    @classmethod
    def from_domain(cls, record: ReferralRecord) -> ReferralRecordResponse:
        return cls(
            id=record.referral.id,
            referrer=ReferralPartyResponse(
                buyer=BuyerResponse.from_domain(record.referrer),
                bonus_balance=record.referrer.bonus_balance,
            ),
            referred=ReferralPartyResponse(
                buyer=BuyerResponse.from_domain(record.referred),
                bonus_balance=record.referred.bonus_balance,
            ),
            created_at=record.referral.created_at,
            discount_used_at=record.referral.discount_used_at,
            discount_purchase_id=record.referral.discount_purchase_id,
            referred_purchase_count=record.referred_purchase_count,
            reward_total=record.reward_total,
        )


__all__ = [
    "ReferralPartyResponse",
    "ReferralRecordResponse",
    "ReferralSettingsResponse",
]
