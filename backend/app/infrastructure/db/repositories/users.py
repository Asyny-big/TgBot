"""SQLAlchemy implementation of the user repository."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, UserNotFoundError
from app.infrastructure.db.errors import violated_constraint
from app.infrastructure.db.mappers import to_user
from app.infrastructure.db.models import UserModel

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.commands import UserDraft
    from app.domain.entities import User


class SqlAlchemyUserRepository:
    """User persistence backed by PostgreSQL."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, telegram_id: int) -> User | None:
        model = await self._session.get(UserModel, telegram_id)
        return to_user(model) if model is not None else None

    async def upsert(self, draft: UserDraft, *, seen_at: datetime | None = None) -> User:
        """Insert the user or refresh their profile snapshot in one round trip."""
        last_seen = seen_at if seen_at is not None else func.now()
        statement = (
            insert(UserModel)
            .values(
                telegram_id=draft.telegram_id,
                username=draft.username,
                first_name=draft.first_name,
                language_code=draft.language_code,
                last_seen_at=last_seen,
            )
            .on_conflict_do_update(
                index_elements=[UserModel.telegram_id],
                set_={
                    "username": draft.username,
                    "first_name": draft.first_name,
                    "language_code": draft.language_code,
                    "last_seen_at": last_seen,
                },
            )
            .returning(UserModel)
        )
        model = (await self._session.execute(statement)).scalar_one()
        return to_user(model)

    async def count(self) -> int:
        return await self._session.scalar(select(func.count()).select_from(UserModel)) or 0

    async def get_by_referral_code(self, code: str) -> User | None:
        """Return the owner of this referral code, or ``None``."""
        statement = select(UserModel).where(UserModel.referral_code == code)
        model = (await self._session.execute(statement)).scalar_one_or_none()
        return to_user(model) if model is not None else None

    async def ensure_referral_code(self, telegram_id: int, *, candidate: str) -> str:
        """Return the user's permanent code, assigning ``candidate`` if unset.

        Read under a row lock so two simultaneous first visits to the bonus
        screen cannot mint two codes for the same person: the second waits, sees
        the code the first stored, and returns it.

        A user does not have to buy anything to get a link — this is reached
        straight from the bonus screen.
        """
        statement = select(UserModel).where(UserModel.telegram_id == telegram_id).with_for_update()
        model = (await self._session.execute(statement)).scalar_one_or_none()
        if model is None:
            raise UserNotFoundError(telegram_id=telegram_id)
        if model.referral_code:
            return model.referral_code

        try:
            async with self._session.begin_nested():
                model.referral_code = candidate
                await self._session.flush()
        except IntegrityError as error:
            if violated_constraint(error) == "uq_users_referral_code":
                message = "This referral code is already taken"
                raise ConflictError(message, candidate=candidate) from error
            raise
        return candidate
