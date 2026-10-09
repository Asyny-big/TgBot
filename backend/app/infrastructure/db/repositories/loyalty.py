"""SQLAlchemy implementation of the loyalty programme's persistence.

Progress is one ``COUNT`` over the buyer's purchases, served by the existing
``ix_purchases_user_id_status`` index (user, status). Nothing is cached: the
count cannot drift from the purchases it describes.

Rewards are written lazily — the first time one is used — and change state only
under a row lock (``SELECT … FOR UPDATE``), each transition checking the state
it starts from. The schema backs that up: one row per (user, milestone), at most
one reward per purchase whichever way it was used, and a CHECK that every status
carries exactly its own facts.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError
from app.domain.enums import LoyaltyRewardStatus, PurchaseStatus
from app.domain.loyalty import PROGRESS_LOCK_NAMESPACE, REWARD_MILESTONES
from app.infrastructure.db.errors import violated_constraint
from app.infrastructure.db.mappers import to_loyalty_reward
from app.infrastructure.db.models import LoyaltyRewardModel, PurchaseModel

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.entities import LoyaltyReward


class SqlAlchemyLoyaltyRepository:
    """Loyalty progress and rewards, in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------- reads

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

    # ------------------------------------------------------------------ writes

    async def ensure_reward(self, user_id: int, milestone: int) -> None:
        statement = (
            insert(LoyaltyRewardModel)
            .values(
                id=uuid4(),
                user_id=user_id,
                milestone=milestone,
                kind=REWARD_MILESTONES[milestone],
            )
            .on_conflict_do_nothing(constraint="uq_loyalty_rewards_user_milestone")
        )
        await self._session.execute(statement)

    async def lock_reward(self, user_id: int, milestone: int) -> LoyaltyReward | None:
        model = await self._locked(
            LoyaltyRewardModel.user_id == user_id,
            LoyaltyRewardModel.milestone == milestone,
        )
        return to_loyalty_reward(model) if model is not None else None

    async def mark_applied(self, reward_id: UUID, purchase_id: UUID) -> LoyaltyReward:
        model = await self._require(reward_id)
        if model.status is not LoyaltyRewardStatus.AVAILABLE:
            message = "The loyalty reward is not available"
            raise ConflictError(message, reward_id=str(reward_id), status=model.status.value)
        return await self._transition(
            model,
            status=LoyaltyRewardStatus.APPLIED,
            applied_purchase_id=purchase_id,
            applied_at=datetime.now(UTC),
        )

    async def mark_consumed(self, reward_id: UUID, purchase_id: UUID) -> LoyaltyReward:
        model = await self._require(reward_id)
        if model.status is LoyaltyRewardStatus.CONSUMED:
            if model.consumed_purchase_id == purchase_id:
                # A replayed payment notification for the same purchase.
                return to_loyalty_reward(model)
            message = "The loyalty reward was already consumed by another purchase"
            raise ConflictError(
                message,
                reward_id=str(reward_id),
                consumed_purchase_id=str(model.consumed_purchase_id),
            )
        return await self._transition(
            model,
            status=LoyaltyRewardStatus.CONSUMED,
            consumed_purchase_id=purchase_id,
            consumed_at=datetime.now(UTC),
        )

    async def mark_released(self, reward_id: UUID, purchase_id: UUID) -> LoyaltyReward | None:
        model = await self._require(reward_id)
        if (
            model.status is not LoyaltyRewardStatus.APPLIED
            or model.applied_purchase_id != purchase_id
        ):
            return None
        return await self._transition(
            model,
            status=LoyaltyRewardStatus.AVAILABLE,
            applied_purchase_id=None,
            applied_at=None,
        )

    async def release_expired(self, *, limit: int = 100) -> tuple[UUID, ...]:
        dead_holds = (
            select(LoyaltyRewardModel.id)
            .join(PurchaseModel, PurchaseModel.id == LoyaltyRewardModel.applied_purchase_id)
            .where(
                LoyaltyRewardModel.status == LoyaltyRewardStatus.APPLIED,
                PurchaseModel.status == PurchaseStatus.EXPIRED,
            )
            .order_by(LoyaltyRewardModel.applied_at.asc())
            .limit(limit)
            # A payment settling one of these right now holds its lock: skip it
            # rather than wait — it is consuming the reward, not releasing it.
            .with_for_update(of=LoyaltyRewardModel, skip_locked=True)
        )
        statement = (
            update(LoyaltyRewardModel)
            .where(
                LoyaltyRewardModel.id.in_(dead_holds.scalar_subquery()),
                LoyaltyRewardModel.status == LoyaltyRewardStatus.APPLIED,
            )
            .values(
                status=LoyaltyRewardStatus.AVAILABLE,
                applied_purchase_id=None,
                applied_at=None,
            )
            .returning(LoyaltyRewardModel.id)
            .execution_options(synchronize_session=False)
        )
        released = (await self._session.execute(statement)).scalars().all()
        return tuple(released)

    async def lock_progress(self, user_id: int) -> None:
        # A transaction-scoped advisory lock: released by the commit or rollback
        # that ends the delivery, never left behind. One key per buyer, derived
        # the same way on every path that delivers.
        key = f"{PROGRESS_LOCK_NAMESPACE}:{user_id}"
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
        )

    # ----------------------------------------------------------------- helpers

    async def _locked(self, *conditions: ColumnElement[bool]) -> LoyaltyRewardModel | None:
        statement = (
            select(LoyaltyRewardModel)
            .where(*conditions)
            .with_for_update()
            # The row may already sit in this session's identity map from an
            # earlier read; the locked read must replace it, not be ignored.
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _require(self, reward_id: UUID) -> LoyaltyRewardModel:
        model = await self._locked(LoyaltyRewardModel.id == reward_id)
        if model is None:
            message = "No such loyalty reward"
            raise ConflictError(message, reward_id=str(reward_id))
        return model

    async def _transition(self, model: LoyaltyRewardModel, **values: object) -> LoyaltyReward:
        """Write one state change in a savepoint, so a refusal costs only that change.

        The fields are set *inside* the savepoint: rolled back, it expires them
        again. Set outside it, they would stay dirty in the session and the next
        autoflush would retry the refused UPDATE — failing whatever transaction
        the caller carries on with, such as the payment that is settling.
        """
        reward_id = model.id
        try:
            async with self._session.begin_nested():
                for name, value in values.items():
                    setattr(model, name, value)
                await self._session.flush()
        except IntegrityError as error:
            message = "The loyalty reward could not change state"
            raise ConflictError(
                message,
                reward_id=str(reward_id),
                constraint=violated_constraint(error),
            ) from error
        await self._session.refresh(model)
        return to_loyalty_reward(model)


__all__ = ["SqlAlchemyLoyaltyRepository"]
