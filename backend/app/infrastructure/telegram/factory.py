"""Construction of the Telegram transport.

Both processes need a ``Bot``: the bot process to receive updates, the admin API
to re-deliver a purchase and to run a manual payment check. Multiple ``Bot``
instances are safe — only long polling is exclusive — so each process owns one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.infrastructure.telegram.daily import TelegramDailyContentSender
from app.infrastructure.telegram.gateways import (
    TelegramDeliveryGateway,
    TelegramStarsInvoiceSender,
)
from app.infrastructure.telegram.notifications import (
    TelegramBonusNotifier,
    TelegramLoyaltyNotifier,
)

if TYPE_CHECKING:
    from app.core.config import DailySettings, Settings


def create_bot(settings: Settings) -> Bot:
    """Create a Bot with HTML parse mode as the default."""
    return Bot(
        token=settings.telegram.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_daily_bot(settings: DailySettings) -> Bot:
    """The Daily Content bot: its own token, the same HTML default as the shop bot.

    Raises:
        RuntimeError: ``DAILY_BOT_TOKEN`` is not set.
    """
    if settings.bot_token is None:
        message = "DAILY_BOT_TOKEN is not set"
        raise RuntimeError(message)
    return Bot(
        token=settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_daily_sender(bot: Bot) -> TelegramDailyContentSender:
    """The Daily Content item sender for the Daily bot."""
    return TelegramDailyContentSender(bot)


def create_delivery_gateway(bot: Bot) -> TelegramDeliveryGateway:
    """The delivery transport for this process."""
    return TelegramDeliveryGateway(bot)


def create_stars_sender(bot: Bot) -> TelegramStarsInvoiceSender:
    """The Stars invoice sender for this process."""
    return TelegramStarsInvoiceSender(bot)


def create_bonus_notifier(bot: Bot) -> TelegramBonusNotifier:
    """The courtesy-message transport for the bonus feature."""
    return TelegramBonusNotifier(bot)


def create_loyalty_notifier(bot: Bot) -> TelegramLoyaltyNotifier:
    """The milestone-notice transport for the loyalty programme."""
    return TelegramLoyaltyNotifier(bot)
