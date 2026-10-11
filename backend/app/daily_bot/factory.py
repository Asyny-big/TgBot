"""Assembly of the Daily Content bot: services and dispatcher."""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Dispatcher

from app.bot.middlewares import LogContextMiddleware
from app.daily_bot.albums import AlbumCollector
from app.daily_bot.handlers import membership, owner, subscriber
from app.daily_bot.middlewares import (
    DailyServices,
    DailyServicesMiddleware,
    DailyThrottleMiddleware,
)
from app.infrastructure.telegram.factory import create_daily_sender

if TYPE_CHECKING:
    from aiogram import Bot

    from app.core.container import Container
    from app.domain.locks import LockManager


def create_daily_services(container: Container, bot: Bot) -> DailyServices:
    """The Daily services, sending through this process's Daily bot."""
    sender = create_daily_sender(bot)
    return DailyServices(
        subscriptions=container.build_daily_subscriptions(),
        delivery=container.build_daily_delivery(sender),
        library=container.build_daily_library(sender),
        albums=AlbumCollector(),
        stock=container.build_daily_stock(),
    )


def create_daily_dispatcher(
    services: DailyServices,
    *,
    locks: LockManager,
    throttle_seconds: float,
) -> Dispatcher:
    """The dispatcher with its middlewares and routers.

    The owner's router comes first, so the owner never reaches the buyer's flow,
    and the owner is never throttled: an album is many messages at once.
    """
    owner_ids = services.subscriptions.settings.admin_telegram_ids
    dispatcher = Dispatcher()
    dispatcher.update.outer_middleware(LogContextMiddleware())
    dispatcher.update.outer_middleware(DailyServicesMiddleware(services))
    dispatcher.message.middleware(
        DailyThrottleMiddleware(
            locks,
            window_seconds=throttle_seconds,
            exempt=frozenset(owner_ids),
        )
    )

    dispatcher.include_router(owner.build_router(owner_ids))
    dispatcher.include_router(membership.build_router())
    dispatcher.include_router(subscriber.build_router())
    return dispatcher


__all__ = ["create_daily_dispatcher", "create_daily_services"]
