"""SQLAlchemy implementation of the purchase repository.

Every state transition is guarded so that a replayed webhook or a duplicated
Telegram update can never charge twice or deliver twice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, DuplicatePurchaseError, PurchaseNotFoundError
from app.domain.enums import ACCESS_GRANTING_STATUSES, PurchaseStatus
from app.domain.pagination import Page
from app.infrastructure.db.errors import LIKE_ESCAPE, like_pattern, violated_constraint
from app.infrastructure.db.mappers import to_purchase, to_record
from app.infrastructure.db.models import ProductModel, PurchaseModel, UserModel

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.commands import PurchaseDraft
    from app.domain.entities import Purchase, PurchaseRecord
    from app.domain.enums import PaymentProvider
    from app.domain.pagination import PageRequest, PurchaseFilters

_TERMINAL_FOR_PAYMENT = (PurchaseStatus.PAID, PurchaseStatus.DELIVERED)


class SqlAlchemyPurchaseRepository:
    """Purchase persistence backed by PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, draft: PurchaseDraft) -> Purchase:
        model = PurchaseModel(
            user_id=draft.user_id,
            product_id=draft.product_id,
            provider=draft.provider,
            status=draft.status or PurchaseStatus.PENDING,
            amount=draft.amount,
            currency=draft.currency,
            external_id=draft.external_id,
        )
        if model.status in _TERMINAL_FOR_PAYMENT:
            model.paid_at = datetime.now(UTC)
        try:
            async with self._session.begin_nested():
                self._session.add(model)
                await self._session.flush()
        except IntegrityError as error:
            raise self._translate(error, draft.external_id) from error
        await self._session.refresh(model)
        return to_purchase(model)

    async def get(self, purchase_id: UUID) -> Purchase | None:
        model = await self._session.get(PurchaseModel, purchase_id)
        return to_purchase(model) if model is not None else None

    async def get_by_external_id(
        self,
        provider: PaymentProvider,
        external_id: str,
    ) -> Purchase | None:
        statement = select(PurchaseModel).where(
            PurchaseModel.provider == provider,
            PurchaseModel.external_id == external_id,
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_purchase(model) if model is not None else None

    async def find_access_granting(self, user_id: int, product_id: UUID) -> Purchase | None:
        statement = select(PurchaseModel).where(
            PurchaseModel.user_id == user_id,
            PurchaseModel.product_id == product_id,
            PurchaseModel.status.in_(ACCESS_GRANTING_STATUSES),
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_purchase(model) if model is not None else None

    async def mark_paid(
        self,
        purchase_id: UUID,
        *,
        paid_at: datetime | None = None,
        telegram_charge_id: str | None = None,
    ) -> Purchase:
        model = await self._lock(purchase_id)

        if model.status in _TERMINAL_FOR_PAYMENT:
            # Replayed payment notification: nothing to do, nothing to fail.
            if telegram_charge_id and model.telegram_charge_id is None:
                model.telegram_charge_id = telegram_charge_id
                await self._session.flush()
            return to_purchase(model)

        if model.status is PurchaseStatus.REFUNDED:
            message = "Refunded purchase cannot be paid again"
            raise ConflictError(message, purchase_id=str(purchase_id), status=model.status.value)

        # Read before the savepoint: a rolled back savepoint expires the loaded
        # attributes, and touching them afterwards would trigger lazy IO.
        external_id = model.external_id
        try:
            async with self._session.begin_nested():
                model.status = PurchaseStatus.PAID
                model.paid_at = paid_at or datetime.now(UTC)
                if telegram_charge_id:
                    model.telegram_charge_id = telegram_charge_id
                await self._session.flush()
        except IntegrityError as error:
            raise self._translate(error, external_id) from error
        await self._session.refresh(model)
        return to_purchase(model)

    async def mark_delivered(
        self,
        purchase_id: UUID,
        *,
        delivered_url: str,
        delivered_at: datetime | None = None,
    ) -> Purchase:
        model = await self._lock(purchase_id)

        if model.status is PurchaseStatus.DELIVERED:
            return to_purchase(model)

        if model.status is not PurchaseStatus.PAID:
            message = "Only a paid purchase can be delivered"
            raise ConflictError(message, purchase_id=str(purchase_id), status=model.status.value)

        model.status = PurchaseStatus.DELIVERED
        model.delivered_url = delivered_url
        model.delivered_at = delivered_at or datetime.now(UTC)
        await self._session.flush()
        await self._session.refresh(model)
        return to_purchase(model)

    async def mark_refunded(self, purchase_id: UUID) -> Purchase:
        model = await self._lock(purchase_id)

        if model.status is PurchaseStatus.REFUNDED:
            return to_purchase(model)

        model.status = PurchaseStatus.REFUNDED
        await self._session.flush()
        await self._session.refresh(model)
        return to_purchase(model)

    async def expire_pending(self, older_than: datetime) -> int:
        statement = (
            update(PurchaseModel)
            .where(
                PurchaseModel.status == PurchaseStatus.PENDING,
                PurchaseModel.created_at < older_than,
            )
            .values(status=PurchaseStatus.EXPIRED)
            .returning(PurchaseModel.id)
        )
        expired = (await self._session.execute(statement)).scalars().all()
        return len(expired)

    async def list_pending(
        self,
        provider: PaymentProvider,
        *,
        limit: int = 100,
    ) -> tuple[Purchase, ...]:
        statement = (
            select(PurchaseModel)
            .where(
                PurchaseModel.provider == provider,
                PurchaseModel.status == PurchaseStatus.PENDING,
            )
            .order_by(PurchaseModel.created_at.asc())
            .limit(limit)
        )
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(to_purchase(model) for model in models)

    async def search(self, filters: PurchaseFilters, page: PageRequest) -> Page[PurchaseRecord]:
        conditions = self._conditions(filters)

        total = await self._session.scalar(
            select(func.count())
            .select_from(PurchaseModel)
            .join(UserModel, PurchaseModel.user_id == UserModel.telegram_id)
            .join(ProductModel, PurchaseModel.product_id == ProductModel.id)
            .where(*conditions)
        )
        statement = (
            select(PurchaseModel, UserModel, ProductModel)
            .join(UserModel, PurchaseModel.user_id == UserModel.telegram_id)
            .join(ProductModel, PurchaseModel.product_id == ProductModel.id)
            .where(*conditions)
            .order_by(PurchaseModel.created_at.desc(), PurchaseModel.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        rows = (await self._session.execute(statement)).all()
        return Page(
            items=tuple(to_record(purchase, user, product) for purchase, user, product in rows),
            total=total or 0,
            limit=page.limit,
            offset=page.offset,
        )

    async def _lock(self, purchase_id: UUID) -> PurchaseModel:
        """Load a purchase with ``SELECT ... FOR UPDATE``.

        Concurrent payment callbacks for the same purchase are serialised by the
        database instead of racing inside the application.
        """
        statement = select(PurchaseModel).where(PurchaseModel.id == purchase_id).with_for_update()
        model = (await self._session.execute(statement)).scalar_one_or_none()
        if model is None:
            raise PurchaseNotFoundError(purchase_id=str(purchase_id))
        return model

    @staticmethod
    def _translate(error: IntegrityError, external_id: str) -> Exception:
        constraint = violated_constraint(error)
        if constraint == "uq_purchases_user_product_paid":
            return DuplicatePurchaseError()
        if constraint == "uq_purchases_provider_external_id":
            message = "This invoice is already recorded"
            return ConflictError(message, constraint=constraint, external_id=external_id)
        if constraint == "uq_purchases_telegram_charge_id":
            message = "This Telegram charge is already recorded"
            return ConflictError(message, constraint=constraint)
        return error

    @staticmethod
    def _conditions(filters: PurchaseFilters) -> list[ColumnElement[bool]]:
        conditions: list[ColumnElement[bool]] = []
        if filters.statuses:
            conditions.append(PurchaseModel.status.in_(filters.statuses))
        if filters.search:
            term = filters.search.strip().lstrip("@")
            pattern = like_pattern(term)
            matches: list[ColumnElement[bool]] = [
                UserModel.username.ilike(pattern, escape=LIKE_ESCAPE),
                ProductModel.title.ilike(pattern, escape=LIKE_ESCAPE),
                ProductModel.slug.ilike(pattern, escape=LIKE_ESCAPE),
                PurchaseModel.external_id.ilike(pattern, escape=LIKE_ESCAPE),
                PurchaseModel.telegram_charge_id.ilike(pattern, escape=LIKE_ESCAPE),
            ]
            if term.isdigit():
                matches.append(PurchaseModel.user_id == int(term))
            conditions.append(or_(*matches))
        return conditions
