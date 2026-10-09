"""SQLAlchemy implementation of the loyalty read model.

Progress is one ``COUNT`` over the buyer's purchases, served by the existing
``ix_purchases_user_id_status`` index (user, status). Nothing is cached and
nothing is written: the count cannot drift from the purchases it describes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select

from app.domain.enums import PurchaseStatus
from app.infrastructure.db.mappers import to_loyalty_reward
from app.infrastructure.db.models import LoyaltyRewardModel, PurchaseModel

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.entities import LoyaltyReward


class SqlAlchemyLoyaltyRepository:
    """Loyalty progress and recorded rewards, read from PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def count_completed(self, user_id: int) -> int:
        statement = (
            select(func.count())
            .select_from(PurchaseModel)
            .where(
                PurchaseModel.user_id == user_id,
                or_(
                    PurchaseModel.status == PurchaseStatus.DELIVERED,
                    and_(
                        PurchaseModel.status == PurchaseStatus.REFUNDED,
                        PurchaseModel.delivered_at.is_not(None),
                    ),
                ),
            )
        )
        return int(await self._session.scalar(statement) or 0)

    async def rewards_of(self, user_id: int) -> tuple[LoyaltyReward, ...]:
        statement = (
            select(LoyaltyRewardModel)
            .where(LoyaltyRewardModel.user_id == user_id)
            .order_by(LoyaltyRewardModel.milestone.asc())
        )
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(to_loyalty_reward(model) for model in models)


__all__ = ["SqlAlchemyLoyaltyRepository"]
