"""SQLAlchemy implementation of Daily Content persistence.

PostgreSQL is the source of truth for every Daily Content guarantee:

* one delivery per buyer and Moscow date — ``uq_daily_deliveries_user_date``;
* never the same item twice — ``uq_daily_deliveries_user_content`` over the
  deliveries that hold or sent an item;
* one sender at a time — a lease (random token + expiry) on the delivery row,
  taken under ``SELECT … FOR UPDATE SKIP LOCKED`` and checked on every outcome.

Access is read through ``completed_purchase_condition``, the loyalty
programme's own definition of a completed purchase: there is no second counter.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, DailyContentDuplicateError
from app.domain.daily import DeliveryCounts, LibraryStats
from app.domain.enums import (
    DailyAttemptOutcome,
    DailyContentKind,
    DailyContentStatus,
    DailyDeliveryStatus,
    DailyDeliveryTrigger,
    DailySubscriptionStatus,
)
from app.infrastructure.db.errors import violated_constraint
from app.infrastructure.db.mappers import (
    to_daily_attempt,
    to_daily_content,
    to_daily_delivery,
    to_daily_subscription,
)
from app.infrastructure.db.models import (
    DailyContentModel,
    DailyDeliveryAttemptModel,
    DailyDeliveryModel,
    DailySubscriptionModel,
    PurchaseModel,
)
from app.infrastructure.db.repositories.loyalty import completed_purchase_condition

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date, datetime, timedelta
    from uuid import UUID

    from sqlalchemy import ColumnElement
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.commands import DailyContentDraft, DailyDeliveryDraft
    from app.domain.entities import (
        DailyContent,
        DailyDelivery,
        DailyDeliveryAttempt,
        DailySubscription,
    )

_CONTENT_SOURCE_CONSTRAINT = "uq_daily_content_source"
_CONTENT_FILE_CONSTRAINT = "uq_daily_content_file_unique_id"
_TEXT_LOCK_NAMESPACE = "daily-content-text"
"""Advisory lock keys for text items: no index can hold a whole text unique."""
_DELIVERY_DATE_CONSTRAINT = "uq_daily_deliveries_user_date"
_DELIVERY_CONTENT_CONSTRAINT = "uq_daily_deliveries_user_content"

_HOLDING_STATUSES = (
    DailyDeliveryStatus.PENDING,
    DailyDeliveryStatus.SENDING,
    DailyDeliveryStatus.SENT,
)
"""Deliveries that hold their item for the buyer: it is not offered again."""

_WAITING_STATUSES = (DailyDeliveryStatus.PENDING, DailyDeliveryStatus.NO_CONTENT)
_OPEN_STATUSES = (*_WAITING_STATUSES, DailyDeliveryStatus.SENDING)


# --------------------------------------------------------------------------- #
# Subscriptions                                                               #
# --------------------------------------------------------------------------- #


class SqlAlchemyDailySubscriptionRepository:
    """Daily Content subscriptions, in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: int) -> DailySubscription | None:
        model = await self._session.get(DailySubscriptionModel, user_id)
        return to_daily_subscription(model) if model is not None else None

    async def lock(self, user_id: int) -> DailySubscription | None:
        model = await self._locked(user_id)
        return to_daily_subscription(model) if model is not None else None

    async def record_first_interaction(self, user_id: int, *, at: datetime) -> DailySubscription:
        statement = (
            insert(DailySubscriptionModel)
            .values(
                user_id=user_id,
                status=DailySubscriptionStatus.INACTIVE,
                first_interaction_at=at,
            )
            .on_conflict_do_nothing(index_elements=[DailySubscriptionModel.user_id])
        )
        await self._session.execute(statement)
        model = await self._require(user_id)
        return to_daily_subscription(model)

    async def activate(
        self,
        user_id: int,
        *,
        at: datetime,
        anchor_minute: int,
    ) -> DailySubscription:
        model = await self._require(user_id)
        if model.status is not DailySubscriptionStatus.INACTIVE:
            message = "The Daily Content subscription is already activated"
            raise ConflictError(message, user_id=user_id, status=model.status.value)
        model.status = DailySubscriptionStatus.ACTIVE
        model.activated_at = at
        model.anchor_minute = anchor_minute
        return await self._save(model)

    async def pause(self, user_id: int, *, at: datetime) -> DailySubscription:
        model = await self._require_activated(user_id)
        if model.status is DailySubscriptionStatus.ACTIVE:
            model.status = DailySubscriptionStatus.PAUSED
            model.paused_at = at
        return await self._save(model)

    async def resume(self, user_id: int, *, at: datetime) -> DailySubscription:
        del at  # resuming leaves only the pause behind; the anchor does not move
        model = await self._require_activated(user_id)
        if model.status is DailySubscriptionStatus.PAUSED:
            model.status = DailySubscriptionStatus.ACTIVE
            model.paused_at = None
        return await self._save(model)

    async def mark_blocked(self, user_id: int, *, at: datetime) -> DailySubscription | None:
        model = await self._locked(user_id)
        if model is None:
            return None
        if model.blocked_at is None:
            model.blocked_at = at
        return await self._save(model)

    async def mark_unblocked(self, user_id: int) -> DailySubscription | None:
        model = await self._locked(user_id)
        if model is None:
            return None
        model.blocked_at = None
        return await self._save(model)

    async def deliverable(
        self,
        *,
        min_completed: int,
        after_user_id: int | None = None,
        limit: int = 100,
        only: tuple[int, ...] = (),
    ) -> tuple[DailySubscription, ...]:
        conditions = _deliverable_conditions(min_completed=min_completed, only=only)
        if after_user_id is not None:
            conditions.append(DailySubscriptionModel.user_id > after_user_id)
        statement = (
            select(DailySubscriptionModel)
            .where(*conditions)
            .order_by(DailySubscriptionModel.user_id.asc())
            .limit(limit)
        )
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(to_daily_subscription(model) for model in models)

    async def received_active_counts(
        self,
        *,
        min_completed: int,
        only: tuple[int, ...] = (),
    ) -> dict[int, int]:
        received = (
            select(func.count(DailyDeliveryModel.id))
            .select_from(DailyDeliveryModel)
            .join(DailyContentModel, DailyContentModel.id == DailyDeliveryModel.content_id)
            .where(
                DailyDeliveryModel.user_id == DailySubscriptionModel.user_id,
                DailyDeliveryModel.status.in_(_HOLDING_STATUSES),
                DailyContentModel.status == DailyContentStatus.ACTIVE,
            )
            .correlate(DailySubscriptionModel)
            .scalar_subquery()
        )
        statement = select(DailySubscriptionModel.user_id, received).where(
            *_deliverable_conditions(min_completed=min_completed, only=only)
        )
        rows = (await self._session.execute(statement)).tuples()
        return {user_id: int(count) for user_id, count in rows}

    async def mark_exhausted_notified(
        self,
        user_id: int,
        *,
        at: datetime,
    ) -> DailySubscription | None:
        model = await self._locked(user_id)
        if model is None:
            return None
        model.exhausted_notified_at = at
        return await self._save(model)

    async def _locked(self, user_id: int) -> DailySubscriptionModel | None:
        statement = (
            select(DailySubscriptionModel)
            .where(DailySubscriptionModel.user_id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _require(self, user_id: int) -> DailySubscriptionModel:
        model = await self._locked(user_id)
        if model is None:
            message = "No Daily Content subscription for this buyer"
            raise ConflictError(message, user_id=user_id)
        return model

    async def _require_activated(self, user_id: int) -> DailySubscriptionModel:
        model = await self._require(user_id)
        if model.status is DailySubscriptionStatus.INACTIVE:
            message = "The Daily Content subscription was never activated"
            raise ConflictError(message, user_id=user_id)
        return model

    async def _save(self, model: DailySubscriptionModel) -> DailySubscription:
        await self._session.flush()
        await self._session.refresh(model)
        return to_daily_subscription(model)


# --------------------------------------------------------------------------- #
# Library                                                                     #
# --------------------------------------------------------------------------- #


class SqlAlchemyDailyContentRepository:
    """The Daily Content library, in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, draft: DailyContentDraft) -> tuple[DailyContent, bool]:
        if draft.kind is DailyContentKind.TEXT and draft.body is not None:
            # Texts have no unique index; the lock queues the same text, so the
            # second one sees the first once it is committed.
            await self._lock_text(draft.body)
            twin = await self._live_text_twin(
                draft.body,
                other_than=and_(
                    DailyContentModel.source_chat_id == draft.source_chat_id,
                    DailyContentModel.source_message_id == draft.source_message_id,
                ),
            )
            if twin is not None:
                raise DailyContentDuplicateError(existing_id=twin)
        statement = (
            insert(DailyContentModel)
            .values(
                kind=draft.kind,
                body=draft.body,
                entities=list(draft.entities) or None,
                file_id=draft.file_id,
                file_unique_id=draft.file_unique_id,
                file_size=draft.file_size,
                file_name=draft.file_name,
                mime_type=draft.mime_type,
                duration=draft.duration,
                width=draft.width,
                height=draft.height,
                source_chat_id=draft.source_chat_id,
                source_message_id=draft.source_message_id,
                added_by=draft.added_by,
            )
            .on_conflict_do_nothing(constraint=_CONTENT_SOURCE_CONSTRAINT)
            .returning(DailyContentModel.id)
        )
        try:
            async with self._session.begin_nested():
                created_id = (await self._session.execute(statement)).scalar_one_or_none()
        except IntegrityError as error:
            if violated_constraint(error) != _CONTENT_FILE_CONSTRAINT or not draft.file_unique_id:
                raise
            raise await self._duplicate_of(draft.file_unique_id) from error

        if created_id is not None:
            created = await self._session.get(DailyContentModel, created_id)
            if created is None:  # pragma: no cover — inserted in this transaction
                message = "The new Daily Content item vanished"
                raise ConflictError(message, content_id=created_id)
            return to_daily_content(created), True

        # The same message again — an update delivered twice: the item it became.
        existing = (
            await self._session.execute(
                select(DailyContentModel).where(
                    DailyContentModel.source_chat_id == draft.source_chat_id,
                    DailyContentModel.source_message_id == draft.source_message_id,
                )
            )
        ).scalar_one()
        return to_daily_content(existing), False

    async def get(self, content_id: int) -> DailyContent | None:
        model = await self._session.get(DailyContentModel, content_id)
        return to_daily_content(model) if model is not None else None

    async def lock(self, content_id: int) -> DailyContent | None:
        model = await self._locked_item(content_id)
        return to_daily_content(model) if model is not None else None

    async def find_by_file(self, file_unique_id: str) -> DailyContent | None:
        return await self._find(DailyContentModel.file_unique_id == file_unique_id)

    async def find_by_text(self, body: str) -> DailyContent | None:
        return await self._find(
            DailyContentModel.kind == DailyContentKind.TEXT,
            DailyContentModel.body == body,
        )

    async def stats(self) -> LibraryStats:
        statement = select(
            DailyContentModel.status,
            DailyContentModel.kind,
            func.count(),
        ).group_by(DailyContentModel.status, DailyContentModel.kind)
        by_status = dict.fromkeys(DailyContentStatus, 0)
        active_by_kind: dict[DailyContentKind, int] = {}
        for status, kind, count in (await self._session.execute(statement)).tuples():
            by_status[status] += count
            if status is DailyContentStatus.ACTIVE:
                active_by_kind[kind] = count
        return LibraryStats(
            active=by_status[DailyContentStatus.ACTIVE],
            disabled=by_status[DailyContentStatus.DISABLED],
            archived=by_status[DailyContentStatus.ARCHIVED],
            active_by_kind=active_by_kind,
        )

    async def set_status(
        self,
        content_id: int,
        status: DailyContentStatus,
        *,
        at: datetime,
        reason: str | None = None,
    ) -> DailyContent:
        model = await self._locked_item(content_id)
        if model is None:
            message = "No such Daily Content item"
            raise ConflictError(message, content_id=content_id)
        if model.status is status:
            return to_daily_content(model)
        leaving_archive = (
            model.status is DailyContentStatus.ARCHIVED
            and status is not DailyContentStatus.ARCHIVED
        )
        if leaving_archive and model.kind is DailyContentKind.TEXT and model.body is not None:
            await self._lock_text(model.body)
            twin = await self._live_text_twin(
                model.body,
                other_than=DailyContentModel.id == content_id,
            )
            if twin is not None:
                raise DailyContentDuplicateError(existing_id=twin)
        # Read before the savepoint: a refused flush expires the model's fields.
        file_unique_id = model.file_unique_id
        try:
            # Set inside the savepoint: a refusal rolls the fields back with it.
            async with self._session.begin_nested():
                model.status = status
                model.status_changed_at = at
                model.status_reason = reason
                await self._session.flush()
        except IntegrityError as error:
            if violated_constraint(error) != _CONTENT_FILE_CONSTRAINT or not file_unique_id:
                raise
            raise await self._duplicate_of(file_unique_id) from error
        await self._session.refresh(model)
        return to_daily_content(model)

    async def pick_for(self, user_id: int) -> DailyContent | None:
        statement = (
            select(DailyContentModel)
            .where(*self._available_to(user_id))
            .order_by(func.random())
            .limit(1)
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_daily_content(model) if model is not None else None

    async def count_available_for(self, user_id: int) -> int:
        statement = (
            select(func.count()).select_from(DailyContentModel).where(*self._available_to(user_id))
        )
        return int(await self._session.scalar(statement) or 0)

    @staticmethod
    def _available_to(user_id: int) -> tuple[ColumnElement[bool], ...]:
        """Active, and neither received by nor held for this buyer."""
        held = (
            select(DailyDeliveryModel.id)
            .where(
                DailyDeliveryModel.user_id == user_id,
                DailyDeliveryModel.content_id == DailyContentModel.id,
                DailyDeliveryModel.status.in_(_HOLDING_STATUSES),
            )
            .correlate(DailyContentModel)
        )
        return (DailyContentModel.status == DailyContentStatus.ACTIVE, ~held.exists())

    async def count_active(self) -> int:
        statement = (
            select(func.count())
            .select_from(DailyContentModel)
            .where(DailyContentModel.status == DailyContentStatus.ACTIVE)
        )
        return int(await self._session.scalar(statement) or 0)

    async def added_since(self, since: datetime) -> int:
        statement = (
            select(func.count())
            .select_from(DailyContentModel)
            .where(DailyContentModel.created_at >= since)
        )
        return int(await self._session.scalar(statement) or 0)

    async def _locked_item(self, content_id: int) -> DailyContentModel | None:
        statement = (
            select(DailyContentModel)
            .where(DailyContentModel.id == content_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _find(self, *conditions: ColumnElement[bool]) -> DailyContent | None:
        statement = (
            select(DailyContentModel)
            .where(*conditions)
            # The live item first (there is at most one), else the newest archived.
            .order_by(
                (DailyContentModel.status == DailyContentStatus.ARCHIVED).asc(),
                DailyContentModel.id.desc(),
            )
            .limit(1)
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_daily_content(model) if model is not None else None

    async def _lock_text(self, body: str) -> None:
        # Transaction-scoped: released by the commit or rollback, never left behind.
        key = f"{_TEXT_LOCK_NAMESPACE}:{body}"
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0)))
        )

    async def _live_text_twin(self, body: str, *, other_than: ColumnElement[bool]) -> int | None:
        """A text item with exactly this body, not archived, other than the one named."""
        statement = (
            select(DailyContentModel.id)
            .where(
                DailyContentModel.kind == DailyContentKind.TEXT,
                DailyContentModel.body == body,
                DailyContentModel.status != DailyContentStatus.ARCHIVED,
                ~other_than,
            )
            .order_by(DailyContentModel.id)
            .limit(1)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _duplicate_of(self, file_unique_id: str) -> DailyContentDuplicateError:
        statement = select(DailyContentModel.id).where(
            DailyContentModel.file_unique_id == file_unique_id,
            DailyContentModel.status != DailyContentStatus.ARCHIVED,
        )
        existing_id = (await self._session.execute(statement)).scalar_one()
        return DailyContentDuplicateError(existing_id=existing_id)


# --------------------------------------------------------------------------- #
# Deliveries                                                                  #
# --------------------------------------------------------------------------- #


class SqlAlchemyDailyDeliveryRepository:
    """Daily Content deliveries and their attempts, in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def open(self, draft: DailyDeliveryDraft) -> DailyDelivery | None:
        statement = (
            insert(DailyDeliveryModel)
            .values(**_opening(draft))
            .on_conflict_do_nothing(constraint=_DELIVERY_DATE_CONSTRAINT)
            .returning(DailyDeliveryModel.id)
        )
        created_id = (await self._session.execute(statement)).scalar_one_or_none()
        if created_id is None:
            return None
        return await self.get(created_id)

    async def open_many(self, drafts: Sequence[DailyDeliveryDraft]) -> int:
        if not drafts:
            return 0
        statement = (
            insert(DailyDeliveryModel)
            .values([_opening(draft) for draft in drafts])
            .on_conflict_do_nothing(constraint=_DELIVERY_DATE_CONSTRAINT)
            .returning(DailyDeliveryModel.id)
        )
        return len((await self._session.execute(statement)).scalars().all())

    async def get(self, delivery_id: int) -> DailyDelivery | None:
        model = await self._session.get(DailyDeliveryModel, delivery_id)
        return to_daily_delivery(model) if model is not None else None

    async def for_date(self, user_id: int, delivery_date: date) -> DailyDelivery | None:
        statement = select(DailyDeliveryModel).where(
            DailyDeliveryModel.user_id == user_id,
            DailyDeliveryModel.delivery_date == delivery_date,
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_daily_delivery(model) if model is not None else None

    async def lock(self, delivery_id: int) -> DailyDelivery | None:
        model = await self._locked(DailyDeliveryModel.id == delivery_id)
        return to_daily_delivery(model) if model is not None else None

    async def lock_due(
        self,
        *,
        now: datetime,
        limit: int,
        activation_grace: timedelta | None = None,
    ) -> tuple[DailyDelivery, ...]:
        conditions: list[ColumnElement[bool]] = [
            DailyDeliveryModel.deadline_at > now,
            or_(
                and_(
                    DailyDeliveryModel.status.in_(_WAITING_STATUSES),
                    DailyDeliveryModel.next_attempt_at <= now,
                ),
                and_(
                    DailyDeliveryModel.status == DailyDeliveryStatus.SENDING,
                    DailyDeliveryModel.lease_until <= now,
                ),
            ),
        ]
        if activation_grace:
            # The bot is still sending the welcome and then this very item.
            conditions.append(
                ~and_(
                    DailyDeliveryModel.trigger == DailyDeliveryTrigger.ACTIVATION,
                    DailyDeliveryModel.status == DailyDeliveryStatus.PENDING,
                    DailyDeliveryModel.attempts == 0,
                    DailyDeliveryModel.scheduled_for > now - activation_grace,
                )
            )
        statement = (
            select(DailyDeliveryModel)
            .where(*conditions)
            .order_by(DailyDeliveryModel.next_attempt_at.asc(), DailyDeliveryModel.id.asc())
            .limit(limit)
            # Another worker is on those rows: take the next ones instead.
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(to_daily_delivery(model) for model in models)

    async def start_attempt(
        self,
        delivery_id: int,
        *,
        content_id: int,
        now: datetime,
        lease_until: datetime,
    ) -> DailyDeliveryAttempt:
        model = await self._locked(DailyDeliveryModel.id == delivery_id)
        if model is None:
            message = "No such Daily Content delivery"
            raise ConflictError(message, delivery_id=delivery_id)
        dead_lease = (
            model.status is DailyDeliveryStatus.SENDING
            and model.lease_until is not None
            and model.lease_until <= now
        )
        if model.status not in _WAITING_STATUSES and not dead_lease:
            message = "The delivery cannot be attempted now"
            raise ConflictError(message, delivery_id=delivery_id, status=model.status.value)
        if model.content_id is not None and model.content_id != content_id:
            message = "A delivery sends the item it started with"
            raise ConflictError(
                message,
                delivery_id=delivery_id,
                content_id=model.content_id,
                requested_content_id=content_id,
            )
        if dead_lease and model.lease_token is not None:
            await self._close_dangling(model, at=now)

        token = uuid4()
        try:
            # Set inside the savepoint: a refusal rolls the fields back with it.
            async with self._session.begin_nested():
                model.status = DailyDeliveryStatus.SENDING
                model.content_id = content_id
                model.lease_token = token
                model.lease_until = lease_until
                model.attempts += 1
                await self._session.flush()
        except IntegrityError as error:
            if violated_constraint(error) != _DELIVERY_CONTENT_CONSTRAINT:
                raise
            message = "The buyer already holds or received this item"
            raise ConflictError(
                message,
                delivery_id=delivery_id,
                content_id=content_id,
                constraint=_DELIVERY_CONTENT_CONSTRAINT,
            ) from error

        attempt = DailyDeliveryAttemptModel(
            delivery_id=model.id,
            attempt_no=model.attempts,
            lease_token=token,
            content_id=content_id,
            started_at=now,
        )
        self._session.add(attempt)
        await self._session.flush()
        await self._session.refresh(attempt)
        return to_daily_attempt(attempt)

    async def finish_sent(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        telegram_message_id: int,
    ) -> DailyDelivery | None:
        model = await self._locked(DailyDeliveryModel.lease_token == lease_token)
        if model is None:
            await self._record_lost_lease(
                lease_token,
                at=at,
                outcome=DailyAttemptOutcome.SENT,
                error="Telegram accepted the message after the lease was taken over",
                telegram_message_id=telegram_message_id,
            )
            return None
        model.status = DailyDeliveryStatus.SENT
        model.sent_at = at
        model.telegram_message_id = telegram_message_id
        model.last_outcome = DailyAttemptOutcome.SENT
        model.last_error = None
        self._release(model)
        await self._close_attempt(
            lease_token,
            at=at,
            outcome=DailyAttemptOutcome.SENT,
            telegram_message_id=telegram_message_id,
        )
        return await self._save(model)

    async def finish_retry(  # noqa: PLR0913 — one outcome, this many facts
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str,
        next_attempt_at: datetime,
        retry_after_seconds: int | None = None,
    ) -> DailyDelivery | None:
        model = await self._locked(DailyDeliveryModel.lease_token == lease_token)
        if model is None:
            await self._record_lost_lease(lease_token, at=at, outcome=outcome, error=error)
            return None
        model.status = DailyDeliveryStatus.PENDING
        model.next_attempt_at = next_attempt_at
        model.last_outcome = outcome
        model.last_error = error
        self._release(model)
        await self._close_attempt(
            lease_token,
            at=at,
            outcome=outcome,
            error=error,
            retry_after_seconds=retry_after_seconds,
        )
        return await self._save(model)

    async def finish_failed(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str,
    ) -> DailyDelivery | None:
        model = await self._locked(DailyDeliveryModel.lease_token == lease_token)
        if model is None:
            await self._record_lost_lease(lease_token, at=at, outcome=outcome, error=error)
            return None
        model.status = DailyDeliveryStatus.FAILED
        model.last_outcome = outcome
        model.last_error = error
        self._release(model)
        await self._close_attempt(lease_token, at=at, outcome=outcome, error=error)
        return await self._save(model)

    async def finish_rejected(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str,
        next_attempt_at: datetime,
    ) -> DailyDelivery | None:
        model = await self._locked(DailyDeliveryModel.lease_token == lease_token)
        if model is None:
            await self._record_lost_lease(lease_token, at=at, outcome=outcome, error=error)
            return None
        model.status = DailyDeliveryStatus.PENDING
        model.content_id = None
        model.next_attempt_at = next_attempt_at
        model.last_outcome = outcome
        model.last_error = error
        self._release(model)
        await self._close_attempt(lease_token, at=at, outcome=outcome, error=error)
        return await self._save(model)

    async def mark_no_content(
        self,
        delivery_id: int,
        *,
        at: datetime,
        recheck_at: datetime,
    ) -> DailyDelivery:
        del at  # nothing was attempted: the row only learns when to look again
        model = await self._locked(DailyDeliveryModel.id == delivery_id)
        if model is None or model.status not in _WAITING_STATUSES or model.content_id is not None:
            message = "Only an open delivery without an item can wait for content"
            raise ConflictError(message, delivery_id=delivery_id)
        model.status = DailyDeliveryStatus.NO_CONTENT
        model.next_attempt_at = recheck_at
        return await self._save(model)

    async def cancel(self, delivery_id: int, *, at: datetime) -> DailyDelivery | None:
        model = await self._locked(DailyDeliveryModel.id == delivery_id)
        if model is None:
            return None
        dead_lease = (
            model.status is DailyDeliveryStatus.SENDING
            and model.lease_until is not None
            and model.lease_until <= at
        )
        if model.status not in _WAITING_STATUSES and not dead_lease:
            return None
        if dead_lease:
            await self._close_dangling(model, at=at)
        model.status = DailyDeliveryStatus.CANCELLED
        self._release(model)
        return await self._save(model)

    async def expire_overdue(self, *, now: datetime, limit: int = 100) -> int:
        statement = (
            select(DailyDeliveryModel)
            .where(
                DailyDeliveryModel.deadline_at <= now,
                DailyDeliveryModel.status.in_(_OPEN_STATUSES),
            )
            .order_by(DailyDeliveryModel.deadline_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        models = (await self._session.execute(statement)).scalars().all()
        for model in models:
            if model.lease_token is not None:
                await self._close_dangling(model, at=now)
            model.status = DailyDeliveryStatus.EXPIRED
            self._release(model)
        await self._session.flush()
        return len(models)

    async def attempts_of(self, delivery_id: int) -> tuple[DailyDeliveryAttempt, ...]:
        statement = (
            select(DailyDeliveryAttemptModel)
            .where(DailyDeliveryAttemptModel.delivery_id == delivery_id)
            .order_by(DailyDeliveryAttemptModel.attempt_no.asc())
        )
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(to_daily_attempt(model) for model in models)

    async def last_sent_at(self, user_id: int) -> datetime | None:
        statement = select(func.max(DailyDeliveryModel.sent_at)).where(
            DailyDeliveryModel.user_id == user_id,
            DailyDeliveryModel.status == DailyDeliveryStatus.SENT,
        )
        last: datetime | None = await self._session.scalar(statement)
        return last

    async def outcome_counts(self, *, since: datetime) -> DeliveryCounts:
        failed = and_(
            DailyDeliveryModel.status.in_(
                (DailyDeliveryStatus.FAILED, DailyDeliveryStatus.EXPIRED),
            ),
            DailyDeliveryModel.last_outcome.is_not(None),
            DailyDeliveryModel.last_outcome != DailyAttemptOutcome.BLOCKED,
        )
        statement = select(
            func.count().filter(DailyDeliveryModel.status == DailyDeliveryStatus.SENT),
            func.count().filter(failed),
        ).where(DailyDeliveryModel.scheduled_for >= since)
        sent, failures = (await self._session.execute(statement)).one()
        return DeliveryCounts(sent=int(sent), failed=int(failures))

    async def count_sent(self, user_id: int) -> int:
        statement = (
            select(func.count())
            .select_from(DailyDeliveryModel)
            .where(
                DailyDeliveryModel.user_id == user_id,
                DailyDeliveryModel.status == DailyDeliveryStatus.SENT,
            )
        )
        return int(await self._session.scalar(statement) or 0)

    # ----------------------------------------------------------------- helpers

    async def _locked(self, *conditions: ColumnElement[bool]) -> DailyDeliveryModel | None:
        statement = (
            select(DailyDeliveryModel)
            .where(*conditions)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _attempt(self, lease_token: UUID) -> DailyDeliveryAttemptModel | None:
        statement = (
            select(DailyDeliveryAttemptModel)
            .where(DailyDeliveryAttemptModel.lease_token == lease_token)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _close_attempt(  # noqa: PLR0913 — one attempt, this many facts
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str | None = None,
        retry_after_seconds: int | None = None,
        telegram_message_id: int | None = None,
    ) -> None:
        attempt = await self._attempt(lease_token)
        if attempt is None:  # pragma: no cover — created with the lease it belongs to
            return
        attempt.finished_at = at
        attempt.outcome = outcome
        attempt.error = error
        attempt.retry_after_seconds = retry_after_seconds
        attempt.telegram_message_id = telegram_message_id

    async def _close_dangling(self, model: DailyDeliveryModel, *, at: datetime) -> None:
        """Close the attempt a dead process left behind; Telegram may have taken it."""
        if model.lease_token is None:  # pragma: no cover — callers check
            return
        attempt = await self._attempt(model.lease_token)
        if attempt is not None and attempt.finished_at is None:
            attempt.finished_at = at
            attempt.outcome = DailyAttemptOutcome.CRASHED
            attempt.error = "The lease ran out before the attempt reported back"
        model.last_outcome = DailyAttemptOutcome.CRASHED
        model.last_error = "The previous attempt never reported back"

    async def _record_lost_lease(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        outcome: DailyAttemptOutcome,
        error: str,
        telegram_message_id: int | None = None,
    ) -> None:
        """An attempt reported back after its lease was taken over.

        Its result is not applied to the delivery — its successor owns it now —
        but it is kept on the attempt, so a duplicate send is visible. An
        attempt that already reported its own outcome keeps it: the same report
        arriving twice changes nothing.
        """
        attempt = await self._attempt(lease_token)
        if attempt is None or attempt.outcome not in (None, DailyAttemptOutcome.CRASHED):
            return
        attempt.finished_at = at
        attempt.outcome = DailyAttemptOutcome.LEASE_LOST
        attempt.error = f"{outcome.value}: {error}"
        attempt.telegram_message_id = telegram_message_id
        await self._session.flush()

    @staticmethod
    def _release(model: DailyDeliveryModel) -> None:
        model.lease_token = None
        model.lease_until = None

    async def _save(self, model: DailyDeliveryModel) -> DailyDelivery:
        await self._session.flush()
        await self._session.refresh(model)
        return to_daily_delivery(model)


def _deliverable_conditions(
    *,
    min_completed: int,
    only: tuple[int, ...],
) -> list[ColumnElement[bool]]:
    """Active, unblocked, entitled by the loyalty programme's own count."""
    completed = (
        select(func.count())
        .select_from(PurchaseModel)
        .where(
            PurchaseModel.user_id == DailySubscriptionModel.user_id,
            completed_purchase_condition(),
        )
        .correlate(DailySubscriptionModel)
        .scalar_subquery()
    )
    conditions: list[ColumnElement[bool]] = [
        DailySubscriptionModel.status == DailySubscriptionStatus.ACTIVE,
        DailySubscriptionModel.blocked_at.is_(None),
        completed >= min_completed,
    ]
    if only:
        conditions.append(DailySubscriptionModel.user_id.in_(only))
    return conditions


def _opening(draft: DailyDeliveryDraft) -> dict[str, Any]:
    """The row a delivery is opened with: pending, due at its first attempt."""
    window = draft.window
    return {
        "user_id": draft.user_id,
        "delivery_date": window.delivery_date,
        "trigger": draft.trigger,
        "status": DailyDeliveryStatus.PENDING,
        "scheduled_for": window.center,
        "window_opens_at": window.opens_at,
        "window_closes_at": window.closes_at,
        "deadline_at": window.deadline_at,
        "next_attempt_at": draft.first_attempt_at,
    }


__all__ = [
    "SqlAlchemyDailyContentRepository",
    "SqlAlchemyDailyDeliveryRepository",
    "SqlAlchemyDailySubscriptionRepository",
]
