"""PostgreSQL repositories of the Daily runtime: the outbox and the alerts.

The outbox follows the deliveries' discipline. A message is claimed with
``FOR UPDATE SKIP LOCKED`` — two workers split the due rows, never share one —
and leased under a fresh random token. Every outcome is written against that
token: an attempt whose lease ran out and was taken over can no longer change
the message, so a late report never undoes what its successor did.

An alert is one row, locked while it is evaluated: two workers deciding the
same transition queue one notification.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.domain.enums import DailyOutboxStatus
from app.infrastructure.db.mappers import to_daily_alert, to_daily_outbox_message
from app.infrastructure.db.models import DailyAlertModel, DailyOutboxModel

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.commands import DailyOutboxDraft
    from app.domain.entities import DailyAlert, DailyOutboxMessage
    from app.domain.enums import DailyAlertKey

_DEDUPE_CONSTRAINT = "uq_daily_outbox_dedupe_key"
_OPEN_STATUSES = (DailyOutboxStatus.PENDING, DailyOutboxStatus.SENDING)
_EXPIRED_ERROR = "The deadline passed before the message could be sent"


class SqlAlchemyDailyOutboxRepository:
    """The Daily outbox, in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(self, draft: DailyOutboxDraft) -> DailyOutboxMessage | None:
        statement = (
            insert(DailyOutboxModel)
            .values(
                kind=draft.kind.value,
                channel=draft.channel.value,
                chat_id=draft.chat_id,
                body=draft.body,
                dedupe_key=draft.dedupe_key,
                content_id=draft.content_id,
                request_item_id=draft.request_item_id,
                status=DailyOutboxStatus.PENDING,
                next_attempt_at=draft.first_attempt_at,
                deadline_at=draft.deadline_at,
            )
            .on_conflict_do_nothing(constraint=_DEDUPE_CONSTRAINT)
            .returning(DailyOutboxModel.id)
        )
        created_id = (await self._session.execute(statement)).scalar_one_or_none()
        if created_id is None:
            return None
        return await self.get(created_id)

    async def get(self, message_id: int) -> DailyOutboxMessage | None:
        model = await self._session.get(DailyOutboxModel, message_id)
        return to_daily_outbox_message(model) if model is not None else None

    async def find_by_key(self, dedupe_key: str) -> DailyOutboxMessage | None:
        statement = select(DailyOutboxModel).where(DailyOutboxModel.dedupe_key == dedupe_key)
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_daily_outbox_message(model) if model is not None else None

    async def claim_due(
        self,
        *,
        now: datetime,
        limit: int,
        lease_until: datetime,
    ) -> tuple[DailyOutboxMessage, ...]:
        statement = (
            select(DailyOutboxModel)
            .where(
                DailyOutboxModel.deadline_at > now,
                or_(
                    and_(
                        DailyOutboxModel.status == DailyOutboxStatus.PENDING,
                        DailyOutboxModel.next_attempt_at <= now,
                    ),
                    and_(
                        DailyOutboxModel.status == DailyOutboxStatus.SENDING,
                        DailyOutboxModel.lease_until <= now,
                    ),
                ),
            )
            .order_by(DailyOutboxModel.next_attempt_at.asc(), DailyOutboxModel.id.asc())
            .limit(limit)
            # Another worker is on those rows: take the next ones instead.
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        models = (await self._session.execute(statement)).scalars().all()
        for model in models:
            model.status = DailyOutboxStatus.SENDING
            model.lease_token = uuid4()
            model.lease_until = lease_until
            model.attempts += 1
        return tuple([await self._save(model) for model in models])

    async def finish_sent(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        telegram_message_id: int,
    ) -> DailyOutboxMessage | None:
        model = await self._holder(lease_token)
        if model is None:
            return None
        model.status = DailyOutboxStatus.SENT
        model.sent_at = at
        model.telegram_message_id = telegram_message_id
        model.last_error = None
        _release(model)
        return await self._save(model)

    async def finish_retry(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        next_attempt_at: datetime,
        error: str,
    ) -> DailyOutboxMessage | None:
        del at
        model = await self._holder(lease_token)
        if model is None:
            return None
        model.status = DailyOutboxStatus.PENDING
        model.next_attempt_at = next_attempt_at
        model.last_error = error
        _release(model)
        return await self._save(model)

    async def finish_failed(
        self,
        lease_token: UUID,
        *,
        at: datetime,
        error: str,
    ) -> DailyOutboxMessage | None:
        del at
        model = await self._holder(lease_token)
        if model is None:
            return None
        model.status = DailyOutboxStatus.FAILED
        model.last_error = error
        _release(model)
        return await self._save(model)

    async def expire_overdue(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> tuple[DailyOutboxMessage, ...]:
        statement = (
            select(DailyOutboxModel)
            .where(
                DailyOutboxModel.deadline_at <= now,
                DailyOutboxModel.status.in_(_OPEN_STATUSES),
            )
            .order_by(DailyOutboxModel.deadline_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        models = (await self._session.execute(statement)).scalars().all()
        for model in models:
            model.status = DailyOutboxStatus.EXPIRED
            model.last_error = _EXPIRED_ERROR
            _release(model)
        return tuple([await self._save(model) for model in models])

    async def _holder(self, lease_token: UUID) -> DailyOutboxModel | None:
        """The message this lease still holds, locked; ``None`` once it was taken over."""
        statement = (
            select(DailyOutboxModel)
            .where(
                DailyOutboxModel.lease_token == lease_token,
                DailyOutboxModel.status == DailyOutboxStatus.SENDING,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def _save(self, model: DailyOutboxModel) -> DailyOutboxMessage:
        await self._session.flush()
        await self._session.refresh(model)
        return to_daily_outbox_message(model)


class SqlAlchemyDailyAlertRepository:
    """The owner's Daily alerts, in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lock(self, key: DailyAlertKey) -> DailyAlert:
        created = (
            insert(DailyAlertModel)
            .values(key=key.value)
            .on_conflict_do_nothing(index_elements=[DailyAlertModel.key])
        )
        await self._session.execute(created)
        return to_daily_alert(await self._locked(key))

    async def save(self, alert: DailyAlert) -> DailyAlert:
        model = await self._locked(alert.key)
        model.state = alert.state
        model.notifications = alert.notifications
        model.value = dict(alert.value)
        model.fired_at = alert.fired_at
        model.notified_at = alert.notified_at
        model.resolved_at = alert.resolved_at
        await self._session.flush()
        await self._session.refresh(model)
        return to_daily_alert(model)

    async def get(self, key: DailyAlertKey) -> DailyAlert | None:
        model = await self._session.get(DailyAlertModel, key.value)
        return to_daily_alert(model) if model is not None else None

    async def list_all(self) -> tuple[DailyAlert, ...]:
        statement = select(DailyAlertModel).order_by(DailyAlertModel.key.asc())
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(to_daily_alert(model) for model in models)

    async def _locked(self, key: DailyAlertKey) -> DailyAlertModel:
        statement = (
            select(DailyAlertModel)
            .where(DailyAlertModel.key == key.value)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one()


def _release(model: DailyOutboxModel) -> None:
    model.lease_token = None
    model.lease_until = None


__all__ = ["SqlAlchemyDailyAlertRepository", "SqlAlchemyDailyOutboxRepository"]
