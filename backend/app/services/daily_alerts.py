"""Daily Content: the owner's alerts — evaluated, never repeated.

Each evaluation of an alert happens in one transaction:

1. lock the alert's row (created ``OK`` the first time);
2. decide with ``decide_alert``: fire, remind (at most once per cooldown),
   resolve, or nothing — the hysteresis is in the observation;
3. save the new state and, when there is something to say, queue it in the
   outbox for every owner account under a ``dedupe_key`` numbered by the
   alert's notifications.

Two workers evaluating at once queue each notification once: the second
waits for the row, then finds nothing new to say — and if it did not, the
``dedupe_key`` would still refuse the copy.

Alerts go to the owner through the Daily bot — except the one saying that
Telegram refuses the Daily bot's token: the Daily bot cannot send that one
itself, so it goes through the shop bot.

Nothing is evaluated, recorded or sent while Daily Content is off.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from app.core.logging import get_logger
from app.daily_bot import texts
from app.domain.commands import DailyOutboxDraft
from app.domain.daily_alerts import AlertNotice, decide_alert
from app.domain.enums import DailyAlertKey, DailyOutboxChannel, DailyOutboxKind

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.core.config import DailySettings
    from app.domain.daily_alerts import AlertObservation
    from app.domain.uow import UnitOfWorkFactory

logger = get_logger(__name__)

ALERT_TTL: Final = timedelta(hours=6)
"""An alert not delivered by then is dropped: the next evaluation speaks for itself."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def alert_channel(key: DailyAlertKey) -> DailyOutboxChannel:
    """The bot an alert goes through."""
    if key is DailyAlertKey.DAILY_TOKEN_UNAUTHORIZED:
        return DailyOutboxChannel.SHOP
    return DailyOutboxChannel.DAILY


@dataclass(frozen=True, slots=True)
class DailyAlertService:
    """Records each alert's state and queues what the owner must hear."""

    uow_factory: UnitOfWorkFactory
    settings: DailySettings
    clock: Callable[[], datetime] = field(default=_utc_now)

    async def observe(
        self,
        key: DailyAlertKey,
        observation: AlertObservation,
    ) -> AlertNotice | None:
        """Apply one observation; return the notification queued, if any."""
        if not self.settings.enabled:
            return None
        now = self.clock()
        async with self.uow_factory() as uow:
            alert = await uow.daily_alerts.lock(key)
            decision = decide_alert(
                alert.state,
                notified_at=alert.notified_at,
                observation=observation,
                now=now,
                cooldown=self.settings.alert_cooldown,
            )
            notice = decision.notice
            updated = replace(
                alert,
                state=decision.state,
                value=dict(observation.value),
                notifications=alert.notifications + (1 if notice is not None else 0),
                notified_at=now if notice is not None else alert.notified_at,
                fired_at=now if notice is AlertNotice.FIRE else alert.fired_at,
                resolved_at=now if notice is AlertNotice.RESOLVE else alert.resolved_at,
            )
            await uow.daily_alerts.save(updated)
            if notice is not None:
                body = texts.alert(key, notice, observation.value)
                for chat_id in self.settings.admin_telegram_ids:
                    await uow.daily_outbox.enqueue(
                        DailyOutboxDraft(
                            kind=DailyOutboxKind.ALERT,
                            channel=alert_channel(key),
                            chat_id=chat_id,
                            body=body,
                            dedupe_key=f"alert:{key.value}:{updated.notifications}:{chat_id}",
                            first_attempt_at=now,
                            deadline_at=now + ALERT_TTL,
                        )
                    )
        if notice is not None:
            logger.warning(
                "daily_alert",
                alert=key.value,
                notice=notice.value,
                value=observation.value,
                owners=len(self.settings.admin_telegram_ids),
            )
        return notice


__all__ = ["ALERT_TTL", "DailyAlertService", "alert_channel"]
