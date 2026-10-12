"""Daily bot middlewares: dependency injection and anti-flood.

Log context comes from the shop bot's own ``LogContextMiddleware``, used as is.
Throttling is the same TTL-bounded Redis lock, under a key of its own: a buyer
writing to both bots at once must not be slowed down by the other one.

The owner is never throttled: an album arrives as several messages within the
same instant, and every one of them must reach the library. A buyer filling a
content request's draft is not turned away either: an album, or several
messages forwarded at once, wait their turn — briefly — instead of being
refused, and only a longer flood is told to slow down.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, Update

from app.core.exceptions import LockBusyError
from app.core.logging import get_logger
from app.daily_bot.texts import TOO_FAST

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.daily_bot.albums import AlbumCollector, DraftAlbumCollector
    from app.domain.locks import LockManager
    from app.services.daily_delivery import DailyDeliveryService
    from app.services.daily_library import DailyLibraryService
    from app.services.daily_requests import DailyRequestService
    from app.services.daily_stock import DailyStockService
    from app.services.daily_subscriptions import DailySubscriptionService

logger = get_logger(__name__)

DAILY_KEY: Final = "daily"
_THROTTLE_KEY_TEMPLATE: Final = "daily-throttle:{user_id}"


class DailyServices:
    """The services a Daily bot handler is allowed to use."""

    def __init__(  # noqa: PLR0913 — one keyword per service
        self,
        *,
        subscriptions: DailySubscriptionService,
        delivery: DailyDeliveryService,
        library: DailyLibraryService,
        albums: AlbumCollector,
        stock: DailyStockService,
        requests: DailyRequestService,
        draft_albums: DraftAlbumCollector,
    ) -> None:
        self.subscriptions = subscriptions
        self.delivery = delivery
        self.library = library
        self.albums = albums
        self.stock = stock
        self.requests = requests
        self.draft_albums = draft_albums


class DailyServicesMiddleware(BaseMiddleware):
    """Injects the Daily services into every handler as ``daily``."""

    def __init__(self, services: DailyServices) -> None:
        self._services = services

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data[DAILY_KEY] = self._services
        return await handler(event, data)


class DailyThrottleMiddleware(BaseMiddleware):
    """One in-flight message per buyer; the rest are answered "too fast".

    ``exempt`` accounts — the owner's — pass straight through, before any lock
    is taken. ``patience`` says, from the handler data, how long a message may
    wait for its turn instead of being refused at once.
    """

    def __init__(
        self,
        locks: LockManager,
        *,
        window_seconds: float,
        exempt: frozenset[int] = frozenset(),
        patience: Callable[[dict[str, Any]], float] | None = None,
    ) -> None:
        self._locks = locks
        self._window = window_seconds
        self._exempt = exempt
        self._patience = patience

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or self._window <= 0 or user.id in self._exempt:
            return await handler(event, data)
        key = _THROTTLE_KEY_TEMPLATE.format(user_id=user.id)
        wait = self._patience(data) if self._patience is not None else 0.0
        try:
            async with self._locks.lock(key, ttl_seconds=self._window, wait_seconds=wait):
                return await handler(event, data)
        except LockBusyError:
            logger.info("daily_update_throttled", telegram_id=user.id)
            await _notify_throttled(event)
            return None


async def _notify_throttled(event: TelegramObject) -> None:
    """Tell the buyer to slow down. Never raises: it is a courtesy."""
    if isinstance(event, Update):
        event = event.message or event
    try:
        if isinstance(event, Message):
            await event.answer(TOO_FAST)
    except Exception as error:  # the notice is best effort
        logger.info("daily_throttle_notice_not_delivered", error=str(error))


__all__ = [
    "DAILY_KEY",
    "DailyServices",
    "DailyServicesMiddleware",
    "DailyThrottleMiddleware",
]
