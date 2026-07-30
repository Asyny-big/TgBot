"""Bot and dispatcher assembly."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Dispatcher

from app.bot.handlers import payments, start
from app.bot.middlewares import (
    BotServices,
    LogContextMiddleware,
    ServicesMiddleware,
    ThrottleMiddleware,
)
from app.core.logging import get_logger
from app.infrastructure.telegram.factory import (
    create_delivery_gateway,
    create_stars_sender,
)

if TYPE_CHECKING:
    from aiogram import Bot

    from app.core.container import Container
    from app.services.checkout import CheckoutService

logger = get_logger(__name__)


def create_checkout(container: Container, bot: Bot) -> CheckoutService:
    """Wire the checkout service to this process's Telegram transport."""
    return container.build_checkout(
        delivery_gateway=create_delivery_gateway(bot),
        stars=create_stars_sender(bot),
    )


def create_dispatcher(container: Container, checkout: CheckoutService) -> Dispatcher:
    """Create the dispatcher with middlewares and routers registered."""
    dispatcher = Dispatcher()
    services = BotServices(purchases=container.purchases, checkout=checkout)

    dispatcher.update.outer_middleware(LogContextMiddleware())
    dispatcher.update.outer_middleware(ServicesMiddleware(services))
    throttle = ThrottleMiddleware(
        container.locks,
        window_seconds=container.settings.bot.throttle_seconds,
    )
    dispatcher.message.middleware(throttle)
    dispatcher.callback_query.middleware(throttle)

    dispatcher.include_router(start.build_router())
    dispatcher.include_router(payments.build_router())
    return dispatcher
