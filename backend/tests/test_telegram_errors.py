"""Classification of Telegram failures.

Getting this table wrong is expensive in both directions: a permanent error
retried four times wastes the buyer's time, and a transient error treated as
permanent drops a paid purchase.
"""

from __future__ import annotations

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage

from app.core.exceptions import DeliveryPermanentError, DeliveryTransientError
from app.infrastructure.telegram.errors import is_permanent_bad_request, to_delivery_error

METHOD = SendMessage(chat_id=1, text="x")


def test_flood_control_is_transient_and_carries_the_wait() -> None:
    error = to_delivery_error(
        TelegramRetryAfter(method=METHOD, message="Too Many Requests", retry_after=12)
    )
    assert isinstance(error, DeliveryTransientError)
    assert error.retry_after == 12.0


@pytest.mark.parametrize(
    "message",
    [
        "Forbidden: bot was blocked by the user",
        "Forbidden: user is deactivated",
        "Forbidden: bot was kicked from the group chat",
    ],
)
def test_a_forbidden_chat_is_permanent(message: str) -> None:
    error = to_delivery_error(TelegramForbiddenError(method=METHOD, message=message))
    assert isinstance(error, DeliveryPermanentError)


@pytest.mark.parametrize(
    "message",
    [
        "Bad Request: chat not found",
        "Bad Request: PEER_ID_INVALID",
        "Bad Request: chat_id is empty",
    ],
)
def test_a_hopeless_bad_request_is_permanent(message: str) -> None:
    bad_request = TelegramBadRequest(method=METHOD, message=message)
    assert is_permanent_bad_request(bad_request)
    assert isinstance(to_delivery_error(bad_request), DeliveryPermanentError)


def test_an_ambiguous_bad_request_is_retried() -> None:
    bad_request = TelegramBadRequest(method=METHOD, message="Bad Request: message is too long")
    assert not is_permanent_bad_request(bad_request)
    assert isinstance(to_delivery_error(bad_request), DeliveryTransientError)


@pytest.mark.parametrize(
    "error",
    [
        TelegramServerError(method=METHOD, message="Internal Server Error"),
        TelegramNetworkError(method=METHOD, message="Connection reset"),
    ],
)
def test_an_outage_is_transient(error: Exception) -> None:
    assert isinstance(to_delivery_error(error), DeliveryTransientError)


def test_an_unknown_failure_is_retried_rather_than_dropped() -> None:
    assert isinstance(to_delivery_error(RuntimeError("who knows")), DeliveryTransientError)
