"""Bot and dispatcher assembly."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.bot.handlers import payments, start
from app.bot.middlewares import (
    BotServices,
    LogContextMiddleware,
    ServicesMiddleware,
    ThrottleMiddleware,
)
from app.core.logging import get_logger
from app.infrastructure.telegram.gateways import (
    TelegramDeliveryGateway,
    TelegramStarsInvoiceSender,
)
from app.services.checkout import CheckoutService

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.core.container import Container

logger = get_logger(__name__)


def create_bot(settings: Settings) -> Bot:
    """Create the Bot with HTML parse mode as the default."""
    return Bot(
        token=settings.telegram.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_checkout(container: Container, bot: Bot) -> CheckoutService:
    """Wire the checkout service to this process's Telegram transport."""
    return CheckoutService(
        purchases=container.purchases,
        delivery=container.build_delivery(TelegramDeliveryGateway(bot)),
        stars=TelegramStarsInvoiceSender(bot),
        crypto=container.crypto_payments,
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
