"""SQLAlchemy implementation of the bonus ledger.

Two invariants are carried by the database rather than by application code,
because both of them are the kind that a race would otherwise break:

* ``uq_bonus_transactions_purchase_type`` — one entry of each kind per purchase.
  A payment notification replayed five times credits one reward, not five: the
  second insert cannot exist. This is the whole idempotency story.
* ``ck_users_bonus_balance_non_negative`` — the balance cannot go below zero.
  Whatever races upstream, the same units can never be spent twice.

The ledger is the truth; ``users.bonus_balance`` is a cache of its sum, always
written in the same transaction as the entry that moved it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import and_, func, literal, or_, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.core.exceptions import InsufficientBonusBalanceError, UserNotFoundError
from app.core.logging import get_logger
from app.domain.bonuses import BonusPolicy, reward_bonuses
from app.domain.commands import BonusTransactionDraft
from app.domain.enums import BONUS_HOLD_TYPES, BonusTransactionType, Currency, PurchaseStatus
from app.infrastructure.db.errors import violated_constraint
from app.infrastructure.db.mappers import to_bonus_transaction, to_product
from app.infrastructure.db.models import (
    BonusTransactionModel,
    ProductModel,
    PurchaseModel,
    ReferralModel,
    UserModel,
)

if TYPE_CHECKING:
    from datetime import datetime
    from decimal import Decimal
    from uuid import UUID

    from sqlalchemy import Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.entities import BonusTransaction

logger = get_logger(__name__)

_UNPAYABLE_STATUSES = (
    PurchaseStatus.EXPIRED,
    PurchaseStatus.REFUNDED,
)
"""Statuses after which an invoice can no longer be paid, so a hold is dead."""

_PERCENT = 100


class SqlAlchemyBonusRepository:
    """Bonus ledger persistence backed by PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def balance(self, user_id: int) -> int:
        statement = select(UserModel.bonus_balance).where(UserModel.telegram_id == user_id)
        return int((await self._session.scalar(statement)) or 0)

    async def lock_balance(self, user_id: int) -> int:
        """Read the balance with ``SELECT ... FOR UPDATE``.

        Serialises spending inside the database, so two checkouts started at the
        same instant cannot both be told the same units are available.
        """
        statement = (
            select(UserModel.bonus_balance)
            .where(UserModel.telegram_id == user_id)
            .with_for_update()
        )
        value = await self._session.scalar(statement)
        if value is None:
            raise UserNotFoundError(telegram_id=user_id)
        return int(value)

    async def add(self, draft: BonusTransactionDraft) -> BonusTransaction | None:
        """Append an entry and move the cached balance atomically.

        Returns ``None`` when an entry of this type already exists for this
        purchase — a replayed notification, not an error.
        """
        model = BonusTransactionModel(
            user_id=draft.user_id,
            amount=draft.amount,
            type=draft.type,
            referral_id=draft.referral_id,
            purchase_id=draft.purchase_id,
            stars_per_usdt=draft.stars_per_usdt,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(model)
                await self._session.flush()
                await self._move_balance(draft.user_id, draft.amount)
        except IntegrityError as error:
            constraint = violated_constraint(error)
            if constraint == "uq_bonus_transactions_purchase_type":
                logger.info(
                    "bonus_entry_already_recorded",
                    purchase_id=str(draft.purchase_id),
                    type=draft.type.value,
                )
                return None
            if constraint == "ck_users_bonus_balance_non_negative":
                raise InsufficientBonusBalanceError(
                    user_id=draft.user_id,
                    requested=abs(draft.amount),
                ) from error
            if constraint == "fk_bonus_transactions_user_id_users":
                # The foreign key fires before the balance update can, so this
                # is where an entry for somebody unknown surfaces.
                raise UserNotFoundError(telegram_id=draft.user_id) from error
            raise
        await self._session.refresh(model)
        return to_bonus_transaction(model)

    async def find_for_purchase(
        self,
        purchase_id: UUID,
        *types: BonusTransactionType,
    ) -> BonusTransaction | None:
        statement = select(BonusTransactionModel).where(
            BonusTransactionModel.purchase_id == purchase_id
        )
        if types:
            statement = statement.where(BonusTransactionModel.type.in_(types))
        statement = statement.order_by(BonusTransactionModel.created_at.asc()).limit(1)
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_bonus_transaction(model) if model is not None else None

    async def settle_hold(self, purchase_id: UUID) -> BonusTransaction | None:
        """Turn a reservation into a spend.

        The amount is untouched: the units already left the balance when the
        invoice was issued. Only the reason they are held changes, which is why
        this is the one place the ledger is updated rather than appended to.
        """
        model = await self._hold(purchase_id)
        if model is None or model.type is BonusTransactionType.BONUS_SPENT:
            return to_bonus_transaction(model) if model is not None else None

        model.type = BonusTransactionType.BONUS_SPENT
        await self._session.flush()
        await self._session.refresh(model)
        return to_bonus_transaction(model)

    async def release_hold(self, purchase_id: UUID) -> BonusTransaction | None:
        """Give back a reservation for an invoice that was never paid.

        Not a refund: nothing was ever charged. Idempotent through the unique
        constraint on ``(purchase_id, type)``.
        """
        model = await self._hold(purchase_id)
        if model is None or model.type is BonusTransactionType.BONUS_SPENT:
            # Nothing reserved, or the units were genuinely spent on a paid
            # purchase. A paid purchase's bonuses are never given back.
            return None

        return await self.add(
            BonusTransactionDraft(
                user_id=model.user_id,
                amount=abs(model.amount),
                type=BonusTransactionType.BONUS_RESERVE_RELEASED,
                purchase_id=purchase_id,
                referral_id=model.referral_id,
                stars_per_usdt=model.stars_per_usdt,
            )
        )

    async def recompute_balance(self, user_id: int) -> int:
        """Sum the ledger from scratch, ignoring the cache."""
        statement = select(func.coalesce(func.sum(BonusTransactionModel.amount), 0)).where(
            BonusTransactionModel.user_id == user_id
        )
        return int((await self._session.scalar(statement)) or 0)

    async def reward_total(self, user_id: int) -> int:
        statement = select(func.coalesce(func.sum(BonusTransactionModel.amount), 0)).where(
            BonusTransactionModel.user_id == user_id,
            BonusTransactionModel.type == BonusTransactionType.REFERRAL_REWARD,
        )
        return int((await self._session.scalar(statement)) or 0)

    async def purchases_awaiting_reward(
        self,
        *,
        reward_percent: int,
        limit: int = 100,
    ) -> tuple[UUID, ...]:
        """Delivered purchases by invited buyers that carry no reward yet.

        The safety net behind reward accrual. A process that died between
        marking a sale delivered and crediting the inviter is repaired from
        here, the same way a lost payment webhook is repaired by reconciliation.

        Only purchases that earn at least one bonus are returned. One that earns
        nothing — a USDT product with no declared Stars price, or a sale too
        small for ``reward_percent`` to reach one bonus — never gets a reward
        entry, because a zero entry does not exist in this ledger. Left in, such
        purchases came back on every pass, oldest first, until they filled the
        batch and hid the purchases that genuinely missed a reward.

        "Earns at least one bonus" is decided by ``reward_bonuses`` itself, the
        function that credits the reward, so the two cannot disagree. The
        database only narrows the candidates first, with the same formula in
        exact arithmetic (``amount * pct >= 100`` on Stars; on USDT
        ``amount * price_stars * pct >= 100 * price_usdt``). That pre-filter
        never drops a purchase ``reward_bonuses`` would reward — see
        ``_reward_candidates`` — but at the exact threshold it may keep one that
        Python rounds to zero, which the final check then removes. Candidates
        are read page by page, so removed rows can never fill a batch.
        """
        if reward_percent <= 0:
            return ()
        policy = BonusPolicy(reward_percent=reward_percent)
        awaiting: list[UUID] = []
        after: tuple[datetime, UUID] | None = None
        while len(awaiting) < limit:
            statement = self._reward_candidates(reward_percent, after=after).limit(limit)
            rows = (await self._session.execute(statement)).all()
            for row in rows:
                if self._earns_a_bonus(row.amount, row.currency, row.ProductModel, policy):
                    awaiting.append(row.id)
                    if len(awaiting) == limit:
                        break
            if len(rows) < limit:
                break
            after = (rows[-1].delivered_at, rows[-1].id)
        return tuple(awaiting)

    @staticmethod
    def _reward_candidates(
        reward_percent: int,
        *,
        after: tuple[datetime, UUID] | None,
    ) -> Select[tuple[UUID, datetime | None, Decimal, Currency, ProductModel]]:
        """Unrewarded delivered sales whose exact reward reaches one bonus.

        Why this never drops a sale ``reward_bonuses`` rewards: written with the
        stored scales (amount ``a / 10**6``, USDT price ``b / 100``), the exact
        reward on USDT is ``a * price_stars * pct / (10**6 * b)``. Unless it is
        exactly 1 it is at least ``1 / (10**6 * b)`` — over ``10**-18`` for any
        storable price — away from 1. ``reward_bonuses`` works in 28-digit
        ``Decimal`` and is off by about ``10**-27``, so it can only land on the
        other side of 1 when the exact value *is* 1. Those rows pass this
        filter and are settled by ``_earns_a_bonus``. On Stars nothing is
        rounded at all: ``amount * pct / 100`` is exact in 28 digits.
        """
        recorded = select(BonusTransactionModel.purchase_id).where(
            BonusTransactionModel.type == BonusTransactionType.REFERRAL_REWARD,
            BonusTransactionModel.purchase_id.is_not(None),
        )
        earns_on_stars = and_(
            PurchaseModel.currency == Currency.XTR,
            PurchaseModel.amount * reward_percent >= _PERCENT,
        )
        earns_on_usdt = and_(
            PurchaseModel.currency == Currency.USDT,
            ProductModel.price_stars.is_not(None),
            ProductModel.price_usdt > 0,
            PurchaseModel.amount * ProductModel.price_stars * reward_percent
            >= ProductModel.price_usdt * _PERCENT,
        )
        statement = (
            select(
                PurchaseModel.id,
                PurchaseModel.delivered_at,
                PurchaseModel.amount,
                PurchaseModel.currency,
                ProductModel,
            )
            .join(ReferralModel, ReferralModel.referred_user_id == PurchaseModel.user_id)
            .join(ProductModel, ProductModel.id == PurchaseModel.product_id)
            .where(
                PurchaseModel.status == PurchaseStatus.DELIVERED,
                PurchaseModel.id.not_in(recorded),
                or_(earns_on_stars, earns_on_usdt),
            )
            .order_by(PurchaseModel.delivered_at.asc(), PurchaseModel.id.asc())
        )
        if after is not None:
            delivered_at, purchase_id = after
            statement = statement.where(
                tuple_(PurchaseModel.delivered_at, PurchaseModel.id)
                > tuple_(
                    literal(delivered_at, PurchaseModel.delivered_at.type),
                    literal(purchase_id, PurchaseModel.id.type),
                )
            )
        return statement

    @staticmethod
    def _earns_a_bonus(
        amount: Decimal,
        currency: Currency,
        product: ProductModel,
        policy: BonusPolicy,
    ) -> bool:
        """``BonusService._reward_for``'s own arithmetic, applied to one candidate."""
        rate = None if currency is Currency.XTR else to_product(product).stars_per_usdt
        return reward_bonuses(amount, currency, policy=policy, stars_per_usdt=rate) > 0

    async def stale_holds(self, *, limit: int = 100) -> tuple[UUID, ...]:
        """Purchases holding reserved units that can no longer be paid for.

        Releasing appends a ``bonus_reserve_released`` entry and leaves the
        reservation row as it is, so a released hold must be excluded here
        explicitly. Without that, every hold ever released kept coming back,
        oldest first, until they filled the whole batch and a new dead hold was
        never reached. A settled hold needs no such check: settling changes the
        row's own type to ``bonus_spent``.
        """
        released = aliased(BonusTransactionModel, name="released")
        already_released = (
            select(released.id)
            .where(
                released.purchase_id == BonusTransactionModel.purchase_id,
                released.type == BonusTransactionType.BONUS_RESERVE_RELEASED,
            )
            .exists()
        )
        statement = (
            select(BonusTransactionModel.purchase_id)
            .join(PurchaseModel, PurchaseModel.id == BonusTransactionModel.purchase_id)
            .where(
                BonusTransactionModel.type == BonusTransactionType.BONUS_RESERVED,
                PurchaseModel.status.in_(_UNPAYABLE_STATUSES),
                ~already_released,
            )
            .order_by(BonusTransactionModel.created_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(statement)).scalars().all()
        return tuple(row for row in rows if row is not None)

    async def _hold(self, purchase_id: UUID) -> BonusTransactionModel | None:
        """The reservation or spend recorded against this purchase, row locked."""
        statement = (
            select(BonusTransactionModel)
            .where(
                BonusTransactionModel.purchase_id == purchase_id,
                BonusTransactionModel.type.in_(BONUS_HOLD_TYPES),
            )
            .with_for_update()
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _move_balance(self, user_id: int, amount: int) -> None:
        """Apply a delta to the cached balance, in the database's own arithmetic.

        Written as ``balance = balance + :delta`` rather than read-modify-write
        so two concurrent credits cannot lose one another.
        """
        statement = (
            update(UserModel)
            .where(UserModel.telegram_id == user_id)
            .values(bonus_balance=UserModel.bonus_balance + amount)
            .returning(UserModel.telegram_id)
        )
        touched = (await self._session.execute(statement)).scalar_one_or_none()
        if touched is None:
            raise UserNotFoundError(telegram_id=user_id)


__all__ = ["SqlAlchemyBonusRepository"]
