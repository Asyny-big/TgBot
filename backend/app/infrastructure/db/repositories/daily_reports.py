"""PostgreSQL reads of the admin panel's Daily Content pages.

Read-only. Every number is a correlated aggregate in one statement — no
per-row round trips — and the completed purchases are read through
``completed_purchase_condition``, the loyalty programme's own definition, so
the panel shows exactly the count access is decided on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from sqlalchemy import func, or_, select

from app.domain.daily_reports import (
    DailyBuyerName,
    DailyDeliveryEntry,
    DailyLibraryEntry,
    DailyOverviewCounts,
    DailyRequestEntry,
    DailySubscriberEntry,
)
from app.domain.daily_requests import OPEN_REQUEST_STATUSES
from app.domain.enums import (
    DailyDeliveryStatus,
    DailyOutboxStatus,
    DailyRequestEventKind,
    DailyRequestStatus,
    DailySubscriptionStatus,
)
from app.domain.pagination import Page
from app.infrastructure.db.errors import LIKE_ESCAPE, like_pattern
from app.infrastructure.db.mappers import (
    to_daily_content,
    to_daily_delivery,
    to_daily_request,
    to_daily_subscription,
)
from app.infrastructure.db.models import (
    DailyContentModel,
    DailyDeliveryModel,
    DailyOutboxModel,
    DailyRequestEventModel,
    DailyRequestItemModel,
    DailyRequestModel,
    DailySubscriptionModel,
    PurchaseModel,
    UserModel,
)
from app.infrastructure.db.repositories.loyalty import completed_purchase_condition

if TYPE_CHECKING:
    from datetime import date, datetime

    from sqlalchemy import ColumnElement, Select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.daily_reports import (
        DailyDeliveryFilters,
        DailyLibraryFilters,
        DailyRequestFilters,
        DailySubscriberFilters,
    )
    from app.domain.entities import DailyContentRequest, DailyDelivery
    from app.domain.pagination import PageRequest
    from app.infrastructure.db.base import Base

_ID_DIGITS: Final = 18
"""Longer digit strings cannot be a BIGINT id: searched as text only."""

_OPEN_OUTBOX: Final = (DailyOutboxStatus.PENDING, DailyOutboxStatus.SENDING)
_LOST_OUTBOX: Final = (DailyOutboxStatus.FAILED, DailyOutboxStatus.EXPIRED)


class SqlAlchemyDailyReportRepository:
    """The admin panel's Daily reads, in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------------------------------------------------------------- overview

    async def overview_counts(self, *, today: date, since: datetime) -> DailyOverviewCounts:
        subscriptions = dict.fromkeys(DailySubscriptionStatus, 0)
        statement = select(DailySubscriptionModel.status, func.count()).group_by(
            DailySubscriptionModel.status
        )
        for subscription_status, count in (await self._session.execute(statement)).tuples():
            subscriptions[subscription_status] = count

        deliveries = dict.fromkeys(DailyDeliveryStatus, 0)
        statement_deliveries = (
            select(DailyDeliveryModel.status, func.count())
            .where(DailyDeliveryModel.delivery_date == today)
            .group_by(DailyDeliveryModel.status)
        )
        for delivery_status, count in (await self._session.execute(statement_deliveries)).tuples():
            deliveries[delivery_status] = count

        requests = dict.fromkeys(DailyRequestStatus, 0)
        statement_requests = select(DailyRequestModel.status, func.count()).group_by(
            DailyRequestModel.status
        )
        for request_status, count in (await self._session.execute(statement_requests)).tuples():
            requests[request_status] = count

        return DailyOverviewCounts(
            subscriptions=subscriptions,
            blocked=await self._count(
                DailySubscriptionModel, DailySubscriptionModel.blocked_at.is_not(None)
            ),
            banned=await self._count(
                DailySubscriptionModel, DailySubscriptionModel.requests_banned_at.is_not(None)
            ),
            deliveries_today=deliveries,
            requests=requests,
            outbox_open=await self._count(
                DailyOutboxModel, DailyOutboxModel.status.in_(_OPEN_OUTBOX)
            ),
            outbox_failed_recently=await self._count(
                DailyOutboxModel,
                DailyOutboxModel.status.in_(_LOST_OUTBOX),
                DailyOutboxModel.updated_at >= since,
            ),
        )

    # ----------------------------------------------------------------- library

    async def library(
        self,
        filters: DailyLibraryFilters,
        page: PageRequest,
    ) -> Page[DailyLibraryEntry]:
        conditions = _library_conditions(filters)
        total = await self._count(DailyContentModel, *conditions)
        statement = (
            select(DailyContentModel, _sent_count())
            .where(*conditions)
            .order_by(DailyContentModel.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        rows = (await self._session.execute(statement)).tuples().all()
        return Page(
            items=tuple(
                DailyLibraryEntry(item=to_daily_content(model), sent_count=int(sent))
                for model, sent in rows
            ),
            total=total,
            limit=page.limit,
            offset=page.offset,
        )

    async def library_entry(self, content_id: int) -> DailyLibraryEntry | None:
        statement = select(DailyContentModel, _sent_count()).where(
            DailyContentModel.id == content_id
        )
        row = (await self._session.execute(statement)).tuples().one_or_none()
        if row is None:
            return None
        model, sent = row
        return DailyLibraryEntry(item=to_daily_content(model), sent_count=int(sent))

    # ------------------------------------------------------------- subscribers

    async def subscribers(
        self,
        filters: DailySubscriberFilters,
        page: PageRequest,
    ) -> Page[DailySubscriberEntry]:
        conditions = _subscriber_conditions(filters)
        total = int(
            await self._session.scalar(
                select(func.count())
                .select_from(DailySubscriptionModel)
                .outerjoin(UserModel, UserModel.telegram_id == DailySubscriptionModel.user_id)
                .where(*conditions)
            )
            or 0
        )
        statement = (
            _subscriber_rows()
            .where(*conditions)
            .order_by(
                DailySubscriptionModel.first_interaction_at.desc(),
                DailySubscriptionModel.user_id.desc(),
            )
            .limit(page.limit)
            .offset(page.offset)
        )
        rows = (await self._session.execute(statement)).tuples().all()
        return Page(
            items=tuple(_subscriber_entry(row) for row in rows),
            total=total,
            limit=page.limit,
            offset=page.offset,
        )

    async def subscriber(self, user_id: int) -> DailySubscriberEntry | None:
        statement = _subscriber_rows().where(DailySubscriptionModel.user_id == user_id)
        row = (await self._session.execute(statement)).tuples().one_or_none()
        return _subscriber_entry(row) if row is not None else None

    # -------------------------------------------------------------- deliveries

    async def deliveries(
        self,
        filters: DailyDeliveryFilters,
        page: PageRequest,
    ) -> Page[DailyDeliveryEntry]:
        conditions: list[ColumnElement[bool]] = []
        if filters.status is not None:
            conditions.append(DailyDeliveryModel.status == filters.status)
        if filters.user_id is not None:
            conditions.append(DailyDeliveryModel.user_id == filters.user_id)
        if filters.delivery_date is not None:
            conditions.append(DailyDeliveryModel.delivery_date == filters.delivery_date)
        total = await self._count(DailyDeliveryModel, *conditions)
        statement = (
            _delivery_rows()
            .where(*conditions)
            .order_by(DailyDeliveryModel.delivery_date.desc(), DailyDeliveryModel.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        rows = (await self._session.execute(statement)).tuples().all()
        return Page(
            items=tuple(_delivery_entry(*row) for row in rows),
            total=total,
            limit=page.limit,
            offset=page.offset,
        )

    async def delivery(self, delivery_id: int) -> DailyDeliveryEntry | None:
        statement = _delivery_rows().where(DailyDeliveryModel.id == delivery_id)
        row = (await self._session.execute(statement)).tuples().one_or_none()
        return _delivery_entry(*row) if row is not None else None

    async def recent_deliveries(self, user_id: int, *, limit: int) -> tuple[DailyDelivery, ...]:
        statement = (
            select(DailyDeliveryModel)
            .where(DailyDeliveryModel.user_id == user_id)
            .order_by(DailyDeliveryModel.delivery_date.desc(), DailyDeliveryModel.id.desc())
            .limit(limit)
        )
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(to_daily_delivery(model) for model in models)

    # ---------------------------------------------------------------- requests

    async def requests(
        self,
        filters: DailyRequestFilters,
        page: PageRequest,
    ) -> Page[DailyRequestEntry]:
        conditions: list[ColumnElement[bool]] = []
        if filters.status is not None:
            conditions.append(DailyRequestModel.status == filters.status)
        if filters.user_id is not None:
            conditions.append(DailyRequestModel.user_id == filters.user_id)
        total = await self._count(DailyRequestModel, *conditions)
        statement = (
            _request_rows()
            .where(*conditions)
            .order_by(DailyRequestModel.created_at.desc(), DailyRequestModel.id.desc())
            .limit(page.limit)
            .offset(page.offset)
        )
        rows = (await self._session.execute(statement)).tuples().all()
        return Page(
            items=tuple(_request_entry(row) for row in rows),
            total=total,
            limit=page.limit,
            offset=page.offset,
        )

    async def request_entry(self, request_id: int) -> DailyRequestEntry | None:
        statement = _request_rows().where(DailyRequestModel.id == request_id)
        row = (await self._session.execute(statement)).tuples().one_or_none()
        return _request_entry(row) if row is not None else None

    async def requests_of(self, user_id: int, *, limit: int) -> tuple[DailyContentRequest, ...]:
        statement = (
            select(DailyRequestModel)
            .where(DailyRequestModel.user_id == user_id)
            .order_by(DailyRequestModel.created_at.desc(), DailyRequestModel.id.desc())
            .limit(limit)
        )
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(to_daily_request(model) for model in models)

    # ----------------------------------------------------------------- helpers

    async def _count(self, model: type[Base], *conditions: ColumnElement[bool]) -> int:
        statement = select(func.count()).select_from(model).where(*conditions)
        return int(await self._session.scalar(statement) or 0)


# --------------------------------------------------------------------------- #
# Statements                                                                  #
# --------------------------------------------------------------------------- #


def _id_term(term: str) -> int | None:
    """``term`` as an id when it can be one."""
    return int(term) if term.isdigit() and len(term) <= _ID_DIGITS else None


def _library_conditions(filters: DailyLibraryFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if filters.status is not None:
        conditions.append(DailyContentModel.status == filters.status)
    if filters.kind is not None:
        conditions.append(DailyContentModel.kind == filters.kind)
    term = (filters.search or "").strip().lstrip("#")
    if term:
        pattern = like_pattern(term)
        matches: list[ColumnElement[bool]] = [
            DailyContentModel.body.ilike(pattern, escape=LIKE_ESCAPE),
            DailyContentModel.file_name.ilike(pattern, escape=LIKE_ESCAPE),
        ]
        content_id = _id_term(term)
        if content_id is not None:
            matches.append(DailyContentModel.id == content_id)
        conditions.append(or_(*matches))
    return conditions


def _sent_count() -> ColumnElement[int]:
    return (
        select(func.count(DailyDeliveryModel.id))
        .where(
            DailyDeliveryModel.content_id == DailyContentModel.id,
            DailyDeliveryModel.status == DailyDeliveryStatus.SENT,
        )
        .correlate(DailyContentModel)
        .scalar_subquery()
    )


def _subscriber_conditions(filters: DailySubscriberFilters) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if filters.status is not None:
        conditions.append(DailySubscriptionModel.status == filters.status)
    if filters.blocked is not None:
        conditions.append(
            DailySubscriptionModel.blocked_at.is_not(None)
            if filters.blocked
            else DailySubscriptionModel.blocked_at.is_(None)
        )
    if filters.banned is not None:
        conditions.append(
            DailySubscriptionModel.requests_banned_at.is_not(None)
            if filters.banned
            else DailySubscriptionModel.requests_banned_at.is_(None)
        )
    term = (filters.search or "").strip().lstrip("@")
    if term:
        matches: list[ColumnElement[bool]] = [
            UserModel.username.ilike(like_pattern(term), escape=LIKE_ESCAPE),
        ]
        user_id = _id_term(term)
        if user_id is not None:
            matches.append(DailySubscriptionModel.user_id == user_id)
        conditions.append(or_(*matches))
    return conditions


type _SubscriberRow = tuple[
    DailySubscriptionModel, str | None, str | None, int, int, "datetime | None", int
]
type _RequestRow = tuple[DailyRequestModel, str | None, str | None, int, int]


def _subscriber_rows() -> Select[_SubscriberRow]:
    user_id = DailySubscriptionModel.user_id
    completed = (
        select(func.count())
        .select_from(PurchaseModel)
        .where(PurchaseModel.user_id == user_id, completed_purchase_condition())
        .correlate(DailySubscriptionModel)
        .scalar_subquery()
    )
    received = (
        select(func.count(DailyDeliveryModel.id))
        .where(
            DailyDeliveryModel.user_id == user_id,
            DailyDeliveryModel.status == DailyDeliveryStatus.SENT,
        )
        .correlate(DailySubscriptionModel)
        .scalar_subquery()
    )
    last_received = (
        select(func.max(DailyDeliveryModel.sent_at))
        .where(
            DailyDeliveryModel.user_id == user_id,
            DailyDeliveryModel.status == DailyDeliveryStatus.SENT,
        )
        .correlate(DailySubscriptionModel)
        .scalar_subquery()
    )
    open_requests = (
        select(func.count(DailyRequestModel.id))
        .where(
            DailyRequestModel.user_id == user_id,
            DailyRequestModel.status.in_(OPEN_REQUEST_STATUSES),
        )
        .correlate(DailySubscriptionModel)
        .scalar_subquery()
    )
    return select(
        DailySubscriptionModel,
        UserModel.username,
        UserModel.first_name,
        completed,
        received,
        last_received,
        open_requests,
    ).outerjoin(UserModel, UserModel.telegram_id == user_id)


def _subscriber_entry(row: _SubscriberRow) -> DailySubscriberEntry:
    model, username, first_name, completed, received, last_received, open_requests = row
    return DailySubscriberEntry(
        subscription=to_daily_subscription(model),
        buyer=DailyBuyerName(username=username, first_name=first_name),
        completed_purchases=int(completed),
        received=int(received),
        last_received_at=last_received,
        open_requests=int(open_requests),
    )


def _delivery_rows() -> Select[tuple[DailyDeliveryModel, str | None, str | None]]:
    return select(DailyDeliveryModel, UserModel.username, UserModel.first_name).outerjoin(
        UserModel, UserModel.telegram_id == DailyDeliveryModel.user_id
    )


def _delivery_entry(
    model: DailyDeliveryModel,
    username: str | None,
    first_name: str | None,
) -> DailyDeliveryEntry:
    return DailyDeliveryEntry(
        delivery=to_daily_delivery(model),
        buyer=DailyBuyerName(username=username, first_name=first_name),
    )


def _request_rows() -> Select[_RequestRow]:
    item_count = (
        select(func.count(DailyRequestItemModel.id))
        .where(DailyRequestItemModel.request_id == DailyRequestModel.id)
        .correlate(DailyRequestModel)
        .scalar_subquery()
    )
    reply_count = (
        select(func.count(DailyRequestEventModel.id))
        .where(
            DailyRequestEventModel.request_id == DailyRequestModel.id,
            DailyRequestEventModel.kind == DailyRequestEventKind.REPLY_QUEUED.value,
        )
        .correlate(DailyRequestModel)
        .scalar_subquery()
    )
    return select(
        DailyRequestModel,
        UserModel.username,
        UserModel.first_name,
        item_count,
        reply_count,
    ).outerjoin(UserModel, UserModel.telegram_id == DailyRequestModel.user_id)


def _request_entry(row: _RequestRow) -> DailyRequestEntry:
    model, username, first_name, item_count, reply_count = row
    return DailyRequestEntry(
        request=to_daily_request(model),
        buyer=DailyBuyerName(username=username, first_name=first_name),
        item_count=int(item_count),
        reply_count=int(reply_count),
    )


__all__ = ["SqlAlchemyDailyReportRepository"]
