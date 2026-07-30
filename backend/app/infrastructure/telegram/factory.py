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

from app.infrastructure.telegram.gateways import (
    TelegramDeliveryGateway,
    TelegramStarsInvoiceSender,
)

if TYPE_CHECKING:
    from app.core.config import Settings


def create_bot(settings: Settings) -> Bot:
    """Create a Bot with HTML parse mode as the default."""
    return Bot(
        token=settings.telegram.bot_token.get_secret_value(),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_delivery_gateway(bot: Bot) -> TelegramDeliveryGateway:
    """The delivery transport for this process."""
    return TelegramDeliveryGateway(bot)


def create_stars_sender(bot: Bot) -> TelegramStarsInvoiceSender:
    """The Stars invoice sender for this process."""
    return TelegramStarsInvoiceSender(bot)
