"""Telegram adapters of the Daily worker: plain messages, and the token check.

``TelegramTextSender`` sends the outbox's alerts and notices through one bot,
with failures classified exactly as the items' are. ``TelegramTokenProbe``
asks Telegram who the bot is: a 401 means the token was revoked, and the bot
can no longer say so itself — which is why that alert goes through the shop
bot instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.methods import GetMe

from app.core.logging import get_logger
from app.domain.daily_alerts import TokenState
from app.infrastructure.telegram.errors import to_daily_send_error

if TYPE_CHECKING:
    from aiogram import Bot

logger = get_logger(__name__)


class TelegramTextSender:
    """Sends plain messages through one bot (HTML, its default)."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send_text(self, chat_id: int, text: str) -> int:
        try:
            message = await self._bot.send_message(chat_id=chat_id, text=text)
        except Exception as error:
            raise to_daily_send_error(error) from error
        return message.message_id


class TelegramTokenProbe:
    """Asks Telegram whether it still accepts this bot's token."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def check(self) -> TokenState:
        try:
            # Not ``bot.me()``: that one is cached and would never notice.
            await self._bot(GetMe())
        except TelegramUnauthorizedError:
            return TokenState.UNAUTHORIZED
        except Exception as error:  # an unclear answer says nothing either way
            logger.warning("daily_token_check_inconclusive", error=str(error))
            return TokenState.UNKNOWN
        return TokenState.OK


__all__ = ["TelegramTextSender", "TelegramTokenProbe"]
