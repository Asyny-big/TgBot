"""Translation of aiogram failures into domain delivery errors.

The retry policy lives in ``DeliveryService``; its only input is whether a
failure is worth retrying. That decision is made here, once.
"""

from __future__ import annotations

from typing import Final

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from app.core.exceptions import DeliveryPermanentError, DeliveryTransientError

# Wording Telegram uses for situations that will not improve by retrying.
_PERMANENT_MARKERS: Final = (
    "chat not found",
    "user is deactivated",
    "bot was blocked",
    "bot was kicked",
    "peer_id_invalid",
    "chat_id is empty",
)


def is_permanent_bad_request(error: TelegramBadRequest) -> bool:
    """Whether a 400 from Telegram means "never going to work"."""
    text = str(error).lower()
    return any(marker in text for marker in _PERMANENT_MARKERS)


def to_delivery_error(error: Exception) -> DeliveryPermanentError | DeliveryTransientError:
    """Map a transport failure onto the delivery error the service understands."""
    if isinstance(error, TelegramRetryAfter):
        # Flood control told us exactly how long to wait.
        return DeliveryTransientError(
            f"Telegram flood control: retry after {error.retry_after}s",
            retry_after=float(error.retry_after),
        )
    if isinstance(error, TelegramForbiddenError):
        return DeliveryPermanentError(f"Telegram refused: {error}")
    if isinstance(error, TelegramBadRequest):
        if is_permanent_bad_request(error):
            return DeliveryPermanentError(f"Telegram rejected the chat: {error}")
        return DeliveryTransientError(f"Telegram bad request: {error}")
    if isinstance(error, TelegramServerError | TelegramNetworkError):
        return DeliveryTransientError(f"Telegram is unavailable: {error}")
    if isinstance(error, TelegramAPIError):
        # Unknown API errors are treated as transient: a retry is cheap, and
        # dropping a paid buyer's link is not.
        return DeliveryTransientError(f"Telegram API error: {error}")
    return DeliveryTransientError(f"Unexpected transport failure: {error}")
