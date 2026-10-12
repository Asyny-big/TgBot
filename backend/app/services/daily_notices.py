"""Daily Content: telling a buyer they have seen everything — once per episode.

When a buyer's day finds nothing they have not received, the delivery waits
for new items (and gets them the same day if they arrive). The buyer is told
so once: "Вы посмотрели все материалы 🎉 …". Told again only after a later item
has reached them and the library has run dry for them once more — a new
episode. A buyer who never received anything (activated on an empty library)
was already told by the bot that items are on their way, and hears nothing
here.

The subscription row is locked while deciding, and the notice is queued
under a ``dedupe_key`` naming the episode: two workers, or the same delivery
looked at again a quarter of an hour later, queue it once.

Nothing is recorded or sent while Daily Content is off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from app.core.logging import get_logger
from app.daily_bot import texts
from app.domain.commands import DailyOutboxDraft
from app.domain.enums import DailyOutboxChannel, DailyOutboxKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.config import DailySettings
    from app.domain.uow import UnitOfWorkFactory

logger = get_logger(__name__)

NOTICE_TTL: Final = timedelta(hours=24)
"""A notice not delivered within a day is no longer news."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class DailyNoticeService:
    """Queues the buyers' "you have seen everything" notice."""

    uow_factory: UnitOfWorkFactory
    settings: DailySettings
    clock: Callable[[], datetime] = field(default=_utc_now)

    async def exhausted(self, user_id: int) -> bool:
        """The buyer has nothing left: tell them, unless this episode was told."""
        if not self.settings.enabled:
            return False
        now = self.clock()
        async with self.uow_factory() as uow:
            subscription = await uow.daily_subscriptions.lock(user_id)
            if subscription is None or not subscription.receives_deliveries:
                return False
            last_sent = await uow.daily_deliveries.last_sent_at(user_id)
            if last_sent is None:
                return False
            told = subscription.exhausted_notified_at
            if told is not None and told >= last_sent:
                return False
            await uow.daily_subscriptions.mark_exhausted_notified(user_id, at=now)
            queued = await uow.daily_outbox.enqueue(
                DailyOutboxDraft(
                    kind=DailyOutboxKind.EXHAUSTED_NOTICE,
                    channel=DailyOutboxChannel.DAILY,
                    chat_id=user_id,
                    body=texts.EXHAUSTED,
                    dedupe_key=f"exhausted:{user_id}:{last_sent.isoformat()}",
                    first_attempt_at=now,
                    deadline_at=now + NOTICE_TTL,
                )
            )
        if queued is not None:
            logger.info("daily_exhausted_notice_queued", user_id=user_id)
        return queued is not None


__all__ = ["NOTICE_TTL", "DailyNoticeService"]
