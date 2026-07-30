"""Telegram side implementations of the delivery and Stars ports."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from aiogram.types import LabeledPrice

from app.bot.texts import delivery_message, invoice_description
from app.core.exceptions import PaymentGatewayError
from app.core.logging import get_logger
from app.infrastructure.telegram.errors import to_delivery_error

if TYPE_CHECKING:
    from aiogram import Bot

    from app.domain.delivery import DeliveryMessage
    from app.domain.payments import InvoiceRequest

logger = get_logger(__name__)

STARS_CURRENCY = "XTR"


class TelegramDeliveryGateway:
    """Sends the purchased link over Telegram, mapping failures to domain errors."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(self, message: DeliveryMessage) -> None:
        """Send the delivery message.

        Raises:
            DeliveryTransientError: worth retrying (flood control, outage).
            DeliveryPermanentError: hopeless (bot blocked, chat gone).
        """
        try:
            await self._bot.send_message(
                chat_id=message.chat_id,
                text=delivery_message(
                    product_title=message.product_title,
                    delivery_url=message.delivery_url,
                    is_repeat=message.is_repeat,
                ),
                disable_web_page_preview=True,
            )
        except Exception as error:  # every transport failure is classified
            raise to_delivery_error(error) from error


class TelegramStarsInvoiceSender:
    """Sends a Telegram Stars invoice as a message in the buyer's chat."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_invoice(self, request: InvoiceRequest) -> None:
        """Send the Stars invoice.

        Raises:
            PaymentGatewayError: Telegram refused to send the invoice.
        """
        amount = int(Decimal(request.amount))
        try:
            await self._bot.send_invoice(
                chat_id=request.user_id,
                title=request.title[:32],
                description=invoice_description(request.description)[:255],
                payload=request.payload,
                currency=STARS_CURRENCY,
                prices=[LabeledPrice(label=request.title[:32], amount=amount)],
                # Telegram Stars payments carry no provider token.
                provider_token=None,
            )
        except TelegramAPIError as error:
            logger.warning(
                "stars_invoice_failed",
                user_id=request.user_id,
                payload=request.payload,
                error=str(error),
            )
            message = "Telegram refused to send the Stars invoice"
            raise PaymentGatewayError(message, payload=request.payload) from error

        logger.info(
            "stars_invoice_sent",
            user_id=request.user_id,
            payload=request.payload,
            amount=amount,
        )
