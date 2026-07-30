"""Background workers of the bot process.

Webhooks are the fast path, not the only path. Two loops make the shop resilient
to notifications that never arrive:

* **reconciliation** asks Crypto Pay for the real state of every invoice that is
  still pending, and settles the ones that were actually paid;
* **housekeeping** expires invoices nobody paid, so they stop being polled.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from app.core.exceptions import AppError, LockBusyError
from app.core.logging import get_logger
from app.domain.enums import PaymentProvider
from app.domain.payments import PaymentState

if TYPE_CHECKING:
    from app.core.config import BotSettings
    from app.domain.payments import CryptoInvoiceGateway
    from app.services.checkout import CheckoutService
    from app.services.purchases import PurchaseService

logger = get_logger(__name__)


class ReconciliationWorker:
    """Settles crypto payments whose webhook never arrived."""

    def __init__(
        self,
        *,
        purchases: PurchaseService,
        checkout: CheckoutService,
        crypto: CryptoInvoiceGateway,
        settings: BotSettings,
    ) -> None:
        self._purchases = purchases
        self._checkout = checkout
        self._crypto = crypto
        self._settings = settings

    async def run_once(self) -> int:
        """Poll pending invoices once; return how many payments were settled."""
        pending = await self._purchases.list_pending(
            PaymentProvider.CRYPTO,
            limit=self._settings.reconciliation_batch_size,
        )
        if not pending:
            return 0

        by_external_id = {purchase.external_id: purchase for purchase in pending}
        try:
            states = await self._crypto.fetch_states(tuple(by_external_id))
        except AppError as error:
            logger.warning("reconciliation_provider_unavailable", error=str(error))
            return 0

        settled = 0
        for external_id, state in states.items():
            if state is not PaymentState.PAID:
                continue
            try:
                result = await self._checkout.settle_payment(
                    provider=PaymentProvider.CRYPTO,
                    external_id=external_id,
                )
            except LockBusyError:
                # A webhook for the same invoice is being processed right now.
                continue
            except AppError as error:
                logger.error(  # noqa: TRY400 — one bad invoice must not stop the loop
                    "reconciliation_settlement_failed",
                    external_id=external_id,
                    error=str(error),
                )
                continue
            settled += 1
            logger.info(
                "reconciliation_settled_payment",
                external_id=external_id,
                delivery_status=result.status.value,
            )
        return settled

    async def run_forever(self) -> None:
        """Poll on a fixed interval until cancelled."""
        interval = self._settings.reconciliation_interval_seconds
        logger.info("reconciliation_started", interval_seconds=interval)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # the loop must survive anything
                logger.error("reconciliation_iteration_failed", error=str(error))  # noqa: TRY400
            await asyncio.sleep(interval)


class HousekeepingWorker:
    """Expires pending purchases whose invoice lifetime has passed."""

    def __init__(self, *, purchases: PurchaseService, settings: BotSettings) -> None:
        self._purchases = purchases
        self._settings = settings

    async def run_once(self) -> int:
        """Expire stale invoices once; return how many were expired."""
        return await self._purchases.expire_stale()

    async def run_forever(self) -> None:
        """Expire on a fixed interval until cancelled."""
        interval = self._settings.housekeeping_interval_seconds
        logger.info("housekeeping_started", interval_seconds=interval)
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # the loop must survive anything
                logger.error("housekeeping_iteration_failed", error=str(error))  # noqa: TRY400
            await asyncio.sleep(interval)
