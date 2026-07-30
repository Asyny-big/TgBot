"""Delivery use cases.

The only component allowed to put a message in front of the buyer. It owns:

* sending the purchased link;
* retrying transient transport failures with exponential back-off and jitter;
* logging every delivery outcome;
* confirming the delivery through ``PurchaseService.mark_delivered``.

Business rules about the purchase itself stay in ``PurchaseService``. The flow is

    payment confirmed
        → PurchaseService.confirm_payment()
        → DeliveryService.deliver_purchase()
        → PurchaseService.mark_delivered()

A failed delivery deliberately leaves the purchase in ``paid``: the buyer keeps
the right to the link, and the next ``/start`` (or a re-delivery) hands it over.
No transaction is held open while the transport is being awaited.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.core.exceptions import (
    ConflictError,
    DeliveryPermanentError,
    DeliveryTransientError,
    ProductNotFoundError,
    PurchaseNotFoundError,
    UserNotFoundError,
)
from app.core.logging import get_logger
from app.domain.delivery import DeliveryMessage, DeliveryResult, DeliveryStatus
from app.domain.enums import PurchaseStatus
from app.domain.locks import delivery_lock_key

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from uuid import UUID

    from app.core.config import DeliverySettings
    from app.domain.delivery import DeliveryGateway
    from app.domain.entities import Product, Purchase, User
    from app.domain.locks import LockManager
    from app.domain.uow import UnitOfWorkFactory
    from app.services.purchases import PurchaseService

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _DeliveryContext:
    """Everything needed for one delivery, read in a single short transaction."""

    purchase: Purchase
    product: Product
    user: User


@dataclass(frozen=True, slots=True)
class DeliveryService:
    """Hands the purchased link to the buyer, reliably."""

    uow_factory: UnitOfWorkFactory
    purchases: PurchaseService
    gateway: DeliveryGateway
    locks: LockManager
    settings: DeliverySettings
    # Injected so tests can exercise the retry policy without real waiting.
    # slots=True keeps these as instance attributes, so they are plain callables
    # rather than bound methods.
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)
    jitter: Callable[[], float] = field(default=random.random)

    async def deliver_purchase(self, purchase_id: UUID) -> DeliveryResult:
        """Deliver a freshly paid purchase.

        Returns:
            The outcome. ``ALREADY_DELIVERED`` when a previous attempt succeeded,
            ``FAILED`` when every retry was spent — never an exception for a
            transport problem, so the caller can acknowledge the payment webhook.

        Raises:
            LockBusyError: another worker is delivering this purchase right now.
            PurchaseNotFoundError: no purchase with this id.
            ConflictError: the purchase is not paid.
        """
        return await self._deliver(purchase_id, is_repeat=False, resend_if_delivered=False)

    async def redeliver(self, purchase_id: UUID) -> DeliveryResult:
        """Send the link again to a buyer who already owns the product.

        Used by the repeat-purchase path: the deep link is opened a second time,
        so the buyer gets the *current* link without paying again.
        """
        return await self._deliver(purchase_id, is_repeat=True, resend_if_delivered=True)

    async def _deliver(
        self,
        purchase_id: UUID,
        *,
        is_repeat: bool,
        resend_if_delivered: bool,
    ) -> DeliveryResult:
        async with self.locks.lock(delivery_lock_key(purchase_id)):
            context = await self._load_context(purchase_id)
            purchase = context.purchase

            if purchase.status is PurchaseStatus.DELIVERED and not resend_if_delivered:
                logger.info("delivery_skipped_already_delivered", purchase_id=str(purchase_id))
                return DeliveryResult(
                    status=DeliveryStatus.ALREADY_DELIVERED,
                    purchase_id=purchase_id,
                )

            if not purchase.grants_access:
                message = "Only a paid purchase can be delivered"
                raise ConflictError(
                    message,
                    purchase_id=str(purchase_id),
                    status=purchase.status.value,
                )

            # The buyer always receives the link the product carries right now,
            # even if the admin rotated it after the sale.
            delivery_url = context.product.delivery_url
            outcome = await self._send_with_retries(
                DeliveryMessage(
                    chat_id=context.user.telegram_id,
                    purchase_id=purchase_id,
                    product_title=context.product.title,
                    delivery_url=delivery_url,
                    is_repeat=is_repeat,
                ),
            )
            if not outcome.succeeded:
                return outcome

            if purchase.status is not PurchaseStatus.DELIVERED:
                await self.purchases.mark_delivered(purchase_id, delivered_url=delivery_url)

        logger.info(
            "delivery_completed",
            purchase_id=str(purchase_id),
            attempts=outcome.attempts,
            is_repeat=is_repeat,
        )
        return outcome

    async def _load_context(self, purchase_id: UUID) -> _DeliveryContext:
        """Read the purchase, product and buyer, then close the transaction."""
        async with self.uow_factory() as uow:
            purchase = await uow.purchases.get(purchase_id)
            if purchase is None:
                raise PurchaseNotFoundError(purchase_id=str(purchase_id))
            product = await uow.products.get(purchase.product_id)
            if product is None:  # pragma: no cover — protected by ON DELETE RESTRICT
                raise ProductNotFoundError(product_id=str(purchase.product_id))
            user = await uow.users.get(purchase.user_id)
            if user is None:  # pragma: no cover — protected by the foreign key
                raise UserNotFoundError(telegram_id=purchase.user_id)
        return _DeliveryContext(purchase=purchase, product=product, user=user)

    async def _send_with_retries(self, message: DeliveryMessage) -> DeliveryResult:
        """Send the message, retrying transient failures with back-off."""
        last_error: str | None = None

        for attempt in range(1, self.settings.max_attempts + 1):
            try:
                await self.gateway.send(message)
            except DeliveryPermanentError as error:
                logger.error(  # noqa: TRY400 — a refusing chat is an outcome, not a crash
                    "delivery_permanently_failed",
                    purchase_id=str(message.purchase_id),
                    chat_id=message.chat_id,
                    attempt=attempt,
                    error=str(error),
                )
                return DeliveryResult(
                    status=DeliveryStatus.FAILED,
                    purchase_id=message.purchase_id,
                    attempts=attempt,
                    error=str(error),
                )
            except DeliveryTransientError as error:
                last_error = str(error)
                remaining = self.settings.max_attempts - attempt
                logger.warning(
                    "delivery_attempt_failed",
                    purchase_id=str(message.purchase_id),
                    chat_id=message.chat_id,
                    attempt=attempt,
                    attempts_left=remaining,
                    retry_after=error.retry_after,
                    error=last_error,
                )
                if remaining == 0:
                    break
                await self.sleep(self._backoff(attempt, error.retry_after))
            else:
                logger.info(
                    "delivery_sent",
                    purchase_id=str(message.purchase_id),
                    chat_id=message.chat_id,
                    attempt=attempt,
                    is_repeat=message.is_repeat,
                )
                return DeliveryResult(
                    status=DeliveryStatus.SENT,
                    purchase_id=message.purchase_id,
                    attempts=attempt,
                )

        logger.error(
            "delivery_exhausted",
            purchase_id=str(message.purchase_id),
            chat_id=message.chat_id,
            attempts=self.settings.max_attempts,
            error=last_error,
        )
        return DeliveryResult(
            status=DeliveryStatus.FAILED,
            purchase_id=message.purchase_id,
            attempts=self.settings.max_attempts,
            error=last_error,
        )

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        """Exponential back-off, jittered, never shorter than the provider's hint."""
        delay = self.settings.initial_backoff_seconds * (
            self.settings.backoff_multiplier ** (attempt - 1)
        )
        delay = min(delay, self.settings.max_backoff_seconds)
        spread = delay * self.settings.jitter_ratio
        # jitter() ∈ [0, 1) → symmetric jitter around the nominal delay.
        delay = max(0.0, delay + spread * (self.jitter() * 2 - 1))
        if retry_after is not None:
            delay = max(delay, retry_after)
        return delay
