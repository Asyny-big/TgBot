"""Daily Content: the way in from the shop bot — "👑 Открыть VIP Daily".

The shop bot offers the button under the VIP notice and on the "🏆 My
progress" screen, and only to a buyer the Daily bot would actually serve:

* Daily Content is on (``DAILY_ENABLED``);
* the Daily bot has a username to link to (``DAILY_BOT_USERNAME``);
* during a beta (``DAILY_BETA_USER_IDS``), the buyer is in it;
* the buyer has 15 completed purchases — the loyalty programme's own count,
  the very predicate the Daily bot activates on.

The button is a deep link: Telegram opens the Daily bot with ``/start vip``.
Nothing here writes anything or talks to the Daily bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.domain.daily import daily_bot_link, has_daily_access

if TYPE_CHECKING:
    from app.core.config import DailySettings


@dataclass(frozen=True, slots=True)
class DailyEntry:
    """Whether the shop bot shows "👑 Открыть VIP Daily", and where it leads."""

    settings: DailySettings

    def link_for(self, user_id: int, *, completed_purchases: int) -> str | None:
        """The deep link to offer this buyer; ``None`` when there is no button."""
        settings = self.settings
        if not settings.enabled or not settings.bot_username:
            return None
        if settings.beta_user_ids and user_id not in settings.beta_user_ids:
            return None
        if not has_daily_access(completed_purchases):
            return None
        return daily_bot_link(settings.bot_username)


__all__ = ["DailyEntry"]
