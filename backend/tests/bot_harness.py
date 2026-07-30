"""Test harness for the bot: a Bot that never touches the network.

``RecordingBot`` intercepts aiogram at its single outbound seam (``Bot.__call__``)
and records the API methods the handlers issue, answering each with a canned
result. Updates then travel the real path — dispatcher, middlewares, filters,
handlers — so what is tested is the bot, not a mock of it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from aiogram import Bot
from aiogram.methods import (
    AnswerCallbackQuery,
    AnswerPreCheckoutQuery,
    GetMe,
    GetUpdates,
    SendInvoice,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    PreCheckoutQuery,
    RefundedPayment,
    SuccessfulPayment,
    Update,
    User,
)

if TYPE_CHECKING:
    from uuid import UUID

    from app.domain.enums import PaymentProvider

BOT_TOKEN = "123456789:AAHfake-Test-Token_for_unit_tests_only01"  # noqa: S105
CHAT_TYPE_PRIVATE = "private"
#: How long a faked getUpdates call blocks, mimicking Telegram's long polling.
POLL_INTERVAL_SECONDS = 0.05


class RecordingBot(Bot):
    """A Bot whose API calls are recorded instead of sent."""

    def __init__(self, *, fail_send_message: list[Exception] | None = None) -> None:
        super().__init__(token=BOT_TOKEN)
        self.calls: list[TelegramMethod[Any]] = []
        self.failed_calls: list[TelegramMethod[Any]] = []
        self.fail_send_message = list(fail_send_message or [])

    async def __call__(
        self,
        method: TelegramMethod[Any],
        request_timeout: int | None = None,
    ) -> Any:
        del request_timeout
        # A call that raises never reached the user, so it is not recorded as sent.
        if isinstance(method, SendMessage) and self.fail_send_message:
            self.failed_calls.append(method)
            raise self.fail_send_message.pop(0)
        if isinstance(method, GetUpdates):
            # Real long polling blocks until Telegram has something or the
            # timeout expires. Answering instantly would turn the polling loop
            # into a busy-wait that starves the event loop.
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            return []
        self.calls.append(method)
        return _canned_result(method)

    # -- assertions helpers -------------------------------------------------- #
    def methods(self, method_type: type[TelegramMethod[Any]]) -> list[Any]:
        """Every recorded call of one type, in order."""
        return [call for call in self.calls if isinstance(call, method_type)]

    def texts(self) -> list[str]:
        """Text of every message the bot tried to send."""
        return [call.text for call in self.methods(SendMessage)]

    def last_text(self) -> str:
        messages = self.methods(SendMessage)
        assert messages, "the bot sent no message"
        return str(messages[-1].text)


def _canned_result(method: TelegramMethod[Any]) -> Any:
    """Minimal plausible response for each method the shop uses."""
    if isinstance(method, SendMessage | SendPhoto | SendInvoice):
        return make_message(text=getattr(method, "text", None) or "sent")
    if isinstance(method, AnswerCallbackQuery | AnswerPreCheckoutQuery):
        return True
    if isinstance(method, GetMe):
        # aiogram identifies the bot before it starts polling.
        return User(id=1, is_bot=True, first_name="Shop", username="MyShopBot")
    return True


def make_user(telegram_id: int = 4242, username: str | None = "buyer") -> User:
    """A private-chat Telegram user."""
    return User(
        id=telegram_id,
        is_bot=False,
        first_name="Buyer",
        username=username,
        language_code="ru",
    )


def make_message(
    *,
    text: str | None = None,
    user: User | None = None,
    successful_payment: SuccessfulPayment | None = None,
    refunded_payment: RefundedPayment | None = None,
) -> Message:
    """A message in a private chat."""
    sender = user or make_user()
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=sender.id, type=CHAT_TYPE_PRIVATE),
        from_user=sender,
        text=text,
        successful_payment=successful_payment,
        refunded_payment=refunded_payment,
    )


def start_update(payload: str | None, *, user: User | None = None, update_id: int = 1) -> Update:
    """``/start`` with (or without) a deep-link payload."""
    text = "/start" if payload is None else f"/start {payload}"
    return Update(update_id=update_id, message=make_message(text=text, user=user))


def pay_button_update(
    *,
    provider: PaymentProvider,
    product_id: UUID,
    user: User | None = None,
    update_id: int = 2,
) -> Update:
    """A press on ⭐ or 💎."""
    sender = user or make_user()
    return Update(
        update_id=update_id,
        callback_query=CallbackQuery(
            id="callback-1",
            from_user=sender,
            chat_instance="instance-1",
            message=make_message(text="card", user=sender),
            data=f"pay:{provider.value}:{product_id}",
        ),
    )


def pre_checkout_update(
    *,
    payload: str,
    amount: int,
    user: User | None = None,
    update_id: int = 3,
) -> Update:
    """Telegram's pre-checkout probe for a Stars invoice."""
    sender = user or make_user()
    return Update(
        update_id=update_id,
        pre_checkout_query=PreCheckoutQuery(
            id="pre-checkout-1",
            from_user=sender,
            currency="XTR",
            total_amount=amount,
            invoice_payload=payload,
        ),
    )


def successful_payment_update(
    *,
    payload: str,
    amount: int,
    charge_id: str = "charge-1",
    user: User | None = None,
    update_id: int = 4,
) -> Update:
    """A captured Stars payment."""
    return Update(
        update_id=update_id,
        message=make_message(
            user=user,
            successful_payment=SuccessfulPayment(
                currency="XTR",
                total_amount=amount,
                invoice_payload=payload,
                telegram_payment_charge_id=charge_id,
                provider_payment_charge_id=charge_id,
            ),
        ),
    )


def refunded_payment_update(
    *,
    payload: str,
    amount: int,
    charge_id: str = "charge-1",
    user: User | None = None,
    update_id: int = 5,
) -> Update:
    """A Stars refund notification."""
    return Update(
        update_id=update_id,
        message=make_message(
            user=user,
            refunded_payment=RefundedPayment(
                currency="XTR",
                total_amount=amount,
                invoice_payload=payload,
                telegram_payment_charge_id=charge_id,
            ),
        ),
    )
