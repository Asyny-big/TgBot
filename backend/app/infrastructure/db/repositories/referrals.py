"""SQLAlchemy implementation of the referral repository.

The rules that decide who gets paid are enforced by constraints, not by a read
followed by a write: "one referrer per buyer" and "one purchase may burn the
discount" are unique indexes, so two concurrent requests cannot both win.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.core.exceptions import (
    ConflictError,
    ReferralAlreadySetError,
    ReferralNotFoundError,
    SelfReferralError,
)
from app.domain.enums import ACCESS_GRANTING_STATUSES, BonusTransactionType
from app.domain.pagination import Page
from app.infrastructure.db.errors import LIKE_ESCAPE, like_pattern, violated_constraint
from app.infrastructure.db.mappers import to_referral, to_referral_record
from app.infrastructure.db.models import (
    BonusTransactionModel,
    PurchaseModel,
    ReferralModel,
    UserModel,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.commands import ReferralDraft
    from app.domain.entities import Referral, ReferralRecord
    from app.domain.pagination import PageRequest, ReferralFilters


class SqlAlchemyReferralRepository:
    """Referral persistence backed by PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, draft: ReferralDraft) -> Referral:
        if draft.referrer_user_id == draft.referred_user_id:
            # Rejected here as well as by the check constraint, so the caller
            # sees a domain error rather than a translated integrity error.
            raise SelfReferralError(user_id=draft.referred_user_id)

        model = ReferralModel(
            referrer_user_id=draft.referrer_user_id,
            referred_user_id=draft.referred_user_id,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(model)
                await self._session.flush()
        except IntegrityError as error:
            raise self._translate(error, draft) from error
        await self._session.refresh(model)
        return to_referral(model)

    async def get(self, referral_id: UUID) -> Referral | None:
        model = await self._session.get(ReferralModel, referral_id)
        return to_referral(model) if model is not None else None

    async def get_by_referred(self, referred_user_id: int) -> Referral | None:
        statement = select(ReferralModel).where(
            ReferralModel.referred_user_id == referred_user_id,
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_referral(model) if model is not None else None

    async def mark_discount_used(
        self,
        referral_id: UUID,
        *,
        purchase_id: UUID,
        used_at: datetime | None = None,
    ) -> Referral:
        """Burn the one-time discount, under a row lock. Idempotent."""
        statement = select(ReferralModel).where(ReferralModel.id == referral_id).with_for_update()
        model = (await self._session.execute(statement)).scalar_one_or_none()
        if model is None:
            raise ReferralNotFoundError(referral_id=str(referral_id))

        if model.discount_purchase_id is not None:
            if model.discount_purchase_id == purchase_id:
                # A replayed payment notification for the same purchase.
                return to_referral(model)
            message = "The referral discount was already used by another purchase"
            raise ConflictError(
                message,
                referral_id=str(referral_id),
                discount_purchase_id=str(model.discount_purchase_id),
            )

        model.discount_purchase_id = purchase_id
        model.discount_used_at = used_at or datetime.now(UTC)
        await self._session.flush()
        await self._session.refresh(model)
        return to_referral(model)

    async def count_invited(self, referrer_user_id: int) -> int:
        statement = (
            select(func.count())
            .select_from(ReferralModel)
            .where(ReferralModel.referrer_user_id == referrer_user_id)
        )
        return await self._session.scalar(statement) or 0

    async def count_referral_purchases(self, referrer_user_id: int) -> int:
        """Settled purchases made by everyone this user invited."""
        statement = (
            select(func.count())
            .select_from(PurchaseModel)
            .join(ReferralModel, ReferralModel.referred_user_id == PurchaseModel.user_id)
            .where(
                ReferralModel.referrer_user_id == referrer_user_id,
                PurchaseModel.status.in_(ACCESS_GRANTING_STATUSES),
            )
        )
        return await self._session.scalar(statement) or 0

    async def search(self, filters: ReferralFilters, page: PageRequest) -> Page[ReferralRecord]:
        """One statement, two correlated aggregates — no per-row round trips."""
        referrer = aliased(UserModel, name="referrer")
        referred = aliased(UserModel, name="referred")

        conditions = self._conditions(filters, referrer=referrer, referred=referred)
        purchase_count = (
            select(func.count())
            .select_from(PurchaseModel)
            .where(
                PurchaseModel.user_id == ReferralModel.referred_user_id,
                PurchaseModel.status.in_(ACCESS_GRANTING_STATUSES),
            )
            .correlate(ReferralModel)
            .scalar_subquery()
        )
        reward_total = (
            select(func.coalesce(func.sum(BonusTransactionModel.amount), 0))
            .where(
                BonusTransactionModel.referral_id == ReferralModel.id,
                BonusTransactionModel.type == BonusTransactionType.REFERRAL_REWARD,
            )
            .correlate(ReferralModel)
            .scalar_subquery()
        )

        total = await self._session.scalar(
            select(func.count())
            .select_from(ReferralModel)
            .join(referrer, ReferralModel.referrer_user_id == referrer.telegram_id)
            .join(referred, ReferralModel.referred_user_id == referred.telegram_id)
            .where(*conditions)
        )
        rows = (
            await self._session.execute(
                select(ReferralModel, referrer, referred, purchase_count, reward_total)
                .join(referrer, ReferralModel.referrer_user_id == referrer.telegram_id)
                .join(referred, ReferralModel.referred_user_id == referred.telegram_id)
                .where(*conditions)
                .order_by(ReferralModel.created_at.desc(), ReferralModel.id.desc())
                .limit(page.limit)
                .offset(page.offset)
            )
        ).all()

        return Page(
            items=tuple(
                to_referral_record(
                    referral,
                    referrer_row,
                    referred_row,
                    referred_purchase_count=int(purchases),
                    reward_total=int(rewards),
                )
                for referral, referrer_row, referred_row, purchases, rewards in rows
            ),
            total=total or 0,
            limit=page.limit,
            offset=page.offset,
        )

    @staticmethod
    def _conditions(
        filters: ReferralFilters,
        *,
        referrer: object,
        referred: object,
    ) -> list[ColumnElement[bool]]:
        """Search one box against the username or Telegram id of either party."""
        conditions: list[ColumnElement[bool]] = []
        if not filters.search:
            return conditions

        term = filters.search.strip().lstrip("@")
        if not term:
            return conditions

        pattern = like_pattern(term)
        matches: list[ColumnElement[bool]] = [
            referrer.username.ilike(pattern, escape=LIKE_ESCAPE),  # type: ignore[attr-defined]
            referred.username.ilike(pattern, escape=LIKE_ESCAPE),  # type: ignore[attr-defined]
        ]
        if term.isdigit():
            matches.append(ReferralModel.referrer_user_id == int(term))
            matches.append(ReferralModel.referred_user_id == int(term))
        conditions.append(or_(*matches))
        return conditions

    @staticmethod
    def _translate(error: IntegrityError, draft: ReferralDraft) -> Exception:
        constraint = violated_constraint(error)
        if constraint == "uq_referrals_referred_user_id":
            return ReferralAlreadySetError(referred_user_id=draft.referred_user_id)
        if constraint == "ck_referrals_no_self_referral":
            return SelfReferralError(user_id=draft.referred_user_id)
        return error


__all__ = ["SqlAlchemyReferralRepository"]
