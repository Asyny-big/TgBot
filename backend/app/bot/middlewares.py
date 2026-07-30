"""Bot middlewares: dependency injection, log context and anti-flood."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

import structlog
from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from app.bot.texts import TOO_FAST
from app.core.exceptions import LockBusyError
from app.core.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.domain.locks import LockManager
    from app.services.checkout import CheckoutService
    from app.services.purchases import PurchaseService

logger = get_logger(__name__)

CONTAINER_KEY: Final = "shop"
_THROTTLE_KEY_TEMPLATE: Final = "throttle:{user_id}"


class BotServices:
    """The services a handler is allowed to use."""

    def __init__(self, purchases: PurchaseService, checkout: CheckoutService) -> None:
        self.purchases = purchases
        self.checkout = checkout


class ServicesMiddleware(BaseMiddleware):
    """Injects the wired services into every handler."""

    def __init__(self, services: BotServices) -> None:
        self._services = services

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data[CONTAINER_KEY] = self._services
        return await handler(event, data)


class LogContextMiddleware(BaseMiddleware):
    """Binds update and user identifiers to the structlog context."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        structlog.contextvars.clear_contextvars()
        context: dict[str, Any] = {}
        if isinstance(event, Update):
            context["update_id"] = event.update_id
        user = data.get("event_from_user")
        if user is not None:
            context["telegram_id"] = user.id
        structlog.contextvars.bind_contextvars(**context)
        try:
            return await handler(event, data)
        finally:
            structlog.contextvars.clear_contextvars()


class ThrottleMiddleware(BaseMiddleware):
    """One in-flight action per buyer.

    Backed by the same TTL bounded Redis lock as the rest of the system, so a
    crashed process cannot leave a user throttled forever.
    """

    def __init__(self, locks: LockManager, *, window_seconds: float) -> None:
        self._locks = locks
        self._window = window_seconds

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or self._window <= 0:
            return await handler(event, data)

        key = _THROTTLE_KEY_TEMPLATE.format(user_id=user.id)
        try:
            async with self._locks.lock(key, ttl_seconds=self._window):
                return await handler(event, data)
        except LockBusyError:
            logger.info("update_throttled", telegram_id=user.id)
            await _notify_throttled(event)
            return None


async def _notify_throttled(event: TelegramObject) -> None:
    """Tell the user to slow down.

    Never raises: failing to deliver a courtesy notice must not turn a throttled
    update into an unhandled error.
    """
    if isinstance(event, Update):
        event = event.callback_query or event.message or event
    try:
        if isinstance(event, CallbackQuery):
            await event.answer(TOO_FAST, show_alert=False)
        elif isinstance(event, Message):
            await event.answer(TOO_FAST)
    except Exception as error:  # the notice is best effort
        logger.info("throttle_notice_not_delivered", error=str(error))
