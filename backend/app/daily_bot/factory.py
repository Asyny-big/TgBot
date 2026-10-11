"""Assembly of the Daily Content bot: services, FSM storage and dispatcher."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from aiogram import Dispatcher
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage

from app.bot.middlewares import LogContextMiddleware
from app.daily_bot.albums import AlbumCollector, DraftAlbumCollector
from app.daily_bot.handlers import membership, owner, requests, subscriber
from app.daily_bot.middlewares import (
    DailyServices,
    DailyServicesMiddleware,
    DailyThrottleMiddleware,
)
from app.domain.daily_requests import REQUEST_DRAFT_TTL_SECONDS
from app.infrastructure.telegram.factory import create_daily_sender

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.fsm.storage.base import BaseStorage
    from redis.asyncio import Redis

    from app.core.container import Container
    from app.domain.locks import LockManager

DRAFT_STORAGE_PREFIX: Final = "daily_fsm"
"""The Daily bot's FSM keys: apart from anything else in the shared Redis."""


def create_draft_storage(
    redis: Redis,
    *,
    ttl_seconds: int = REQUEST_DRAFT_TTL_SECONDS,
) -> RedisStorage:
    """Where content request drafts live: Redis, under their own prefix.

    A draft untouched for ``ttl_seconds`` is gone — its state and its data
    expire together. The connection is the process's own and is closed with it.
    """
    return RedisStorage(
        redis=redis,
        key_builder=DefaultKeyBuilder(prefix=DRAFT_STORAGE_PREFIX),
        state_ttl=ttl_seconds,
        data_ttl=ttl_seconds,
    )


def create_daily_services(container: Container, bot: Bot) -> DailyServices:
    """The Daily services, sending through this process's Daily bot."""
    sender = create_daily_sender(bot)
    return DailyServices(
        subscriptions=container.build_daily_subscriptions(),
        delivery=container.build_daily_delivery(sender),
        library=container.build_daily_library(sender),
        albums=AlbumCollector(),
        stock=container.build_daily_stock(),
        requests=container.build_daily_requests(),
        draft_albums=DraftAlbumCollector(),
    )


def create_daily_dispatcher(
    services: DailyServices,
    *,
    locks: LockManager,
    throttle_seconds: float,
    storage: BaseStorage | None = None,
) -> Dispatcher:
    """The dispatcher with its middlewares and routers.

    The owner's router comes first, so the owner never reaches the buyer's flow,
    and the owner is never throttled: an album is many messages at once. The
    content requests' router comes before the buyer's general one, so a draft
    takes its messages — while commands and the menu keep working.

    ``storage`` keeps the drafts; in memory when none is given (tests).
    """
    owner_ids = services.subscriptions.settings.admin_telegram_ids
    dispatcher = Dispatcher(storage=storage) if storage is not None else Dispatcher()
    dispatcher.update.outer_middleware(LogContextMiddleware())
    dispatcher.update.outer_middleware(DailyServicesMiddleware(services))
    dispatcher.message.middleware(
        DailyThrottleMiddleware(
            locks,
            window_seconds=throttle_seconds,
            exempt=frozenset(owner_ids),
            patience=requests.draft_patience,
        )
    )

    dispatcher.include_router(owner.build_router(owner_ids))
    dispatcher.include_router(membership.build_router())
    dispatcher.include_router(requests.build_router())
    dispatcher.include_router(subscriber.build_router())
    return dispatcher


__all__ = [
    "DRAFT_STORAGE_PREFIX",
    "create_daily_dispatcher",
    "create_daily_services",
    "create_draft_storage",
]
