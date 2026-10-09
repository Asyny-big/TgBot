"""Loyalty programme use cases — the read side only, for now.

``get_progress`` answers "how far is this buyer?" from their purchases and the
rewards recorded so far. It never writes: opening the progress screen a hundred
times leaves the database exactly as it was, and a buyer who qualified before
the programme existed sees their rewards without anything being backfilled.

Using a reward — recording it, applying the 50% discount, handing over a free
pack — is a later step and lives elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.exceptions import LoyaltyDisabledError
from app.core.logging import get_logger
from app.domain.loyalty import progress_of

if TYPE_CHECKING:
    from app.core.config import LoyaltySettings
    from app.domain.loyalty import LoyaltyProgress
    from app.domain.uow import UnitOfWorkFactory

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LoyaltyService:
    """A buyer's loyalty progress, read on demand."""

    uow_factory: UnitOfWorkFactory
    settings: LoyaltySettings

    @property
    def enabled(self) -> bool:
        """Whether the programme is switched on (``LOYALTY_ENABLED``)."""
        return self.settings.enabled

    async def get_progress(self, user_id: int) -> LoyaltyProgress:
        """Completed purchases, the two milestone rewards and VIP. Read only.

        Raises:
            LoyaltyDisabledError: the programme is switched off. Checked here as
                well as in the bot, so no caller can read it while it is off.
        """
        if not self.enabled:
            raise LoyaltyDisabledError(user_id=user_id)
        async with self.uow_factory() as uow:
            completed = await uow.loyalty.count_completed(user_id)
            rewards = await uow.loyalty.rewards_of(user_id)
        progress = progress_of(completed, rewards)
        logger.info(
            "loyalty_progress_read",
            user_id=user_id,
            completed=progress.completed_count,
            discount_50=progress.discount_50.value,
            free_pack=progress.free_pack.value,
            vip=progress.vip,
        )
        return progress


__all__ = ["LoyaltyService"]
