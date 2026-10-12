"""PostgreSQL repository of buyers' content requests.

The database holds the request's guarantees:

* one request per draft — ``uq_daily_requests_client_draft_id``: the same draft
  sent twice, from two taps or two processes, becomes one request;
* the owner's change applies only to the version it was read at — checked on
  the row locked ``FOR UPDATE``, so two tabs of the panel cannot both win;
* a history line under an idempotency key is written once —
  ``uq_daily_request_events_idempotency_key``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.core.exceptions import ConflictError
from app.domain.daily_requests import OPEN_REQUEST_STATUSES, is_closed, user_actor
from app.domain.enums import DailyRequestEventKind, DailyRequestStatus
from app.domain.patch import is_set
from app.infrastructure.db.mappers import (
    to_daily_request,
    to_daily_request_event,
    to_daily_request_item,
)
from app.infrastructure.db.models import (
    DailyRequestEventModel,
    DailyRequestItemModel,
    DailyRequestModel,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.commands import DailyRequestDraft, DailyRequestEventDraft
    from app.domain.entities import DailyContentRequest, DailyRequestEvent, DailyRequestItem
    from app.domain.patch import Maybe

_DRAFT_CONSTRAINT = "uq_daily_requests_client_draft_id"
_EVENT_KEY_CONSTRAINT = "uq_daily_request_events_idempotency_key"


class SqlAlchemyDailyRequestRepository:
    """Content requests, in PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        draft: DailyRequestDraft,
        *,
        at: datetime,
    ) -> tuple[DailyContentRequest, bool]:
        statement = (
            insert(DailyRequestModel)
            .values(
                user_id=draft.user_id,
                status=DailyRequestStatus.NEW,
                client_draft_id=draft.client_draft_id,
                purchases_at_submit=draft.purchases_at_submit,
                version=1,
                created_at=at,
                updated_at=at,
            )
            .on_conflict_do_nothing(constraint=_DRAFT_CONSTRAINT)
            .returning(DailyRequestModel.id)
        )
        created_id = (await self._session.execute(statement)).scalar_one_or_none()
        if created_id is None:
            # The same draft again — a second tap, a retry: the request it became.
            existing = await self.find_by_draft(draft.client_draft_id)
            if existing is None:  # pragma: no cover — the conflict names an existing row
                message = "The content request vanished"
                raise ConflictError(message, client_draft_id=str(draft.client_draft_id))
            if existing.user_id != draft.user_id:
                message = "This draft belongs to another buyer"
                raise ConflictError(message, client_draft_id=str(draft.client_draft_id))
            return existing, False

        self._session.add_all(
            [
                DailyRequestItemModel(
                    request_id=created_id,
                    position=position,
                    kind=item.kind,
                    body=item.body,
                    entities=list(item.entities) or None,
                    file_id=item.file_id,
                    file_unique_id=item.file_unique_id,
                    file_size=item.file_size,
                    file_name=item.file_name,
                    mime_type=item.mime_type,
                    duration=item.duration,
                    source_chat_id=item.source_chat_id,
                    source_message_id=item.source_message_id,
                    created_at=at,
                )
                for position, item in enumerate(draft.items, start=1)
            ]
        )
        self._session.add(
            DailyRequestEventModel(
                request_id=created_id,
                kind=DailyRequestEventKind.SUBMITTED.value,
                actor=user_actor(draft.user_id),
                to_status=DailyRequestStatus.NEW,
                created_at=at,
            )
        )
        await self._session.flush()
        model = await self._session.get(DailyRequestModel, created_id)
        if model is None:  # pragma: no cover — inserted in this transaction
            message = "The new content request vanished"
            raise ConflictError(message, request_id=created_id)
        return to_daily_request(model), True

    async def get(self, request_id: int) -> DailyContentRequest | None:
        model = await self._session.get(DailyRequestModel, request_id)
        return to_daily_request(model) if model is not None else None

    async def lock(self, request_id: int) -> DailyContentRequest | None:
        model = await self._locked(request_id)
        return to_daily_request(model) if model is not None else None

    async def find_by_draft(self, client_draft_id: UUID) -> DailyContentRequest | None:
        statement = select(DailyRequestModel).where(
            DailyRequestModel.client_draft_id == client_draft_id
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_daily_request(model) if model is not None else None

    async def items_of(self, request_id: int) -> tuple[DailyRequestItem, ...]:
        statement = (
            select(DailyRequestItemModel)
            .where(DailyRequestItemModel.request_id == request_id)
            .order_by(DailyRequestItemModel.position.asc())
        )
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(to_daily_request_item(model) for model in models)

    async def get_item(self, item_id: int) -> DailyRequestItem | None:
        model = await self._session.get(DailyRequestItemModel, item_id)
        return to_daily_request_item(model) if model is not None else None

    async def events_of(self, request_id: int) -> tuple[DailyRequestEvent, ...]:
        statement = (
            select(DailyRequestEventModel)
            .where(DailyRequestEventModel.request_id == request_id)
            .order_by(DailyRequestEventModel.created_at.asc(), DailyRequestEventModel.id.asc())
        )
        models = (await self._session.execute(statement)).scalars().all()
        return tuple(to_daily_request_event(model) for model in models)

    async def count_open(self, user_id: int) -> int:
        statement = (
            select(func.count())
            .select_from(DailyRequestModel)
            .where(
                DailyRequestModel.user_id == user_id,
                DailyRequestModel.status.in_(OPEN_REQUEST_STATUSES),
            )
        )
        return int(await self._session.scalar(statement) or 0)

    async def count_since(self, user_id: int, since: datetime) -> int:
        statement = (
            select(func.count())
            .select_from(DailyRequestModel)
            .where(DailyRequestModel.user_id == user_id, DailyRequestModel.created_at >= since)
        )
        return int(await self._session.scalar(statement) or 0)

    async def update(
        self,
        request_id: int,
        *,
        expected_version: int,
        at: datetime,
        status: Maybe[DailyRequestStatus],
        admin_note: Maybe[str | None],
    ) -> DailyContentRequest | None:
        # Locked: the version read here cannot move until this transaction ends.
        model = await self._locked(request_id)
        if model is None or model.version != expected_version:
            return None
        if is_set(status) and status is not model.status:
            model.status = status
            model.closed_at = at if is_closed(status) else None
        if is_set(admin_note):
            model.admin_note = admin_note
        model.version += 1
        model.updated_at = at
        await self._session.flush()
        await self._session.refresh(model)
        return to_daily_request(model)

    async def add_event(
        self,
        draft: DailyRequestEventDraft,
        *,
        at: datetime,
    ) -> DailyRequestEvent | None:
        statement = (
            insert(DailyRequestEventModel)
            .values(
                request_id=draft.request_id,
                kind=draft.kind.value,
                actor=draft.actor,
                from_status=draft.from_status,
                to_status=draft.to_status,
                body=draft.body,
                outbox_message_id=draft.outbox_message_id,
                idempotency_key=draft.idempotency_key,
                created_at=at,
            )
            .on_conflict_do_nothing(constraint=_EVENT_KEY_CONSTRAINT)
            .returning(DailyRequestEventModel.id)
        )
        created_id = (await self._session.execute(statement)).scalar_one_or_none()
        if created_id is None:
            return None
        model = await self._session.get(DailyRequestEventModel, created_id)
        return to_daily_request_event(model) if model is not None else None

    async def find_event(self, idempotency_key: str) -> DailyRequestEvent | None:
        statement = select(DailyRequestEventModel).where(
            DailyRequestEventModel.idempotency_key == idempotency_key
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_daily_request_event(model) if model is not None else None

    async def reply_of(self, outbox_message_id: int) -> DailyRequestEvent | None:
        statement = select(DailyRequestEventModel).where(
            DailyRequestEventModel.outbox_message_id == outbox_message_id,
            DailyRequestEventModel.kind == DailyRequestEventKind.REPLY_QUEUED.value,
        )
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_daily_request_event(model) if model is not None else None

    async def _locked(self, request_id: int) -> DailyRequestModel | None:
        statement = (
            select(DailyRequestModel)
            .where(DailyRequestModel.id == request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()


__all__ = ["SqlAlchemyDailyRequestRepository"]
