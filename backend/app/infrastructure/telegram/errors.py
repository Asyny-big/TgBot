"""Translation of aiogram failures into domain delivery errors.

The retry policy lives in ``DeliveryService``; its only input is whether a
failure is worth retrying. That decision is made here, once.

Daily Content needs one distinction more: whether Telegram *definitely* did
not take the message — then the item is given back to the buyer — or the
outcome is uncertain — then the same item is sent again. ``to_daily_send_error``
makes that call.
"""

from __future__ import annotations

from typing import Final

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

from app.core.exceptions import (
    DailyContentRejectedError,
    DailySendBlockedError,
    DailySendError,
    DailySendRejectedError,
    DailySendTransientError,
    DeliveryPermanentError,
    DeliveryTransientError,
)

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


# Wording Telegram uses when the *item* is the problem — its file, its text or
# its caption — rather than the chat. Another item would go through.
_CONTENT_MARKERS: Final = (
    "wrong file identifier",
    "wrong remote file identifier",
    "wrong file_id",
    "file reference",
    "file_reference",
    "media_empty",
    "file must be non-empty",
    "wrong type of the web page content",
    "failed to get http url content",
    "message to copy not found",
    "can't parse entities",
    "entity_bounds_invalid",
    "message is too long",
    "caption is too long",
    "message text is empty",
    "text must be non-empty",
    "video_content_type_invalid",
    "photo_invalid_dimensions",
    "type of file mismatch",
)


def is_content_rejection(error: TelegramBadRequest) -> bool:
    """Whether a 400 from Telegram is about the item rather than the chat."""
    text = str(error).lower()
    return any(marker in text for marker in _CONTENT_MARKERS)


def to_daily_send_error(error: Exception) -> DailySendError:
    """Classify a failure to send a Daily Content item.

    *Definite* — Telegram answered and said no, nothing reached the buyer:

    * ``DailySendBlockedError`` — the bot is blocked, the chat is gone;
    * ``DailyContentRejectedError`` — the item itself is unusable;
    * ``DailySendRejectedError`` — any other refusal Telegram will repeat.

    *Uncertain* — ``DailySendTransientError``: flood control, the network,
    Telegram's servers, or anything unrecognised. A timeout in particular may
    hide a message Telegram did take, so these are retried with the same item.
    """
    if isinstance(error, DailySendError):
        return error
    if isinstance(error, TelegramRetryAfter):
        return DailySendTransientError(
            f"Telegram flood control: retry after {error.retry_after}s",
            retry_after=float(error.retry_after),
        )
    if isinstance(error, TelegramForbiddenError):
        return DailySendBlockedError(f"Telegram refused: {error}")
    if isinstance(error, TelegramEntityTooLarge):
        return DailyContentRejectedError(f"Telegram refused the item: {error}")
    if isinstance(error, TelegramBadRequest):
        if is_permanent_bad_request(error):
            return DailySendBlockedError(f"Telegram rejected the chat: {error}")
        if is_content_rejection(error):
            return DailyContentRejectedError(f"Telegram refused the item: {error}")
        return DailySendRejectedError(f"Telegram refused the message: {error}")
    if isinstance(error, TelegramServerError | TelegramNetworkError):
        return DailySendTransientError(f"Telegram is unavailable: {error}")
    if isinstance(error, TelegramAPIError):
        return DailySendTransientError(f"Telegram API error: {error}")
    return DailySendTransientError(f"Unexpected transport failure: {error}")
